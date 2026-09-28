# 初始探索目标

Sol-medium 长期批次采用四个目标、45个名义候选，实际按硬件和ASM去重。目标固定已审计来源与源码epoch；源码变化后必须重新生成目标，不能直接沿用旧epoch。后续AI追加目标进入运行状态，事实仍写实验账本。模型固定方式见 scripts/codex-analyst/codex；正式配置见 configs/pipeline-astra-long.yaml。

文件名astra为已启动批次的稳定ID，实际模型为gpt-6-sol/medium；禁止因更名修改正在运行的目标身份。
