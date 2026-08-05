---
name: export
description: 将当前项目的自托管 Mem0 记忆导出为可迁移的 Markdown 文件。
---

# 导出记忆

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 确定当前 `project_id`，调用 `get_memories(project_id=<项目>, limit=1000)`。
2. 按返回顺序输出 UTF-8 Markdown，每条包含 ID、创建或更新时间（如有）和完整正文。
3. 默认文件名为 `mem0-export-<project_id>-<YYYYMMDD>.md`，写入用户指定位置；未指定时写入当前目录。
4. 文件开头记录导出时间、项目 ID 和条目数，不写入令牌、服务端地址或本机敏感路径。
5. 返回实际文件绝对路径和导出数量。
