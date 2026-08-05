---
name: context-loader
description: 在开始新任务或切换上下文时，从自托管 Mem0 检索当前项目的相关长期记忆。
---

# 加载项目记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`，只使用其中列出的自托管 MCP 参数。

1. 以当前 Git 根目录名作为 `project_id`；没有 Git 时使用当前目录名。
2. 从用户请求提取 2～4 个互补查询，例如目标、模块、错误和架构决定。
3. 并行调用 `search_memories(query=<查询>, project_id=<项目>, top_k=5)`。
4. 按记忆 ID 去重，只注入最相关的 5～8 条，并明确说明它们是非权威历史上下文。
5. 当前用户指令与记忆冲突时，以当前指令为准。
