---
name: dream
description: 合并重复记忆、处理矛盾并清理陈旧内容，用于整理自托管 Mem0 项目记忆。
---

# 整理记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`，不得使用云端过滤器或批量删除工具。

1. 调用 `get_memories(project_id=<当前项目>, limit=200)`。
2. 找出明显重复、相互矛盾和已被新决定取代的记忆；含 `[置顶]` 的记忆不得删除。
3. 先向用户展示拟合并、拟更新和拟删除清单。涉及删除时必须取得明确确认。
4. 合并时用 `update_memory(memory_id=<保留项>, project_id=<项目>, text=<合并正文>)`。
5. 确认后逐条调用 `delete_memory(memory_id=<旧项>, project_id=<项目>)`。
6. 汇报更新、删除、跳过和失败数量，不把凭据或敏感正文写入日志。
