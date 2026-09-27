'D1 稠密权重跨提交边界保留在每个 SM 的四个合法工作组中。每层在每个 SM 上分别配置 attention 与 FFN 工作组；权重位于 RF4..7，激活计算使用 RF0..3，跨工作组算子通过屏障同步。'
import re
from contextlib import contextmanager
from . import compiler


class PersistentBuilder(compiler.Builder):
    sm_count = 16
    layer_count = 2
    preload_group_size = 4
    # 算子类型、每个 SM 的列数、K 偏移与长度、常驻 RF 通道。
    specifications = {
        "wqkv": ("a", 24, ((0, 64, 4), (64, 64, 5))),
        "wo": ("a", 8, ((0, 128, 6),)),
        "w1": ("f", 32, ((0, 64, 4), (64, 64, 5))),
        "w2": ("f", 8, ((0, 256, 6), (256, 256, 7))),
    }

    def __init__(self, *args, **kwargs):
        self.active_layer, self.active_kind, self.active_shard = 0, "a", 0
        self.partition_loops = set()
        self.weight_specs = {}
        super().__init__(*args, **kwargs)
        if not self.config.gemm_epilogue or not self.config.attention_rf:
            raise ValueError("Persistent D1 requires fused epilogues and RF attention")
        self.preload_weights()

    def group_name(self, layer=None, kind=None, shard=None):
        return (f"l{self.active_layer if layer is None else layer}_"
                f"{self.active_kind if kind is None else kind}_"
                f"{self.active_shard if shard is None else shard}")

    def all_groups(self):
        return [self.group_name(layer, kind, shard)
                for layer in range(self.layer_count) for kind in ("a", "f")
                for shard in range(self.sm_count)]

    def emit(self, op, **args):
        if op == "WG.BEGIN":
            for layer in range(self.layer_count):
                for kind in ("a", "f"):
                    for shard in range(self.sm_count):
                        super().emit(op, wg=self.group_name(layer, kind, shard), sm=shard,
                                     shared_bytes=0)
            return
        if op == "WG.END":
            for group in self.all_groups():
                super().emit(op, wg=group)
            return
        def rewrite(value):
            if isinstance(value, dict):
                return {key: (self.group_name() if key == "wg" and item == "g" else rewrite(item))
                        for key, item in value.items()}
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            return value
        super().emit(op, **rewrite(args))

    def barrier(self):
        self.emit("BARRIER", wgs=self.all_groups(), events=[])

    def select_layer(self, name):
        match = re.match(r"^d\d+l(\d+)", name)
        if not match:
            raise ValueError(f"Unrecognized D1 operator: {name}")
        self.active_layer = int(match[1])
        self.active_kind, self.active_shard = "a", 0

    @contextmanager
    def loop(self, name, start, stop, step=1):
        if name in self.partition_loops:
            start = min(start + self.active_shard * step, stop)
            step *= 4
        with super().loop(name, start, stop, step) as value:
            yield value

    def preload_weights(self):
        # 先处理偶数列，使相邻 N8 分片复用已填充的缓存行。
        # 小批次预载限制冷启动访存的功耗突发。
        previous_groups = {}
        pending_by_sm = {}
        for layer in range(self.layer_count):
            self.active_layer = layer
            for name, (kind, columns, chunks) in self.specifications.items():
                order = (list(range(self.sm_count)) if columns % 16 == 0 else
                         list(range(0, self.sm_count, 2)) + list(range(1, self.sm_count, 2)))
                weight = self.symbol(f"layer{layer}/{name}")
                k_extent, n_extent = weight.shape
                if n_extent != columns * self.sm_count or sum(c[1] for c in chunks) != k_extent:
                    raise ValueError("Weight partition does not match model shape")
                self.weight_specs[weight.offset] = (layer, kind, columns, chunks)
                self.active_kind = kind
                for begin in range(0, self.sm_count, self.preload_group_size):
                    for shard in order[begin:begin + self.preload_group_size]:
                        self.active_shard = shard
                        group = self.group_name()
                        if previous_groups.get(shard) not in (None, group):
                            self.emit("WAIT", wg=group, events=pending_by_sm[shard])
                            pending_by_sm[shard] = []
                        previous_groups[shard] = group
                        pending_by_sm.setdefault(shard, [])
                        for inner, depth, lane in chunks:
                            if depth * columns > 2048:
                                raise ValueError("Resident weight exceeds RF lane capacity")
                            self.ld(compiler.hbm(weight.offset + inner * n_extent + shard * columns,
                                                 depth * columns, [depth, columns], [n_extent, 1]),
                                    compiler.rf(lane, depth * columns))
                            pending_by_sm[shard].append(f"e{self.serial}")
        self.barrier()
        self.active_layer, self.active_kind, self.active_shard = 0, "a", 0

    def layernorm(self, source, gamma, beta, out, rows, d, name):
        self.select_layer(name)
        super().layernorm(source, gamma, beta, out, rows, d, name)
        self.barrier()

    def attention_decode(self, qkv, context, history_k, history_v, new_k, new_v,
                         step, past, d, h, hd, name):
        self.select_layer(name)
        if h != 4:
            raise ValueError("Expected four D1 heads")
        self.partition_loops = {f"{name}kh", f"{name}vh", f"{name}head"}
        for shard in range(h):
            self.active_shard = shard
            super().attention_decode(qkv, context, history_k, history_v, new_k, new_v,
                                     step, past, d, h, hd, name)
        self.partition_loops = set()
        self.active_shard = 0
        self.barrier()

    def gemm(self, a, b, out, m_extent, k_extent, n_extent, name, *,
             bias=None, activation=False, residual=None):
        self.select_layer(name)
        layer, kind, columns, chunks = self.weight_specs[b.offset]
        if layer != self.active_layer or m_extent != 1 or b.shape != (k_extent, n_extent):
            raise ValueError("Unexpected persistent GEMM shape/layer")
        self.active_kind = kind
        for shard in range(self.sm_count):
            self.active_shard = shard
            col = shard * columns
            acc = compiler.rf(2, columns)
            self.vec("add", [compiler.imm(0), compiler.imm(0)], acc)
            for inner, depth, lane in chunks:
                self.ld(compiler.hbm(a.offset + inner, depth), compiler.rf(0, depth))
                self.emit("MMA.ACC", a=compiler.rf(0, depth),
                          b=compiler.rf(lane, depth * columns), acc=acc,
                          m=1, n=columns, k=depth, event=None)
            if residual is not None:
                self.ld(compiler.hbm(residual.offset + col, columns), compiler.rf(0, columns))
                self.vec("add", [compiler.rf(0, columns), acc], acc)
            if bias is not None:
                self.ld(compiler.hbm(bias.offset + col, columns), compiler.rf(0, columns))
                self.vec("add", [acc, compiler.rf(0, columns)], acc)
            if activation:
                self.gelu_rf(acc, compiler.rf(1, columns))
            self.st(acc, compiler.hbm(out.offset + col, columns))
        self.active_kind, self.active_shard = "a", 0
        self.barrier()


def generate(case, sm_count=16, schedule="operator", config=None, **kwargs):
    if case != "M2_D1" or sm_count != 16 or schedule != "operator" or kwargs:
        raise ValueError("This persistent scheduler supports D1 on 16 SMs")
    previous = compiler.Builder
    compiler.Builder = PersistentBuilder
    try:
        return compiler.generate_m1_d1(config=config)
    finally:
        compiler.Builder = previous
