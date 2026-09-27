'稳定的分片 softmax：合并局部最大值、分母及加权上下文。'
from math import sqrt
from . import compiler
from .attention_base import ResidentBuilder

rf, hbm, imm = compiler.rf, compiler.hbm, compiler.imm


class OnlineBuilder(ResidentBuilder):
    def attention_decode(self, qkv, context, history_k, history_v, new_k, new_v,
                         step, past, d, h, hd, name):
        self.select_layer(name)
        if (past, d, h, hd) != (128, 128, 4, 32):
            raise ValueError("Expected D1 attention geometry")
        generated = step + 1
        width = hd + 2
        partials = self.alloc(name + "_online_partials", h, 4, width)
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
            count = 32 + (generated if part == 0 else 0)
            self.ld(hbm(qkv.offset + head * hd, hd), rf(0, hd))
            self.vec("add", [imm(0), imm(0)], rf(2, count))
            keys = dict(rf(7, 1024), shape=[hd, 32], strides=[1, hd])
            self.emit("MMA.ACC", a=rf(0, hd), b=keys, acc=rf(2, 32),
                      m=1, n=32, k=hd, event=None)
            if part == 0:
                new_keys = dict(rf(3, hd * generated, 512), shape=[hd, generated], strides=[1, hd])
                self.emit("MMA.ACC", a=rf(0, hd), b=new_keys, acc=rf(2, generated, 32),
                          m=1, n=generated, k=hd, event=None)
            self.vec("mul", [rf(2, count), imm(1 / sqrt(hd))], rf(2, count))
            self.reduce("max", rf(2, count), rf(1, 1, hd))
            self.vec("sub", [rf(2, count), rf(1, 1, hd)], rf(2, count))
            self.sfu("exp", rf(2, count), rf(2, count))
            self.reduce("sum", rf(2, count), rf(1, 1, hd + 1))
            self.vec("add", [imm(0), imm(0)], rf(1, hd))
            self.emit("MMA.ACC", a=rf(2, 32), b=rf(7, 1024, 1024), acc=rf(1, hd),
                      m=1, n=hd, k=32, event=None)
            if part == 0:
                self.emit("MMA.ACC", a=rf(2, generated, 32), b=rf(3, generated * hd, 1024),
                          acc=rf(1, hd), m=1, n=hd, k=generated, event=None)
            else:
                self.st(rf(1, width), hbm(partials.offset + shard * width, width))
                partial_events[head].append(f"e{self.serial}")
        for head in range(h):
            self.active_shard = 4 * head
            self.emit("WAIT", wg=self.group_name(), events=partial_events[head])
            self.ld(hbm(partials.offset + (head * 4 + 1) * width, 3 * width), rf(0, 3 * width))
            others_max = dict(rf(0, 3, hd), shape=[3], strides=[width])
            others_sum = dict(rf(0, 3, hd + 1), shape=[3], strides=[width])
            self.vec("add", [rf(1, 1, hd), imm(0)], rf(2, 1))
            self.vec("add", [others_max, imm(0)], rf(2, 3, 1))
            self.reduce("max", rf(2, 4), rf(2, 1, 256))
            self.vec("sub", [rf(2, 4), rf(2, 1, 256)], rf(2, 4))
            self.sfu("exp", rf(2, 4), rf(2, 4))
            self.vec("mul", [others_sum, rf(2, 3, 1)], rf(2, 3, 4))
            self.vec("mul", [rf(1, 1, hd + 1), rf(2, 1)], rf(2, 1, 7))
            self.reduce("sum", rf(2, 4, 4), rf(2, 1, 8))
            self.vec("mul", [rf(1, hd), rf(2, 1)], rf(1, hd))
            contexts = dict(rf(0, 3 * hd), shape=[3, hd], strides=[width, 1])
            self.emit("MMA.ACC", a=rf(2, 3, 1), b=contexts, acc=rf(1, hd),
                      m=1, n=hd, k=3, event=None)
            self.vec("div", [rf(1, hd), rf(2, 1, 8)], rf(1, hd))
            self.st(rf(1, hd), hbm(context.offset + head * hd, hd))
        self.active_shard = 0
        self.barrier()


def generate(case, sm_count=16, schedule="operator", config=None, **kwargs):
    if case != "M2_D1" or sm_count != 16 or schedule != "operator" or kwargs:
        raise ValueError("Online attention scheduler supports D1 on 16 SMs")
    previous = compiler.Builder
    compiler.Builder = OnlineBuilder
    try:
        return compiler.generate_m1_d1(config=config)
    finally:
        compiler.Builder = previous
