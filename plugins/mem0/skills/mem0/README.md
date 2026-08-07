# Mem0 SDK 参考技能

本目录保留 Mem0 上游 Python、TypeScript、REST 和框架集成资料，供用户明确要求编写应用集成代码时查阅。

## 与本插件运行时的边界

- Codex 插件运行时连接仓库配置的自托管 MCP，只读取自部署 Mem0 生成且用途为 MCP 的 `MEM0_SELF_HOSTED_API_KEY`。
- 本目录中的上游示例可能使用 Mem0 Platform、官方云端地址或云端凭据变量；这些示例不参与插件安装、认证或生命周期钩子。
- 不得根据 SDK 示例修改插件的 `.mcp.json`、固定身份、项目隔离或批量删除安全边界。
- 自托管插件的日常记忆操作应使用同插件中的 `remember`、`peek`、`tour`、`forget`、`health` 等技能。

## 资料索引

- `client/python.md`：Python 客户端
- `client/node.md`：TypeScript 客户端
- `client/differences.md`：两端差异
- `references/quickstart.md`：快速入门
- `references/sdk-guide.md`：SDK 方法
- `references/api-reference.md`：REST 接口与过滤器
- `references/architecture.md`：处理流程、作用域与性能
- `references/features.md`：检索、图记忆、分类和多模态
- `references/integration-patterns.md`：常见框架集成
- `references/use-cases.md`：应用模式

资料来源与许可证见本目录的 `LICENSE` 以及仓库根目录的 `NOTICE.md`。
