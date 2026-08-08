---
name: switch-project
description: 把当前工作区的 Mem0 读写范围持久切换到用户指定的自托管 project_id。
---

# 切换项目范围

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 先确认目标 `project_id` 由 1～64 位字母、数字、点、下划线或连字符组成，再使用 `search_memories(query="项目", project_id=<目标项目>, top_k=1)` 验证目标范围可访问；没有记忆不代表项目无效。
2. 同一仓库需要跨机器或多个克隆共享记忆时，所有副本必须显式使用同一个目标 ID；不要依赖目录名或自动碰撞后缀。若启动上下文提示同名仓库冲突，先告知用户旧目录名范围可能含有混合记忆，不能自动迁移或删除；需要迁移时先用 `export` 审查旧范围，再导入明确的新范围。
3. 用户确认后，解析本技能所在插件根目录。Windows 执行 `python <插件根>/scripts/mem0_self_hosted.py --set-project <目标项目> --cwd <当前工作目录>`，macOS/Linux 执行 `python3 <插件根>/scripts/mem0_self_hosted.py --set-project <目标项目> --cwd <当前工作目录>`。不得把项目 ID 拼入 shell 字符串；必须作为独立参数传递。
4. 告知用户映射保存在插件数据目录，不修改仓库；当前工作区后续任务的生命周期钩子和 Mem0 工具参数补全都会使用该范围。
5. 本任务后续所有 Mem0 工具调用也显式传入目标 `project_id`。
6. 用户要求恢复自动识别时，使用相同系统解释器执行 `<解释器> <插件根>/scripts/mem0_self_hosted.py --clear-project --cwd <当前工作目录>`；恢复后仍沿用本机已分配的防碰撞范围，不重新占用其他仓库的旧 ID。
