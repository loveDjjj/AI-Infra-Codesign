# AI Infra 协同设计研究工程

本仓库实现课程工作负载 v0.7 的硬件与程序协同设计。`vendor/official/` 是冻结的官方评估器；最终正确性、周期、功耗与分数以它生成的原始报告为准。

**截至 2026-09-29 的本机验证**：最高已审计整案为跨源码组合 **49,697.962184** 分；其可提交 ZIP 已在 `data/submissions/best-local.json` 登记，并通过干净目录再生校验。后续最高分、运行任务与可提交包以实时看板和本机指针为准。根目录 `configs/best.yaml` 是另一条已晋升源码基线，不代表跨源码冠军。GitHub 推送不等于课程网站提交。

## 从哪里开始

| 要做什么 | 入口 |
|---|---|
| 理解代码与数据归属 | [架构](docs/architecture.md) |
| 构建、评估、发布与恢复 | [工作流程](docs/workflow.md) |
| 查优化结论及适用范围 | [研究经验](docs/knowledge.md) |
| 看下一轮工程优先级与清理规则 | [自动化方案](docs/automation-plan.md) |
| 看课程原文快照 | [课程材料](docs/assignment/homework-announcement.md) |

```bash
# 使用本机锁定环境；克隆后需按课程来源恢复原始官方包和环境。
./lab build configs/best.yaml --out workspace/builds/current
./lab run workspace/builds/current --level functional --out workspace/evaluations/functional.json
./lab dashboard --host 0.0.0.0 --port 8765      # 实时看板；浏览器打开 http://服务器地址:8765/
./lab pipeline configs/pipeline.yaml                 # 只检查计划
./lab pipeline configs/pipeline.yaml --execute       # 显式执行
./lab clean --dry-run                               # 按目录查看清理计划
./lab clean --apply                                 # 压缩历史账本并清理旧产物
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

`./lab dashboard` 提供实时只读页面，每 5 秒更新最佳已审计分数、P1/D1 周期趋势和当前任务；它不装载大型历史报告。默认仅监听 `127.0.0.1`，跨机器查看时按上例指定 `--host 0.0.0.0`。`./lab report` 仍可生成离线快照，但不会实时更新。

## 三种保存边界

- **GitHub**：当前源码、配置、测试、文档，以及轻量实验账本、决策和课程迭代记录。
- **本机 `data/`**：完整课程代理轨迹、官方原始包、最高分及其两案来源、已晋升基线与回退原件；其他实验只留账本指标。Git 忽略大原件，`lab package` 从本机选取课程材料。
- **`workspace/`**：构建、缓存、运行状态和日志，不入 Git。清理会保留全局 AI 会话指针及独立搜索环境。

当前最高分是跨源码组合，已从受保护的两案源码归档完成独立再生、打包与 ZIP 校验；完整代理轨迹以无损整组压缩形式进入提交包。自动流水线会在新的已审计成绩超过冠军时更新本地 ZIP。

网站上传是单独动作：`scripts/submit.py` 默认只做本地检查，只有显式 `--submit` 才上传；上传规则见 [工作流程](docs/workflow.md)。
