# 项目结构与事实边界

主线是「配置 → 隔离构建 → 功能与时序 → 官方整案 → 审计 → 账本 → 看板」。搜索和 AI 只能提出与筛选候选；`vendor/official/` 的原始评分器负责最终事实。

| 目录或文件 | 唯一职责 | 长期保留方式 |
|---|---|---|
| `configs/` | 基线、当前根源码最佳、试验与流水线预算 | Git；`best.yaml` 仅由晋升流程更新 |
| `src/codesign_lab/codegen/` | P1、D1 的当前生成器及实际依赖 | Git；结构变化使用独立源码 epoch |
| `src/codesign_lab/evaluation/` | 隔离评估、缓存、官方调用、审计与诊断 | Git；官方原件不可修改 |
| `src/codesign_lab/search/` | 目标、采样、剪枝、进程调度、AI 决策与结构提案 | Git；运行状态留在 `workspace/` |
| `src/codesign_lab/{config,build,records,report,release,cli,ai_bridge}.py` | 输入、构建、事实账本、展示、发布、命令与模型桥接 | Git |
| `schemas/`、`tests/` | AI 目标合同和回归边界 | Git |
| `data/experiments.jsonl`、`data/decisions.jsonl`、`data/state.json`、`data/iteration-log.md` | 事实、决策、指针、课程过程记录 | Git；不要把整案与单案混成一个最佳值 |
| `data/agent-trace/`、`data/releases/`、`data/evidence/`、`data/official-starter.zip` | 完整课程轨迹、原始报告、隔离版本与复现锚点 | 受保护本机材料；不入 Git，须单独备份 |
| `docs/` | 架构、流程、经验、路线与自动化方案 | 源文档入 Git；生成的 `dashboard.html` 不入 Git |
| `workspace/` | 候选、缓存、完整运行状态、隔离源码和临时日志 | 不入 Git；清理前先看具体路径 |
| `scripts/` | 兼容入口、基准脚本、课程网站提交 | Git；主逻辑在 `src/` |
| `vendor/official/` | 冻结课程工具 | Git；哈希验证，不改写 |

`configs/*.yaml` 当前实际采用 JSON 兼容 YAML；加载器不要求额外 YAML 依赖。搜索环境 `workspace/search-env` 与官方 `.venv` 分离，正式评分由锁定的官方环境执行。

## 三种“最佳”

1. `data/state.json.promoted_record` 是**当前根源码可再生且已晋升**的版本；目前为 joint28。
2. 账本中最高 `eligible && audited && scope == full` 是**最高已审计事实**；目前为隔离 epoch 的 49,283.865188 分。`epoch_verified` 说明隔离源码可复现，不说明根源码可复现。
3. 网站已提交版本由回执和 `submitted_release` 表示；Git push 不改变它。

看板从账本和已保全报告生成。算子跨度是测量；“等待原因”和 bound 依据资源忙碌比例推算时必须标成低置信度假设，不能当成因果结论。

结构改动先保持默认配置生成的硬件及两份 ASM 逐字节一致，再给新增开关做功能、时序和官方整案验证。源码批次启动后固定生成器与评估器身份；不能在运行中改变共享源码。完整自动化边界见 [自动化方案](automation-plan.md)。
