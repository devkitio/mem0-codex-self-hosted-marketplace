---
name: peek
description: 快速按查询或 ID 查看自托管 Mem0 中的项目记忆。
---

# 查看记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

- 输入像记忆 ID 时，调用 `get_memory(memory_id=<ID>, project_id=<当前项目>)`。
- 其他输入调用 `search_memories(query=<输入>, project_id=<当前项目>, top_k=10)`。
- 默认紧凑展示 ID、相关度（如有）和一行正文；用户要求详情时才展示完整内容。
- 没有结果时如实说明，不自动改用其他项目或全局范围。
