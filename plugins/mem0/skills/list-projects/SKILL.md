---
name: list-projects
description: 根据自托管 Mem0 可返回的记忆，列出可见项目和记忆数量。
---

# 列出项目

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 调用 `get_memories(limit=1000)`，不要传 `filters`。
2. 如果结果包含 `project_id`，按其分组并统计数量与最近更新时间。
3. 如果服务只返回当前默认范围且不暴露 `project_id`，明确说明服务端不支持全局项目枚举，并至少展示当前 Git 根目录推导出的项目 ID。
4. 不根据记忆正文猜测项目名，也不把 `app_id` 当作自托管项目字段。
