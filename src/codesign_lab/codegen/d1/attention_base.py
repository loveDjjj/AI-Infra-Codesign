'D1 权重与历史 K/V 常驻 RF，采用分片 attention 和融合归一化。每个 head 的四个分片分别在 RF7 保存 32 个历史位置；分片通过 HBM 与屏障交换分数、概率和局部上下文。每个消费者本地执行 LayerNorm，结果保留在 RF0。'
from math import sqrt
from . import compiler
from .persistent_base import PersistentBuilder

rf, hbm, imm = compiler.rf, compiler.hbm, compiler.imm


class ResidentBuilder(PersistentBuilder):
    preload_group_size = 16

    def preload_weights(self):
        self.normalized_inputs = {}
        self.saved_sources = {}
        self.bias_views = {}
        super().preload_weights()
        for layer in range(self.layer_count):
            self.active_layer = layer
            prefix = f"layer{layer}/"
            history_k, history_v = self.symbol(prefix + "history_k"), self.symbol(prefix + "history_v")
            for begin in range(0, self.sm_count, self.preload_group_size):
                for shard in range(begin, min(begin + self.preload_group_size, self.sm_count)):
                    self.active_shard = shard
                    self.active_kind = "a"
                    first_transfer = self.serial + 1
                    head, part = divmod(shard, 4)
                    history_offset = (head * 128 + part * 32) * 32
                    self.ld(hbm(history_k.offset + history_offset, 1024), rf(7, 1024))
                    self.ld(hbm(history_v.offset + history_offset, 1024), rf(7, 1024, 1024))
                    self.ld(hbm(self.symbol(prefix + "ln1_g").offset, 128), rf(6, 128, 1024))
                    self.ld(hbm(self.symbol(prefix + "ln1_b").offset, 128), rf(6, 128, 1152))
                    self.active_kind = "f"
                    self.emit("WAIT", wg=self.group_name(),
                              events=[f"e{n}" for n in range(first_transfer, self.serial + 1)])
                    self.ld(hbm(self.symbol(prefix + "ln2_g").offset, 128), rf(3, 128))
                    self.ld(hbm(self.symbol(prefix + "ln2_b").offset, 128), rf(3, 128, 128))
                    b1, b2 = self.symbol(prefix + "b1"), self.symbol(prefix + "b2")
                    self.ld(hbm(b1.offset + shard * 32, 32), rf(3, 32, 256))
                    self.ld(hbm(b2.offset + shard * 8, 8), rf(3, 8, 288))
                    self.bias_views[b1.offset] = (3, 256, 32)
                    self.bias_views[b2.offset] = (3, 288, 8)
                self.barrier()
        self.active_layer, self.active_kind, self.active_shard = 0, "a", 0

    def layernorm(self, source, gamma, beta, out, rows, d, name):
        self.select_layer(name)
        if rows != 1 or d != 128:
            raise ValueError("Expected a single D1 row")
        kind = "a" if name.endswith("ln1") else "f"
        self.active_kind = kind
        self.normalized_inputs[out.offset] = (self.active_layer, kind)
        self.saved_sources[self.active_layer, kind] = source.offset
        for shard in range(self.sm_count):
            self.active_shard = shard
            self.ld(hbm(source.offset, d), rf(0, d))
            saved = rf(6, d, 1280) if kind == "a" else rf(3, d, 512)
            self.vec("add", [rf(0, d), imm(0)], saved)
            scalar = rf(1, 1, 256)
            self.reduce("sum", rf(0, d), scalar)
            self.vec("mul", [scalar, imm(1 / d)], scalar)
            self.vec("sub", [rf(0, d), scalar], rf(2, d))
            self.vec("mul", [rf(2, d), rf(2, d)], rf(1, d))
            self.reduce("sum", rf(1, d), scalar)
            self.vec("fma", [scalar, imm(1 / d), imm(1e-5)], scalar)
            self.sfu("rsqrt", scalar, scalar)
            self.vec("mul", [rf(2, d), scalar], rf(2, d))
            g = rf(6, d, 1024) if kind == "a" else rf(3, d)
            b = rf(6, d, 1152) if kind == "a" else rf(3, d, 128)
            self.vec("fma", [rf(2, d), g, b], rf(0, d))
        # 每个消费者读取自己的 RF0，不引入跨工作组依赖。
        self.active_shard = 0

    def gemm(self, a, b, out, m_extent, k_extent, n_extent, name, *,
             bias=None, activation=False, residual=None):
        self.select_layer(name)
        layer, kind, columns, chunks = self.weight_specs[b.offset]
        if layer != self.active_layer or m_extent != 1 or b.shape != (k_extent, n_extent):
            raise ValueError("Unexpected persistent GEMM")
        resident_input = self.normalized_inputs.get(a.offset)
        if resident_input is not None and resident_input != (layer, kind):
            raise ValueError("Normalized RF input belongs to another workgroup")
        self.active_kind = kind
        for shard in range(self.sm_count):
            self.active_shard = shard
            col = shard * columns
            if resident_input is None:
                self.ld(hbm(a.offset, k_extent), rf(0, k_extent))
            acc = rf(2, columns)
            self.vec("add", [imm(0), imm(0)], acc)
            for inner, depth, lane in chunks:
                self.emit("MMA.ACC", a=rf(0, depth, inner), b=rf(lane, depth * columns),
                          acc=acc, m=1, n=columns, k=depth, event=None)
            if residual is not None:
                if self.saved_sources.get((layer, kind)) != residual.offset:
                    raise ValueError("Residual differs from saved LayerNorm input")
                saved = rf(6, columns, 1280 + col) if kind == "a" else rf(3, columns, 512 + col)
                self.vec("add", [saved, acc], acc)
            if bias is not None:
                lane, offset, size = self.bias_views[bias.offset]
                if size != columns:
                    raise ValueError("Bias shape mismatch")
                self.vec("add", [acc, rf(lane, size, offset)], acc)
            if activation:
                self.gelu_rf(acc, rf(1, columns))
            self.st(acc, hbm(out.offset + col, columns))
        self.active_kind, self.active_shard = "a", 0
        self.barrier()

    def attention_decode(self, qkv, context, history_k, history_v, new_k, new_v,
                         step, past, d, h, hd, name):
        self.select_layer(name)
        if (past, d, h, hd) != (128, 128, 4, 32):
            raise ValueError("Expected D1 attention geometry")
        generated, total = step + 1, past + step + 1
        scores = self.alloc(name + "_scores", h, total)
        partials = self.alloc(name + "_partials", h, 4, hd)
        score_events = [[] for _ in range(h)]
        partial_events = [[] for _ in range(h)]
        for head in range(h):
            self.active_shard = 4 * head
            for target, component in ((new_k, 1), (new_v, 2)):
                resident = rf(3, hd, component * 512 + step * hd)
                self.ld(hbm(qkv.offset + component * d + head * hd, hd), resident)
                self.st(resident, hbm(target.offset + (head * target.shape[2] + step) * hd, hd))
        for shard in range(self.sm_count):
            self.active_shard = shard
            head, part = divmod(shard, 4)
            self.ld(hbm(qkv.offset + head * hd, hd), rf(0, hd))
            self.vec("add", [imm(0), imm(0)], rf(2, 32))
            keys = dict(rf(7, 1024), shape=[hd, 32], strides=[1, hd])
            self.emit("MMA.ACC", a=rf(0, hd), b=keys, acc=rf(2, 32),
                      m=1, n=32, k=hd, event=None)
            self.vec("mul", [rf(2, 32), imm(1 / sqrt(hd))], rf(2, 32))
            self.st(rf(2, 32), hbm(scores.offset + head * total + part * 32, 32))
            score_events[head].append(f"e{self.serial}")
            if part == 0:
                self.vec("add", [imm(0), imm(0)], rf(2, generated))
                resident_keys = dict(rf(3, hd * generated, 512), shape=[hd, generated], strides=[1, hd])
                self.emit("MMA.ACC", a=rf(0, hd), b=resident_keys,
                          acc=rf(2, generated), m=1, n=generated, k=hd, event=None)
                self.vec("mul", [rf(2, generated), imm(1 / sqrt(hd))], rf(2, generated))
                self.st(rf(2, generated), hbm(scores.offset + head * total + past, generated))
                score_events[head].append(f"e{self.serial}")
        for shard in range(self.sm_count):
            self.active_shard = shard
            head, part = divmod(shard, 4)
            self.emit("WAIT", wg=self.group_name(), events=score_events[head])
            probability = rf(3, total)
            self.ld(hbm(scores.offset + head * total, total), probability)
            self.reduce("max", probability, rf(1, 1))
            self.vec("sub", [probability, rf(1, 1)], probability)
            self.sfu("exp", probability, probability)
            self.reduce("sum", probability, rf(1, 1))
            self.vec("div", [probability, rf(1, 1)], probability)
            self.vec("add", [imm(0), imm(0)], rf(2, hd))
            self.emit("MMA.ACC", a=rf(3, 32, part * 32), b=rf(7, 1024, 1024), acc=rf(2, hd),
                      m=1, n=hd, k=32, event=None)
            if part:
                self.st(rf(2, hd), hbm(partials.offset + shard * hd, hd))
                partial_events[head].append(f"e{self.serial}")
        for head in range(h):
            self.active_shard = 4 * head
            self.emit("WAIT", wg=self.group_name(), events=partial_events[head])
            self.ld(hbm(partials.offset + (head * 4 + 1) * hd, 3 * hd), rf(0, 3 * hd))
            for part in range(1, 4):
                self.vec("add", [rf(2, hd), rf(0, hd, (part - 1) * hd)], rf(2, hd))
            self.emit("MMA.ACC", a=rf(3, generated, past), b=rf(3, generated * hd, 1024), acc=rf(2, hd),
                      m=1, n=hd, k=generated, event=None)
            self.st(rf(2, hd), hbm(context.offset + head * hd, hd))
        self.active_shard = 0
        self.barrier()


def generate(case, sm_count=16, schedule="operator", config=None, **kwargs):
    if case != "M2_D1" or sm_count != 16 or schedule != "operator" or kwargs:
        raise ValueError("Resident attention scheduler supports D1 on 16 SMs")
    previous = compiler.Builder
    compiler.Builder = ResidentBuilder
    try:
        return compiler.generate_m1_d1(config=config)
    finally:
        compiler.Builder = previous
