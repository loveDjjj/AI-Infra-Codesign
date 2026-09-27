# 代理工作规则

使用 ./lab 和锁定的 Python 环境。vendor/official 为独立冻结副本，禁止修改。
实验事实写入 data/experiments.jsonl，解释与经验写入 docs/knowledge.md。
资源利用率不能直接证明因果等待；推算和假设必须明确标注。
data/agent-trace 是唯一维护的完整课程轨迹。保护 data/releases 中的当前与回退证据。
仅清理 workspace，先列出具体路径与原因，保全实验事实后再执行。
结构修改先确认硬件和两份 ASM 逐字节再生，再做算法修改。
项目自有文档、源码注释和文档字符串使用中文；标识符、ISA、原始评分报告、官方冻结文件和历史会话保持原样。
