> 历史调研，写于 2026-09-26。规则以冻结官方版本为准；旧实验路径仅表示当时来源。

**AI Infra 作业分析与调研（2026-09-26）**

建议采用“先复现与画像，再用小步实验改生成器，最后联合搜索硬件”的路线。最有证据的优化入口是：扩大合理的 GEMM 分块、让多个 SM 承担独立工作、减少反复搬运，随后研究融合、缓存和流水重叠。当前基线只使用 SM0，不能靠增加 SM 数直接加速。

本报告依据课程官方题目、下载的 starter 源码、冻结基线报告和公开原始论文。没有参考其他参赛者的代码、设计、提交或 agent 输出。明确区分三类证据：**官方规则/源码事实**、**本机复现结果**、**尚待实验的优化假设**。本轮不修改基线设计、不进行候选搜索、不向服务器提交。

**1．实际需要完成什么**

这是固定工作负载、固定 ISA 的单芯片软硬件协同设计。交付物是一份硬件 JSON 和两份汇编，硬件必须相同，程序可以针对各自形状采用不同算法、分块和调度。Python 的角色是生成汇编、组织实验；评分器真正执行的是提交的汇编。

| 维度 | `M1_P1` | `M2_D1` |
|---|---|---|
| 模型 | 3 层，hidden 256，8 个 head，每 head 32，FFN 1024 | 2 层，hidden 128，4 个 head，每 head 32，FFN 512 |
| 批量 | 2 | 1 |
| 初始输入 | 两个各 64 位置的 prompt | 每层已有 128 位置的历史 K/V |
| 工作 | 两个 prompt 的全部计算，再各算第 65 个位置 | 连续计算 8 个新位置 |
| Attention | 每条序列内部的 dense causal attention | 第 s 步看 128 个历史位置和 s+1 个新位置 |
| 步边界 | 两条 prompt 的输出及每层 K/V 都写完后 commit 0，才可读任一新输入 | 每步输出及每层 K/V 写完后 commit s，共 0–7 |
| 必须输出 | 所有最终 hidden，以及每一层新增 K/V | 同左 |

每层数据流是：`LayerNorm → QKV → causal attention → output projection + residual → LayerNorm → FFN(bias + tanh-GELU + bias) + residual`。所有输入、权重、存储和指令算术遵循 FP32 契约；公开参考 oracle 内部使用 Float64 计算来比较误差。没有训练、词表投影、token sampling、MoE、量化或跨芯片通信。新位置输入是外部提供的 hidden vector，不能自行选择 token，也不能提前读取尚未释放的输入。

