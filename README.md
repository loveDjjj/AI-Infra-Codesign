# AI Infra 协同设计研究工程

本仓库实现课程工作负载 v0.7 的硬件与程序协同设计。`vendor/official/` 是冻结的官方评估器；最终正确性、周期、功耗与分数以它生成的原始报告为准。

**截至 2026-09-28 的已知状态**：实验账本最高已审计整案为 `official-1790581630771528243`，**49,283.865188** 分。它属于隔离生成器源码 epoch，原件在本机 `data/releases/`；根目录 `configs/best.yaml` 和 `data/state.json` 的已晋升指针仍指向可由当前根目录源码复现的 joint28（47,957.06 分）。两者不能混用。最近的第四个源码批次已结束，当前没有运行中的流水线。网站提交仍关闭；GitHub 提交也不代表课程提交。

## 从哪里开始

| 要做什么 | 入口 |
|---|---|
| 理解代码与数据归属 | [架构](docs/architecture.md) |
| 构建、评估、发布与恢复 | [工作流程](docs/workflow.md) |
| 查优化结论及适用范围 | [研究经验](docs/knowledge.md) |
| 看下一轮工程优先级 | [自动化方案](docs/automation-plan.md) 与 [路线图](docs/roadmap.md) |
| 看本次仓库整理依据 | [仓库审计](docs/repository-audit.md) |
| 看课程原文快照 | [课程材料](docs/assignment/homework-announcement.md) |

```bash
# 使用本机锁定环境；克隆后需按课程来源恢复原始官方包和环境。
./lab build configs/best.yaml --out workspace/builds/current
./lab run workspace/builds/current --level functional --out workspace/evaluations/functional.json
./lab report
./lab pipeline configs/pipeline.yaml                 # 只检查计划
./lab pipeline configs/pipeline.yaml --execute       # 显式执行
./lab family-restore official-1790581630771528243 --out workspace/families/official-1790581630771528243  # 恢复已审计实现族
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

`docs/dashboard.html` 是 `./lab report` 生成的本地快照，不入 Git；浏览器直接打开即可。它会显示候选、整案和测量阶段；资源忙碌比例只能用于提出瓶颈假设，不能当作因果等待证明。

## 三种保存边界

- **GitHub**：当前源码、配置、测试、文档，以及轻量实验账本、决策和课程迭代记录。
- **本机 `data/`**：完整代理轨迹、官方原始包、关键发布原件与隔离源码证据；这些材料受保护，不能按临时文件清理。Git 忽略它们，但 `lab package` 会从本机选取课程所需材料。
- **`workspace/`**：隔离源码、评估缓存、运行状态、日志和生成看板的中间数据。它不入 Git，也不能成为唯一证据来源。`lab clean --dry-run` 只列出清理预览。

当前最高分属于隔离源码 epoch，因此根目录 `lab package` 的逐字节再生条件尚不满足；从对应 epoch 打包前应先复核其源码、官方报告及课程 ZIP 限制。不要把 49,283 分版本误称为根目录 `best.yaml`。

网站上传是单独动作：`scripts/submit.py` 默认只做本地检查，只有显式 `--submit` 才上传；上传规则见 [工作流程](docs/workflow.md)。
