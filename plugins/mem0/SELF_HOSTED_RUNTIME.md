# 自托管运行时约定

本插件运行时只调用 `https://mem0-api.jiang.in/mcp`，并且只使用 `MEM0_MCP_TOKEN` 认证。不得请求 `api.mem0.ai`、`mcp.mem0.ai`，也不得要求 `MEM0_API_KEY`。

## 工具签名

- `add_memory(text?, messages?, project_id?, infer?, metadata?, run_id?, expiration_date?)`
- `search_memories(query, project_id?, top_k?, threshold?, filters?, rerank?, explain?, show_expired?)`
- `get_memories(project_id?, limit?, page?, page_size?, filters?, sort_by?, sort_order?, show_expired?)`
- `get_memory(memory_id, project_id?)`
- `update_memory(memory_id, project_id?, text?, metadata?, expiration_date?)`
- `delete_memory(memory_id, project_id?)`
- `get_memory_history(memory_id, project_id?)`
- `list_entities(entity_type?, project_id?)`
- `delete_all_memories(project_id, run_id?, confirmation_token?)`
- `delete_entities(entity_type, entity_id, project_id?, confirmation_token?)`

`text` 与 `messages` 必须且只能提供一个。`get_memories.limit` 仅用于兼容旧调用；新调用使用 `page/page_size`，单页最多 200 条，并处理结构化返回中的 `results/count/next/previous/partial`。出现 `partial=true` 时必须明确说明结果可能不完整。

## 身份与项目边界

- 服务端固定 `user_id=codex-primary` 和 `mcp_owner=codex-primary-adapter`。不得传入或尝试覆盖 `user_id`、`agent_id`、`app_id`、`mcp_owner`、`scope`、`source`。
- 项目默认范围是当前 Git 根目录名；`switch-project` 技能可在插件数据目录中保存当前工作区的持久映射。
- 项目搜索和列表可以返回当前项目及全局记忆；精确读取、历史、更新和单条删除必须匹配传入的项目范围。
- `metadata` 和 `filters` 只用于非保留业务字段，并受服务端的大小、深度、字段和操作符限制。不要在客户端拼入身份过滤条件。
- `list_entities` 只枚举从受管记忆推导出的 `project` 和 `run`。列出全部项目时不要传 `project_id`；列出运行时传入所属项目。

生命周期脚本只在语义明确时补齐项目范围：普通读写、历史和 `delete_all_memories` 补当前项目；`delete_entities(entity_type="run")` 补当前项目；`list_entities` 与项目实体删除不自动补项目，避免把跨项目枚举或目标项目错误限缩。

## 生命周期设置与项目策略

生命周期设置的覆盖顺序固定为：内置默认值、项目 `mem0.md`、插件数据目录中的 `settings.json`、环境变量。支持的键及默认值如下：

- `auto_save=true`
- `auto_search=true`
- `search_limit=5`，有效范围 1～20
- `confidence_threshold=0.25`，有效范围 0～1
- `rerank=true`；自动检索默认使用服务端真实 rerank
- `debug=false`；只记录决策代码和错误类型，不记录提示、记忆正文或路径
- `session_retention_days=90`，有效范围 0～3650；0 表示不设置 `expiration_date`，非零值按服务端要求写为 `YYYY-MM-DD`

环境变量使用对应的 `MEM0_AUTO_SAVE`、`MEM0_AUTO_SEARCH`、`MEM0_SEARCH_LIMIT`、`MEM0_CONFIDENCE_THRESHOLD`、`MEM0_RERANK`、`MEM0_DEBUG` 和 `MEM0_SESSION_RETENTION_DAYS`。可通过 `scripts/mem0_self_hosted.py --init-settings` 创建默认本机设置，或用 `--show-settings [--cwd 路径]` 查看合并后的有效值。

`mem0.md` 原生识别 `## Settings`、`## Search`、`## Ignore`、`## Identity`、`## Categories` 和 `## Retention`。规则只是当前项目的本地策略，不得改变服务端固定用户、所有者和项目隔离：

- `Search` 为复杂提示补充检索重点。
- `Ignore` 通过不区分大小写的文本匹配跳过自动检索；不支持执行代码或正则表达式。
- `Identity` 只在任务启动和自动总结提示中作为当前项目约定使用。
- `Categories` 只指导 `infer=true` 的自动总结分类，不伪装成官方云端类别目录。
- `Retention` 可按 `metadata.type` 设置分类保留期，例如 `session_summary: 90d`、`compact_summary: 90d`、`decision: forever`；旧的 `days`、`retention_days` 和 `retention_session_days` 继续作为会话总结保留期别名。`exclude`/`ignore` 可排除不应自动保存的内容。

复杂提示最多产生四个确定性查询，客户端并发请求后按记忆 ID 去重；单个查询失败允许使用其他查询的结果。纯确认、寒暄、命中 `Ignore` 的提示不会触发检索。自动总结只有在包含决定、目标、完成事项、验证、风险、待办、文件触达等长期价值信号时才写入；使用 `messages=[{"role":"assistant",...}]` 防止把模型观点误归因给用户，并写入 `type/confidence/session_id/branch/files_touched` 等非保留 metadata。`files_touched` 必须是项目内相对路径，`Stop`、`PreCompact` 与压缩后 `SessionStart` 继续使用摘要哈希和正文哈希去重。

仓库中的 `mcp-schema.snapshot.json` 固定 10 个工具的参数、必填项、类型、默认值、枚举和四项 `ToolAnnotations`。`scripts/mem0_self_hosted.py --check` 必须实时比较生产 `tools/list`；差异只报告工具或字段名，不输出 schema 正文、令牌或记忆内容。

## 破坏性操作

- `update_memory` 与 `delete_memory` 继续要求用户确认具体目标。
- `delete_all_memories` 与 `delete_entities` 在 `.mcp.json` 中默认禁用。只有用户明确启用并要求按项目或运行删除时才可使用。
- 两个批量工具必须先省略 `confirmation_token` 获取预览，再向用户展示范围、数量和截止时间；取得明确确认后，原样提交 5 分钟 HMAC 令牌执行。
- 令牌过期、篡改、范围变化或重复使用时不得重试执行，应重新预览并再次确认。禁止用户级或全局清空。

自动导入继续使用 `[mem0:auto-import]` 正文标记，并且只删除本地状态中记录为插件生成的旧分块。
