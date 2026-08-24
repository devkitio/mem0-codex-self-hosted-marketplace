# 自托管运行时约定

本插件运行时只调用 `https://mem0-api.jiang.in/mcp`，并且只使用 `MEM0_SELF_HOSTED_API_KEY` 认证。该凭据必须由当前自部署 Mem0 控制台生成且用途为 MCP；MCP 只通过 Mem0 `/auth/introspect` 校验用途和吊销状态，不得把客户端 Key 转发给记忆或管理员接口。工具调用只使用服务器挂载的内部服务 Secret。不得请求 `api.mem0.ai`、`mcp.mem0.ai`，也不得使用官方云端 `MEM0_API_KEY`。

钩子客户端兼容 Streamable HTTP 的 `application/json` 与 `text/event-stream` 响应，并按 JSON-RPC 请求 ID 忽略 SSE 中先到达的通知或其他消息。远程 MCP 必须使用 HTTPS，仅回环地址允许 HTTP；认证只跟随同源重定向。只读工具使用 15 秒请求超时，写工具使用 50 秒请求超时，外层钩子超时必须更长。单次 MCP 响应最多读取 2 MB，钩子标准输入最多读取 4 MB，服务端错误正文不得透传。钩子兜底日志只记录异常类型或脱敏后的本地诊断，不记录令牌、用户目录、IP 地址或记忆内容。

## 系统兼容性

- Windows 钩子使用 `commandWindows` 和 `python`；Linux、macOS 钩子使用 `command` 和 `python3`。
- 插件数据默认写入 `Path.home()/.codex/plugin-data/mem0-self-hosted`，也可由 `PLUGIN_DATA` 指定；状态文件统一使用 UTF-8、独占文件锁和同目录原子替换。
- 工作区状态键遵循当前系统的路径大小写语义：Windows 归一化大小写，Linux 和 macOS 保留 POSIX 路径大小写；Windows 盘符与 UNC、Linux `/home`、macOS `/Users` 路径均按字符串形态保护和脱敏。
- 仓库 CI 使用 Python 3.10 在 Ubuntu、Windows 和 macOS 上分别运行结构校验与完整插件单元测试，并在 Linux 使用 Python 3.12 验证 MCP Adapter 和真实 ASGI Bearer 鉴权链。

## 工具签名

- `add_memory(text?, messages?, project_id?, infer?, metadata?, run_id?, expiration_date?, write_mode?)`
- `search_memories(query, project_id?, top_k?, threshold?, filters?, rerank?, explain?, show_expired?)`
- `get_memories(project_id?, limit?, page?, page_size?, filters?, sort_by?, sort_order?, show_expired?)`
- `get_memory(memory_id, project_id?)`
- `update_memory(memory_id, project_id?, text?, metadata?, expiration_date?)`
- `delete_memory(memory_id, project_id?)`
- `get_memory_history(memory_id, project_id?)`
- `list_entities(entity_type?, project_id?, show_expired?)`
- `resolve_project_scope(repository_fingerprint)`
- `delete_all_memories(project_id, run_id?, confirmation_token?)`
- `delete_entities(entity_type, entity_id, project_id?, confirmation_token?)`
- `list_memory_candidates(project_id?, status?, limit?)`
- `review_memory_candidate(candidate_id, action, project_id?, text?, reason?)`
- `submit_memory_feedback(memory_id, verdict, project_id?, reason?, retrieval_id?)`

`text` 与 `messages` 必须且只能提供一个。`write_mode` 默认为兼容旧调用和用户主动保存的 `direct`；生命周期自动写入必须使用 `risk_assessed`，由服务端决定直接晋升、进入候选或跳过。候选只能在当前 MCP 项目范围内读取和审核；`edit` 必须提供新正文并重新执行敏感信息、重复和冲突检查。反馈由服务端绑定当前 `payload_version` 与认证主体。`get_memories.limit` 仅用于兼容旧调用；新调用使用 `page/page_size`，单页最多 20 条，并处理结构化返回中的 `results/count/next/previous/partial`。出现 `partial=true` 时必须明确说明结果可能不完整。`list_entities` 默认排除过期记忆，只有显式传入 `show_expired=true` 才会计入。

## 身份与项目边界

