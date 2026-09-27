# 项目结构与职责

配置 → build → evaluation → records → report，是唯一主线。

- configs/: baseline 冻结官方产物；best 由晋升生成；trials 是活跃设计；search 是候选变量与数量预算；toolchain 锁定解释器和官方路径。
- src/codesign_lab/config.py: 路径、配置和官方完整性检查。
- build.py: 每案独立生成器、硬件合法化、输出哈希、算子源指令范围；两案源码只保留当前实现。
- codegen/p1/: prompt 和 P1 decode 的生成器；codegen/d1/: decode 的持久化、online attention、lazy preload 与 split W2 依赖闭包。保留实际继承，未把不同算法误合并。
- evaluation/: runner 做功能/性能分阶段检查；cache 绑定真实输入与引擎；official 独立执行原始公共 grade；trace 获取事件；profile 整理事实和推算；monitor 采集主机进程树。
- search/: space 展开变量与去重；prune 只做可证明约束；runner 输出有预算的候选；solver 可选 MILP 评估预算选择子问题，当前环境未安装后端。
- records.py: 加锁写轻账本；report.py: 单一事实来源生成看板与 AI 可读 profile-summary；release.py: 证据保全、审计、晋升、白名单打包、ZIP 验证与清理预览。
- data/: 唯一长期材料目录，包含实验账本、状态、课程轨迹、官方原始包和 releases/joint28、joint24 两个版本。
- vendor/official: 独立保存的冻结官方工具副本，不修改。
- workspace/: 生成产物、缓存和详细 trace；不进 Git。晋升版本的必要证据保存到 data/releases/，不依赖临时产物。

当前迁移选择先保留 9 个生成器源码文件。这些文件是算法依赖闭包，不是多个历史版本。下一次拆分算子时仍必须验证三份提交文件字节一致。

配置使用 JSON 兼容 YAML（合法 YAML），因此锁定环境不新增解析依赖。未来安装 PyYAML 后可使用普通 YAML 写法。
