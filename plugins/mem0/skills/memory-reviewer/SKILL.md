---
name: memory-reviewer
description: 审查自托管 Mem0 中重复、矛盾、陈旧和低质量的项目记忆。
---

# 审查记忆质量

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 使用 `page/page_size=20` 分页调用 `get_memories(project_id=<当前项目>)`，最多审查 200 条；出现 `partial=true` 时明确说明结果不完整。
2. 优先按 `metadata.type` 分组，再检查：同组正文关键词重合超过约 60% 的近义重复；同一主题的相反结论；`confidence < 0.5` 的低置信记忆；缺少 `metadata.type` 的孤立记忆；超过 90 天的 `session_summary/compact_summary`。
3. 将发现分为“建议合并”“建议更新”“建议删除”“无需处理”，附记忆 ID 和简短理由。
4. 本技能只审查，不自动修改或删除；用户要求执行时再使用 `update_memory` 或 `delete_memory`，删除前确认。含 `[置顶]` 或 `metadata.pinned=true` 的记忆不得建议自动删除。
