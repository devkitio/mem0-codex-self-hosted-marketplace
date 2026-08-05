# Mem0 Codex 自托管应用市场

这是一个可直接通过 Git 安装的 Codex 插件市场。它把自托管 Mem0 MCP、生命周期钩子和 16 个记忆技能打包为 `mem0@mem0-self-hosted`。

当前市场连接：

- MCP：`https://mem0-api.jiang.in/mcp`
- 认证环境变量：`MEM0_MCP_TOKEN`
- 插件版本：`0.2.13-selfhosted.3`

仓库不会保存任何 Mem0 令牌或用户记忆。

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
3. 审核并信任 `Mem0 自托管版` 的四个钩子。
4. 在插件目录中确认 `Mem0 自托管版` 已启用。

Codex 不会自动信任第三方钩子；未完成审核时，MCP 技能仍可使用，但自动加载和自动总结不会运行。

## 钩子行为

| 钩子 | 触发时机 | Mem0 行为 |
| --- | --- | --- |
| `SessionStart` | 启动、恢复或压缩后 | 检索项目目标、决定、待办和偏好 |
| `UserPromptSubmit` | 每次提交提示 | 按当前提示进行语义检索 |
| `Stop` | 每轮助手输出结束 | 自动提取并写入可跨会话复用的长期记忆 |
| `PreCompact` | 手动或自动压缩前 | 从最近转录保存压缩前总结 |

自动写入使用 `infer=true`，由自托管 Mem0 从对话中提取适合长期保留的信息。短消息、寒暄和空内容会被跳过。

## 技能

插件包含上下文加载、记住、查看、忘记、置顶、整理、导入、导出、项目切换、统计、巡览和健康检查等 16 个技能。所有运行型技能使用自托管服务的 `project_id` 参数，不向 MCP 传递云端专用的 `user_id`、`app_id`、`filters`、`metadata` 或 `rerank`。

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
codex plugin marketplace add D:\code\mem0-codex-self-hosted-marketplace
codex plugin add mem0@mem0-self-hosted
```

真实连通性检查需要先设置 `MEM0_MCP_TOKEN`：

```powershell
python plugins\mem0\scripts\mem0_self_hosted.py --check
```

## 许可证与来源

本仓库采用 Apache-2.0 许可证。SDK 参考材料来源于 Mem0 官方插件，详见 [`NOTICE.md`](NOTICE.md)。
