---
name: pin
description: 通过 metadata 和兼容正文标记在自托管 Mem0 中置顶或取消置顶关键记忆。
---

# 置顶记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。自托管 MCP 支持非保留 metadata 更新，同时保留正文前缀兼容旧版本。

1. 按 ID 调用 `get_memory`，或先用 `search_memories` 找到候选。
2. 调用 `get_memory` 读取原正文和 metadata。置顶时在正文开头添加一次 `[置顶] `，并把原 metadata 合并为 `pinned=true`；已有标记不得重复添加。
3. 取消置顶时只移除开头的 `[置顶] `，并在可取得原 metadata 时合并为 `pinned=false`。
4. 从读取结果的 metadata 中剔除 `user_id`、`agent_id`、`app_id`、`mcp_owner`、`scope`、`project_id`、`source`、`run_id`，再合并 `pinned` 并调用 `update_memory(memory_id=<ID>, project_id=<项目>, text=<新正文>, metadata=<过滤后的 metadata>)`。如果服务未返回 metadata，只更新正文，避免覆盖未知字段。
5. 过滤后的 metadata 为空且无需写入 `pinned` 时省略 `metadata`；任何保留字段都不得回传。
