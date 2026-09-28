'公开字面 ISA 编译器的并行工作组调度。算子仍由 compiler.Builder 实现；本模块把独立输出循环分给多个 SM，并在算子之间添加跨工作组屏障。硬件、评分、工作负载与参考程序保持不变。'

import argparse
from dataclasses import replace
import json
import re
from contextlib import contextmanager
from pathlib import Path

from . import compiler


class ParallelBuilder(compiler.Builder):
    sm_count = 8
    batch_epochs = False

    def __init__(self, *args, **kwargs):
        self.active_shard = 0
        self.group_epoch = 0
        self.batch_marker = None
        self.partition_loops = set()
        self.partition_count = self.sm_count
        super().__init__(*args, **kwargs)

    def group_name(self, shard):
        return f"g{self.group_epoch}_{shard}" if self.batch_epochs else f"g{shard}"

    def emit(self, op, **args):
        if op == "WG.BEGIN":
            for shard in range(2 * self.sm_count):
                super().emit(op, wg=self.group_name(shard), sm=shard % self.sm_count, shared_bytes=0)
            return
        if op == "WG.END":
            for shard in range(2 * self.sm_count):
                super().emit(op, wg=self.group_name(shard))
            return

        def rewrite(value):
            if isinstance(value, dict):
                return {
                    key: self.group_name(self.active_shard) if key == "wg" and item == "g" else rewrite(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            return value

        super().emit(op, **rewrite(args))
        if (getattr(self, "collect_attention_exports", False) and op == "ST"
                and args["src"]["space"] == "RF" and args["src"]["lane"] in (0, 3, 5)):
            event = json.loads(self.lines[-1].split(" ", 1)[1])["event"]
            for loop_name in self.loops:
                if loop_name == self.export_operator_name + "h":
                    value = self.active_shard % 8
                elif loop_name == self.export_operator_name + "r":
                    value = 0
                else:
                    raise ValueError("Unexpected attention export loop")
                event = event.replace("{" + loop_name + "}", str(value))
            self.current_head_exports.append(event)

    @contextmanager
    def loop(self, name, start, stop, step=1):
        if name in self.partition_loops:
            index, count = (self.partition_loops[name](self.active_shard)
                            if isinstance(self.partition_loops, dict)
                            else (self.active_shard, self.partition_count))
            start = start + index * step
            # 即使循环为空，ISA 也要求 stop >= start。
            start = min(start, stop) if isinstance(stop, int) else compiler.minimum(start, stop)
            step *= count
        with super().loop(name, start, stop, step) as value:
            yield value

    def barrier(self):
        self.emit("BARRIER", wgs=[self.group_name(shard) for shard in range(2 * self.sm_count)], events=[])

    def switch_batch_epoch(self, args):
        '为独立 P1 批次分配不同的依赖与 RF 生命周期。源代码顺序不增加同步；直到 STEP.COMMIT 才汇合，包括已关闭的工作组指令流。每个 P1 阶段内，每个 SM 最多重叠两个批次指令流。'
        if not self.batch_epochs:
            return
        matches = [re.match(r"^([ps]b[01])l", value) for value in args if isinstance(value, str)]
        marker = next((match[1] for match in matches if match), None)
        if marker is None or marker == self.batch_marker:
            return
        if self.batch_marker is not None:
            for shard in range(self.sm_count):
                super().emit("WG.END", wg=self.group_name(shard))
            self.group_epoch += 1
            for shard in range(self.sm_count):
                super().emit("WG.BEGIN", wg=self.group_name(shard), sm=shard, shared_bytes=0)
        self.batch_marker = marker

    def parallel(self, method, partition_loops, *args, shards=None, **kwargs):
        '发射独立部分，再进行真实的全局完成同步。'
        self.switch_batch_epoch(args)
        self.partition_loops = partition_loops if isinstance(partition_loops, dict) else set(partition_loops)
        self.partition_count = self.sm_count if shards is None else shards
        for shard in range(self.partition_count):
            self.active_shard = shard
            method(*args, **kwargs)
        self.active_shard = 0
        self.partition_loops = set()
        self.barrier()

    def gemm(self, a, b, out, m_extent, k_extent, n_extent, name, **kwargs):
        if name.startswith("sboth") and name.endswith(("wo", "w1", "w2")):
            return self.gemm_decode_waves(a,b,out,m_extent,k_extent,n_extent,name,**kwargs)
        if name.startswith("pboth"):
            if name.endswith("qkv"):
                return self.gemm_resident_qkv(a,b,out,m_extent,k_extent,n_extent,name,**kwargs)
            if name.endswith("wo"):
                return self.gemm_resident_wo(a,b,out,m_extent,k_extent,n_extent,name,**kwargs)
            if name.endswith("w1"):
                return self.gemm_persistent_w1(a,b,out,m_extent,k_extent,n_extent,name,**kwargs)
            if name.endswith("w2"):
                return self.gemm_persistent_w2(a,b,out,m_extent,k_extent,n_extent,name,**kwargs)
        return self.gemm_partitioned(a,b,out,m_extent,k_extent,n_extent,name,**kwargs)

    def gemm_persistent_w1(self, a, b, out, m_extent, k_extent, n_extent, name, *,
                           bias=None, activation=False, residual=None):
        '每个工作组负责一个 N32 列分片；四个 K64 权重分块跨 M128 行常驻。每个物理 SM 有两个工作组；RF2/3/6/7 保存只读 B，RF0/4 为 A 双缓冲，RF1 保存累加器和偏置，RF5 暂存偏置广播与 GELU。每个输出按 K 递增顺序执行原有四条 K64 MMA。'
        if (self.sm_count, m_extent, k_extent, n_extent, b.shape) != (16, 128, 256, 1024, (256, 1024)):
            raise ValueError("Persistent W1 requires the prompt M128/K256/N1024 shape")
        if bias is None or not activation or residual is not None or self.batch_epochs:
            raise ValueError("Persistent W1 requires bias plus GELU and default P1 scheduling")
        rf, hbm, add, mul, imm = compiler.rf, compiler.hbm, compiler.add, compiler.mul, compiler.imm
        weight_lanes = (2, 3, 6, 7)
        self.partition_loops = set()
        # 按 K32 切分流量，每个切片结束后汇合同步所有工作组。
        # 这些屏障隔开有限的 HBM 突发；计算仍然使用
        # 原来的常驻 K64 操作数分块及累加顺序。
        with self.loop(name + "preload", 0, 1):
            preload_k = self.config.w1_preload_k
            for inner in range(0, 256, preload_k):
                lane = weight_lanes[inner // 64]
                offset = (inner % 64) * 32
                for shard in range(32):
                    self.active_shard = shard
                    col = shard * 32
                    self.ld(hbm(b.offset + inner * 1024 + col, preload_k * 32,
                                [preload_k, 32], [1024, 1]), rf(lane, preload_k * 32, offset))
                self.active_shard = 0
                self.barrier()
            for shard in range(32):
                self.active_shard = shard
                self.ld(hbm(bias.offset + shard * 32, 32), rf(1, 32, 1024))
        self.active_shard = 0
        self.barrier()
        for shard in range(32):
            self.active_shard = shard
            col = shard * 32
            with self.loop(name + "m", 0, 128, 32) as row:
                self.vec("add", [imm(0), imm(0)], rf(1, 1024))
                self.ld(hbm(add(a.offset, mul(row, 256)), 2048, [32, 64], [256, 1]), rf(0, 2048))
                for index, lane in enumerate(weight_lanes):
                    if index < 3:
                        upcoming = (index + 1) * 64
                        self.ld(hbm(add(a.offset, mul(row, 256), upcoming), 2048,
                                    [32, 64], [256, 1]), rf(4 * ((index + 1) % 2), 2048))
                    self.emit("MMA.ACC", a=rf(4 * (index % 2), 2048), b=rf(lane, 2048),
                              acc=rf(1, 1024), m=32, n=32, k=64, event=None)
                self.vec("add", [rf(1, 32, 1024), imm(0)], rf(5, 32))
                for copied_rows in (1, 2, 4, 8, 16):
                    self.vec("add", [rf(5, copied_rows * 32), imm(0)],
                             rf(5, copied_rows * 32, copied_rows * 32))
                self.vec("add", [rf(1, 1024), rf(5, 1024)], rf(1, 1024))
                self.gelu_rf(rf(1, 1024), rf(5, 1024))
                self.st(rf(1, 1024), hbm(add(out.offset, mul(row, 1024), col),
                                       1024, [32, 32], [1024, 1]))
        self.active_shard = 0
        self.barrier()

    def gemm_persistent_w2(self, a, b, out, m_extent, k_extent, n_extent, name, *,
                           bias=None, activation=False, residual=None):
        '16 个 N16 输出分片，每个分片分给两个常驻的 K512 工作组。'
        if (self.sm_count,m_extent,k_extent,n_extent,b.shape)!=(16,128,1024,256,(1024,256)):
            raise ValueError("Persistent W2 requires M128/K1024/N256")
        if bias is None or activation or residual is None:
            raise ValueError("Persistent W2 requires residual and bias")
        rf,hbm,add,mul,imm=compiler.rf,compiler.hbm,compiler.add,compiler.mul,compiler.imm
        partial=self.alloc(name+"_ksplit",128,256)
        weight_lanes=(2,3,6,7)
        self.partition_loops=set()
        with self.loop(name+"preload",0,1):
            preload_k = self.config.w2_preload_k
            for inner in range(0,512,preload_k):
                lane=weight_lanes[inner//128]
                offset=(inner%128)*16
                for shard in range(32):
                    self.active_shard=shard
                    part,col=shard//16,(shard%16)*16
                    self.ld(hbm(b.offset+(part*512+inner)*256+col,preload_k*16,
                                [preload_k,16],[256,1]),rf(lane,preload_k*16,offset))
                self.active_shard=0
                self.barrier()
            for shard in range(16):
                self.active_shard=shard
                self.ld(hbm(bias.offset+shard*16,16),rf(5,16,1024))
        self.active_shard=0
        self.barrier()
        with self.loop(name+"m",0,128,16) as row:
            for shard in range(32):
                self.active_shard=shard
                part,col=shard//16,(shard%16)*16
                acc=rf(1,256,mul(row,16)) if part==0 else rf(1,256)
                self.vec("add",[imm(0),imm(0)],acc)
                self.ld(hbm(add(a.offset,mul(row,1024),part*512),2048,[16,128],[1024,1]),rf(0,2048))
                for index,lane in enumerate(weight_lanes):
                    if index<3:
                        upcoming=part*512+(index+1)*128
                        self.ld(hbm(add(a.offset,mul(row,1024),upcoming),2048,[16,128],[1024,1]),rf(4*((index+1)%2),2048))
                    self.emit("MMA.ACC",a=rf(4*(index%2),2048),b=rf(lane,2048),acc=acc,m=16,n=16,k=128,event=None)
                if part==1:
                    self.st(acc,hbm(add(partial.offset,mul(row,256),col),256,[16,16],[256,1]))
            self.active_shard=0
            self.barrier()
        self.active_shard=0
        self.barrier()
        for shard in range(16):
            self.active_shard=shard
            col=shard*16
            with self.loop(name+"merge",0,128,16) as row:
                acc=rf(1,256,mul(row,16))
                self.ld(hbm(add(partial.offset,mul(row,256),col),256,[16,16],[256,1]),rf(0,256))
                self.vec("add",[acc,rf(0,256)],acc)
                self.ld(hbm(add(residual.offset,mul(row,256),col),256,[16,16],[256,1]),rf(5,256))
                self.vec("add",[rf(5,256),acc],acc)
                self.vec("add",[rf(5,16,1024),imm(0)],rf(5,16))
                for copied_rows in (1,2,4,8):
                    self.vec("add",[rf(5,copied_rows*16),imm(0)],rf(5,copied_rows*16,copied_rows*16))
                self.vec("add",[acc,rf(5,256)],acc)
                self.st(acc,hbm(add(out.offset,mul(row,256),col),256,[16,16],[256,1]))
                self.emit("WAIT",wg=self.group_name(shard),events=[json.loads(self.lines[-1].split(" ",1)[1])["event"]])
        self.active_shard=0
        self.barrier()

    def gemm_resident_qkv(self,a,b,out,m_extent,k_extent,n_extent,name,**kwargs):
        '每个 SM 以 N32 和 N16 的 RF 视图保留完整 K256 × N48 权重。'
        if (m_extent,k_extent,n_extent)!=(128,256,768) or kwargs:
            raise ValueError("QKV resident requires plain prompt M128K256N768")
        rf,hbm,add,mul,imm=compiler.rf,compiler.hbm,compiler.add,compiler.mul,compiler.imm
        self.partition_loops=set()
        for inner in range(0,256,32):
            for shard in range(16):
                self.active_shard=shard;col=shard*48
                self.ld(hbm(b.offset+inner*768+col,1024,[32,32],[768,1]),
                        rf(2+inner//64,1024,(inner%64)*32))
                self.ld(hbm(b.offset+inner*768+col+32,512,[32,16],[768,1]),
                        rf(6+inner//128,512,(inner%128)*16))
            self.active_shard=0;self.barrier()
        for shard in range(16):
            self.active_shard=shard;col=shard*48
            with self.loop(name+'m',0,128,16) as row:
                self.vec('add',[imm(0),imm(0)],rf(1,768))
                self.ld(hbm(add(a.offset,mul(row,256)),1024,[16,64],[256,1]),rf(0,1024))
                for index in range(4):
                    if index<3:
                        self.ld(hbm(add(a.offset,mul(row,256),(index+1)*64),1024,
                                    [16,64],[256,1]),rf(0,1024,((index+1)%2)*1024))
                    av=rf(0,1024,(index%2)*1024)
                    self.emit('MMA.ACC',a=av,b=rf(2+index,2048),acc=rf(1,512),
                              m=16,n=32,k=64,event=None)
                    self.emit('MMA.ACC',a=av,b=rf(6+index//2,1024,(index%2)*1024),
                              acc=rf(1,256,512),m=16,n=16,k=64,event=None)
                self.st(rf(1,512),hbm(add(out.offset,mul(row,768),col),512,[16,32],[768,1]))
                self.st(rf(1,256,512),hbm(add(out.offset,mul(row,768),col+32),256,[16,16],[768,1]))
        self.active_shard=0;self.barrier()


    def gemm_resident_wo(self,a,b,out,m_extent,k_extent,n_extent,name,*,residual=None,**kwargs):
        if (m_extent,k_extent,n_extent)!=(128,256,256) or residual is None or kwargs:
            raise ValueError('Resident WO requires prompt 128x256x256 and residual')
        rf,hbm,add,mul,imm=compiler.rf,compiler.hbm,compiler.add,compiler.mul,compiler.imm
        self.partition_loops=set()
        with self.loop(name+'preload',0,1):
            for inner in range(0,256,64):
                for shard in range(16):
                    self.active_shard=shard;col=shard*16
                    self.ld(hbm(b.offset+inner*256+col,1024,[64,16],[256,1]),
                            rf(6+inner//128,1024,(inner%128)*16))
                self.active_shard=0;self.barrier()
        for shard in range(16):
            self.active_shard=shard;col=shard*16
            with self.loop(name+'m',0,128,32) as row:
                self.vec('add',[imm(0),imm(0)],rf(2,512))
                self.ld(hbm(add(a.offset,mul(row,256)),2048,[32,64],[256,1]),rf(0,2048))
                for index in range(4):
                    if index<3:
                        self.ld(hbm(add(a.offset,mul(row,256),(index+1)*64),2048,
                                    [32,64],[256,1]),rf(4*((index+1)%2),2048))
                    self.emit('MMA.ACC',a=rf(4*(index%2),2048),
                              b=rf(6+index//2,1024,(index%2)*1024),acc=rf(2,512),
                              m=32,n=16,k=64,event=None)
                self.ld(hbm(add(residual.offset,mul(row,256),col),512,[32,16],[256,1]),rf(1,512))
                self.vec('add',[rf(1,512),rf(2,512)],rf(2,512))
                self.st(rf(2,512),hbm(add(out.offset,mul(row,256),col),512,[32,16],[256,1]))
        self.active_shard=0;self.barrier()


    def gemm_decode_waves(self, a, b, out, m_extent, k_extent, n_extent, name, *,
                          bias=None, activation=False, residual=None):
        '控制 decode GEMM 的功耗：跨 SM 同步有限的加载和计算波次。WO/W2 权重波次为 64 KiB，W1 为 128 KiB；屏障保证前一波完成后才进入后续计算与下一波。QKV 的实测功耗合格，因此保留既有调度。'
        if self.sm_count != 16 or m_extent != 2 or b.shape != (k_extent,n_extent):
            raise ValueError("Decode wave GEMM requires P1 two-row decode and sixteen SMs")
        rf,hbm,add,mul,imm=compiler.rf,compiler.hbm,compiler.add,compiler.mul,compiler.imm
        tile_n,tile_k=(64,32) if name.endswith("w1") else (16,64)
        if n_extent % tile_n or k_extent % tile_k:
            raise ValueError("Decode wave GEMM requires exact tiles")
        self.partition_loops=set()
        with self.loop(name+"wave",0,1):
            for col_base in range(0,n_extent,16*tile_n):
                active=min(16,(n_extent-col_base)//tile_n)
                for shard in range(active):
                    self.active_shard=shard
                    self.vec("add",[imm(0),imm(0)],rf(2,2*tile_n))
                for inner in range(0,k_extent,tile_k):
                    for shard in range(active):
                        self.active_shard=shard;col=col_base+shard*tile_n
                        self.ld(hbm(a.offset+inner,2*tile_k,[2,tile_k],[k_extent,1]),rf(0,2*tile_k))
                        self.ld(hbm(b.offset+inner*n_extent+col,tile_k*tile_n,
                                    [tile_k,tile_n],[n_extent,1]),rf(1,tile_k*tile_n))
                    self.active_shard=0;self.barrier()
                    for shard in range(active):
                        self.active_shard=shard
                        self.emit("MMA.ACC",a=rf(0,2*tile_k),b=rf(1,tile_k*tile_n),
                                  acc=rf(2,2*tile_n),m=2,n=tile_n,k=tile_k,event=None)
                    # 下一次 LD 复用 RF0/1；公开定义的 RF
                    # 先写后读与先读后写冲突检查保证数据正确。
                    # 受限加载波次可以与当前 MMA 的尾部重叠。
                    self.active_shard=0
                for shard in range(active):
                    self.active_shard=shard;col=col_base+shard*tile_n;acc=rf(2,2*tile_n)
                    if residual is not None:
                        self.ld(hbm(residual.offset+col,2*tile_n,[2,tile_n],[n_extent,1]),rf(0,2*tile_n))
                        self.vec("add",[rf(0,2*tile_n),acc],acc)
                    if bias is not None:
                        self.ld(hbm(bias.offset+col,tile_n),rf(0,tile_n))
                        self.vec("add",[rf(0,tile_n),imm(0)],rf(0,tile_n,tile_n))
                        self.vec("add",[acc,rf(0,2*tile_n)],acc)
                    if activation:self.gelu_rf(acc,rf(1,2*tile_n))
                    self.st(acc,hbm(out.offset+col,2*tile_n,[2,tile_n],[n_extent,1]))
                self.active_shard=0;self.barrier()


    def gemm_partitioned(self, a, b, out, m_extent, k_extent, n_extent, name, **kwargs):
        # 使用二维分片覆盖较大的 M 分块，
        # 当 SM 数多于 M 分块数时，避免大部分硬件空闲。
        tile_m = getattr(getattr(self, "config", None), "gemm_m_tile", 8)
        tile_n = (self.gemm_tile_n(n_extent) if hasattr(self, "gemm_tile_n")
                  else self.gemm_n_tile)
        tiles_m = (m_extent + tile_m - 1) // tile_m
        tiles_n = (n_extent + tile_n - 1) // tile_n
        candidates = [groups for groups in range(1, self.sm_count + 1)
                      if self.sm_count % groups == 0 and groups <= tiles_m]
        m_groups = min(candidates, key=lambda groups: (
            ((tiles_m + groups - 1) // groups)
            * ((tiles_n + self.sm_count // groups - 1) // (self.sm_count // groups)),
            -groups,
        ))
        n_groups = self.sm_count // m_groups
        partitions = {f"{name}m": lambda shard: (shard % m_groups, m_groups),
                      f"{name}n": lambda shard: (shard // m_groups, n_groups)}
        self.parallel(super().gemm, partitions,
                      a, b, out, m_extent, k_extent, n_extent, name, **kwargs)

    def copy_rows(self, source, out, rows, d, name):
        self.parallel(super().copy_rows, [name], source,out,rows,d,name,
                      shards=min(self.sm_count,rows))

    def layernorm(self, source, gamma, beta, out, rows, d, name):
        self.parallel(super().layernorm, [name], source, gamma, beta, out, rows, d, name,
                      shards=min(self.sm_count, rows))

    def add_rows(self, left, right, out, rows, width, name, bias=None):
        self.parallel(super().add_rows, [name if rows > 1 else f"{name}c"],
                      left, right, out, rows, width, name, bias)

    def add_bias(self, source, bias, out, rows, width, name):
        self.parallel(super().add_bias, [name if rows > 1 else f"{name}c"],
                      source, bias, out, rows, width, name)

    def gelu(self, source, out, rows, width, name, **kwargs):
        self.parallel(super().gelu, [name if rows > 1 else f"{name}c"],
                      source, out, rows, width, name, **kwargs)

    def attention(self, qkv, context, k_out, v_out, rows, past, d, h, hd, name):
        if self.sm_count == 16 and self.config.combine_batches:
            match = re.match(r"^[ps]bothl\d+b([01])a$", name)
            if match is None or h != 8:
                raise ValueError("Expected two independent eight-head P1 batches")
            member = int(match[1])
            if member == 0:
                self.previous_batch_exports = {}
            self.partition_loops = {
                f"{name}h": lambda shard: (shard % h, h),
                f"{name}head": lambda shard: (shard % h, h),
            }
            for head in range(h):
                self.active_shard = member * h + head
                self.collect_attention_exports = member == 0
                self.export_operator_name = name
                self.current_head_exports = []
                if member == 1:
                    self.emit("WAIT", wg=self.group_name(self.active_shard),
                              events=self.previous_batch_exports[head])
                super().attention(qkv, context, k_out, v_out, rows, past, d, h, hd, name)
                if member == 0:
                    if len(self.current_head_exports) != 2:
                        raise ValueError("Expected exactly one K and one V export per head")
                    self.previous_batch_exports[head] = self.current_head_exports
                self.collect_attention_exports = False
            self.active_shard = 0
            self.partition_loops = set()
            if member == 1:
                self.barrier()
            return
        # 导出与消费循环均只处理各分片对应的 head。
        # 每次调用单独分配暂存空间，保证并发 softmax
        # 的临时数据不会在不同工作组之间重叠。
        self.parallel(super().attention, [f"{name}h", f"{name}head"],
                      qkv, context, k_out, v_out, rows, past, d, h, hd, name,
                      shards=min(self.sm_count, h))

    def attention_decode(self, qkv, context, history_k, history_v, new_k, new_v,
                         step, past, d, h, hd, name):
        self.parallel(super().attention_decode,
                      [f"{name}kh", f"{name}vh", f"{name}head"],
                      qkv, context, history_k, history_v, new_k, new_v,
                      step, past, d, h, hd, name, shards=min(self.sm_count, h))


class BatchBuilder(ParallelBuilder):
    '两个独立的 P1 批次指令流，仅在公开提交边界汇合。'

    sm_count = 2

    def parallel(self, method, partition_loops, *args, shards=None, **kwargs):
        # 公开编译器的算子名称包含批次标识。
        name = next((value for value in args if isinstance(value, str)
                     and re.match(r"^[ps]b[01]l", value)), None)
        if name is None:
            raise ValueError("Cannot identify batch for operator")
        self.active_shard = int(name[2])
        method(*args, **kwargs)

    @contextmanager
    def loop(self, name, start, stop, step=1):
        # 最终 hidden 输出复制在算子方法之外完成。
        if re.match(r"^[ps]b[01]output$", name):
            self.active_shard = int(name[2])
        with compiler_base_loop(self, name, start, stop, step) as value:
            yield value


compiler_base_loop = compiler.Builder.loop


def generate(case, sm_count=8, schedule="operator", batch_epochs=False, **kwargs):
    '使用与 compiler.py 相同的算子配置生成指定案例。'
    if schedule == "batch" and case != "M1_P1":
        raise ValueError("Batch-only schedule is defined for P1")
    if batch_epochs and (schedule != "operator" or case != "M1_P1"):
        raise ValueError("Independent batch epochs require P1 operator scheduling")
    if (schedule == "batch" or batch_epochs) and getattr(kwargs.get("config"), "combine_batches", False):
        raise ValueError("Combined batches require the default operator schedule")
    base = BatchBuilder if schedule == "batch" else ParallelBuilder

    class ConfiguredBuilder(base):
        pass

    ConfiguredBuilder.sm_count = 2 if schedule == "batch" else sm_count
    ConfiguredBuilder.batch_epochs = batch_epochs
    previous = compiler.Builder
    compiler.Builder = ConfiguredBuilder
    try:
        function = compiler.generate_m1_p1 if case == "M1_P1" else compiler.generate_m1_d1
        return function(**kwargs)
    finally:
        compiler.Builder = previous


def generate_m1_p1(sm_count=8, **kwargs):
    return generate("M1_P1", sm_count, **kwargs)


def generate_m1_d1(sm_count=8, **kwargs):
    return generate("M2_D1", sm_count, **kwargs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sm-count", type=int, default=8)
    parser.add_argument("--case", choices=["M1_P1", "M2_D1", "both"], default="both")
    parser.add_argument("--config", type=Path, help="JSON keyword arguments for CompilerConfig")
    parser.add_argument("--schedule", choices=["operator", "batch"], default="operator")
    parser.add_argument("--batch-epochs", action="store_true",
                        help="Use independent eight-WG lifetimes for the two P1 batches")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases = ["M1_P1", "M2_D1"] if args.case == "both" else [args.case]
    metadata = {}
    config = compiler.CompilerConfig(**json.loads(args.config.read_text())) if args.config else None
    for case in cases:
        program, _, cursor = generate(case, args.sm_count, schedule=args.schedule,
                                      batch_epochs=args.batch_epochs, config=config)
        (args.output_dir / f"{case}.asm").write_text(program)
        metadata[case] = {"lines": len(program.splitlines()), "scratch_end_words": cursor}
    (args.output_dir / "compiler-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata))
