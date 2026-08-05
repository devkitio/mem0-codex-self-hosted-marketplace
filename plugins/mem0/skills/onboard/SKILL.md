---
name: onboard
description: 为当前项目完成自托管 Mem0 初始检查、连接验证和可选项目资料导入。
---

# 初始化自托管 Mem0

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。本插件地址已固定在 `.mcp.json`，认证只使用 `MEM0_MCP_TOKEN`。

1. 确认 `MEM0_MCP_TOKEN` 已设置，但不得显示令牌值。
2. 运行健康检查技能，验证连接以及真实读写和清理。
3. 以当前 Git 根目录名作为 `project_id`，搜索已有项目记忆，避免重复导入。
4. 检查 `AGENTS.md`、`README.md`、`MEMORY.md` 等项目资料，只提出适合长期保存的候选摘要。
5. 获得用户确认后，使用 `add_memory(text=<摘要>, project_id=<项目>, infer=true)` 导入。
6. 告知用户插件钩子需要在 Codex `/hooks` 中审核并信任后才会自动运行。
