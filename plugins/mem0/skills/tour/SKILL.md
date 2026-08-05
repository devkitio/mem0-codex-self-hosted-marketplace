---
name: tour
description: 浏览自托管 Mem0 当前项目的全部可见记忆，并按正文分类展示。
---

# 浏览记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 调用 `get_memories(project_id=<当前项目>, limit=1000)`。
2. 优先按 `[置顶]`、`[决定]`、`[偏好]`、`[约定]`、`[经验]` 等正文前缀分组，其余归入“未分类”。
3. 每组默认展示 ID 和完整正文；数量较多时先展示目录与计数，再按用户选择展开。
4. 如果用户同时给出查询，额外调用 `search_memories(query=<查询>, project_id=<项目>, top_k=20)` 并把相关结果置前。
