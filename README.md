# AI Infra 研究工程

当前代码已从旧工程迁入；joint28 硬件和两份 ASM 已逐字节复现。旧工程已删除，必要轨迹与当前、回退版本证据保存在 data/；新白名单打包入口已通过独立 ZIP 再生验证。

```bash
./lab build configs/best.yaml --out workspace/builds/new
./lab run workspace/builds/new --level functional --out workspace/evaluations/check.json
./lab search configs/search.yaml
./lab report
./lab package joint28_v072_w2_owner_powerfix --out workspace/reports/release.zip
```

打开 [研究看板](docs/dashboard.html)。目录职责见 [架构](docs/architecture.md)，操作见 [规范](docs/workflow.md)，结论见 [经验](docs/knowledge.md)，后续见 [路线](docs/roadmap.md)。

看板数字来自 data/experiments.jsonl 和原始报告，不手工更新。事件跨度是测量，等待与 bound 推断是低置信度估计。JSON 兼容 YAML 配置可直接编辑，不新增运行依赖。

执行测试：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`。

长期材料统一放在 `data/`：账本与状态、课程轨迹、官方原始包，以及 `releases/joint28/` 当前版本和 `releases/joint24/` 回退版本。每个版本的提交包为 `submission.zip`，原始成绩为 `local-grade.json`。轨迹只维护 `data/agent-trace/` 一份，ZIP 中的轨迹是提交时的快照。

`workspace/` 是程序自动创建的临时区：builds 构建、evaluations 评测、profiles 诊断、search 候选和缓存、reports 生成报告及试包。不进 Git；不要把它作为唯一证据来源。日常只需查看配置、文档和看板。

版本目录内的文件由程序维护：hardware.json 和 programs/ 是提交输入；local-grade.json 是官方成绩原件；audit/isolated-run 文件说明验证与评测来源；config/source/build 文件用于复现；p1/d1-profile 是算子阶段摘要。你无需手工编辑这些文件。晋升新版本会先把必要证据存入 data/releases/，再更新 best。

看板由 `./lab report` 更新，随后在本机浏览器打开 `docs/dashboard.html`；远程服务器可下载该单文件查看，或在项目根目录运行 `.venv/bin/python -m http.server 8000 --bind 127.0.0.1`，通过 SSH 端口转发访问 `http://localhost:8000/docs/dashboard.html`。看板是离线生成的快照，不会自动实时刷新。

Git 排除压缩包、.venv、workspace 和 Python 缓存。data/official-starter.zip 是原始官方工具压缩包，供打包时嵌入并核对冻结来源；本地保留，克隆仓库后需从课程官方来源补回。源码归档和提交 ZIP 同样不进入 Git，因此 Git 克隆并不包含完整历史复现材料。运行所需官方工具源码仍保留在 vendor/official。官方冻结文件及 docs/assignment 下的原始材料保留原语言；项目自有说明和注释采用中文。
