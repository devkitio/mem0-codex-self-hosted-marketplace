#!/usr/bin/env python3
"""使用标准库验证市场结构、插件配置和自托管运行时边界。"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "mem0"
MEM0_TOOLS = {
    "add_memory",
    "search_memories",
    "get_memories",
    "get_memory",
    "update_memory",
    "delete_memory",
    "get_memory_history",
    "list_entities",
    "delete_all_memories",
    "delete_entities",
}
BULK_TOOLS = {"delete_all_memories", "delete_entities"}
READ_ONLY_TOOLS = {
    "search_memories",
    "get_memories",
    "get_memory",
    "get_memory_history",
    "list_entities",
}
DESTRUCTIVE_TOOLS = {
    "update_memory",
    "delete_memory",
    "delete_all_memories",
    "delete_entities",
}
OFFICIAL_CODEX_HOOK_EVENTS = {
    "PreToolUse",
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "PreCompact",
}
OFFICIAL_CODEX_SKILLS = {
    "context-loader",
    "dream",
    "export",
    "forget",
    "health",
    "import",
    "list-projects",
    "mem0",
    "memory-reviewer",
    "onboard",
    "peek",
    "pin",
    "remember",
    "stats",
    "switch-project",
    "tour",
}


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path} 必须是 JSON 对象"
    return value


def main() -> None:
    marketplace = load_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    manifest = load_json(PLUGIN / ".codex-plugin" / "plugin.json")
    mcp = load_json(PLUGIN / ".mcp.json")
    hooks = load_json(PLUGIN / "hooks" / "hooks.json")
    schema_snapshot = load_json(PLUGIN / "mcp-schema.snapshot.json")

    assert marketplace["name"] == "mem0-self-hosted"
    assert marketplace["plugins"][0]["source"]["path"] == "./plugins/mem0"
    assert manifest["name"] == "mem0"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert "hooks" not in manifest, "默认 hooks/hooks.json 无需在 manifest 重复声明"
    assert (PLUGIN / "hooks" / "hooks.json").is_file(), "插件缺少默认生命周期钩子文件"
    assert mcp["mcpServers"]["mem0"]["url"] == "https://mem0-api.jiang.in/mcp"
    assert mcp["mcpServers"]["mem0"]["bearer_token_env_var"] == "MEM0_MCP_TOKEN"
    assert set(mcp["mcpServers"]["mem0"]["disabled_tools"]) == BULK_TOOLS
    assert schema_snapshot["snapshot_version"] == 1
    snapshot_tools = schema_snapshot.get("tools", {})
    assert set(snapshot_tools) == MEM0_TOOLS, "MCP 契约快照的工具集合不完整"
    for name, contract in snapshot_tools.items():
        assert isinstance(contract.get("properties"), list), f"{name} 快照缺少 properties"
        assert isinstance(contract.get("required"), list), f"{name} 快照缺少 required"
        assert isinstance(contract.get("types"), dict), f"{name} 快照缺少 types"
        assert isinstance(contract.get("defaults"), dict), f"{name} 快照缺少 defaults"
        assert isinstance(contract.get("enums"), dict), f"{name} 快照缺少 enums"
        annotations = contract.get("annotations", {})
        assert set(annotations) == {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        }, f"{name} 快照的 annotations 不完整"
        assert annotations["readOnlyHint"] is (name in READ_ONLY_TOOLS)
        assert annotations["destructiveHint"] is (name in DESTRUCTIVE_TOOLS)
    assert snapshot_tools["list_entities"]["enums"]["entity_type"] == ["project", "run"]
    assert "show_expired" in snapshot_tools["list_entities"]["properties"]
    assert snapshot_tools["list_entities"]["defaults"]["show_expired"] is False
    assert snapshot_tools["delete_entities"]["enums"]["entity_type"] == ["project", "run"]
    assert snapshot_tools["get_memories"]["enums"]["sort_by"] == [
        "created_at",
        "updated_at",
        "expiration_date",
    ]
    assert snapshot_tools["get_memories"]["enums"]["sort_order"] == ["asc", "desc"]
    for name in BULK_TOOLS:
        assert "confirmation_token" in snapshot_tools[name]["properties"], (
            f"{name} 快照缺少两阶段确认令牌"
        )
        assert "confirmation_token" not in snapshot_tools[name]["required"], (
            f"{name} 必须允许省略确认令牌以生成预览"
        )
        assert snapshot_tools[name]["annotations"]["idempotentHint"] is True, (
            f"{name} 必须声明持久化幂等语义"
        )
    assert set(hooks["hooks"]) == OFFICIAL_CODEX_HOOK_EVENTS
    serialized_hooks = json.dumps(hooks, ensure_ascii=False)
    assert "commandWindows" in serialized_hooks, "钩子缺少 Windows 命令"
    assert "apply_patch" in serialized_hooks, "钩子缺少记忆文件写入保护"
    assert "PostToolUse" in serialized_hooks and "Bash" in serialized_hooks
    hook_commands = [
        hook
        for groups in hooks["hooks"].values()
        for group in groups
        for hook in group.get("hooks", [])
    ]
    assert hook_commands, "钩子命令不能为空"
    for hook in hook_commands:
        assert hook.get("type") == "command", "生命周期钩子必须使用命令类型"
        assert hook.get("command") == (
            'python3 "${PLUGIN_ROOT}/scripts/mem0_self_hosted.py"'
        ), "macOS/Linux 钩子命令无效"
        assert hook.get("commandWindows") == (
            'python "${PLUGIN_ROOT}\\scripts\\mem0_self_hosted.py"'
        ), "Windows 钩子命令无效"
    mem0_matchers = [
        group["matcher"]
        for group in hooks["hooks"]["PreToolUse"]
        if "mcp__mem0__" in group.get("matcher", "")
    ]
    assert len(mem0_matchers) == 1
    matcher = re.compile(mem0_matchers[0])
    for name in MEM0_TOOLS:
        assert matcher.fullmatch(f"mcp__mem0__{name}"), f"钩子未匹配 Mem0 工具：{name}"
        assert matcher.fullmatch(f"mcp__plugin_mem0_mem0__{name}"), f"钩子未匹配插件工具：{name}"

    script = (PLUGIN / "scripts" / "mem0_self_hosted.py").read_text(encoding="utf-8")
    syntax_tree = ast.parse(script)
    tool_assignment = next(
        node
        for node in syntax_tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "MEM0_TOOL_NAMES" for target in node.targets)
    )
    assert ast.literal_eval(tool_assignment.value) == MEM0_TOOLS
    runtime_text = script + json.dumps(mcp) + json.dumps(hooks)
    for forbidden in ("api.mem0.ai", "mcp.mem0.ai", "MEM0_API_KEY", "C:\\Users\\"):
        assert forbidden not in runtime_text, f"运行时包含禁止内容：{forbidden}"

    skill_dirs = [path for path in (PLUGIN / "skills").iterdir() if path.is_dir()]
    assert {path.name for path in skill_dirs} == OFFICIAL_CODEX_SKILLS, "技能集合必须对齐官方 Codex 插件"
    assert all((path / "SKILL.md").is_file() for path in skill_dirs), "技能缺少 SKILL.md"
    skill_text = "\n".join(
        (path / "SKILL.md").read_text(encoding="utf-8")
        for path in skill_dirs
    )
    assert "page_size=200" not in skill_text, "技能仍请求超过服务端上限的单页大小"
    health_skill = (PLUGIN / "skills" / "health" / "SKILL.md").read_text(encoding="utf-8")
    assert "../../scripts/mem0_self_hosted.py" in health_skill, "健康检查脚本路径无效"
    switch_skill = (PLUGIN / "skills" / "switch-project" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Windows" in switch_skill and "macOS/Linux" in switch_skill
    assert "python " in switch_skill and "python3 " in switch_skill, (
        "项目切换技能缺少跨平台 Python 命令"
    )
    workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    for runner in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert runner in workflow, f"CI 缺少 {runner} 验证"
    runtime_contract = (PLUGIN / "SELF_HOSTED_RUNTIME.md").read_text(encoding="utf-8")
    for name in MEM0_TOOLS:
        assert f"`{name}(" in runtime_contract, f"运行时约定缺少工具：{name}"
    assert "默认禁用" in runtime_contract and all(name in runtime_contract for name in BULK_TOOLS)
    print("验证通过：市场、MCP 十工具、六类增强钩子、运行时边界和 16 个技能均有效")


if __name__ == "__main__":
    main()
