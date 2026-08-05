#!/usr/bin/env python3
"""使用标准库验证市场结构、插件配置和自托管运行时边界。"""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "mem0"


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path} 必须是 JSON 对象"
    return value


def main() -> None:
    marketplace = load_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    manifest = load_json(PLUGIN / ".codex-plugin" / "plugin.json")
    mcp = load_json(PLUGIN / ".mcp.json")
    hooks = load_json(PLUGIN / "hooks" / "hooks.json")

    assert marketplace["name"] == "mem0-self-hosted"
    assert marketplace["plugins"][0]["source"]["path"] == "./plugins/mem0"
    assert manifest["name"] == "mem0"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert mcp["mcpServers"]["mem0"]["url"] == "https://mem0-api.jiang.in/mcp"
    assert mcp["mcpServers"]["mem0"]["bearer_token_env_var"] == "MEM0_MCP_TOKEN"
    assert set(hooks["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop", "PreCompact"}

    script = (PLUGIN / "scripts" / "mem0_self_hosted.py").read_text(encoding="utf-8")
    ast.parse(script)
    runtime_text = script + json.dumps(mcp) + json.dumps(hooks)
    for forbidden in ("api.mem0.ai", "mcp.mem0.ai", "MEM0_API_KEY", "C:\\Users\\"):
        assert forbidden not in runtime_text, f"运行时包含禁止内容：{forbidden}"

    skill_dirs = [path for path in (PLUGIN / "skills").iterdir() if path.is_dir()]
    assert len(skill_dirs) == 16, "技能数量必须为 16"
    assert all((path / "SKILL.md").is_file() for path in skill_dirs), "技能缺少 SKILL.md"
    print("验证通过：市场、MCP、四类钩子、运行时边界和 16 个技能均有效")


if __name__ == "__main__":
    main()
