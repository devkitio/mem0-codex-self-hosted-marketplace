# Mem0 自托管版插件

本目录是市场中的 `mem0` 插件包，包含自托管 MCP、Codex 生命周期钩子和 16 个 Mem0 技能。

## 运行配置

- MCP 地址：`https://mem0-api.jiang.in/mcp`
- 认证变量：`MEM0_MCP_TOKEN`
- Python：3.10 或更高版本；Windows 命令为 `python`，macOS/Linux 命令为 `python3`
- 项目范围：默认使用当前 Git 仓库根目录名作为 `project_id`

脚本从插件自己的 `.mcp.json` 读取地址，不依赖用户级 `config.toml`，也不会请求官方 `api.mem0.ai`。

## 生命周期钩子

| 事件 | 行为 |
| --- | --- |
| `SessionStart` | 加载当前项目的关键长期记忆 |
| `UserPromptSubmit` | 按当前问题检索相关记忆并注入上下文 |
| `Stop` | 从最近一轮对话中自动提取长期记忆 |
| `PreCompact` | 在上下文压缩前保存最近对话中的长期信息 |

插件钩子首次安装或内容发生变化后，需要在 Codex `/hooks` 页面中审核并信任。

## 安全边界

- 令牌只从进程环境读取，禁止提交到仓库。
- 钩子日志只记录错误类型，不记录令牌和记忆正文。
- 记忆按 `project_id` 隔离；当前用户指令始终高于历史记忆。
- SDK 参考资料可能包含上游云端示例，但不参与插件运行时请求。
