---
name: import
description: 从 Mem0 Markdown 导出文件或 MEMORY.md 向自托管 Mem0 导入项目记忆。
---

# 导入记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 读取用户指定的 Markdown 文件，解析条目正文；忽略导出标题和空项。
2. 展示目标 `project_id`、条目数和前 3 条预览，批量写入前取得确认。
3. 对需要原样保存的条目调用 `add_memory(text=<正文>, project_id=<项目>, infer=false)`。
4. 如果用户明确要求由 Mem0 提炼，再使用 `infer=true`。
5. 逐条记录成功和失败，不因单项失败中断全部导入。
6. 汇报总数、成功数和失败项；不得传入 `metadata`、`user_id` 或 `app_id`。
