# 工作流程

所有命令从仓库根目录执行。`./lab` 使用锁定环境；`vendor/official/` 冻结且只读。每个运行批次固定源码与评估器身份。

## 构建、评估与搜索

```bash
./lab build configs/best.yaml --out workspace/builds/check
./lab run workspace/builds/check --level functional --case M1_P1 --out workspace/evaluations/p1.json
./lab pipeline configs/pipeline.yaml                    # 预览
./lab pipeline configs/pipeline.yaml --execute          # 启动
./lab pipeline configs/pipeline.yaml --execute --resume # 相同源码与批次身份恢复
```

功能检查、单案时序、预测组合、完整官方评分和审计分别入账。只有合格且已审计的完整整案可更新最高分事实。`--resume` 不增加预算；源码变化必须新开批次。搜索器做合法性检查、产物去重、精确缓存和受控并发；同硬件 P1/D1 才可组合，完整官方评分仍独立执行。AI 决策通过结构化目标进入队列，不能直接改账本或正在运行的生成器。

当前最高已审计结果、可提交 ZIP 和实时任务从看板读取。`data/submissions/best-local.json` 是本地冠军包指针；只有独立解包再生验证成功才会更新。跨源码组合从两案各自受保护的来源再生，不能用根目录单份生成器重建。单一生成器的受保护 release 可用 `lab family-restore` 恢复实现族。

## 实时看板与轻资产整理

```bash
./lab dashboard --host 0.0.0.0 --port 8765
./lab clean --dry-run
./lab clean --apply
```

看板每五秒读取轻量账本和真实进程身份，显示最高分、P1/D1 趋势及在途任务。`lab report` 只在需要离线 HTML 时手工执行；流水线不周期生成。`clean --dry-run` 按目录列出路径、体积与理由；`clean --apply` 首先确认没有搜索/评估进程，压缩账本与 AI 证据，随后清理已结束工作区、非最高分发布原件与旧诊断原件。

长期保留当前最高分及其两案最佳源码、已晋升 joint28、回退 joint24、课程代理轨迹、官方起始包、AI 全局会话指针和独立搜索环境。普通实验仅保留完整配置、关键周期/功耗/分数、失败原因、来源哈希和少量资源压力摘要。清理后普通历史为 `record_only`，不可当成可再生先验或再次审计的原件；需要原始证据的候选应在清理前晋升为受保护 release。

## 审计、打包与网站上传

```bash
./lab audit <完整整案记录ID>
./lab package <已审计记录ID> --out workspace/reports/release.zip
./lab verify workspace/reports/release.zip
.venv/bin/python scripts/submit.py workspace/reports/release.zip          # 本地检查
.venv/bin/python scripts/submit.py workspace/reports/release.zip --submit # 实际上传
```

课程包必须包含 ZIP 根目录的 `local-grade.json`、`hardware.json`、两份 ASM，以及 `project/iteration-log.md` 与完整代理轨迹。流水线在官方整案合格并审计后，自动打包更高分版本，逐份校验无损压缩的代理轨迹，并在干净目录再生三份产物。`package` 和 `verify` 不上传网站。网站上传受至少提高 1000 分、间隔至少 10 分钟的脚本规则约束；回执只保存在本机 `data/submissions/`。GitHub 推送与课程网站提交是两件事。

源码、当前配置、核心文档、测试和轻量账本进入 Git；配置命名与保留规则见 [configs/README.md](../configs/README.md)。旧批次 YAML 不长期保留，历史结论从实验账本和决策账本读取。`data/agent-trace/`、`data/releases/`、提交 ZIP、官方起始包及工作区大原件不进入 Git。课程明确要求完整代理会话轨迹，因此轨迹虽不作为 AI 日常搜索输入，仍须在提交包中保留。独立规划、编码调用只把会话 ID 写入 `data/agent-trace/session-links.jsonl`，详细调用日志放在可清理的 `workspace/agent-calls/`；打包时按 ID 导出原生会话，清单也覆盖根会话树。克隆仓库后须另外恢复受保护材料才能复现最高分和打出课程 ZIP。资源忙碌比例只支持瓶颈假设，不能当成已证明的因果等待。
