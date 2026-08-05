---
name: stats
description: 显示自托管 Mem0 当前项目的记忆数量、时间分布和查询延迟。
---

# 记忆统计

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 调用 `get_memories(project_id=<当前项目>, limit=1000)`。
2. 统计总数、最早与最近时间、带 `[置顶]` 前缀数量，以及常见中文分类前缀。
3. 测量一次 `search_memories(query="项目", project_id=<项目>, top_k=1)` 的端到端耗时。
4. 仅基于实际返回字段统计；缺少时间或分类字段时显示“服务未提供”，不得推测。
