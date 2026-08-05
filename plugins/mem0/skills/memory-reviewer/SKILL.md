---
name: memory-reviewer
description: 审查自托管 Mem0 中重复、矛盾、陈旧和低质量的项目记忆。
---

# 审查记忆质量

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 调用 `get_memories(project_id=<当前项目>, limit=200)`。
2. 检查近义重复、同一主题的冲突结论、缺少上下文的片段和明显过期状态。
3. 将发现分为“建议合并”“建议更新”“建议删除”“无需处理”，附记忆 ID 和简短理由。
4. 本技能只审查，不自动修改或删除；用户要求执行时再使用 `update_memory` 或 `delete_memory`，删除前确认。
