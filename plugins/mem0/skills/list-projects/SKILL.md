---
name: list-projects
description: 根据自托管 Mem0 可返回的记忆，列出可见项目和记忆数量。
---

# 列出项目

最高优先级：先阅读 `../../SELF_HOSTED_RUNTIME.md`。

1. 调用 `list_entities(entity_type="project")`，不要传 `project_id`，以免只返回当前项目。
2. 展示服务端返回的项目 ID、受管记忆数量和最近更新时间；按最近更新时间降序排列，缺失时间时再按项目 ID 排序。
3. 如果返回 `partial=true`，明确说明扫描达到容量上限，项目列表可能不完整。
4. 需要查看某项目的运行时，再调用 `list_entities(entity_type="run", project_id=<项目>)`。
5. 明确说明项目与运行是从固定用户和所有者的受管记忆推导，不是官方云端实体目录；不得根据正文猜测项目名，也不得暴露固定用户身份。
