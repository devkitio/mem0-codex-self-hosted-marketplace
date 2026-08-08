---
name: export
description: 将当前项目的自托管 Mem0 记忆导出为可迁移的 Markdown 文件。
---

# 导出记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 确定当前 `project_id`，从 `page=1, page_size=20` 开始分页调用 `get_memories(project_id=<项目>, sort_by="created_at", sort_order="asc")`，按 `next` 继续。
2. 输出 UTF-8 `mem0-self-hosted-export-v1` Markdown。文件头固定记录 `format`、`exported_at`、`project_id`、`count` 和 `partial`；每条记忆使用独立的 `## 记忆 <序号>` 标题和一个 `json` 围栏代码块，块内是单个 JSON 对象，字段固定为 `id`、`created_at`、`updated_at`、`run_id`、`expiration_date`、`metadata`、`text`。JSON 字符串必须正确转义，不能用手工分隔正文。
3. 导出 metadata 前剔除 `user_id`、`agent_id`、`app_id`、`mcp_owner`、`scope`、`project_id`、`source`、`run_id`；顶层 `run_id` 和 `expiration_date` 单独保留。缺失字段写为 `null`，metadata 缺失时写 `{}`。
4. 默认文件名为 `mem0-export-<project_id>-<YYYYMMDD>.md`，写入用户指定位置；未指定时写入当前目录。
5. 不写入令牌、服务端地址或本机敏感路径。任一页出现 `partial=true` 时，文件头的 `partial` 必须为 `true`，结果中明确标记“部分导出”，不得声称备份完整。
6. 返回实际文件绝对路径和导出数量。
