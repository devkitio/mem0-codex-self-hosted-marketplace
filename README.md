# Mem0 Codex 自托管应用市场

这是一个可直接通过 Git 安装的 Codex 插件市场。它把自托管 Mem0 MCP、生命周期钩子和 16 个记忆技能打包为 `mem0@mem0-self-hosted`。

当前市场连接：

- MCP：`https://mem0-api.jiang.in/mcp`
- 认证环境变量：`MEM0_MCP_TOKEN`
- 插件版本：以 `plugins/mem0/.codex-plugin/plugin.json` 为准

仓库不会保存任何 Mem0 令牌或用户记忆。

## 当前能力

| 层级 | 已提供能力 |
| --- | --- |
| MCP | 10 个工具，覆盖新增、搜索、分页读取、详情、更新、单条删除、历史、实体枚举和两阶段批量管理 |
| 生命周期钩子 | `PreToolUse`、`SessionStart`、`UserPromptSubmit`、`PostToolUse`、`Stop`、`PreCompact` 六类事件 |
| 技能 | 16 个自托管技能，覆盖初始化、健康检查、记住、查看、置顶、遗忘、整理、导入导出、项目切换和统计 |
| 项目策略 | 原生解析 `mem0.md` 的 `Settings/Search/Ignore/Identity/Categories/Retention` 六个区段 |
| 自动记忆 | 智能多查询检索、真实 rerank、质量门禁、90 天默认保留、分类保留、压缩后摘要和跨事件去重 |
| 安全 | 固定用户与所有者、强制项目边界、敏感信息脱敏、项目内相对路径、批量删除默认关闭 |

旧版 6 个 MCP 工具保持参数兼容；当前插件和未升级的旧客户端仍可继续使用基础读写能力。

## 与官方 Mem0 插件的关系

本仓库复用了官方插件的交互理念和 SDK 参考资料，但运行时是面向自托管服务的独立实现，不会连接 Mem0 官方云端。

| 对比项 | 官方插件 | 本仓库 |
| --- | --- | --- |
| 服务地址 | Mem0 官方 API/MCP | 固定为仓库配置的自托管 MCP |
| 认证 | `MEM0_API_KEY` 等云端凭据 | 只使用 `MEM0_MCP_TOKEN` |
| 工具数量 | 官方 9 个主要记忆工具 | 官方语义对应工具加 `get_memory_history`，共 10 个 |
| 身份范围 | 云端账号、用户与应用语义 | 服务端固定用户和所有者，客户端只选择项目或运行范围 |
| 实体目录 | 官方云端实体目录 | 从当前 Adapter 管理的记忆推导项目和运行实体 |
| 搜索与列表 | 由官方云端控制 | 项目查询包含当前项目与全局记忆，精确读取和修改必须匹配项目 |
| 批量删除 | 按官方服务策略执行 | 默认禁用，只允许项目或运行范围的“预览 → 确认令牌 → 执行” |
| 生命周期 | 官方脚本请求云端 API | 本地钩子直接调用同一自托管 MCP，并增加契约漂移检查 |

因此，本仓库已经尽量补齐官方常用能力，但不会模拟官方云端的多租户账号、计费、托管实体目录或后台控制台。

## 安装

### 1. 准备环境

安装 Python 3.10 或更高版本，并确认：

- Windows 可执行 `python --version`
- macOS/Linux 可执行 `python3 --version`

把自托管服务令牌设置为 `MEM0_MCP_TOKEN`。Windows PowerShell 可持久写入当前用户环境：

```powershell
[Environment]::SetEnvironmentVariable("MEM0_MCP_TOKEN", "你的令牌", "User")
```

macOS/Linux 可加入 shell 配置：

```bash
export MEM0_MCP_TOKEN="你的令牌"
```

设置后必须完全退出并重新打开 Codex，使新进程能够读取该变量。不要把真实令牌写入仓库、截图或日志。

### 2. 添加 Git 市场并安装插件

```bash
codex plugin marketplace add devkitio/mem0-codex-self-hosted-marketplace --ref main
codex plugin add mem0@mem0-self-hosted
```

也可以使用完整 Git URL：

```bash
codex plugin marketplace add https://github.com/devkitio/mem0-codex-self-hosted-marketplace.git --ref main
codex plugin add mem0@mem0-self-hosted
```

### 3. 信任钩子

1. 重启 Codex 并新建一个任务。
2. 打开 `/hooks`。
3. 逐项查看标记为“新建”或“已修改”的 `Mem0 自托管版` 钩子，使用信任操作确认内容。
4. 在插件目录中确认 `Mem0 自托管版` 已启用。

