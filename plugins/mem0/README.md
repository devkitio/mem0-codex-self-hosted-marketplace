# Mem0 自托管版插件

本目录是市场中的 `mem0` 插件包，包含自托管 MCP、10 个服务端工具、六类 Codex 生命周期钩子和 16 个 Mem0 技能，支持 Windows、Linux 与 macOS。

## 运行配置

- MCP 地址：`https://mem0-api.jiang.in/mcp`
- 认证变量：`MEM0_SELF_HOSTED_API_KEY`
- Python：3.10 或更高版本；Windows 命令为 `python`，macOS/Linux 命令为 `python3`
- 项目范围：默认使用当前 Git 仓库根目录名作为 `project_id`；只允许 1～64 位字母、数字、点、下划线或连字符，不符合规则的目录名会生成稳定的 `project-<哈希>` 标识

脚本从插件自己的 `.mcp.json` 读取地址，不依赖用户级 `config.toml`，也不会请求官方 `api.mem0.ai`。

## MCP 工具

基础工具为 `add_memory`、`search_memories`、`get_memories`、`get_memory`、`update_memory` 和 `delete_memory`。扩展工具为 `get_memory_history`、`list_entities`、`delete_all_memories` 和 `delete_entities`。

服务端支持受限 `messages`、`metadata`、`filters`、`run_id`、过期时间、rerank、分页和历史记录。`list_entities(show_expired=false)` 只返回从受管记忆推导出的项目与运行，不是官方云端实体目录。两个批量删除工具采用“预览 → 用户确认 → 5 分钟 HMAC 令牌执行”，持久化执行进度并对同一令牌幂等收敛，同时在 `.mcp.json` 中默认禁用。

## 生命周期钩子

| 事件 | 行为 |
| --- | --- |
| `PreToolUse` | 补齐 Mem0 工具的项目范围；为 `add_memory` 补充 `type/confidence/codex_origin/session_id`；保护托管记忆文件、读取文件前检索相关历史 |
| `SessionStart` | 加载当前项目的关键长期记忆；压缩后提取并保存真实 `isCompactSummary` 摘要 |
| `UserPromptSubmit` | 跳过纯确认和忽略项；复杂请求按 2～4 个互补查询检索、去重后注入上下文 |
| `PostToolUse` | 记录 Mem0 操作统计，并在命令失败时检索历史解决记录 |
| `Stop` | 通过质量门禁后从最近对话提取长期记忆，并应用保留期限 |
| `PreCompact` | 在上下文压缩前保存最近对话中的长期信息 |

首次启动还会自动导入 `CLAUDE.md`、`AGENTS.md`、`.cursorrules`、`.windsurfrules` 和 `mem0.md`。每个文件最多读取 100 KB；文件超过限制时不会上传，并会按远端精确标记清理此前由插件导入的旧版本。导入状态保存在插件数据目录，通过项目、来源文件与 SHA-256 跳过未变化内容；短章节会在分块上限内合并，写入前先记录待完成状态。项目资料更新时，上一份有效版本会保留到新分块序号完整覆盖、写入验证和旧分块逐 ID 清理全部成功；失败或中断后会在下次启动续跑。项目资料被清空、删除或变得过大时，只删除远端搜索结果中项目、来源、哈希和格式标记均精确匹配的旧分块；本地状态里的未确认 ID 不会直接触发删除。不同项目和项目映射的并发状态写入会合并保存。

`mem0.md` 还会原生解析 `Settings`、`Search`、`Ignore`、`Identity`、`Categories` 和 `Retention` 六个二级标题。本机 `settings.json` 位于插件数据目录，支持 `auto_save`、`auto_search`、`search_limit`、`confidence_threshold`、`rerank`、`debug` 和 `session_retention_days`；会话总结默认保留 90 天，`Retention` 可按 `metadata.type` 覆盖为天数或永久保留。覆盖顺序为内置默认值、项目 `mem0.md`、本机设置、环境变量；详细格式见仓库根目录 `README.md`。

自动检索对复杂请求最多并发四次查询，默认请求服务端真实 rerank，按记忆 ID 去重并只注入 `search_limit` 条。自动保存会跳过纯确认、短对话和没有长期价值信号的内容；保存的会话总结使用 `assistant` 角色的 `messages`，包含受控 metadata，并在保留天数大于 0 时写入 `YYYY-MM-DD` 格式的 `expiration_date`。摘要去重按 `project_id` 隔离，相同正文不会在同一项目的 `Stop` 与 `PreCompact` 之间重复保存，触达文件只保留项目内相对路径。

初始化或查看本机设置：

```powershell
python scripts\mem0_self_hosted.py --init-settings
python scripts\mem0_self_hosted.py --show-settings --cwd "D:\你的项目"
python scripts\mem0_self_hosted.py --current-project --cwd "D:\你的项目"
```

```bash
python3 scripts/mem0_self_hosted.py --init-settings
python3 scripts/mem0_self_hosted.py --show-settings --cwd "/你的项目"
python3 scripts/mem0_self_hosted.py --current-project --cwd "/你的项目"
```

插件钩子首次安装或内容发生变化后，需要在 Codex `/hooks` 页面中审核并信任。

## 安全边界

- `MEM0_SELF_HOSTED_API_KEY` 必须是自部署 Mem0 生成的 MCP 用途 Key；MCP 只通过 `/auth/introspect` 校验，不会把它转发给记忆或管理员接口，禁止提交到仓库。
- 工具调用只使用服务器挂载的内部服务 Secret；客户端无法读取或覆盖该 Secret。
- 远程 MCP 必须使用 HTTPS，仅回环地址允许 HTTP；认证不会跟随跨源重定向。
- 钩子标准输入限制为 4 MB，MCP 响应限制为 2 MB。
- 钩子日志只记录错误类型，不记录令牌和记忆正文。
- 错误检索只发送脱敏后的错误签名，不发送完整命令输出。
- 文件历史检索只发送项目内相对路径，不发送文件内容；`.env` 和项目外文件会被跳过。
- 记忆按 `project_id` 隔离；当前用户指令始终高于历史记忆。
- 服务端固定用户与所有者字段；调用者不能通过 metadata 或 filters 覆盖身份和项目边界。
- 批量删除只允许项目或运行范围，禁止全局用户清空；令牌过期、篡改或范围变化会失败，同一有效令牌只恢复未完成操作或返回既有结果。
- SDK 参考资料可能包含上游云端示例，但不参与插件运行时请求。
- `mcp-schema.snapshot.json` 只保存工具契约元数据；`--check` 会与生产 `tools/list` 比较，不保存令牌或记忆正文。
