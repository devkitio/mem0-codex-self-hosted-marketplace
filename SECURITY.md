# 安全说明

## 凭据

只通过 `MEM0_SELF_HOSTED_API_KEY` 环境变量提供自部署 Mem0 生成且 `purpose=mcp` 的 API Key。MCP 只通过 `/auth/introspect` 校验该 Key，不会把它转发给记忆或管理员接口；后续调用使用服务器挂载的内部服务 Secret。管理员用途 Key 会被 MCP 拒绝，MCP 用途 Key 会被 Mem0 管理员接口拒绝。不要把任何 Key 写入 `.mcp.json`、提交记录、Issue、截图或日志。

## 钩子审查

安装或更新后，请在 Codex `/hooks` 中逐项检查命令和脚本内容，再决定是否信任。插件不会尝试绕过 Codex 的钩子信任机制。

自动导入会把项目根目录中的 `CLAUDE.md`、`AGENTS.md`、`.cursorrules`、`.windsurfrules` 和 `mem0.md` 发送到配置的自托管服务。不要在这些文件中保存凭据；如不希望导入，请在信任钩子前关闭或修改 `SessionStart` 钩子。

命令错误回忆只发送脱敏后的错误签名，文件历史检索只发送项目内相对路径。插件不会把完整命令输出或文件正文用于错误/文件路径检索。

## 报告问题

报告安全问题时请提供不含令牌、记忆正文和本机敏感路径的最小复现。公开仓库建立后，优先使用 GitHub 私密安全报告，不要在公开 Issue 中披露凭据。
