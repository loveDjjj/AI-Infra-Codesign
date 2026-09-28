# 项目结构与事实边界

主线是「配置 → 隔离构建 → 功能与时序 → 官方整案 → 审计 → 账本 → 看板」。搜索和 AI 只能提出与筛选候选；`vendor/official/` 的原始评分器负责最终事实。

| 目录或文件 | 唯一职责 | 长期保留方式 |
|---|---|---|
| `configs/` | 基线、当前根源码最佳、试验与流水线预算 | Git；`best.yaml` 仅由晋升流程更新 |
| `src/codesign_lab/codegen/` | P1、D1 的当前生成器及实际依赖 | Git；结构变化使用独立源码 epoch |
| `src/codesign_lab/evaluation/` | 隔离评估、缓存、官方调用、审计与诊断 | Git；官方原件不可修改 |
| `src/codesign_lab/search/` | 目标、采样、剪枝、进程调度、AI 决策与结构提案 | Git；运行状态留在 `workspace/` |
| `src/codesign_lab/{config,build,records,report,release,maintenance,cli,ai_bridge}.py` | 输入、构建、轻量账本、实时展示、发布、清理、命令与模型桥接 | Git |
| `schemas/`、`tests/` | AI 目标合同和回归边界 | Git |
| `data/experiments.jsonl`、`data/decisions.jsonl`、`data/state.json`、`data/iteration-log.md` | 事实、决策、指针、课程过程记录 | Git；不要把整案与单案混成一个最佳值 |
| `data/agent-trace/`、`data/releases/`、`data/official-starter.zip` | 完整课程轨迹、最高分与两案来源、基线/回退原件、官方起始包 | 受保护本机材料；不入 Git，须单独备份 |
| `docs/` | 架构、流程、经验与自动化方案 | 四份核心文档入 Git；实时看板页面在 `src/` |
| `workspace/` | 候选、缓存、完整运行状态、隔离源码和临时日志 | 不入 Git；清理前先看具体路径 |
| `scripts/` | 兼容入口、基准脚本、课程网站提交 | Git；主逻辑在 `src/` |
| `vendor/official/` | 冻结课程工具 | Git；哈希验证，不改写 |

`configs/*.yaml` 当前实际采用 JSON 兼容 YAML；加载器不要求额外 YAML 依赖。搜索环境 `workspace/search-env` 与官方 `.venv` 分离，正式评分由锁定的官方环境执行。

## 三种“最佳”

1. `data/state.json.promoted_record` 是**当前根源码可再生且已晋升**的版本；目前为 joint28。
2. 账本中最高 `eligible && audited && scope == full` 是**最高已审计事实**；目前为跨源码组合的 49,420.082119 分，受保护原件可逐字节独立再生。
3. 网站已提交版本由回执和 `submitted_release` 表示；Git push 不改变它。

实时看板从轻量账本和进程身份生成，不依赖旧工作区原件。算子跨度是测量；“等待原因”和 bound 依据资源忙碌比例推算时必须标成低置信度假设，不能当成因果结论。

结构改动先保持默认配置生成的硬件及两份 ASM 逐字节一致，再给新增开关做功能、时序和官方整案验证。源码批次启动后固定生成器与评估器身份；不能在运行中改变共享源码。完整自动化边界见 [自动化方案](automation-plan.md)。

当前主控可以在同一批次中调度多个冻结实现族的参数搜索，并把结构提案拆成编码和验证两个持久任务。编码占探索槽；验证占受控验收槽，且为普通官方整案保留一个槽位。通过功能与单案时序的结构候选可在原批次继续有限邻域搜索，来源源码和产物身份逐次核对。验证任务内部的回归、功能、时序与整案仍按顺序执行；各关卡独立调度和真实 Codex 长时运行验证属于[后续工作](automation-plan.md)。全局 Planner 负责研究决策，独立 Coder 负责各自隔离源码；同一硬件/ASM 的探索评估共享精确缓存，完整官方评分始终独立执行。
