# AI Infra 协同设计研究工程

本仓库实现课程工作负载 v0.7 的硬件与程序协同设计。`vendor/official/` 是冻结的官方评估器；最终正确性、周期、功耗与分数以它生成的原始报告为准。

**截至 2026-09-28 的已知状态**：实验账本最高已审计整案为跨源码组合 `pair-w2-throttle-highest-full-1790611520350037544`，**49,420.082119** 分，原件在本机 `data/releases/`；根目录 `configs/best.yaml` 和 `data/state.json` 的已晋升指针仍指向可由当前根目录源码复现的 joint28（47,957.06 分）。两者不能混用。运行中任务以实时看板核对进程身份为准。网站提交仍关闭；GitHub 提交也不代表课程提交。

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

当前最高分是跨源码组合，可用受保护的两案源码归档独立再生并打包；根目录 `best.yaml` 仍是 joint28。清理前已对最高分包完成三份产物逐字节再生验证。

网站上传是单独动作：`scripts/submit.py` 默认只做本地检查，只有显式 `--submit` 才上传；上传规则见 [工作流程](docs/workflow.md)。
