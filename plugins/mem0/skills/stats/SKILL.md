---
name: stats
description: 显示自托管 Mem0 当前项目的记忆数量、时间分布和查询延迟。
---

# 记忆统计

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 从 `page=1, page_size=20` 开始分页调用 `get_memories(project_id=<当前项目>, sort_by="updated_at", sort_order="desc")`，按 `next` 继续，直到没有下一页。
2. 统计 `count`、实际读取数、最早与最近时间、过期状态和 run 分布。分类优先使用结果的 `categories[0]`，其次使用 `metadata.type`，最后回退到 `[决定]`、`[偏好]`、`[约定]`、`[经验]` 等正文前缀；置顶同时识别 `metadata.pinned=true` 和 `[置顶]`。
3. 任一页返回 `partial=true` 时停止声称全量，报告扫描上限和实际读取数。
4. 测量一次 `search_memories(query="项目", project_id=<项目>, top_k=1)` 的端到端耗时。
5. 仅基于实际返回字段统计；缺少时间、run 或分类字段时显示“服务未提供”，不得推测。
