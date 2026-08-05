---
name: forget
description: 按查询或记忆 ID 删除自托管 Mem0 记忆，并在删除前要求确认。
---

# 忘记记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 如果用户给出 ID，调用 `get_memory(memory_id=<ID>, project_id=<项目>)`；否则调用 `search_memories(query=<查询>, project_id=<项目>, top_k=10)`。
2. 展示候选 ID 和简短预览，不显示无关敏感内容。
3. 删除属于不可恢复操作，必须由用户明确确认具体 ID；用户在同一请求中已明确指定删除该 ID 时可视为已确认。
4. 逐条调用 `delete_memory(memory_id=<ID>, project_id=<项目>)`。
5. 汇报成功和失败的 ID。
6. 只有用户明确要求清空整个当前项目或其中一个运行、且管理员已显式启用批量工具时，才可调用 `delete_all_memories`：先省略 `confirmation_token` 获取预览，展示范围、数量和截止时间，取得明确确认后原样提交令牌。不得把查询结果转换成未确认的批量删除。
