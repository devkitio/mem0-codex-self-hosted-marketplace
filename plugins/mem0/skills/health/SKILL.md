---
name: health
description: 诊断自托管 Mem0 的连接、令牌、工具清单和真实读写能力。
---

# 健康检查

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。认证变量是 `MEM0_MCP_TOKEN`，不是 `MEM0_API_KEY`。

依次检查：

1. `MEM0_MCP_TOKEN` 是否存在，只报告“已设置/未设置”，绝不打印值。
2. MCP 是否暴露运行时约定中的六个工具。
3. 调用 `search_memories(query="项目", project_id=<当前项目>, top_k=1)` 验证读取。
4. 写入带随机标识的临时记忆：`add_memory(text=<探针>, project_id=<项目>, infer=false)`。
5. 搜索并取得该探针 ID，再调用 `delete_memory` 清理。
6. 分别报告配置、连接、读取、写入和清理状态；清理失败必须明确提醒。
