# 自托管运行时约定

本插件运行时只调用自托管 MCP，并使用 `MEM0_MCP_TOKEN` 认证。所有运行型技能必须遵守以下工具签名：

- `search_memories(query, project_id, top_k, threshold?)`
- `get_memories(project_id?, limit?)`
- `add_memory(text, project_id?, infer?)`
- `get_memory(memory_id, project_id?)`
- `update_memory(memory_id, project_id?, text?)`
- `delete_memory(memory_id, project_id?)`

项目默认范围是当前 Git 根目录名。不得向这些工具传入 `user_id`、`app_id`、`filters`、`metadata`、`rerank`、`page_size` 或 `source`。需要分类时，把简短分类前缀写入记忆正文。自托管服务没有批量删除、元数据更新或原生置顶接口；相关技能必须通过逐条操作或正文标记实现。
