---
name: remember
description: 将用户明确要求记住的决定、偏好、约定或经验保存到自托管 Mem0。
---

# 记住内容

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 仅保存用户明确要求记住的内容，不扩写敏感凭据。
2. 按内容选择 `metadata.type`：架构或取舍用 `decision`，失败方法用 `anti_pattern`，稳定偏好用 `user_preference`，编码约定用 `convention`，环境配置用 `environmental`，其他经验用 `task_learning`。
3. 为兼容旧记忆，在正文前保留 `[决定]`、`[偏好]`、`[约定]` 或 `[经验]` 等中文前缀；同时调用 `add_memory(text=<正文>, project_id=<当前项目>, metadata={"type": <分类>, "confidence": 1.0, "codex_origin": "remember"}, infer=false)`。
4. 如果用户明确要求自动提炼多段材料，才使用 `infer=true`。
5. 返回成功状态和记忆 ID（服务提供时），不得声称异步事件一定已完成，也不得传入保留的身份或 `source` 字段。
