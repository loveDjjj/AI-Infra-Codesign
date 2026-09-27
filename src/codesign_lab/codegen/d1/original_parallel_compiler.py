'公开字面 ISA 编译器的并行工作组调度。算子仍由 compiler.Builder 实现；本模块把独立输出循环分给多个 SM，并在算子之间添加跨工作组屏障。硬件、评分、工作负载与参考程序保持不变。'

import argparse
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
            for shard in range(self.sm_count):
                super().emit(op, wg=self.group_name(shard), sm=shard, shared_bytes=0)
            return
        if op == "WG.END":
            for shard in range(self.sm_count):
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
        self.emit("BARRIER", wgs=[self.group_name(shard) for shard in range(self.sm_count)], events=[])

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
