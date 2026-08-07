---
name: onboard
description: 为当前项目完成自托管 Mem0 初始检查、连接验证和可选项目资料导入。
---

# 初始化自托管 Mem0

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。本插件地址已固定在 `.mcp.json`，认证只使用 `MEM0_MCP_TOKEN`。

1. 确认 `MEM0_MCP_TOKEN` 已设置，但不得显示令牌值。
2. 运行健康检查技能，验证连接以及真实读写和清理。
3. 解析本技能所在插件根目录。Windows 执行 `python <插件根>/scripts/mem0_self_hosted.py --current-project --cwd <当前工作目录>`，macOS/Linux 执行 `python3 <插件根>/scripts/mem0_self_hosted.py --current-project --cwd <当前工作目录>`；从安全 JSON 输出读取 `project_id` 后搜索已有项目记忆，不得自行推导持久映射或哈希标识。
4. 检查插件钩子是否已在 Codex `/hooks` 中审核并信任；已信任时，新的启动任务会自动导入不超过 100 KB 的 `CLAUDE.md`、`AGENTS.md`、`.cursorrules`、`.windsurfrules` 和 `mem0.md`，并通过 SHA-256 跳过未变化内容。文件变得过大时不会上传，并会按远端精确标记清理旧导入。
5. 如果项目存在 `mem0.md`，核对 `Settings`、`Search`、`Ignore`、`Identity`、`Categories` 和 `Retention` 六个二级标题；如果用户需要全局覆盖，说明插件数据目录 `settings.json` 和对应 `MEM0_*` 环境变量的更高优先级。不得把本地设置或令牌写回项目仓库。
6. 对 `README.md`、`MEMORY.md` 或用户指定的其他资料，只提出适合长期保存的候选摘要；获得确认后再调用 `add_memory(text=<摘要>, project_id=<项目>, infer=true)`。
7. 汇报 MCP 连通性、服务端 10 工具、契约快照、当前 `project_id`、六类钩子状态、自动导入范围和当前自动检索/自动保存开关。说明项目/运行实体由受管记忆推导，两个批量工具默认禁用；不要为了初始化启用它们。
