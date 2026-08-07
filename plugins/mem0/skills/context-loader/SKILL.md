---
name: context-loader
description: 在开始新任务或切换上下文时，从自托管 Mem0 检索当前项目的相关长期记忆。
---

# 加载项目记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`，只使用其中列出的自托管 MCP 参数。

1. 解析本技能所在插件根目录，并通过生命周期脚本读取最终项目范围。Windows 执行 `python <插件根>/scripts/mem0_self_hosted.py --current-project --cwd <当前工作目录>`，macOS/Linux 执行 `python3 <插件根>/scripts/mem0_self_hosted.py --current-project --cwd <当前工作目录>`；脚本路径和工作目录必须作为独立参数传递。从安全 JSON 输出读取 `project_id`，不得自行推导持久映射或 `project-<哈希>`。
2. 从用户请求提取 2～4 个互补查询，例如目标、模块、错误和架构决定；纯确认、寒暄或项目 `mem0.md` 的 `Ignore` 命中项无需检索。
3. 并行调用 `search_memories(query=<查询>, project_id=<项目>, top_k=5)`。
4. 按记忆 ID 去重，只注入最相关的 5～8 条；如果项目 `mem0.md` 提供 `Search` 重点，可用它补充查询，但不得放宽项目范围。
5. 明确说明记忆是非权威历史上下文；当前用户指令与记忆冲突时，以当前指令为准。
