---
name: health
description: 诊断自托管 Mem0 的连接、令牌、工具清单和真实读写能力。
---

# 健康检查

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。认证变量是 `MEM0_MCP_TOKEN`，不是 `MEM0_API_KEY`。

依次检查：

1. `MEM0_MCP_TOKEN` 是否存在，只报告“已设置/未设置”，绝不打印值。
2. 运行 `scripts/mem0_self_hosted.py --check`，确认服务端暴露运行时约定中的 10 个工具，并核对参数、必填项、类型、默认值、枚举和 `ToolAnnotations` 与 `mcp-schema.snapshot.json` 一致。普通 Codex 会话默认看不到两个批量工具，这是 `.mcp.json` 的预期禁用结果，不是故障。
3. 调用 `search_memories(query="项目", project_id=<当前项目>, top_k=1)` 验证读取。
4. 写入带随机标识的临时记忆：`add_memory(text=<探针>, project_id=<项目>, infer=false)`。
5. 搜索并取得该探针 ID，再调用 `get_memory_history` 验证历史接口，最后调用 `delete_memory` 清理。
6. 分别报告配置、连接、10 工具清单、契约快照、读取、写入、历史和清理状态；清理失败或契约漂移必须明确提醒。不要为了健康检查启用或调用批量删除工具。

用户要求“深度检查”或 `--deep` 时，在标准检查后调用 `get_memories(project_id=<当前项目>, page_size=200)`，只读分析同一 `metadata.type` 内的近义重复、超过 90 天的 `session_summary/compact_summary`、`confidence < 0.5`、相反结论和缺少 `metadata.type` 的孤立记忆。报告数量和短 ID，不自动更新或删除；有问题时建议运行 `memory-reviewer` 或 `dream`。