Codex 不会自动信任第三方钩子；插件更新并改变钩子内容后也需要重新审核。未完成审核时，MCP 技能仍可使用，但自动加载和自动总结不会运行。

### 4. 验证连接

在新的 Codex 任务中运行 `$mem0:health`。该检查会核对令牌、10 个工具、生产契约以及最小读写链路，不会输出令牌或完整记忆正文；用户明确要求“深度检查”时，还会只读审查重复、陈旧和低置信度记忆。

## 钩子行为

| 钩子 | 触发时机 | Mem0 行为 |
| --- | --- | --- |
| `PreToolUse` | 读取、编辑或调用 Mem0 工具前 | 按工具语义补齐 `project_id`；为 `add_memory` 补充受控 metadata；保护托管记忆文件，并按文件路径检索历史 |
| `SessionStart` | 启动、恢复或压缩后 | 检索项目目标、决定、待办和偏好；压缩后提取并保存真实 `isCompactSummary` 摘要 |
| `UserPromptSubmit` | 每次提交提示 | 跳过纯确认和忽略项；复杂请求并发执行 2～4 个互补查询并去重 |
| `PostToolUse` | Mem0 或命令工具结束后 | 记录会话统计；检测命令错误并检索历史解决记录 |
| `Stop` | 每轮助手输出结束 | 通过质量门禁后提取长期记忆，并按保留策略设置过期时间 |
| `PreCompact` | 手动或自动压缩前 | 从最近转录保存压缩前总结 |

`SessionStart` 首次启动时还会扫描 `CLAUDE.md`、`AGENTS.md`、`.cursorrules`、`.windsurfrules` 和 `mem0.md`，按标题分块后以 `infer=false` 导入；本地 SHA-256 状态会跳过未变化内容，文件更新成功后只删除插件此前生成的旧分块。

自动总结读取最近 12 条用户/助手消息，最多处理 50,000 字符，同时记录分支、触达文件和会话内 Mem0 操作计数。写入使用 `messages` 与 `infer=true`，模型生成的正文始终标记为 `assistant`，避免误记为用户观点；metadata 包含 `type`、`confidence`、`session_id`、分支和项目内相对文件路径。写入前会清除常见系统标签并脱敏令牌、密码和认证头；短消息、寒暄和空内容会被跳过，`Stop` 与 `PreCompact` 的相同正文也不会重复保存。

## 本地设置与 `mem0.md`

插件默认启用自动检索和自动保存。设置按“内置默认值 → 项目 `mem0.md` → 本机 `settings.json` → 环境变量”的顺序覆盖，仓库不能覆盖用户的本机选择。本机设置文件位于 `~/.codex/plugin-data/mem0-self-hosted/settings.json`；也可通过 `PLUGIN_DATA` 改变数据目录。

```json
{
  "auto_save": true,
  "auto_search": true,
  "search_limit": 5,
  "confidence_threshold": 0.25,
  "rerank": true,
  "debug": false,
  "session_retention_days": 90
}
```

对应环境变量为 `MEM0_AUTO_SAVE`、`MEM0_AUTO_SEARCH`、`MEM0_SEARCH_LIMIT`、`MEM0_CONFIDENCE_THRESHOLD`、`MEM0_RERANK`、`MEM0_DEBUG` 和 `MEM0_SESSION_RETENTION_DAYS`。`search_limit` 会限制在 1～20，阈值限制在 0～1，保留天数限制在 0～3650；保留天数为 0 时不写入过期时间，非零值按服务端要求写为 `YYYY-MM-DD`。

可用插件脚本初始化或查看本机设置，不需要手工创建 JSON：

```powershell
python plugins\mem0\scripts\mem0_self_hosted.py --init-settings
python plugins\mem0\scripts\mem0_self_hosted.py --show-settings --cwd D:\你的项目
```

项目根目录的 `mem0.md` 可使用六个二级标题。未知标题和字段会被安全忽略，解析失败会回退到默认行为：

```markdown
## Settings
- auto_search: true
- auto_save: true
- search_limit: 5
- confidence_threshold: 0.25
- rerank: true

## Search
- 架构决定和安全边界
- 最近完成事项与回归测试

## Ignore
- node_modules
- 临时生成文件

## Identity
- 本项目是面向 Windows 的桌面应用

## Categories
- 决定：长期架构选择
- 经验：可复用的问题解决方法

## Retention
- session_summary: 90d
- compact_summary: 90d
- decision: forever
- exclude: 临时日志
```

