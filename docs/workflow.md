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

当前最高已审计整案为 `pair-w2-throttle-highest-full-1790611520350037544`，49,420.082119 分，属于跨源码组合。根目录 `best.yaml` 仍是 joint28，47,957.06 分；网站未提交。跨源码组合从各自受保护源码再生，不能用单份当前生成器重建。单一生成器的受保护 release 才使用 `lab family-restore` 恢复实现族。

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

课程包必须包含 ZIP 根目录的 `local-grade.json`、`hardware.json`、两份 ASM，以及 `project/iteration-log.md` 与完整代理轨迹。`package` 和 `verify` 不上传网站。网站上传受至少提高 1000 分、间隔至少 10 分钟的脚本规则约束；回执只保存在本机 `data/submissions/`。GitHub 推送与课程网站提交是两件事。

源码、配置、四份核心文档、测试和轻量账本进入 Git；`data/agent-trace/`、`data/releases/`、提交 ZIP、官方起始包及工作区大原件不进入 Git。克隆仓库后须另外恢复受保护材料才能复现最高分和打出课程 ZIP。资源忙碌比例只支持瓶颈假设，不能当成已证明的因果等待。
