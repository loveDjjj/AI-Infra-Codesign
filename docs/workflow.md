# 工作流程

所有命令从仓库根目录执行。`./lab` 使用锁定的 `.venv`；`vendor/official/` 是只读冻结副本。构建和正式评估使用独立进程，运行中的 campaign 固定源码、配置和官方身份。

## 1. 单个设计

```bash
./lab build configs/best.yaml --out workspace/builds/check
./lab run workspace/builds/check --level functional --case M1_P1 --out workspace/evaluations/p1-functional.json
./lab run workspace/builds/check --level both --case M1_P1 --out workspace/evaluations/p1-both.json
./lab run workspace/builds/check --level full --out workspace/evaluations/full.json
./lab audit <完整整案记录ID>
./lab report
```

输出路径必须不存在。功能结果、局部周期、预测组合、官方整案和审计分别记录；只有合格且审计通过的整案可视为正式性能事实。`audit` 不自动 `promote`。改变生成器时，先校验默认硬件和两份 ASM 的逐字节再生；改变官方工具则拒绝运行。

## 2. 搜索与动态流水线

```bash
./lab search configs/search.yaml                                  # 只预览候选
./lab search configs/search-parallel.yaml --execute --workers 8 --out workspace/search/example
./lab pipeline configs/pipeline.yaml                               # 只预览配置
./lab pipeline configs/pipeline.yaml --execute                     # 启动新批次
./lab pipeline configs/pipeline.yaml --execute --resume            # 相同身份、预算和目录恢复
./lab pipeline-status --campaign closed-loop-v2
./lab pipeline-inject configs/targets/<目标>.yaml --campaign closed-loop-v2
```

流水线按目标展开候选，静态检查后隔离构建，用硬件和对应 ASM 哈希去重；功能通过后释放单案时序，只有同硬件的 P1/D1 可组成预测分数。超过当前已审计最佳的组合才能排入独立官方整案槽位，随后自动审计。官方完整评分不使用探索缓存。任务由内存预算、槽位和累计冷调用预算约束；worker 完成后动态补位。

目标、调度和模型状态在 `workspace/pipeline/<campaign>/`，实际成绩在 `data/experiments.jsonl`。`--resume` 不增加原预算；源码或设置变化必须新开 campaign。项目当前的全局 AI 分析能提出并校验新目标，结构改动则在隔离源码副本中进行。它仍会在候选空间耗尽或结构提案预算用尽时结束，并非保证永不空转。`./lab implementation-loop --campaign <批次> --proposal-id <ID> --execute` 可单独处理已有结构提案。

从已审计 release 准备平铺实现族：`./lab family-restore official-1790581630771528243 --out workspace/families/official-1790581630771528243`。命令核对归档源码和硬件、P1、D1 三份生成产物，并只在隔离目录中设置最高分基线；不启动流水线。结构提案的编码会话与长期全局规划会话独立；相同 `transformation_id` 在同源码、同案例只处理一次。多个冻结实现族的参数搜索、结构编码和结构验证共用主流水线的进程与内存预算；编码使用探索槽，验证使用受控验收槽。验证内部仍串行执行回归、功能、时序和整案，暂不能把几个隔离目录各自开成满负载流水线。

结构候选功能正确且功耗合格、但首次收益不足时，会留下单案研究证据并启动至多 3 个受影响案例的对照点；通过完整官方验收但低于晋升门槛的候选也可继续研究。当前批次若还有足够时间和单案调用余额，这些对照点直接加入原批次；否则交给新批次。研究准入不改变主工程的最佳指针。混合源码的 P1/D1 组合按各自冻结生成器再生，完成复合审计后才允许正式归档；混合来源课程 ZIP 已支持独立再生验证。

## 3. 诊断、看板与知识

```bash
./lab profile workspace/builds/check --case M1_P1 --compare workspace/evaluations/full.json --out workspace/profiles/p1.json
./lab report
./lab clean --dry-run
```

看板生成到 `docs/dashboard.html`，本地浏览器打开即可。`lab clean --dry-run` 只列具体文件，不执行删除。诊断报告中资源利用率与算子跨度是观察，等待原因是推断；适用范围和失败经验写入 `docs/knowledge.md`，实验数字仍以账本和原始报告为准。

## 4. 晋升、打包与网站上传

```bash
./lab promote <已审计、当前根源码可再生的整案ID>
./lab package <记录ID> --out workspace/reports/release.zip
./lab verify workspace/reports/release.zip
.venv/bin/python scripts/submit.py workspace/reports/release.zip          # 本地检查
.venv/bin/python scripts/submit.py workspace/reports/release.zip --submit # 实际网站上传
```

`package` 要求 `reproduction=verified` 或 `verified_composite`。前者由当前根源码再生；后者分别从受保护的 P1/D1 源码快照再生，适用于跨源码组合。隔离 epoch 的 `epoch_verified` 报告即使分数更高，也不能直接在根目录打包；必须从对应源码及保全原件另行审计。课程 ZIP 保留根目录 `hardware.json`、两份 ASM、`local-grade.json`，以及迭代记录和课程代理轨迹；大型轨迹在 ZIP 内以无损 `.jsonl.xz` 保存，`lab verify` 会逐份解压核对原始哈希。完整实验账本和生成看板不重复塞入 ZIP。打包与验证不上传网站。

网站脚本默认学号 `260010081`、名称 `GPT-6-Astra-Ultra`，可显式覆盖。实际上传要求完整合格分数比本地已知个人最佳至少提高 1000 分，且与上次上传间隔至少 10 分钟；回执和查询密钥保存在不入 Git 的 `data/submissions/`。HTTP 连接中断后先查询，避免重复提交。

## 5. GitHub 与本机证据

Git 收录当前实现、配置、文档、测试和轻量事实。`data/agent-trace/`、`data/releases/`、`data/evidence/`、`data/official-starter.zip` 和 `workspace/` 保留在本机并独立备份；`docs/dashboard.html` 随时可生成。克隆 GitHub 仓库后无法仅凭 Git 恢复全部历史隔离源码或直接打出课程 ZIP，须恢复对应受保护材料。冻结官方工具本身保留在 `vendor/official/`。不要为了清理仓库删除课程轨迹或发布原件。