`Search` 会补充检索重点，`Ignore` 使用不区分大小写的文本匹配来跳过自动检索，`Identity` 在任务启动时作为当前项目约定注入，`Categories` 指导自动总结分类，`Retention` 可按 `metadata.type` 设置 `90d`、`forever` 等分类保留策略和排除项。旧的 `days`、`retention_days` 与 `retention_session_days` 仍作为会话总结保留期别名。`mem0.md` 仍会按原有 SHA-256 机制作为项目资料导入，因此旧行为保持兼容。

如果 `[features] hooks = false`，生命周期钩子不会运行。`codex_hooks` 仍可兼容，但已经是旧别名；推荐使用 `hooks = true`。

## 技能

插件包含上下文加载、记住、查看、忘记、置顶、整理、导入、导出、项目切换、统计、巡览和健康检查等 16 个技能。运行时固定服务端用户与所有者字段；技能只传受控的项目、运行、metadata 和 filters，不传云端专用的 `user_id`、`agent_id` 或 `app_id`。

常用入口：

| 技能 | 用途 |
| --- | --- |
| `$mem0:health` | 检查连接、令牌、工具契约和真实读写能力 |
| `$mem0:onboard` | 初始化当前项目并可选导入项目资料 |
| `$mem0:remember` | 保存明确的决定、偏好、约定或经验 |
| `$mem0:peek` / `$mem0:tour` | 快速查询或浏览当前项目记忆 |
| `$mem0:pin` / `$mem0:forget` | 置顶关键记忆或在确认后删除单条记忆 |
| `$mem0:memory-reviewer` / `$mem0:dream` | 审查重复、矛盾和陈旧内容并进行整理 |
| `$mem0:switch-project` | 将当前工作区持久映射到指定 `project_id` |

`switch-project` 技能会把工作区到 `project_id` 的映射保存在插件数据目录中，因此切换结果可跨任务生效且不会修改仓库；也可以随时恢复为 Git 根目录名自动识别。

自托管服务现提供 10 个工具：旧 6 个工具保持兼容，并增加 `get_memory_history`、`list_entities`、`delete_all_memories` 和 `delete_entities`。同时支持受限 metadata/filters、分页、过期时间和真实 rerank。项目与运行实体从受管记忆推导，不等同于官方云端实体目录；搜索会分别检索项目与全局范围后按分数合并。

两个批量工具在插件配置中默认禁用。需要使用时必须由用户明确启用，并遵循“预览 → 明确确认 → 5 分钟 HMAC 令牌执行”的流程；不支持用户级或全局清空。

## 更新

```bash
codex plugin marketplace upgrade mem0-self-hosted
codex plugin add mem0@mem0-self-hosted
```

更新后重启 Codex。钩子内容发生变化时，需要在 `/hooks` 中重新审核。

## 卸载

```bash
codex plugin remove mem0@mem0-self-hosted
codex plugin marketplace remove mem0-self-hosted
```

卸载插件不会删除自托管服务中已经保存的记忆。

## 使用其他自托管地址

Fork 本仓库并修改 [`plugins/mem0/.mcp.json`](plugins/mem0/.mcp.json) 中的 `url`。生命周期脚本会读取同一个文件，因此不需要再修改钩子代码。修改后应提升插件版本，避免 Codex 继续使用旧缓存。

## 本地开发与验证

```powershell
python scripts/validate_repo.py
python -m unittest discover -s tests -v
codex plugin marketplace add D:\code\mem0-codex-self-hosted-marketplace
codex plugin add mem0@mem0-self-hosted
```

真实连通性检查需要先设置 `MEM0_MCP_TOKEN`：

```powershell
python plugins\mem0\scripts\mem0_self_hosted.py --check
```

该命令不仅核对 10 个工具，还会把生产 `tools/list` 的参数、必填项、类型、默认值、枚举和四项 `ToolAnnotations` 与仓库快照比较；发现漂移时返回失败，但不会输出令牌或记忆正文。

当前实现还通过以下验收：

- 仓库结构、市场配置、六类钩子和 16 个技能校验。
- 20 项生命周期脚本单元测试。
- 生产 `messages + infer=true`、metadata、到期日和 rerank 探针。
- `update_memory` metadata 合并、置顶取消和单条清理探针。
- 生产 10 工具契约与 `mcp-schema.snapshot.json` 一致性检查。

## 许可证与来源

本仓库采用 Apache-2.0 许可证。SDK 参考材料来源于 Mem0 官方插件，详见 [`NOTICE.md`](NOTICE.md)。
