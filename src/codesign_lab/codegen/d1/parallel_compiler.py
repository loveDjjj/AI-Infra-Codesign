'在 16 个 SM 上，W2 使用八个 N16 输出分片和两个 K256 归约分片。'
from . import compiler
from .lazy_base import LazyBuilder

rf, hbm, imm = compiler.rf, compiler.hbm, compiler.imm


class SplitW2Builder(LazyBuilder):
    def gemm(self, a, b, out, m_extent, k_extent, n_extent, name, *,
             bias=None, activation=False, residual=None):
        if not name.endswith("w2"):
            return super().gemm(a, b, out, m_extent, k_extent, n_extent, name,
                                bias=bias, activation=activation, residual=residual)
        self.select_layer(name)
        self.active_kind = "f"
        if (m_extent, k_extent, n_extent) != (1, 512, 128) or activation:
            raise ValueError("Expected final FFN projection")
        cold = b.offset not in self.loaded_weights
        partials = self.alloc(name + "_w2_partials", 8, 16)
        events = {}
        for shard in range(self.sm_count):
            self.active_shard = shard
            self.ld(hbm(a.offset, 512), rf(0, 512))
            self.vec("add", [imm(0), imm(0)], rf(2, 16))
        for index, (inner, lane) in enumerate(((0, 6), (128, 7))):
            for shard in range(self.sm_count):
                self.active_shard = shard
                start_k = (shard // 8) * 256 + inner
                col = (shard % 8) * 16
                if cold:
                    self.ld(hbm(b.offset + start_k * n_extent + col, 2048,
                                [128, 16], [n_extent, 1]), rf(lane, 2048))
                self.emit("MMA.ACC", a=rf(0, 128, start_k), b=rf(lane, 2048),
                          acc=rf(2, 16), m=1, n=16, k=128, event=None)
                # 仅首次权重加载分组同步；默认 16 保持原汇编字节。
                if cold and (shard + 1) % self.config.w2_load_group_size == 0 and shard + 1 < self.sm_count:
                    self.barrier()
            if cold and index == 0:
                self.barrier()
        for shard in range(8, 16):
            self.active_shard = shard
            self.st(rf(2, 16), hbm(partials.offset + (shard - 8) * 16, 16))
            events[shard - 8] = f"e{self.serial}"
        for owner in range(8):
            self.active_shard = owner
            col = owner * 16
            self.emit("WAIT", wg=self.group_name(), events=[events[owner]])
            self.ld(hbm(partials.offset + col, 16), rf(1, 16))
            self.vec("add", [rf(2, 16), rf(1, 16)], rf(2, 16))
            if residual is not None:
                if self.saved_sources.get((self.active_layer, "f")) != residual.offset:
                    raise ValueError("Residual differs from saved LayerNorm input")
                self.vec("add", [rf(3, 16, 512 + col), rf(2, 16)], rf(2, 16))
            if bias is not None:
                self.vec("add", [rf(2, 16), rf(3, 16, 288)], rf(2, 16))
            self.st(rf(2, 16), hbm(out.offset + col, 16))
        self.loaded_weights.add(b.offset)
        self.active_kind, self.active_shard = "a", 0
        self.barrier()


def generate(case, sm_count=16, schedule="operator", config=None, **kwargs):
    if case != "M2_D1" or sm_count != 16 or schedule != "operator" or kwargs:
        raise ValueError("Split W2 scheduler supports D1 on 16 SMs")
    previous = compiler.Builder
    compiler.Builder = SplitW2Builder
    try:
        return compiler.generate_m1_d1(config=config)
    finally:
        compiler.Builder = previous