- 服务端通过部署配置固定 `user_id` 和 `mcp_owner`，客户端不可获知或覆盖具体值。不得传入或尝试覆盖 `user_id`、`agent_id`、`app_id`、`mcp_owner`、`scope`、`source`。
- 项目标识只允许 1～64 位字母、数字、点、下划线或连字符。自动范围优先使用当前 Git 根目录名；目录名不符合规则时生成 `project-<哈希>` 标识。同一台机器检测到同名但远端身份不同的仓库时，第一个范围保持不变，后续仓库使用带身份哈希的隔离 ID，并在 `SessionStart` 提示旧范围不会自动迁移。
- `SessionStart` 默认为没有本机旧范围的新 Git 仓库自动解析跨机器范围。客户端规范化 Git 远端并只发送 64 位小写 SHA-256 指纹；MCP 从认证上下文读取稳定 `subject`，使用独立长期 `MCP_PROJECT_SCOPE_SECRET` 执行 HMAC-SHA256，并只返回合法的私有 `project_id`。不得访问或转发访问令牌正文。
- 同一 Mem0 用户的不同 MCP Key、不同机器和同一远端的 SSH/HTTPS 表示会得到相同范围。派生结果按当前连接凭据的不可逆指纹隔离缓存到本机 `server_project_scopes.json`；原始 Key、仓库地址和 `project_id` 都不写入业务仓库或插件仓库。缓存命中时不得重复请求范围解析；Key 变化时必须重新解析，不能复用其他凭据的缓存。
- 已存在本机自动范围的旧仓库不得静默切换或迁移，启动时返回 `migration_required=true` 并提示用户通过 `switch-project` 确认。解析失败时本次会话只保留本机读取，暂停自动导入、会话总结和全部 Mem0 变更工具，后续启动重试，避免写入稍后会失去可见性的临时范围。无 Git 远端时使用完整的本机范围；可由本机设置或 `MEM0_AUTO_SYNC_PROJECT=false` 关闭自动解析并明确恢复可写本机范围，项目 `mem0.md` 不得控制该安全选项。
- 解析优先级固定为本机显式映射、服务端同步范围、本机自动范围。`--sync-project` 用于旧范围迁移确认或强制刷新；`--clear-project` 同时清除当前工作区的显式映射和所有凭据下对应的远端同步缓存，但不删除任何记忆。
- 项目搜索和列表可以返回当前项目及全局记忆；精确读取、历史、更新和单条删除必须匹配传入的项目范围。
- `metadata` 和 `filters` 只用于非保留业务字段，并受服务端的大小、深度、字段和操作符限制。保留 metadata 字段为 `user_id`、`agent_id`、`app_id`、`mcp_owner`、`scope`、`project_id`、`source`、`run_id`；从读取结果再次写入前必须全部剔除。顶层 `run_id` 仍可在 `add_memory` 中使用。不要在客户端拼入身份过滤条件。
- `list_entities` 只枚举从受管记忆推导出的 `project` 和 `run`。列出全部项目时不要传 `project_id`；列出运行时传入所属项目。

生命周期脚本只在语义明确时补齐项目范围：普通读写、历史、候选审核、反馈和 `delete_all_memories` 补当前项目；`delete_entities(entity_type="run")` 补当前项目；`list_entities`、`resolve_project_scope` 与项目实体删除不自动补项目，避免把跨项目枚举、范围解析或目标项目错误限缩。

## 生命周期设置与项目策略

生命周期设置的覆盖顺序固定为：内置默认值、项目 `mem0.md`、插件数据目录中的 `settings.json`、环境变量。支持的键及默认值如下：

- `auto_save=true`
- `auto_search=true`
- `auto_sync_project=true`；仅本机设置和环境变量可覆盖，项目 `mem0.md` 中的同名项无效
- `search_limit=5`，有效范围 1～20
- `confidence_threshold=0.25`，有效范围 0～1
- `rerank=true`；自动检索默认使用服务端真实 rerank
- `debug=false`；只记录决策代码和错误类型，不记录提示、记忆正文或路径
- `session_retention_days=90`，有效范围 0～3650；0 表示不设置 `expiration_date`，非零值按服务端要求写为 `YYYY-MM-DD`

环境变量使用对应的 `MEM0_AUTO_SAVE`、`MEM0_AUTO_SEARCH`、`MEM0_AUTO_SYNC_PROJECT`、`MEM0_SEARCH_LIMIT`、`MEM0_CONFIDENCE_THRESHOLD`、`MEM0_RERANK`、`MEM0_DEBUG` 和 `MEM0_SESSION_RETENTION_DAYS`。可通过 `scripts/mem0_self_hosted.py --init-settings` 创建默认本机设置，用 `--show-settings [--cwd 路径]` 查看合并后的有效值，用 `--current-project [--cwd 路径]` 读取最终 `project_id`、来源、同步状态与迁移状态，或用 `--sync-project [--cwd 路径]` 确认旧范围迁移或强制刷新私有跨机器范围。

`mem0.md` 原生识别 `## Settings`、`## Search`、`## Ignore`、`## Identity`、`## Categories` 和 `## Retention`。规则只是当前项目的本地策略，不得改变服务端固定用户、所有者和项目隔离：