来源：[完整题目](https://linux-slai.tail6d76d1.ts.net:8443/statement/)、本地 [reference.py](../../vendor/official/codesign/challenge/reference.py)、[workload_v07.json](../../vendor/official/codesign/challenge/workload_v07.json)。

**2．评分目标应该怎样理解**

合格时：

```text
score = 1000 × sqrt((30,996,995 / T_P1) × (1,302,032 / T_D1))
```

| 硬门限 | 要求 |
|---|---|
| 功能 | 两程序所有必需 hidden/K/V 均通过；每个要求输出的元素恰好写一次 |
| 误差 | abs(candidate-reference) ≤ 1e-3 + 1e-3×abs(reference)；非有限值失败 |
| 面积 | ≤24 mm² |
| 功率 | 每个 case 的最大滚动 1000-cycle 平均功率 ≤20 W |
| P1 周期 | ≤61,993,990 |
| D1 周期 | ≤2,604,064 |

评分对两种**相对加速比**同等加权。P1 的总周期更长，不代表 D1 不重要：P1、D1 各快 2 倍得到 2000；只有 P1 快 4 倍也得到 2000；P1 快 2 倍但 D1 慢 2 倍只有 1000，而且 D1 已碰到时延门限。目标可以写成最小化 `log(T_P1)+log(T_D1)`，但必须先满足全部门限。

面积和功率是门限，没有额外“越低越高分”的直接奖励；节省它们的价值在于为其他有效资源留预算。功率应读取 `peak_window_power_w`，不能用 `average_power_w` 或 `peak_power_upper_bound_w` 替代。时钟固定 500 MHz。

来源：[评分规则](https://linux-slai.tail6d76d1.ts.net:8443/statement/#6-scoring-and-leaderboard)、本地 [score.py](../../vendor/official/codesign/challenge/score.py)、[runner.py](../../vendor/official/codesign/challenge/runner.py)。

**3．已经取得的资料和环境**

官方 starter 已保存在 `AI_Infra/references/`，ZIP 原件也保留。归档 SHA-256 为 `fe5db656cc7a9818b23989185e767ce743831dd7625273aa244bc7d8157ec785`。已核对 `MANIFEST.json` 的 41 个文件，全部哈希一致；还在内存中运行原生成器，两份输出均与发布的汇编逐字一致。

原默认 Python 是 3.13.5，且没有 NumPy。已为本作业建立独立 `AI_Infra/.venv/`，准确安装 Python **3.12.13** 和 NumPy **2.5.3**。这两个版本是 `grade` 的严格检查条件。不要通过修改 scorer 或伪造版本绕过检查。

本机拥有充足 CPU 和内存，可以执行这个 Python 模拟器；基线复现不需要启动 GPU 训练或推理服务。模拟周期与宿主执行秒数是不同指标，应同时记录，不能用宿主墙钟耗时作为作业分数。

本机完整基线评分已经成功：`eligible=true`、`experimental_score=1000.0`，两案的全部 timing 字段均与官方冻结 manifest 一致，公开 seed 7 的所有 hidden/KV 检查通过。耗时 403.86 秒，峰值 RSS 3.09 GB。证据与命令记录在 [本机复现结果](baseline-reproduction.md)。本报告下面的基线画像取自已校验且已复现的官方冻结 manifest。

已保留 [资料与环境记录](provenance.json)、[冻结基线摘要](frozen-baseline-summary.json)、[静态统计结果](static-stats.json)及[只读统计脚本](static-stats.py)。更细的源码行号与公式见 [编译器分析](notes/compiler-analysis.md)和[硬件分析](notes/hardware-analysis.md)。

**4．从基线中得到的直接证据**

| 项目 | P1 | D1 |
|---|---:|---:|
| 官方冻结周期 | 30,996,995 | 1,302,032 |
| 换算模拟时间 | 61.994 ms | 2.604 ms（8 步合计） |
| 最大窗口功率 | 1.771586 W | 1.448349 W |
| HBM 读取 | 284,816,512 B | 16,096,640 B |
| HBM 写入 | 10,541,440 B | 268,544 B |
| 展开指令数 | 336,275 | 22,362 |
| SM0 RF read 利用率 | 29.26% | 21.82% |
| SM0 RF write 利用率 | 34.27% | 40.54% |
| SM0 TC 利用率 | 18.15% | 18.68% |

硬件面积为 **5.59764068864 mm²**。配置里有 8 个 SM、每 SM 128 KiB SH，但 `Builder` 只生成 `WG.BEGIN wg="g", sm=0, shared_bytes=0`，两份程序都只有这一个 WG。其余 SM 没有被程序使用，SH 读写为零，cache 为零。

这些数据支持“并行性、RF 服务和数据移动值得优先研究”，但不能把利用率直接当作可相加的总时延占比，也不能据此断言唯一瓶颈。特别是报告中的 NoC busy-cycle 占比不等于带宽占比：按 `noc_bytes/(width×cycles)` 计算，P1、D1 的平均 NoC 带宽占用约为 3.73%、4.94%。端到端时延还包含串行依赖、等待、访存形状及资源排队。

静态展开进一步显示，四类 GEMM 贡献 P1 约 86.7%、D1 约 84.5% 的逻辑 HBM 读取。P1 的 GEMM B 权重被两批 prompt 和新位置合计读取 18 遍。基线只使用 RF lane 0–3，其余分区没有使用。这使“增加行复用”和“将短 attention 中间结果保留在额外 RF 分区”成为具体、可定位的候选方向。这里的逻辑流量按 operand count 统计，不等同于计时模型按物理行统计的 HBM 流量。

源码与清单见 [compiler.py](../../vendor/official/project/compiler.py)、[baseline_manifest.json](../../vendor/official/baseline_manifest.json)、[resource_calendar.py](../../vendor/official/codesign/challenge/resource_calendar.py)。

**5．必须理解的硬件模型细节**

| 资源 | 真实含义与调参影响 |
|---|---|
| SM | 程序显式给 WG 指定 SM；增大 `sm_count` 不会自动分配工作 |
| TC | 数量、阵列形状和 K 并行共同决定一条 MMA 的服务时间；不能视为多个任意独立指令队列 |
| RF | 每 SM 256 KiB，最多 4 个驻留 WG；每 WG 固定 64 KiB，不会因少驻留 WG 自动变大 |
| `vector_lanes` | 同时决定向量吞吐和 RF 分区数；RF lane 是存储分区，不是 CUDA 线程 |
| RF ports | 影响所有计算和数据传输的 RF 读写服务；基线资源画像支持优先做敏感性实验 |
| SH | 每 WG 私有配额，需要显式搬入 RF 才能计算；增加容量不会自动缓存 HBM |
| Cache | 芯片共享、自动管理，没有汇编 cache-control 指令；必须看命中、冲突、延迟和流量 |
| DMA | 引擎/深度要和独立搬运、缓冲及事件调度配合，才可能产生重叠 |
| NoC/HBM | 需区分芯片 NoC、每 SM 链路、HBM channel 的限制；更多并行可能把瓶颈转移至这里 |
| Multicast | 只针对满足调度条件的相同行读取共享源服务，不是免费向所有 SM 广播 |

每个 RF lane 可容纳的 FP32 数量是 `16384/vector_lanes`。当 lanes=16/32/64 时，分别是 1024/512/256。基线 B tile 为 `48×16=768` 元素，因此直接把 lanes 改为 32 或 64 会使原程序不合法。调整 lanes 必须同步调整 tile 和 RF 布局。

TC 服务公式来自 `service.py`。设阵列为 `p_m × p_n`：

```text
blocks = ceil(M/p_m) × ceil(N/p_n)
groups = ceil(K/tc_k_parallel)
compute_cycles = 2 + ceil(log2(tc_k_parallel))
                 + ceil(blocks/tc_count) × groups
```

基线 P1 tile `(8,16,48)` 在 `4×8` 阵列上产生 4 个 block，TC 数从 2 加到 4 可能缩短其 compute 服务，但加到 8 对这条指令已没有同等收益。D1 的 M=1、N=16 只有 2 个 block，原本 2 个 TC 已能同时处理这两个 block；仅加 TC 数未必有用。`tc_k_parallel` 会改变 FP32 求和顺序，必须重新做功能检查。

正式 `pipeline-global-events-v2` 对一条计算指令安排 RF-read、compute、RF-write 三个阶段。不要拿历史 `perf.py` 中简单的 `max(compute, RF)` 近似替代正式评分路径。

例如只看一条孤立 MMA，包含 issue 后的 1-cycle 起步，但不含 HBM load 和资源排队：

| 硬件改动（其余沿用 baseline） | P1 tile 8×16×48 | D1 tile 1×16×48 |
|---|---:|---:|
| 原配置 | 267 cycles | 108 cycles |
| TC array 改成 8×16 | 147 cycles | 105 cycles |
| K parallel 改成 4 | 197 cycles | 74 cycles |

这是服务公式的计算结果，不是完整程序加速比。它直观说明，同一个硬件改动对 prefill 和 decode 的价值可以很不一样。

Cache 也有本模型特有的成本：命中存在固定服务延迟；减少 HBM 流量可能节能，但未必让单次无拥塞读取更快。Multicast 需要同 issue cycle、相同行及不同目的 WG 等条件，NoC 仍对目的地收费。8 个 HBM channel 理想满载时，仅 HBM 动态功率按系数就可达约 19.2 W，因此强并行后需要重新检查 20 W 窗口门限；这并不表示 8 channel 配置必然失败。

来源：[硬件菜单与成本](https://linux-slai.tail6d76d1.ts.net:8443/model/hardware.py)、[计算服务公式](https://linux-slai.tail6d76d1.ts.net:8443/model/service.py)、本地 [pipeline_events.py](../../vendor/official/codesign/challenge/pipeline_events.py)、[cost_v04.json](../../vendor/official/codesign/challenge/cost_v04.json)、[timed_cache.py](../../vendor/official/codesign/challenge/timed_cache.py)。

**6．P1 和 D1 应采取不同软件策略**

P1 有 prompt 行并行和两个独立 batch，适合矩阵分块与权重复用。四个线性变换是 QKV、O、FFN1、FFN2；在相邻 M tiles 之间重复加载 B 是重要研究对象。先让两个 batch 使用独立 WG/SM，是依赖结构较简单的第一种多 SM 方案；之后可以按输出 tile、query 行或 head 扩展并行。在 prompt 阶段，多个位置可进行矩阵化计算，但 attention 仍必须遵守各自 causal mask。

D1 每步只有一个位置，主要线性运算是 M=1 的 GEMV。不能把 8 步简单拼成一个可同时访问输入的 GEMM。应在**当前步内部**沿输出 N 维或 attention heads 分工，并研究权重、历史 K/V 的局部复用。D1 的 TC M 维尾部利用率差，P1 适合的更大 M 阵列不一定适合它；两程序应使用不同 tile，但共用同一硬件配置。

根据公开模型形状计算，P1 权重约 **9.026 MiB**，D1 权重约 **1.509 MiB**，不含激活与 scratch。D1 最多 136 位置的两层 K/V 约 0.266 MiB，因此 2/4 MiB cache 是有理由测试的候选；不能仅凭总容量宣布一定命中，仍要考虑布局冲突、scratch 干扰和访问时序。P1 单层权重约 3.009 MiB，可研究按层处理与缓存驻留，但还需保持每个 batch 的中间值及输出语义。

粗略运算量（一次 MAC 算 2 FLOPs，未计 norm/softmax/GELU 等逐元素计算）如下：

| 部分 | P1 | D1 |
|---|---:|---:|
| 四个线性投影合计 | 613,416,960 FLOPs | 6,291,456 FLOPs |
| QK 和 PV 合计 | 13,178,880 FLOPs | 1,085,440 FLOPs |

以上是数学工作量推导，不是模拟周期预测。它支持优先研究线性层，但 attention 内的 SFU、访存和指令开销可能放大实际成本，应以模型事件和实验为准。

**7．建议的实验顺序和具体假设**

| 顺序 | 实验 | 为什么值得做 | 必须同时检查 |
|---|---|---|---|
| E0 | 原始硬件与程序复现 | 建立可信评分起点 | 数值、周期、门限、hash、runtime |
| E1 | GEMM K tile 从 48 测到 64 | 现有 RF 下 64×16=1024 元素合法，减少 K loop 次数 | FP32 累加误差、尾块、两 case 周期 |
| E2 | 固定程序，小范围测 RF ports、TC、K parallel | 识别硬件敏感性，成本较易解释 | 容量、求和顺序、面积与窗口功率 |
| E3 | attention scores/probs 留在 RF | 最大仅 65/136 元素，可以保留原稳定 softmax 算法并消除中间落盘 | 额外 RF lane、QK/PV accumulator 与中间值生命周期 |
| E4 | P1 两个 batch 分配独立 WG/SM | 先验证跨 WG 调度和公共 commit 的正确实现 | scratch 隔离、两个 prompt 完成后才能 commit |
| E5 | P1/D1 按输出 N tile、head 等进一步并行 | 让额外 SM 对两个 case 都产生价值 | producer/consumer 事件、输出唯一写入、带宽 |
| E6 | M/N/K 分块、循环顺序、RF 复用 | 减少重复加载，匹配阵列形状 | 所有 operand 分区容量、跨阶段生命周期 |
| E7 | FFN bias/GELU、residual 等局部融合 | 减少 scratch 往返和 RF/HBM 指令 | 同位置语义、舍入、最终输出只写一次 |
| E8 | cache、SH、双缓冲、DMA、multicast | 在已有可利用并行/复用时评估这些资源 | 命中与冲突、等待、RF/SH源被覆盖风险 |
| E9 | 分块 attention、online softmax | 当 attention 已成为重要成本时再扩大投入 | 稳定数值、causal mask、history/new KV边界 |

E1 是一个可检验的初始实验，不是已证明的提升。当前生成器固定 M=8、K=48，仅 N 支持 8/16；扩展 tiling 时，可先在 M∈{8,16}、N∈{8,16,32}、K∈{32,48,64} 中做容量预筛选。对独立放在一个 RF lane 的 A/B/C，需要分别满足 `M×K`、`K×N`、`M×N` 不超过该 lane 容量。D1 则使用实际 M=1。

Fusion 还有一个实现细节：ISA 不会自动把 16 元素 bias 广播成 8×16 矩阵；需要逐行 RF view 处理或显式构造等长 operand。Attention 的原 scores/probs scratch 被串行 head 循环复用，改成 head 并行时必须同时消除或隔离这些缓冲区。

单因素实验用于理解原因；确定方向后还需联合复测。比如“TC 增加无收益”只说明当前 tile 或 RF/供数条件下无收益，换 tile 后需要重新评价。不能把第一轮单因素结论当成永久规则。

**8．搜索方法与实验工程**

完整硬件菜单约有 1207 亿组合，不宜全枚举，也不必先建 MILP。建议先维护少量明确的软件策略族，用硬件合法性、面积、RF容量、SH驻留和 tile 利用率筛掉明显不合适的点，再做粗粒度搜索和局部细化。保留多种 P1/D1 折中点，而不是过早固定一个只对单个 case 有利的配置。

每个候选需有稳定 ID，保存硬件、生成器参数、程序 hash、命令、运行版本、功能结果、两案周期、窗口功率、面积、资源统计和接受/拒绝原因。缓存应按 scorer版本、硬件、两份程序、seed 等关键条件建立，避免把不同候选的结果混用。

`estimate` 跳过功能验证，不代表正确。`check` 和 `grade` 都会运行完整 timing；为了避免无意义地重复三遍，语义有变化的候选先 `check`，正确后才根据需要 `grade`；大范围硬件探索可用 `estimate` 预筛，但最终候选仍须准确环境下完整 `grade`。原始基线直接跑一次 `grade` 已包含功能检查和计时。本机基线完整一轮约 6 分 44 秒，因此不能把大量候选全量评分当作廉价操作，应先静态裁剪；并发实验数也应依据后续候选的实际 CPU/RSS 测量设置。

自动脚本不能只看退出码：`grade` 可能在面积、功率或时延门限失败时仍返回 0。必须解析 `eligible`、`experimental_score` 和 `gate_diagnostics`；顶层 `score` 不是本地 experimental score 的可靠读取位置。每次报告必须使用新文件名，官方写入采用独占创建。

数值回归以公开 seed 7 起步，入选候选再用多个不同 seed；CLI 支持重复 `--seed`。通过公开种子不能保证所有隐藏输入都通过，因此要坚持稳定 softmax 和正确的层/步边界，避免依赖特定数值。

保存源码与报告的建议结构（后续实施时建立工作副本）：

```text
AI_Infra/
  references/                 官方原件与冻结 scorer
  .venv/                      准确版本环境，不打包提交
  research/                   本轮报告和 baseline 复现记录
  work/                       后续从 starter 建立的开发副本
    hardware.json
    programs/
    project/compiler.py       或自己的生成器
    project/iteration-log.md
    project/experiments/
    agent-trace/
```

**9．正确性最容易出问题的地方**

1. ABI 网页和汇编使用 FP32 元素偏移；Python layout 的 `address` 是字节，生成器会除以 4。二维 tile 必须同时设置 shape/strides/count，不能把 strided tile 误写成连续数组。
2. K/V 是 head-major 布局；P1 batch 不能互相读取，D1 历史和新增 K/V 在不同区域，后续 step 必须包含前面新生成的位置。
3. 每层都要输出新 K/V，不能只检查最终 hidden。所有规定输出必须完整且恰好写一次，累加中间结果应放 RF 或 scratch。
4. 不同 WG 的源码排列顺序不会自动建立 HBM 依赖。跨 WG 的读写需 WAIT、BARRIER 或合适的 COMMIT。每个展开 event 名必须唯一。
5. 异步 ST 可能仍在读取 RF/SH 源；重用缓冲前要确认依赖。所有 RF/SH/scratch 数据读取前必须初始化，MMA accumulator 同样如此。
6. SH 属于 WG 私有空间，不能直接用来做不同 WG 的共享通讯。每 SM 最多 4 个驻留 WG，同时受 SH 总配额限制。
7. 固定 FP32 不代表运算重排后逐位相同。K parallel、tile、归约、fusion 都可能改变误差，不能以真实 GPU 的经验代替 checker。
8. Softmax 需要减最大值或等价稳定实现。分块 attention 必须处理 causal mask，避免空块、全 masked 行或错误初始化产生 NaN。
9. frozen `codesign/`、cost/workload 和 baseline manifest 都有 provenance 检查；优化自有 generator，而不是更改评分器。

来源：[ISA](https://linux-slai.tail6d76d1.ts.net:8443/isa/)、[HBM ABI](https://linux-slai.tail6d76d1.ts.net:8443/abi/)、[汇编导读](https://linux-slai.tail6d76d1.ts.net:8443/assembly-guide/)。

**10．公开研究能提供什么，不能直接套什么**

Roofline 把计算上限、实际内存流量和运算强度结合起来，适合用来提出“提高复用还是增加吞吐”的假设。本作业还需要分别考虑 RF、每 SM 链路、共享 NoC、HBM 和事件依赖，所以单条 roofline 不能精确预测得分。参考作者原文：[Roofline, Berkeley technical report](https://www2.eecs.berkeley.edu/Pubs/TechRpts/2008/EECS-2008-134.pdf)。

FlashAttention 的可借鉴点是分块、片上复用、在线稳定 softmax，以及避免把完整 attention 中间矩阵反复写回 HBM。这里的序列仅 64/128 左右，且教学 ISA、RF服务和缓存延迟不同；不能搬用 CUDA kernel，不能承诺论文中的加速比。参考：[FlashAttention 原论文](https://arxiv.org/abs/2205.14135)。

若后期实现 online softmax，可在每个 key block 上维护行最大值 m、归一化和 l、未归一化的加权和 o。对新 block 的 logits s 与 value V：

```text
m_new = max(m, max(s))
alpha = exp(m - m_new)
p = exp(s - m_new)
l_new = alpha*l + sum(p)
o_new = alpha*o + p@V
最终输出 = o/l
```

这是算法思路，尚未实现为本题 ISA。首块初始化、masked 元素、RF布局和 FP32 误差需要另外设计与验证。

**11．时间安排与提交**

从 9 月 26 日起，可按下表推进；实验收益不确定，时间分配应随测量调整。

| 日期（北京时间） | 建议完成的可验收结果 |
|---|---|
| 9/26–9/27 | baseline 完整复现、报告解析、独立工作副本、日志与 trace 保存流程 |
| 9/28–9/29 | 少量 tile 与硬件敏感性实验；保留首个正确且优于 baseline 的版本 |
| 9/30–10/2 | P1 batch/WG 并行，D1 单步内部并行，逐步修正同步 |
| 10/3–10/5 | 联合调参、局部融合/复用、多种子回归 |
| 10/6 | 完成第一阶段可提交包并留出排队与修正时间 |
| 10/7 12:00 前 | 第一阶段提交截止；影响后续合作提议遴选 |
| 10/7–10/10 | 只在保留最佳合格版本的前提下继续较大改进 |
| 10/11 | 冻结最终候选、复测、检查包和完整 trace，完成最终上传 |
| 10/12 12:00 | 最终停止接收新提交 |

两个截止时间均为北京时间中午 12:00，即 UTC 04:00。服务器通常需 10–30 分钟评分，繁忙时更久；应在本地试验，通过后再上传。每个学生 ID 10 分钟最多一次，保持真实学号与相同显示名，保存 receipt ID 与 lookup key，最终保留同一学号下最高合格成绩。

ZIP 根目录必须有：

```text
hardware.json
programs/M1_P1.asm
programs/M2_D1.asm
project/iteration-log.md
agent-trace/
```

自有 generator、搜索脚本和实验记录放 `project/`。每份汇编源码 ≤8 MiB、静态循环嵌套 ≤32、展开指令 ≤1000 万，ZIP ≤25 MiB。`agent-trace/` 要保存本项目使用的完整原生 agent 会话记录；本轮调研及自有辅助 agent 的相关记录也应覆盖，摘要不能替代完整轨迹。项目记录和 trace 会人工审核，网站未自动拦截缺失不代表可以省略。本轮目录不是最终提交包。

来源：[课程作业入口与时间](https://mlsys.github.io/homework/)、[提交要求](https://linux-slai.tail6d76d1.ts.net:8443/statement/#3-what-to-submit)。

**建议首先实施的工作**：基线已经确认，下一步建立候选结果表，以 K=64 的合法 tile 实验、RF ports 小范围实验和 attention 中间值驻留 RF 验证优化方向，再实施 P1 两 batch 的独立 WG 调度。由这些结果决定是否先扩大并行、扩大 tile 或减少数据移动；目前没有足够证据指定最优硬件，也不应承诺最终分数。
