'为两个试验案例生成字面 ISA 程序。Python 仅负责生成汇编；输出没有 LayerNorm、attention、softmax 或 GELU 的算子级指令。'

import json
from contextlib import contextmanager
from dataclasses import dataclass
from math import sqrt

from codesign.challenge.abi import Layout, build_layout
from codesign.challenge.workload import MODELS, SCENARIOS


def var(name):
    return {"var": name}


def add(*items):
    return {"add": list(items)}


def mul(*items):
    return {"mul": list(items)}


def minimum(*items):
    return {"min": list(items)}


def sub(a, b):
    return add(a, mul(-1, b))


def hbm(offset, count, shape=None, strides=None):
    result = {"space": "HBM", "offset": offset, "count": count, "wg": None, "lane": 0}
    if shape is not None:
        result.update(shape=shape, strides=strides)
    return result


def rf(lane, count, offset=0):
    return {"space": "RF", "offset": offset, "count": count, "wg": "g", "lane": lane}


def imm(value):
    return {"imm": value}


@dataclass(frozen=True)
class Tensor:
    offset: int
    shape: tuple[int, ...]


@dataclass(frozen=True)
class CompilerConfig:
    '软件配置，与提交的硬件配置独立。'

    gemm_m_tile: int = 8
    gemm_n_tile: int = 16
    gemm_narrow_n_tile: int | None = None
    gemm_k_tile: int = 48
    w1_preload_k: int = 32
    w2_preload_k: int = 32
    attention_rf: bool = False
    attention_key_tile: int = 16
    attention_value_tile: int = 16
    fuse_ffn: bool = False
    gemm_epilogue: bool = False
    gemm_double_buffer: bool = False
    gemm_prefetch: bool = False
    gemm_n_group: int = 1
    attention_blocked: bool = False
    attention_query_tile: int = 32
    attention_triangular: bool = False
    attention_dedicated_rf: bool = False
    vector_tile: int = 384
    combine_batches: bool = False

    def __post_init__(self):
        if self.gemm_n_group not in (1, 2):
            raise ValueError("gemm_n_group must be 1 or 2")
        if self.gemm_n_group == 2 and not self.gemm_prefetch:
            raise ValueError("Paired N tiles require prefetch")
        if self.gemm_narrow_n_tile is not None and (
            type(self.gemm_narrow_n_tile) is not int or self.gemm_narrow_n_tile <= 0
        ):
            raise ValueError("gemm_narrow_n_tile must be a positive integer or None")
        for name in (
            "gemm_m_tile", "gemm_n_tile", "gemm_k_tile",
            "attention_key_tile", "attention_value_tile",
            "attention_query_tile",
            "vector_tile",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("w1_preload_k", "w2_preload_k"):
            if getattr(self, name) not in (16, 32, 64):
                raise ValueError(f"{name} must be one of 16, 32, 64")


class Builder:
    def __init__(self, layout: Layout, gemm_n_tile: int = 16, *, config: CompilerConfig | None = None):
        self.layout = layout
        self.config = config or CompilerConfig(gemm_n_tile=gemm_n_tile)
        self.gemm_n_tile = self.config.gemm_n_tile
        self.lines = []
        self.loops = []
        self.serial = 0
        self.cursor = layout.symbols["scratch"].address // 4
        self.tensors = {}
        self.emit("WG.BEGIN", wg="g", sm=0, shared_bytes=0)

    def symbol(self, name):
        item = self.layout.symbols[name]
        return Tensor(item.address // 4, item.shape)

    def alloc(self, name, *shape):
        size = 1
        for extent in shape:
            size *= extent
        result = Tensor(self.cursor, tuple(shape))
        self.cursor += size
        self.tensors[name] = result
        return result

    def emit(self, op, **args):
        if "event" in args and args["event"] is None:
            self.serial += 1
            suffix = "".join(f"_{{{name}}}" for name in self.loops)
            args["event"] = f"e{self.serial}{suffix}"
        self.lines.append(f"{op} {json.dumps(args, separators=(',', ':'))}")

    @contextmanager
    def loop(self, name, start, stop, step=1):
        self.emit("FOR", var=name, start=start, stop=stop, step=step)
        self.loops.append(name)
        yield var(name)
        self.loops.pop()
        self.emit("END.FOR")

    def ld(self, source, target):
        self.emit("LD", src=source, dst=target, event=None)

    def st(self, source, target):
        self.emit("ST", src=source, dst=target, event=None)

    def vec(self, kind, sources, target):
        self.emit("VEC", kind=kind, src=sources, dst=target, event=None)

    def reduce(self, kind, source, target):
        self.emit("REDUCE", kind=kind, src=source, dst=target, event=None)

    def sfu(self, kind, source, target):
        self.emit("SFU", kind=kind, src=source, dst=target, event=None)

    def gemm_tile_n(self, n_extent: int) -> int:
        if n_extent <= 128 and self.config.gemm_narrow_n_tile is not None:
            return self.config.gemm_narrow_n_tile
        return self.gemm_n_tile

    def gemm(
        self,
        a: Tensor,
        b: Tensor,
        out: Tensor,
        m_extent: int,
        k_extent: int,
        n_extent: int,
        name: str,
        *,
        bias: Tensor | None = None,
        activation: bool = False,
        residual: Tensor | None = None,
    ):
        if b.shape != (k_extent, n_extent):
            raise ValueError("GEMM weight shape")
        if self.config.gemm_n_group == 2:
            return self.gemm_pair(a, b, out, m_extent, k_extent, n_extent, name,
                                  bias=bias, activation=activation, residual=residual)
        tile_n = self.gemm_tile_n(n_extent)
        if n_extent % tile_n:
            raise ValueError("GEMM N extent does not fit tile")
        tile_m, tile_k = self.config.gemm_m_tile, self.config.gemm_k_tile
        with self.loop(f"{name}m", 0, m_extent, tile_m) as row:
            rows = minimum(tile_m, sub(m_extent, row))
            with self.loop(f"{name}n", 0, n_extent, tile_n) as col:
                cells = mul(rows, tile_n)
                self.vec("add", [imm(0), imm(0)], rf(2, cells))

                def tile_operands(inner):
                    depth = minimum(tile_k, sub(k_extent, inner))
                    buffer_lane = (mul(4, {"mod": [{"ceildiv": [inner, tile_k]}, 2]})
                                   if self.config.gemm_double_buffer or self.config.gemm_prefetch else 0)
                    a_block = rf(buffer_lane, mul(rows, depth))
                    b_block = rf(1 if buffer_lane == 0 else add(buffer_lane, 1), mul(depth, tile_n))
                    return depth, a_block, b_block

                def load_tile(inner):
                    depth, a_block, b_block = tile_operands(inner)
                    self.ld(
                        hbm(
                            add(a.offset, mul(row, k_extent), inner),
                            mul(rows, depth),
                            [rows, depth],
                            [k_extent, 1],
                        ),
                        a_block,
                    )
                    self.ld(
                        hbm(
                            add(b.offset, mul(inner, n_extent), col),
                            mul(depth, tile_n),
                            [depth, tile_n],
                            [n_extent, 1],
                        ),
                        b_block,
                    )

                if self.config.gemm_prefetch:
                    load_tile(0)
                with self.loop(f"{name}k", 0, k_extent, tile_k) as inner:
                    if self.config.gemm_prefetch:
                        with self.loop(f"{name}pref", minimum(add(inner, tile_k), k_extent),
                                       minimum(add(inner, 2 * tile_k), k_extent), tile_k) as upcoming:
                            load_tile(upcoming)
                    else:
                        load_tile(inner)
                    depth, a_block, b_block = tile_operands(inner)
                    self.emit(
                        "MMA.ACC",
                        a=a_block,
                        b=b_block,
                        acc=rf(2, cells),
                        m=rows,
                        n=tile_n,
                        k=depth,
                        event=None,
                    )
                if residual is not None:
                    self.ld(
                        hbm(add(residual.offset, mul(row, n_extent), col), cells,
                            [rows, tile_n], [n_extent, 1]),
                        rf(0, cells),
                    )
                    self.vec("add", [rf(0, cells), rf(2, cells)], rf(2, cells))
                if bias is not None:
                    self.ld(hbm(add(bias.offset, col), tile_n), rf(0, tile_n))
                    # 用对数次数的复制在 RF 中展开偏置向量。
                    # 公开 ISA 禁止使用零步长。
                    copied_rows = 1
                    while copied_rows < min(tile_m, m_extent):
                        with self.loop(f"{name}bc{copied_rows}", minimum(copied_rows, rows), rows, tile_m):
                            copied_cells = mul(minimum(copied_rows, sub(rows, copied_rows)), tile_n)
                            self.vec("add", [rf(0, copied_cells), imm(0)],
                                     rf(0, copied_cells, copied_rows * tile_n))
                        copied_rows *= 2
                    self.vec("add", [rf(2, cells), rf(0, cells)], rf(2, cells))
                if activation:
                    self.gelu_rf(rf(2, cells), rf(1, cells))
                self.st(
                    rf(2, cells),
                    hbm(
                        add(out.offset, mul(row, n_extent), col),
                        cells,
                        [rows, tile_n],
                        [n_extent, 1],
                    ),
                )

    def gemm_pair(self, a, b, out, m_extent, k_extent, n_extent, name, *,
                  bias=None, activation=False, residual=None):
        '两个独立 N 累加器复用同一个 A 分块。每个输出的 K 顺序和 MMA 形状不变；A 使用 RF0/4 双缓冲，B 使用 RF1/5，独立累加器使用 RF2/3。'
        tile_m, tile_k = self.config.gemm_m_tile, self.config.gemm_k_tile
        tile_n = self.gemm_tile_n(n_extent)
        if n_extent % (2 * tile_n):
            raise ValueError("Paired GEMM requires an even number of N tiles")
        with self.loop(f"{name}m", 0, m_extent, tile_m) as row:
            rows = minimum(tile_m, sub(m_extent, row))
            cells = mul(rows, tile_n)
            with self.loop(f"{name}n", 0, n_extent, 2 * tile_n) as col:
                self.vec("add", [imm(0), imm(0)], rf(2, cells))
                self.vec("add", [imm(0), imm(0)], rf(3, cells))

                def depth(inner):
                    return minimum(tile_k, sub(k_extent, inner))

                def a_view(inner):
                    lane = mul(4, {"mod": [{"ceildiv": [inner, tile_k]}, 2]})
                    return rf(lane, mul(rows, depth(inner)))

                def load_a(inner):
                    self.ld(hbm(add(a.offset, mul(row, k_extent), inner),
                                mul(rows, depth(inner)), [rows, depth(inner)],
                                [k_extent, 1]), a_view(inner))

                def load_b(inner, index):
                    self.ld(hbm(add(b.offset, mul(inner, n_extent), col, index * tile_n),
                                mul(depth(inner), tile_n), [depth(inner), tile_n],
                                [n_extent, 1]), rf(1 + 4 * index, mul(depth(inner), tile_n)))

                load_a(0)
                load_b(0, 0)
                with self.loop(f"{name}k", 0, k_extent, tile_k) as inner:
                    with self.loop(f"{name}pa", minimum(add(inner, tile_k), k_extent),
                                   minimum(add(inner, 2 * tile_k), k_extent), tile_k) as upcoming:
                        load_a(upcoming)
                    load_b(inner, 1)
                    for index in (0, 1):
                        self.emit("MMA.ACC", a=a_view(inner),
                                  b=rf(1 + 4 * index, mul(depth(inner), tile_n)),
                                  acc=rf(2 + index, cells), m=rows, n=tile_n,
                                  k=depth(inner), event=None)
                    with self.loop(f"{name}pb", minimum(add(inner, tile_k), k_extent),
                                   minimum(add(inner, 2 * tile_k), k_extent), tile_k) as upcoming:
                        load_b(upcoming, 0)
                for index in (0, 1):
                    acc = rf(2 + index, cells)
                    if residual is not None:
                        self.ld(hbm(add(residual.offset, mul(row, n_extent), col,
                                        index * tile_n), cells,
                                    [rows, tile_n], [n_extent, 1]), rf(0, cells))
                        self.vec("add", [rf(0, cells), acc], acc)
                    if bias is not None:
                        self.ld(hbm(add(bias.offset, col, index * tile_n), tile_n),
                                rf(0, tile_n))
                        copied_rows = 1
                        while copied_rows < min(tile_m, m_extent):
                            with self.loop(f"{name}pair{index}bc{copied_rows}",
                                           minimum(copied_rows, rows), rows, tile_m):
                                copied_cells = mul(minimum(copied_rows, sub(rows, copied_rows)), tile_n)
                                self.vec("add", [rf(0, copied_cells), imm(0)],
                                         rf(0, copied_cells, copied_rows * tile_n))
                            copied_rows *= 2
                        self.vec("add", [acc, rf(0, cells)], acc)
                    if activation:
                        self.gelu_rf(acc, rf(1, cells))
                    self.st(acc,
                            hbm(add(out.offset, mul(row, n_extent), col, index * tile_n),
                                cells, [rows, tile_n], [n_extent, 1]))

    def gelu_rf(self, values, temporary):
        '对同一个 tanh GELU 做代数重组，减少两条 VEC 指令。'
        scale = sqrt(2 / 3.141592653589793)
        self.vec("mul", [values, values], temporary)
        self.vec("fma", [temporary, imm(0.044715 * scale), imm(scale)], temporary)
        self.vec("mul", [temporary, values], temporary)
        self.sfu("tanh", temporary, temporary)
        self.vec("fma", [temporary, imm(0.5), imm(0.5)], temporary)
        self.vec("mul", [values, temporary], values)

    def copy_rows(self, source, out, rows, d, name):
        with self.loop(name,0,rows) as row:
            self.ld(hbm(add(source.offset,mul(row,d)),d),rf(0,d))
            self.st(rf(0,d),hbm(add(out.offset,mul(row,d)),d))

    def layernorm(
        self, source: Tensor, gamma: Tensor, beta: Tensor, out: Tensor, rows: int, d: int, name: str
    ):
        self.ld(hbm(gamma.offset, d), rf(6, d))
        self.ld(hbm(beta.offset, d), rf(7, d))
        with self.loop(name, 0, rows) as row:
            base = add(source.offset, mul(row, d))
            target = add(out.offset, mul(row, d))
            self.ld(hbm(base, d), rf(0, d))
            self.reduce("sum", rf(0, d), rf(1, 1))
            self.vec("mul", [rf(1, 1), imm(1 / d)], rf(1, 1))
            self.vec("sub", [rf(0, d), rf(1, 1)], rf(2, d))
            self.vec("mul", [rf(2, d), rf(2, d)], rf(3, d))
            self.reduce("sum", rf(3, d), rf(1, 1))
            self.vec("mul", [rf(1, 1), imm(1 / d)], rf(1, 1))
            self.vec("add", [rf(1, 1), imm(1e-5)], rf(1, 1))
            self.sfu("rsqrt", rf(1, 1), rf(1, 1))
            self.vec("mul", [rf(2, d), rf(1, 1)], rf(2, d))
            self.vec("mul", [rf(2, d), rf(6, d)], rf(2, d))
            self.vec("add", [rf(2, d), rf(7, d)], rf(2, d))
            self.st(rf(2, d), hbm(target, d))

    def add_rows(
        self,
        left: Tensor,
        right: Tensor,
        out: Tensor,
        rows: int,
        width: int,
        name: str,
        bias: Tensor | None = None,
    ):
        with self.loop(name, 0, rows) as row:
            with self.loop(f"{name}c", 0, width, self.config.vector_tile) as col:
                n = minimum(self.config.vector_tile, sub(width, col))
                self.ld(hbm(add(left.offset, mul(row, width), col), n), rf(0, n))
                self.ld(hbm(add(right.offset, mul(row, width), col), n), rf(1, n))
                self.vec("add", [rf(0, n), rf(1, n)], rf(2, n))
                if bias is not None:
                    self.ld(hbm(add(bias.offset, col), n), rf(3, n))
                    self.vec("add", [rf(2, n), rf(3, n)], rf(2, n))
                self.st(rf(2, n), hbm(add(out.offset, mul(row, width), col), n))

    def add_bias(self, source: Tensor, bias: Tensor, out: Tensor, rows: int, width: int, name: str):
        with self.loop(name, 0, rows) as row:
            with self.loop(f"{name}c", 0, width, self.config.vector_tile) as col:
                n = minimum(self.config.vector_tile, sub(width, col))
                self.ld(hbm(add(source.offset, mul(row, width), col), n), rf(0, n))
                self.ld(hbm(add(bias.offset, col), n), rf(1, n))
                self.vec("add", [rf(0, n), rf(1, n)], rf(2, n))
                self.st(rf(2, n), hbm(add(out.offset, mul(row, width), col), n))

    def gelu(
        self, source: Tensor, out: Tensor, rows: int, width: int, name: str,
        bias: Tensor | None = None,
    ):
        with self.loop(name, 0, rows) as row:
            with self.loop(f"{name}c", 0, width, self.config.vector_tile) as col:
                n = minimum(self.config.vector_tile, sub(width, col))
                addr = add(source.offset, mul(row, width), col)
                self.ld(hbm(addr, n), rf(0, n))
                if bias is not None:
                    self.ld(hbm(add(bias.offset, col), n), rf(1, n))
                    self.vec("add", [rf(0, n), rf(1, n)], rf(0, n))
                self.vec("mul", [rf(0, n), rf(0, n)], rf(1, n))
                self.vec("mul", [rf(1, n), rf(0, n)], rf(1, n))
                self.vec("fma", [rf(1, n), imm(0.044715), rf(0, n)], rf(1, n))
                self.vec("mul", [rf(1, n), imm(sqrt(2 / 3.141592653589793))], rf(1, n))
                self.sfu("tanh", rf(1, n), rf(1, n))
                self.vec("add", [rf(1, n), imm(1)], rf(1, n))
                self.vec("mul", [rf(0, n), imm(0.5)], rf(0, n))
                self.vec("mul", [rf(0, n), rf(1, n)], rf(0, n))
                self.st(rf(0, n), hbm(add(out.offset, mul(row, width), col), n))

    def attention(
        self,
        qkv: Tensor,
        context: Tensor,
        k_out: Tensor,
        v_out: Tensor,
        rows: int,
        past: int,
        d: int,
        h: int,
        hd: int,
        name: str,
    ):
        if self.config.attention_blocked and past == 0 and rows > 1:
            return self.attention_prompt_blocked(qkv, context, k_out, v_out, rows, d, h, hd, name)
        # 按公开 ABI 的 head 优先布局导出每层 K/V。
        with self.loop(f"{name}h", 0, h) as head:
            with self.loop(f"{name}r", 0, rows) as row:
                for kind, target, component in (("k", k_out, 1), ("v", v_out, 2)):
                    source_addr = add(qkv.offset, mul(row, 3 * d), component * d, mul(head, hd))
                    target_addr = add(
                        target.offset, mul(head, target.shape[2] * hd), mul(add(past, row), hd)
                    )
                    self.ld(hbm(source_addr, hd), rf(0, hd))
                    self.st(rf(0, hd), hbm(target_addr, hd))
        total = past + rows
        resident = self.config.attention_rf
        if not resident:
            scores = self.alloc(f"{name}_scores", rows, total)
            probs = self.alloc(f"{name}_probs", rows, total)
        with self.loop(f"{name}q", 0, rows) as row:
            limit = add(past, row, 1)
            with self.loop(f"{name}head", 0, h) as head:
                q_addr = add(qkv.offset, mul(row, 3 * d), mul(head, hd))
                self.ld(hbm(q_addr, hd), rf(0, hd))
                with self.loop(f"{name}key", 0, limit, self.config.attention_key_tile) as key:
                    n = minimum(self.config.attention_key_tile, sub(limit, key))
                    cells = mul(hd, n)
                    score_acc = rf(3, n, key) if resident else rf(2, n)
                    self.ld(
                        hbm(
                            add(k_out.offset, mul(head, k_out.shape[2] * hd), mul(key, hd)),
                            cells,
                            [hd, n],
                            [1, hd],
                        ),
                        rf(1, cells),
                    )
                    self.vec("add", [imm(0), imm(0)], score_acc)
                    self.emit(
                        "MMA.ACC",
                        a=rf(0, hd),
                        b=rf(1, cells),
                        acc=score_acc,
                        m=1,
                        n=n,
                        k=hd,
                        event=None,
                    )
                    if not resident:
                        self.vec("mul", [score_acc, imm(1 / sqrt(hd))], score_acc)
                        self.st(score_acc, hbm(add(scores.offset, mul(row, total), key), n))
                probability = rf(3 if resident else 0, limit)
                if not resident:
                    self.ld(hbm(add(scores.offset, mul(row, total)), limit), probability)
                else:
                    self.vec("mul", [probability, imm(1 / sqrt(hd))], probability)
                self.reduce("max", probability, rf(1, 1))
                self.vec("sub", [probability, rf(1, 1)], probability)
                self.sfu("exp", probability, probability)
                self.reduce("sum", probability, rf(1, 1))
                self.vec("div", [probability, rf(1, 1)], probability)
                if not resident:
                    self.st(probability, hbm(add(probs.offset, mul(row, total)), limit))
                self.vec("add", [imm(0), imm(0)], rf(2, hd))
                with self.loop(f"{name}value", 0, limit, self.config.attention_value_tile) as key:
                    depth = minimum(self.config.attention_value_tile, sub(limit, key))
                    probability_block = rf(3, depth, key) if resident else rf(0, depth)
                    if not resident:
                        self.ld(hbm(add(probs.offset, mul(row, total), key), depth), probability_block)
                    self.ld(
                        hbm(
                            add(v_out.offset, mul(head, v_out.shape[2] * hd), mul(key, hd)),
                            mul(depth, hd),
                        ),
                        rf(1, mul(depth, hd)),
                    )
                    self.emit(
                        "MMA.ACC",
                        a=probability_block,
                        b=rf(1, mul(depth, hd)),
                        acc=rf(2, hd),
                        m=1,
                        n=hd,
                        k=depth,
                        event=None,
                    )
                self.st(rf(2, hd), hbm(add(context.offset, mul(row, d), mul(head, hd)), hd))

    def attention_prompt_blocked(self, qkv, context, k_out, v_out, rows, d, h, hd, name):
        '复用完整 prompt K/V，并逐行执行精确因果 softmax。完整 prompt 已释放；只有因果分数切片进入 softmax，未来位置的概率显式初始化为零。'
        with self.loop(f"{name}h", 0, h) as head:
            for target, component, lane in ((k_out, 1, 3), (v_out, 2, 5)):
                self.ld(
                    hbm(add(qkv.offset, component * d, mul(head, hd)), rows * hd,
                        [rows, hd], [3 * d, 1]),
                    rf(lane, rows * hd),
                )
                self.st(rf(lane, rows * hd),
                        hbm(add(target.offset, mul(head, target.shape[2] * hd)), rows * hd))
        query_tile = self.config.attention_query_tile
        # 将多个因果行视图与 GEMM 累加器分开。
        # 这里只改变物理 RF 分配；指令算术和全部
        # HBM 视图保持一致，其他内核不使用 RF6/7。
        score_lane, probability_lane = ((6, 7) if self.config.attention_dedicated_rf else (2, 3))
        with self.loop(f"{name}head", 0, h) as head:
            with self.loop(f"{name}q", 0, rows, query_tile) as query:
                queries = minimum(query_tile, sub(rows, query))
                keys = add(query, queries) if self.config.attention_triangular else rows
                score_cells = mul(queries, keys)
                self.ld(
                    hbm(add(qkv.offset, mul(query, 3 * d), mul(head, hd)), mul(queries, hd),
                        [queries, hd], [3 * d, 1]),
                    rf(0, mul(queries, hd)),
                )
                key_view = dict(rf(3, mul(hd, keys)), shape=[hd, keys], strides=[1, hd])
                self.vec("add", [imm(0), imm(0)], rf(score_lane, score_cells))
                self.emit("MMA.ACC", a=rf(0, mul(queries, hd)), b=key_view,
                          acc=rf(score_lane, score_cells), m=queries, n=keys, k=hd, event=None)
                self.vec("mul", [rf(score_lane, score_cells), imm(1 / sqrt(hd))], rf(score_lane, score_cells))
                self.vec("add", [imm(0), imm(0)], rf(probability_lane, score_cells))
                with self.loop(f"{name}softmax", 0, queries) as local_query:
                    limit = add(query, local_query, 1)
                    values = rf(score_lane, limit, mul(local_query, keys))
                    self.reduce("max", values, rf(4, 1))
                    self.vec("sub", [values, rf(4, 1)], values)
                    self.sfu("exp", values, values)
                    self.reduce("sum", values, rf(4, 1))
                    self.vec("div", [values, rf(4, 1)],
                             rf(probability_lane, limit, mul(local_query, keys)))

                self.vec("add", [imm(0), imm(0)], rf(2, mul(queries, hd)))
                self.emit("MMA.ACC", a=rf(probability_lane, score_cells), b=rf(5, mul(keys, hd)),
                          acc=rf(2, mul(queries, hd)), m=queries, n=hd, k=keys, event=None)
                self.st(
                    rf(2, mul(queries, hd)),
                    hbm(add(context.offset, mul(query, d), mul(head, hd)), mul(queries, hd),
                        [queries, hd], [d, 1]),
                )

    def attention_decode(
        self,
        qkv: Tensor,
        context: Tensor,
        history_k: Tensor,
        history_v: Tensor,
        new_k: Tensor,
        new_v: Tensor,
        step: int,
        past: int,
        d: int,
        h: int,
        hd: int,
        name: str,
    ):
        '单步 decode：分别计算历史和新生成 K/V 的分数。'
        generated = step + 1
        total = past + generated
        resident = self.config.attention_rf
        if not resident:
            scores = self.alloc(f"{name}_scores", total)
            probs = self.alloc(f"{name}_probs", total)
        for kind, target, component in (("k", new_k, 1), ("v", new_v, 2)):
            with self.loop(f"{name}{kind}h", 0, h) as head:
                source = add(qkv.offset, component * d, mul(head, hd))
                destination = add(target.offset, mul(head, target.shape[2] * hd), step * hd)
                self.ld(hbm(source, hd), rf(0, hd))
                self.st(rf(0, hd), hbm(destination, hd))
        with self.loop(f"{name}head", 0, h) as head:
            self.ld(hbm(add(qkv.offset, mul(head, hd)), hd), rf(0, hd))
            for source, count, score_base, stride in (
                (history_k, past, 0, past),
                (new_k, generated, past, new_k.shape[2]),
            ):
                with self.loop(f"{name}key{score_base}", 0, count, self.config.attention_key_tile) as key:
                    n = minimum(self.config.attention_key_tile, sub(count, key))
                    cells = mul(hd, n)
                    score_acc = rf(3, n, add(score_base, key)) if resident else rf(2, n)
                    self.ld(
                        hbm(
                            add(source.offset, mul(head, stride * hd), mul(key, hd)),
                            cells,
                            [hd, n],
                            [1, hd],
                        ),
                        rf(1, cells),
                    )
                    self.vec("add", [imm(0), imm(0)], score_acc)
                    self.emit(
                        "MMA.ACC",
                        a=rf(0, hd),
                        b=rf(1, cells),
                        acc=score_acc,
                        m=1,
                        n=n,
                        k=hd,
                        event=None,
                    )
                    if not resident:
                        self.vec("mul", [score_acc, imm(1 / sqrt(hd))], score_acc)
                        self.st(score_acc, hbm(add(scores.offset, score_base, key), n))
            probability = rf(3 if resident else 0, total)
            if not resident:
                self.ld(hbm(scores.offset, total), probability)
            else:
                self.vec("mul", [probability, imm(1 / sqrt(hd))], probability)
            self.reduce("max", probability, rf(1, 1))
            self.vec("sub", [probability, rf(1, 1)], probability)
            self.sfu("exp", probability, probability)
            self.reduce("sum", probability, rf(1, 1))
            self.vec("div", [probability, rf(1, 1)], probability)
            if not resident:
                self.st(probability, hbm(probs.offset, total))
            self.vec("add", [imm(0), imm(0)], rf(2, hd))
            for source, count, prob_base, stride in (
                (history_v, past, 0, past),
                (new_v, generated, past, new_v.shape[2]),
            ):
                with self.loop(f"{name}value{prob_base}", 0, count, self.config.attention_value_tile) as key:
                    depth = minimum(self.config.attention_value_tile, sub(count, key))
                    probability_block = rf(3, depth, add(prob_base, key)) if resident else rf(0, depth)
                    if not resident:
                        self.ld(hbm(add(probs.offset, prob_base, key), depth), probability_block)
                    self.ld(
                        hbm(
                            add(source.offset, mul(head, stride * hd), mul(key, hd)),
                            mul(depth, hd),
                        ),
                        rf(1, mul(depth, hd)),
                    )
                    self.emit(
                        "MMA.ACC",
                        a=probability_block,
                        b=rf(1, mul(depth, hd)),
                        acc=rf(2, hd),
                        m=1,
                        n=hd,
                        k=depth,
                        event=None,
                    )
            self.st(rf(2, hd), hbm(add(context.offset, mul(head, hd)), hd))

    def finish(self):
        self.emit("WG.END", wg="g")
        return "\n".join(self.lines) + "\n"


def generate_m1_p1(config: CompilerConfig | None = None) -> tuple[str, Layout, int]:
    if config is not None and config.combine_batches:
        return generate_m1_p1_combined(config)
    model = MODELS["M1"]
    d, f, h, hd = model.width, model.ffn, model.heads, model.head_width
    batch, prompt_rows, _ = SCENARIOS["P1"]
    layout = build_layout(model, "P1")
    b = Builder(layout, config=config)
    prompt = b.symbol("input/prompt")
    step = b.symbol("input/step0")
    output = b.symbol("output/hidden")
    # 所有 prompt 完成后，STEP.COMMIT 才释放任一批成员的第一个新输入。
    for phase, rows in (("p", prompt_rows), ("s", 1)):
        for member in range(batch):
            initial = (
                Tensor(prompt.offset + member * prompt_rows * d, (rows, d))
                if phase == "p"
                else Tensor(step.offset + member * d, (rows, d))
            )
            x = initial
            for layer in range(model.layers):
                prefix = f"layer{layer}/"
                name = f"{phase}b{member}l{layer}"
                weights = {
                    key: b.symbol(prefix + key)
                    for key in (
                        "ln1_g",
                        "ln1_b",
                        "wqkv",
                        "wo",
                        "ln2_g",
                        "ln2_b",
                        "w1",
                        "b1",
                        "w2",
                        "b2",
                    )
                }
                ln1 = b.alloc(f"{name}_ln1", rows, d)
                qkv = b.alloc(f"{name}_qkv", rows, 3 * d)
                ctx = b.alloc(f"{name}_ctx", rows, d)
                att = b.alloc(f"{name}_att", rows, d)
                res = b.alloc(f"{name}_res", rows, d)
                ln2 = b.alloc(f"{name}_ln2", rows, d)
                ff1 = b.alloc(f"{name}_ff1", rows, f)
                biased = b.alloc(f"{name}_biased", rows, f)
                activated = b.alloc(f"{name}_gelu", rows, f)
                ff2 = b.alloc(f"{name}_ff2", rows, d)
                y = b.alloc(f"{name}_out", rows, d)
                b.layernorm(x, weights["ln1_g"], weights["ln1_b"], ln1, rows, d, name + "ln1")
                b.gemm(ln1, weights["wqkv"], qkv, rows, d, 3 * d, name + "qkv")
                k_out, v_out = (b.symbol(prefix + f"new_{kind}") for kind in ("k", "v"))
                kv_offset = member * h * (prompt_rows + 1) * hd
                k_out = Tensor(k_out.offset + kv_offset, k_out.shape)
                v_out = Tensor(v_out.offset + kv_offset, v_out.shape)
                b.attention(
                    qkv,
                    ctx,
                    k_out,
                    v_out,
                    rows,
                    0 if phase == "p" else prompt_rows,
                    d,
                    h,
                    hd,
                    name + "a",
                )
                if b.config.gemm_epilogue:
                    b.gemm(ctx, weights["wo"], res, rows, d, d, name + "wo", residual=x)
                else:
                    b.gemm(ctx, weights["wo"], att, rows, d, d, name + "wo")
                    b.add_rows(x, att, res, rows, d, name + "res")
                b.layernorm(res, weights["ln2_g"], weights["ln2_b"], ln2, rows, d, name + "ln2")
                if b.config.gemm_epilogue:
                    b.gemm(ln2, weights["w1"], activated, rows, d, f, name + "w1",
                           bias=weights["b1"], activation=True)
                elif b.config.fuse_ffn:
                    b.gemm(ln2, weights["w1"], ff1, rows, d, f, name + "w1")
                    b.gelu(ff1, activated, rows, f, name + "gelu", bias=weights["b1"])
                else:
                    b.gemm(ln2, weights["w1"], ff1, rows, d, f, name + "w1")
                    b.add_bias(ff1, weights["b1"], biased, rows, f, name + "bias1")
                    b.gelu(biased, activated, rows, f, name + "gelu")
                if b.config.gemm_epilogue:
                    b.gemm(activated, weights["w2"], y, rows, f, d, name + "w2",
                           residual=res, bias=weights["b2"])
                else:
                    b.gemm(activated, weights["w2"], ff2, rows, f, d, name + "w2")
                    b.add_rows(res, ff2, y, rows, d, name + "out", weights["b2"])
                x = y
            with b.loop(f"{phase}b{member}output", 0, rows) as row:
                b.ld(hbm(add(x.offset, mul(row, d)), d), rf(0, d))
                b.st(
                    rf(0, d),
                    hbm(
                        add(
                            output.offset,
                            member * (prompt_rows + 1) * d,
                            (0 if phase == "p" else prompt_rows) * d,
                            mul(row, d),
                        ),
                        d,
                    ),
                )
        if phase == "p":
            b.emit("STEP.COMMIT", step=0)
    program = b.finish()
    return program, layout, b.cursor


def generate_m1_p1_combined(config: CompilerConfig) -> tuple[str, Layout, int]:
    '将两个批成员共同送入逐层稠密算子。attention 和公开 K/V 张量保持独立；稠密算子与归一化逐行计算，合并行维度保持参考算术并改善权重复用。'
    model = MODELS["M1"]
    d, f, h, hd = model.width, model.ffn, model.heads, model.head_width
    batch, prompt_rows, _ = SCENARIOS["P1"]
    layout = build_layout(model, "P1")
    b = Builder(layout, config=config)
    output = b.symbol("output/hidden")
    for phase, member_rows, input_name in (
        ("p", prompt_rows, "input/prompt"), ("s", 1, "input/step0"),
    ):
        rows = batch * member_rows
        x = Tensor(b.symbol(input_name).offset, (rows, d))
        for layer in range(model.layers):
            prefix = f"layer{layer}/"
            name = f"{phase}bothl{layer}"
            weights = {key: b.symbol(prefix + key) for key in (
                "ln1_g", "ln1_b", "wqkv", "wo", "ln2_g", "ln2_b",
                "w1", "b1", "w2", "b2",
            )}
            ln1 = b.alloc(f"{name}_ln1", rows, d)
            qkv = b.alloc(f"{name}_qkv", rows, 3 * d)
            ctx = b.alloc(f"{name}_ctx", rows, d)
            att = b.alloc(f"{name}_att", rows, d)
            res = b.alloc(f"{name}_res", rows, d)
            ln2 = b.alloc(f"{name}_ln2", rows, d)
            ff1 = b.alloc(f"{name}_ff1", rows, f)
            biased = b.alloc(f"{name}_biased", rows, f)
            activated = b.alloc(f"{name}_gelu", rows, f)
            ff2 = b.alloc(f"{name}_ff2", rows, d)
            y = b.alloc(f"{name}_out", rows, d)
            b.layernorm(x, weights["ln1_g"], weights["ln1_b"], ln1, rows, d, name + "ln1")
            b.gemm(ln1, weights["wqkv"], qkv, rows, d, 3 * d, name + "qkv")
            for member in range(batch):
                k_out, v_out = (b.symbol(prefix + f"new_{kind}") for kind in ("k", "v"))
                kv_offset = member * h * (prompt_rows + 1) * hd
                k_out = Tensor(k_out.offset + kv_offset, k_out.shape)
                v_out = Tensor(v_out.offset + kv_offset, v_out.shape)
                b.attention(
                    Tensor(qkv.offset + member * member_rows * 3 * d, (member_rows, 3 * d)),
                    Tensor(ctx.offset + member * member_rows * d, (member_rows, d)),
                    k_out, v_out, member_rows,
                    0 if phase == "p" else prompt_rows, d, h, hd, name + f"b{member}a",
                )
            if config.gemm_epilogue:
                b.gemm(ctx, weights["wo"], res, rows, d, d, name + "wo", residual=x)
            else:
                b.gemm(ctx, weights["wo"], att, rows, d, d, name + "wo")
                b.add_rows(x, att, res, rows, d, name + "res")
            b.layernorm(res, weights["ln2_g"], weights["ln2_b"], ln2, rows, d, name + "ln2")
            if config.gemm_epilogue:
                b.gemm(ln2, weights["w1"], activated, rows, d, f, name + "w1",
                       bias=weights["b1"], activation=True)
            else:
                b.gemm(ln2, weights["w1"], ff1, rows, d, f, name + "w1")
                if config.fuse_ffn:
                    b.gelu(ff1, activated, rows, f, name + "gelu", bias=weights["b1"])
                else:
                    b.add_bias(ff1, weights["b1"], biased, rows, f, name + "bias1")
                    b.gelu(biased, activated, rows, f, name + "gelu")
            if config.gemm_epilogue:
                b.gemm(activated, weights["w2"], y, rows, f, d, name + "w2",
                       residual=res, bias=weights["b2"])
            else:
                b.gemm(activated, weights["w2"], ff2, rows, f, d, name + "w2")
                b.add_rows(res, ff2, y, rows, d, name + "out", weights["b2"])
            x = y
        for member in range(batch):
            b.copy_rows(Tensor(x.offset + member * member_rows * d, (member_rows,d)),
                        Tensor(output.offset + member * (prompt_rows+1)*d +
                               (0 if phase == "p" else prompt_rows)*d, (member_rows,d)),
                        member_rows,d,f"{phase}bothb{member}output")
        if phase == "p":
            b.emit("STEP.COMMIT", step=0)
    return b.finish(), layout, b.cursor


def generate_m1_d1(gemm_n_tile: int = 16, *, config: CompilerConfig | None = None) -> tuple[str, Layout, int]:
    '生成 D1 历史状态及按因果边界释放的 decode 步骤。'
    model = MODELS["M2"]
    batch, past, steps = SCENARIOS["D1"]
    if batch != 1:
        raise ValueError("D1 baseline requires batch one")
    d, f, h, hd = model.width, model.ffn, model.heads, model.head_width
    layout = build_layout(model, "D1")
    b = Builder(layout, gemm_n_tile=gemm_n_tile, config=config)
    output = b.symbol("output/hidden")
    for step in range(steps):
        x = b.symbol(f"input/step{step}")
        for layer in range(model.layers):
            prefix = f"layer{layer}/"
            name = f"d{step}l{layer}"
            weights = {
                key: b.symbol(prefix + key)
                for key in (
                    "ln1_g",
                    "ln1_b",
                    "wqkv",
                    "wo",
                    "ln2_g",
                    "ln2_b",
                    "w1",
                    "b1",
                    "w2",
                    "b2",
                )
            }
            ln1 = b.alloc(f"{name}_ln1", 1, d)
            qkv = b.alloc(f"{name}_qkv", 1, 3 * d)
            ctx = b.alloc(f"{name}_ctx", 1, d)
            att = b.alloc(f"{name}_att", 1, d)
            res = b.alloc(f"{name}_res", 1, d)
            ln2 = b.alloc(f"{name}_ln2", 1, d)
            ff1 = b.alloc(f"{name}_ff1", 1, f)
            biased = b.alloc(f"{name}_biased", 1, f)
            activated = b.alloc(f"{name}_gelu", 1, f)
            ff2 = b.alloc(f"{name}_ff2", 1, d)
            y = b.alloc(f"{name}_out", 1, d)
            b.layernorm(x, weights["ln1_g"], weights["ln1_b"], ln1, 1, d, name + "ln1")
            b.gemm(ln1, weights["wqkv"], qkv, 1, d, 3 * d, name + "qkv")
            b.attention_decode(
                qkv,
                ctx,
                b.symbol(prefix + "history_k"),
                b.symbol(prefix + "history_v"),
                b.symbol(prefix + "new_k"),
                b.symbol(prefix + "new_v"),
                step,
                past,
                d,
                h,
                hd,
                name + "a",
            )
            if b.config.gemm_epilogue:
                b.gemm(ctx, weights["wo"], res, 1, d, d, name + "wo", residual=x)
            else:
                b.gemm(ctx, weights["wo"], att, 1, d, d, name + "wo")
                b.add_rows(x, att, res, 1, d, name + "res")
            b.layernorm(res, weights["ln2_g"], weights["ln2_b"], ln2, 1, d, name + "ln2")
            if b.config.gemm_epilogue:
                b.gemm(ln2, weights["w1"], activated, 1, d, f, name + "w1",
                       bias=weights["b1"], activation=True)
            elif b.config.fuse_ffn:
                b.gemm(ln2, weights["w1"], ff1, 1, d, f, name + "w1")
                b.gelu(ff1, activated, 1, f, name + "gelu", bias=weights["b1"])
            else:
                b.gemm(ln2, weights["w1"], ff1, 1, d, f, name + "w1")
                b.add_bias(ff1, weights["b1"], biased, 1, f, name + "bias1")
                b.gelu(biased, activated, 1, f, name + "gelu")
            if b.config.gemm_epilogue:
                b.gemm(activated, weights["w2"], y, 1, f, d, name + "w2",
                       residual=res, bias=weights["b2"])
            else:
                b.gemm(activated, weights["w2"], ff2, 1, f, d, name + "w2")
                b.add_rows(res, ff2, y, 1, d, name + "out", weights["b2"])
            x = y
        b.ld(hbm(x.offset, d), rf(0, d))
        b.st(rf(0, d), hbm(output.offset + step * d, d))
        b.emit("STEP.COMMIT", step=step)
    return b.finish(), layout, b.cursor


if __name__ == "__main__":
    from pathlib import Path

    output = Path("programs")
    output.mkdir(exist_ok=True)
    for filename, generate in (("M1_P1.asm", generate_m1_p1), ("M2_D1.asm", generate_m1_d1)):
        program, _, _ = generate()
        (output / filename).write_text(program, encoding="utf-8")
        print(f"Generated {output / filename}: {len(program.splitlines())} lines")
