'权重 K 分块首次使用时加载，并保留给后续 decode 步骤。'
from . import compiler
from .online_base import OnlineBuilder

rf, hbm, imm = compiler.rf, compiler.hbm, compiler.imm


class LazyBuilder(OnlineBuilder):
    def preload_weights(self):
        self.normalized_inputs, self.saved_sources, self.bias_views = {}, {}, {}
        self.loaded_weights, self.loaded_norms, self.loaded_history = set(), set(), set()
        for layer in range(self.layer_count):
            for name, (kind, columns, chunks) in self.specifications.items():
                weight = self.symbol(f"layer{layer}/{name}")
                self.weight_specs[weight.offset] = (layer, kind, columns, chunks)
            self.bias_views[self.symbol(f"layer{layer}/b1").offset] = (3, 256, 32)
            self.bias_views[self.symbol(f"layer{layer}/b2").offset] = (3, 288, 8)

    def layernorm(self, source, gamma, beta, out, rows, d, name):
        self.select_layer(name)
        kind = "a" if name.endswith("ln1") else "f"
        key = self.active_layer, kind
        if key not in self.loaded_norms:
            self.active_kind = kind
            prefix = f"layer{self.active_layer}/"
            for shard in range(self.sm_count):
                self.active_shard = shard
                if kind == "a":
                    self.ld(hbm(gamma.offset, d), rf(6, d, 1024))
                    self.ld(hbm(beta.offset, d), rf(6, d, 1152))
                else:
                    self.ld(hbm(gamma.offset, d), rf(3, d))
                    self.ld(hbm(beta.offset, d), rf(3, d, 128))
                    self.ld(hbm(self.symbol(prefix + "b1").offset + shard * 32, 32), rf(3, 32, 256))
                    if shard < 8:
                        self.ld(hbm(self.symbol(prefix + "b2").offset + shard * 16, 16), rf(3, 16, 288))
            self.loaded_norms.add(key)
        super().layernorm(source, gamma, beta, out, rows, d, name)

    def attention_decode(self, qkv, context, history_k, history_v, new_k, new_v,
                         step, past, d, h, hd, name):
        self.select_layer(name)
        if self.active_layer not in self.loaded_history:
            for shard in range(self.sm_count):
                self.active_shard = shard
                head, part = divmod(shard, 4)
                offset = (head * past + part * 32) * hd
                self.ld(hbm(history_k.offset + offset, 1024), rf(7, 1024))
                self.ld(hbm(history_v.offset + offset, 1024), rf(7, 1024, 1024))
            self.loaded_history.add(self.active_layer)
        super().attention_decode(qkv, context, history_k, history_v, new_k, new_v,
                                 step, past, d, h, hd, name)

    def gemm(self, a, b, out, m_extent, k_extent, n_extent, name, *,
             bias=None, activation=False, residual=None):
        if b.offset in self.loaded_weights:
            return super().gemm(a, b, out, m_extent, k_extent, n_extent, name,
                                bias=bias, activation=activation, residual=residual)
        self.select_layer(name)
        layer, kind, columns, chunks = self.weight_specs[b.offset]
        if layer != self.active_layer or m_extent != 1 or b.shape != (k_extent, n_extent):
            raise ValueError("Unexpected lazy GEMM")
        resident_input = self.normalized_inputs.get(a.offset)
        if resident_input is not None and resident_input != (layer, kind):
            raise ValueError("Normalized RF input belongs to another workgroup")
        self.active_kind = kind
        for shard in range(self.sm_count):
            self.active_shard = shard
            if resident_input is None:
                self.ld(hbm(a.offset, k_extent), rf(0, k_extent))
            self.vec("add", [imm(0), imm(0)], rf(2, columns))
        for index, (inner, depth, lane) in enumerate(chunks):
            for shard in range(self.sm_count):
                self.active_shard = shard
                self.ld(hbm(b.offset + inner * n_extent + shard * columns,
                            depth * columns, [depth, columns], [n_extent, 1]),
                        rf(lane, depth * columns))
                self.emit("MMA.ACC", a=rf(0, depth, inner), b=rf(lane, depth * columns),
                          acc=rf(2, columns), m=1, n=columns, k=depth, event=None)
            if index + 1 < len(chunks):
                self.barrier()
        for shard in range(self.sm_count):
            self.active_shard = shard
            col = shard * columns
            acc = rf(2, columns)
            if residual is not None:
                if self.saved_sources.get((layer, kind)) != residual.offset:
                    raise ValueError("Residual differs from saved LayerNorm input")
                saved = rf(6, columns, 1280 + col) if kind == "a" else rf(3, columns, 512 + col)
                self.vec("add", [saved, acc], acc)
            if bias is not None:
                lane, offset, size = self.bias_views[bias.offset]
                self.vec("add", [acc, rf(lane, size, offset)], acc)
            if activation:
                self.gelu_rf(acc, rf(1, columns))
            self.st(acc, hbm(out.offset + col, columns))
        self.loaded_weights.add(b.offset)
        self.active_kind, self.active_shard = "a", 0
        self.barrier()


def generate(case, sm_count=16, schedule="operator", config=None, **kwargs):
    if case != "M2_D1" or sm_count != 16 or schedule != "operator" or kwargs:
        raise ValueError("Lazy scheduler supports D1 on 16 SMs")
    previous = compiler.Builder
    compiler.Builder = LazyBuilder
    try:
        return compiler.generate_m1_d1(config=config)
    finally:
        compiler.Builder = previous
