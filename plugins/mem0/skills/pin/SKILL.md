---
name: pin
description: 通过正文标记在自托管 Mem0 中置顶或取消置顶关键记忆。
---

# 置顶记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。自托管 MCP 没有元数据置顶接口，统一使用正文前缀 `[置顶]`。

1. 按 ID 调用 `get_memory`，或先用 `search_memories` 找到候选。
2. 置顶时在正文开头添加一次 `[置顶] `；已有标记则不重复添加。
3. 取消置顶时只移除开头的 `[置顶] `。
4. 调用 `update_memory(memory_id=<ID>, project_id=<项目>, text=<新正文>)`。
5. 不得尝试传入 `metadata` 或 `source`。
