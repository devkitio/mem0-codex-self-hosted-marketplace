---
name: switch-project
description: 切换当前工作区的 Mem0 范围，并处理自动派生的私有跨机器 project_id 或旧范围迁移。
---

# 切换项目范围

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 先运行当前范围命令。Windows 执行 `python <插件根>/scripts/mem0_self_hosted.py --current-project --cwd <当前工作目录>`，macOS/Linux 执行 `python3 <插件根>/scripts/mem0_self_hosted.py --current-project --cwd <当前工作目录>`。读取 `project_id`、`source`、`sync_available`、`synchronized` 和 `migration_required`，不得自行推导项目 ID。
2. 没有本机旧范围的新 Git 仓库会在 `SessionStart` 自动获取私有跨机器范围；缓存命中后不再请求。同步只发送规范化远端的 `repository_fingerprint`，结果按当前连接凭据的不可逆指纹隔离缓存在本机 `server_project_scopes.json`，不会修改或新增仓库文件。不同 API Key 只要属于同一 Mem0 用户就会得到相同范围，但每个新 Key 首次使用都必须重新向服务端解析。
3. 如果 `migration_required=true`，说明旧本机范围可能已有记忆。先向用户说明切换后旧记忆不会自动迁移或删除，取得明确确认后再执行 `--sync-project`。如果当前 `source` 是“本机显式映射”，先确认放弃该覆盖并执行 `--clear-project`，再执行 `--sync-project`；否则保留显式映射。Windows 使用 `python`，macOS/Linux 使用 `python3`，项目路径必须作为独立参数传递。
4. 自动或手动同步后读取 `server_project_id`、最终 `project_id` 和 `source`。最终来源必须是“服务端同步范围”；再使用 `search_memories(query="项目", project_id=<最终项目>, top_k=1)` 验证目标范围可访问。没有记忆不代表项目无效。`--sync-project` 也可用于强制刷新当前 Key 的范围缓存。
5. 同一 Mem0 用户可在其他 Windows、Linux 或 macOS 机器克隆同一 Git 远端，首次启动即得到相同范围。不得复制、公开或提交命令返回的私有 `project_id`、缓存中的凭据指纹或任何 Key。
6. 普通本机切换继续使用 `--set-project <目标项目>`，只写入本机插件数据目录且优先级最高。若旧范围已有记忆，不能自动迁移或删除；需要迁移时先用 `export` 审查旧范围，再导入明确的新范围。本任务后续所有 Mem0 工具调用显式传入最终 `project_id`。
7. 用户要求恢复自动识别时，执行 `<解释器> <插件根>/scripts/mem0_self_hosted.py --clear-project --cwd <当前工作目录>`。该命令同时清除当前工作区的显式映射和所有凭据下对应 Git 远端的同步缓存，不删除任何远端记忆；下次 `SessionStart` 会重新自动解析。用户要求只使用本机范围时，在本机 `settings.json` 设置 `auto_sync_project=false` 或配置 `MEM0_AUTO_SYNC_PROJECT=false`。