- `Search` 为复杂提示补充检索重点。
- `Ignore` 通过不区分大小写的文本匹配跳过自动检索；不支持执行代码或正则表达式。
- `Identity` 只在任务启动和自动总结提示中作为当前项目约定使用。
- `Categories` 只指导 `infer=true` 的自动总结分类，不伪装成官方云端类别目录。
- `Retention` 可按 `metadata.type` 设置分类保留期，例如 `session_summary: 90d`、`compact_summary: 90d`、`decision: forever`；旧的 `days`、`retention_days` 和 `retention_session_days` 继续作为会话总结保留期别名。`exclude`/`ignore` 可排除不应自动保存的内容。

复杂提示最多产生四个确定性查询，客户端并发请求后按记忆 ID 去重；单个查询失败允许使用其他查询的结果。纯确认、寒暄、命中 `Ignore` 的提示不会触发检索。自动总结只有在包含决定、目标、完成事项、验证、风险、待办、文件触达等长期价值信号时才提交风险评估；使用 `messages=[{"role":"assistant",...}]` 防止把模型观点误归因给用户，并写入 `type/confidence/session_id/source_type/branch/files_touched` 等非保留 metadata。高置信、来源完整且无冲突的明确用户陈述或工具验证结果可以直接晋升；助手推断、截断来源、中等置信、重复或冲突内容进入候选；低置信、寒暄、过程描述和无长期价值内容跳过。`PreCompact` 不得绕过门禁。模型返回的 `linked_memory_ids` 只能引用本次真实检索映射。`files_touched` 必须是项目内相对路径，普通文本和 JSON 形式的凭据必须先脱敏；会话计数和摘要去重状态必须在文件锁内合并更新，摘要哈希与正文哈希去重键必须按 `project_id` 隔离，`Stop`、`PreCompact` 与压缩后 `SessionStart` 只在同一项目内去重。

仓库中的 `mcp-schema.snapshot.json` 固定 14 个工具的参数、必填项、类型、默认值、枚举和四项 `ToolAnnotations`。`scripts/mem0_self_hosted.py --check` 必须实时比较生产 `tools/list`；差异只报告工具或字段名，不输出 schema 正文、令牌或记忆内容。

## 破坏性操作

- `update_memory` 与 `delete_memory` 继续要求用户确认具体目标。
- `delete_all_memories` 与 `delete_entities` 在 `.mcp.json` 中默认禁用。只有用户明确启用并要求按项目或运行删除时才可使用。
- 两个批量工具必须先省略 `confirmation_token` 获取预览，再向用户展示范围、数量和截止时间；取得明确确认后，原样提交 5 分钟 HMAC 令牌执行。
- 令牌过期、篡改或范围变化时不得执行，应重新预览并再次确认。同一有效令牌的重复请求只接管未完成操作或返回持久化结果，不重复删除已经完成的目标；客户端仍不得在结果明确成功后主动重放。禁止用户级或全局清空。

自动导入继续使用 `[mem0:auto-import]` 正文标记，每个候选文件最多读取 100 KB；超限文件不上传，并按与删除文件相同的精确标记流程清理旧导入。检索结果必须同时精确匹配项目、来源文件、内容哈希和导入格式，不同来源文件或项目不得仅因内容哈希相同而互相跳过。短 Markdown 章节必须合并到受限分块中，验证时要求 `分块：i/n` 完整覆盖，重复序号不能代替缺失分块。新增分块前必须持久化 `pending` 状态；项目资料更新时，待处理状态必须保留上一份有效版本的哈希、格式和必要的记忆 ID，新分块写入并验证成功前不得删除旧版本。旧分块清理进度必须逐 ID 持久化；本地状态 ID 只用于恢复进度，只有精确搜索返回且正文标记完整匹配的 ID 才允许调用删除。搜索未返回或标记已变化的状态 ID 不调用删除，并从本地清理队列安全收敛。旧分块清理失败时允许新旧版本暂时共存，但不得重复写入新分块；后续启动必须继续清理并在全部成功后提交新状态。项目资料被清空、删除或变得过大时，只删除精确正文标记确认由插件生成的旧分块。所有项目范围共用状态文件时，写入必须在共享锁内重新读取并仅合并当前范围，禁止覆盖其他项目状态；文件与远端分块均未变化时不得重写状态文件。

`export` 技能使用 `mem0-self-hosted-export-v1` Markdown 格式：文件头记录导出时间、源项目、数量和 `partial`，每条记忆使用一个只含 JSON 的围栏代码块，字段固定为 `id`、`created_at`、`updated_at`、`run_id`、`expiration_date`、`metadata` 和 `text`。`metadata` 只能包含非保留字段。`import` 恢复正文、合法的顶层 `run_id`、`expiration_date` 和过滤后的 metadata；服务端生成的 ID、时间戳、所有者、范围、来源、得分和类别不恢复。
