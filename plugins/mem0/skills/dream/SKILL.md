---
name: dream
description: 合并重复记忆、处理矛盾并清理陈旧内容，用于整理自托管 Mem0 项目记忆。
---

# 整理记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。两个批量删除工具默认禁用，不得为了本技能自动启用。

1. 使用 `page/page_size=20` 分页调用 `get_memories(project_id=<当前项目>)`；出现 `partial=true` 时说明本次审查不完整。
2. 优先按 `metadata.type` 分组，找出明显重复、相互矛盾和已被新决定取代的记忆；含 `[置顶]` 或 `metadata.pinned=true` 的记忆不得删除。
3. 对可能由多次更新造成的冲突调用 `get_memory_history` 核对演变，不跨项目读取历史。
4. 先向用户展示拟合并、拟更新和拟删除清单。涉及删除时必须取得明确确认。
5. 读取项目 `mem0.md` 的 `Retention`：支持 `<metadata.type>: <N>d` 和 `<metadata.type>: forever`；未配置时只把超过 90 天的 `session_summary/compact_summary` 视为过期候选。
6. 合并前从读取结果的 metadata 中剔除 `user_id`、`agent_id`、`app_id`、`mcp_owner`、`scope`、`project_id`、`source`、`run_id`，只保留其余业务字段；再调用 `update_memory(memory_id=<保留项>, project_id=<项目>, text=<合并正文>, metadata=<过滤后的 metadata>)`。过滤后为空时省略 `metadata`，不得修改身份、所有者或范围。
7. 确认后逐条调用 `delete_memory(memory_id=<旧项>, project_id=<项目>)`；不得用批量工具替代逐条审查。
8. 汇报更新、删除、跳过和失败数量，不把凭据或敏感正文写入日志。
