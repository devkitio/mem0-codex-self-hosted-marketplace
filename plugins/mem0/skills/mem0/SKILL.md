---
name: mem0
description: Mem0 Python 与 TypeScript SDK 参考，用于编写应用集成代码；插件自身运行时使用自托管 MCP。
license: Apache-2.0
metadata:
  author: mem0ai
  version: "0.1.1-selfhosted"
  category: ai-memory
  tags: "memory, self-hosted, mcp, python, typescript"
compatibility: 插件运行时只需 MEM0_MCP_TOKEN；SDK 示例的依赖和认证方式以所选部署模式为准。
---

# Mem0 SDK 参考

## 运行时边界

本插件自身不通过 Mem0 官方云端 API 运行。执行记忆读写时，必须先阅读 `../../SELF_HOSTED_RUNTIME.md`，使用插件提供的自托管 MCP 工具和 `MEM0_MCP_TOKEN`。

`client/` 与 `references/` 下的文件是上游 Python、TypeScript、REST 和框架集成参考，可能包含 `MEM0_API_KEY`、`api.mem0.ai` 或云端 v3 参数。这些内容仅用于用户明确要求编写上游 SDK/REST 集成代码时，不能据此改变本插件的 MCP 地址、认证变量或工具参数。

## 路由

- Python 客户端：读取 `client/python.md`。
- TypeScript 客户端：读取 `client/node.md`。
- 两端差异：读取 `client/differences.md`。
- 快速入门、架构、功能、API 与集成模式：按需读取 `references/` 中对应文档。
- 自托管插件的记忆操作：使用本插件其他技能，不直接请求官方 URL。
