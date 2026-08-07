---
name: switch-project
description: 把当前工作区的 Mem0 读写范围持久切换到用户指定的自托管 project_id。
---

# 切换项目范围

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 先确认目标 `project_id` 由 1～64 位字母、数字、点、下划线或连字符组成，再使用 `search_memories(query="项目", project_id=<目标项目>, top_k=1)` 验证目标范围可访问；没有记忆不代表项目无效。
2. 用户确认后，解析本技能所在插件根目录。Windows 执行 `python <插件根>/scripts/mem0_self_hosted.py --set-project <目标项目> --cwd <当前工作目录>`，macOS/Linux 执行 `python3 <插件根>/scripts/mem0_self_hosted.py --set-project <目标项目> --cwd <当前工作目录>`。不得把项目 ID 拼入 shell 字符串；必须作为独立参数传递。
3. 告知用户映射保存在插件数据目录，不修改仓库；当前工作区后续任务的生命周期钩子和 Mem0 工具参数补全都会使用该范围。
4. 本任务后续所有 Mem0 工具调用也显式传入目标 `project_id`。
5. 用户要求恢复 Git 根目录自动识别时，使用相同系统解释器执行 `<解释器> <插件根>/scripts/mem0_self_hosted.py --clear-project --cwd <当前工作目录>`。
