---
name: import
description: 从 Mem0 Markdown 导出文件或 MEMORY.md 向自托管 Mem0 导入项目记忆。
---

# 导入记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 读取用户指定的 Markdown 文件。对 `format: mem0-self-hosted-export-v1` 文件，只解析 `## 记忆 <序号>` 下的单个 `json` 围栏对象并要求 `text` 为字符串；格式或 JSON 无效的条目记为失败，不猜测或拼接。其他 `MEMORY.md` 才按普通 Markdown 条目解析。
2. 展示目标 `project_id`、条目数和前 3 条预览，批量写入前取得确认。
3. 导入 v1 导出条目时，从 metadata 再次剔除 `user_id`、`agent_id`、`app_id`、`mcp_owner`、`scope`、`project_id`、`source`、`run_id`，然后调用 `add_memory(text=<正文>, project_id=<目标项目>, infer=false, metadata=<过滤后非空对象>, run_id=<合法顶层 run_id>, expiration_date=<合法值>)`；空的可选字段必须省略。导出 ID、时间戳、所有者、范围、来源、得分和类别只作参考，不得传入。
4. 普通 `MEMORY.md` 条目默认使用 `infer=false`；只有用户明确要求由 Mem0 提炼时才使用 `infer=true`。v1 导出条目始终使用 `infer=false`，保证正文不被改写。
5. 逐条记录成功和失败，不因单项失败中断全部导入。
6. 汇报总数、成功数和失败项；不得把任何保留 metadata、导出源 `project_id` 或导出 ID 传给服务端。
