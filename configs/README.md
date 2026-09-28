# 配置约定

`baseline.yaml` 是固定基线；`best.yaml` 由晋升流程生成；`pipeline.yaml` 是当前自动搜索入口。`toolchain.yaml` 固定官方评估环境，`search-toolchain.yaml` 和 `search-requirements.lock` 固定可选搜索环境。

当前可执行的人工探索目标才放入 `targets/`，正在编辑的设计才放入 `trials/`。目标必须通过源码 epoch、基线记录、变量域和案例校验。结束的目标与评估结果进入 `data/decisions.jsonl` 和 `data/experiments.jsonl`，不长期保留旧批次 YAML。

新文件采用 `领域-算子-机制.yaml`，例如 `p1-w2-input-reuse.yaml`。模型、日期、`next`、`final` 和版本号属于记录字段或程序生成的批次 ID，不写进配置文件名。历史记录 ID、课程案例名和原生代理轨迹文件名保持不变。
