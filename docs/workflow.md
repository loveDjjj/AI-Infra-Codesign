# 工作规范

在工程根目录执行 ./lab --help。使用项目启动器，避免 Python/NumPy 版本漂移。

```bash
./lab build configs/best.yaml --out workspace/builds/my-design
./lab run workspace/builds/my-design --level functional --case M2_D1 --out workspace/evaluations/check.json
./lab run workspace/builds/my-design --level both --out workspace/evaluations/local.json
./lab run workspace/builds/my-design --level full --out workspace/evaluations/local-grade.json
./lab profile workspace/builds/my-design --case M2_D1 --compare workspace/evaluations/local-grade.json --out workspace/profiles/d1-trace.json
./lab audit <full-run-id>
./lab promote <full-run-id>
./lab search configs/search.yaml
./lab report
./lab clean --dry-run
```

输出路径必须不存在，避免覆盖证据。局部 run 默认开启精确缓存；完整 grade 不读缓存，先后检查官方工具和输入哈希。功能种子进入缓存键，主机监控随 run/profile 自动启动。

账本区分 functional、estimate、both、full。局部更快不能晋升。晋升要求整包合格、报告审计与当前源码再生检查。此次 joint28 已以原审计和新工程字节再生作为证据晋升；新框架已归档 joint28，joint24 保留作回退点；未发生外部上传。

实验记录加锁追加，评估在独立子进程。search 当前只生成候选；不自动并发大规模模拟。并发构建必须使用进程，不能让继承生成器的动态替换共享线程状态。

清理仅提供具体文件预览，不删除。data/、vendor/、核心文档 不纳入清理范围。

新工程 ./lab package <record-id> --out workspace/reports/release.zip 使用白名单打包并核对原始审计、grade provenance、冻结基线和 seed 7。ZIP 含原始根目录 local-grade.json、完整代理轨迹、迭代记录、独立再生代码和官方 starter。./lab verify 验证 ZIP 哈希、CRC、原始报告和独立再生；本地打包不代表上传。无需为结构迁移重复一次昂贵整包 grade，因为提交输入字节完全一致；真正算法改变后必须再跑原公共命令。
