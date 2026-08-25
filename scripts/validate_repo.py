#!/usr/bin/env python3
"""使用标准库验证市场结构、插件配置和自托管运行时边界。"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "mem0"
MCP_ADAPTER = ROOT / "services" / "mem0-mcp"
MEM0_SERVER = ROOT / "services" / "mem0-server"
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEM0_TOOLS = {
    "add_memory",
    "search_memories",
    "get_memories",
    "get_memory",
    "update_memory",
    "delete_memory",
    "get_memory_history",
    "list_entities",
    "resolve_project_scope",
    "delete_all_memories",
    "delete_entities",
    "list_memory_candidates",
    "review_memory_candidate",
    "submit_memory_feedback",
}
BULK_TOOLS = {"delete_all_memories", "delete_entities"}
READ_ONLY_TOOLS = {
    "search_memories",
    "get_memories",
    "get_memory",
    "get_memory_history",
    "list_entities",
    "resolve_project_scope",
    "list_memory_candidates",
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

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
    assert mcp["mcpServers"]["mem0"]["bearer_token_env_var"] == "MEM0_SELF_HOSTED_API_KEY"
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
    reserved_metadata = {
        "user_id",
        "agent_id",
        "app_id",
        "mcp_owner",
        "scope",
        "project_id",
        "source",
        "run_id",
    }
    for skill_name in ("dream", "pin"):
        text = (PLUGIN / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert all(name in text for name in reserved_metadata), (
            f"{skill_name} 技能没有完整剔除保留 metadata"
        )
    export_skill = (PLUGIN / "skills" / "export" / "SKILL.md").read_text(encoding="utf-8")
    import_skill = (PLUGIN / "skills" / "import" / "SKILL.md").read_text(encoding="utf-8")
    for text in (export_skill, import_skill):
        assert "mem0-self-hosted-export-v1" in text, "导入导出技能格式不一致"
        assert all(name in text for name in ("metadata", "run_id", "expiration_date")), (
            "导入导出技能缺少可迁移字段"
        )
    health_skill = (PLUGIN / "skills" / "health" / "SKILL.md").read_text(encoding="utf-8")
    assert "../../scripts/mem0_self_hosted.py" in health_skill, "健康检查脚本路径无效"
    switch_skill = (PLUGIN / "skills" / "switch-project" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Windows" in switch_skill and "macOS/Linux" in switch_skill
    assert "python " in switch_skill and "python3 " in switch_skill, (
        "项目切换技能缺少跨平台 Python 命令"
    )
    for command in ("--sync-project", "--clear-project", "--current-project"):
        assert command in switch_skill and command in script, f"项目范围缺少命令：{command}"
    assert "repository_fingerprint" in switch_skill and "server_project_scopes.json" in switch_skill, (
        "项目切换技能缺少私有服务端同步流程"
    )
    workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    for runner in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert runner in workflow, f"CI 缺少 {runner} 验证"
    for name in (
        "server.py",
        "test_adapter.py",
        "Dockerfile",
        "requirements.in",
        "requirements.lock",
    ):
        assert (MCP_ADAPTER / name).is_file(), f"仓库缺少 MCP Adapter 文件：{name}"
    adapter_requirements = (MCP_ADAPTER / "requirements.in").read_text(encoding="utf-8")
    adapter_lock = (MCP_ADAPTER / "requirements.lock").read_text(encoding="utf-8")
    adapter_dockerfile = (MCP_ADAPTER / "Dockerfile").read_text(encoding="utf-8")
    adapter_dockerignore = (MCP_ADAPTER / ".dockerignore").read_text(encoding="utf-8")
    assert "--require-hashes" in adapter_dockerfile, "Adapter 镜像未强制校验依赖哈希"
    assert "--require-hashes" in workflow, "CI 未强制校验 Adapter 依赖哈希"
    assert "--hash=sha256:" in adapter_lock, "Adapter 锁文件缺少依赖哈希"
    assert "secrets/" in adapter_dockerignore.splitlines(), "Adapter 构建上下文未排除 secrets 目录"
    for requirement in ("httpx", "mcp", "PyJWT", "uvicorn[standard]"):
        assert requirement in adapter_requirements, f"Adapter 直接依赖清单缺少 {requirement}"
    adapter_source = (MCP_ADAPTER / "server.py").read_text(encoding="utf-8")
    ast.parse(adapter_source)
    assert '"/auth/introspect"' in adapter_source, "Adapter 未使用 MCP Key 内省接口"
    assert '"/internal/mcp/search"' in adapter_source, "Adapter 搜索未走内部服务接口"
    assert '"/auth/me"' not in adapter_source, "Adapter 仍使用管理员身份接口校验 Key"
    assert '"/search"' not in adapter_source, "Adapter 仍调用公开搜索接口"
    assert "get_access_token" in adapter_source, "Adapter 未从认证上下文读取稳定主体"
    assert "access_token.token" not in adapter_source, "Adapter 可能读取或转发客户端 Key"
    assert "MCP_PROJECT_SCOPE_SECRET" in adapter_source, "Adapter 缺少私有项目范围 Secret"
    assert "_identity_env" in adapter_source, "Adapter 未强制显式配置内部身份"
    for forbidden in ("mem0-api.jiang.in", "codex-primary", "codex-primary-adapter"):
        assert forbidden not in adapter_source, f"Adapter 泄露生产默认值：{forbidden}"
    assert "services/mem0-mcp/requirements.lock" in workflow, "CI 未验证 MCP Adapter"

    upstream_manifest_path = MEM0_SERVER / "upstream.json"
    materializer_path = ROOT / "scripts" / "materialize_mem0.py"
    assert upstream_manifest_path.is_file(), "仓库缺少 Mem0 上游清单"
    assert materializer_path.is_file(), "仓库缺少 Mem0 物化脚本"
    upstream = load_json(upstream_manifest_path)
    assert upstream.get("schema_version") == 1, "Mem0 上游清单版本无效"
    assert upstream.get("repository") == "https://github.com/mem0ai/mem0.git", (
        "Mem0 上游仓库不在允许范围内"
    )
    commit = upstream.get("commit")
    patch_name = upstream.get("patch")
    expected_patch_hash = upstream.get("patch_sha256")
    assert isinstance(commit, str) and GIT_SHA_RE.fullmatch(commit), "Mem0 上游提交必须是完整 SHA"
    assert isinstance(patch_name, str) and PurePosixPath(patch_name).name == patch_name, (
        "Mem0 补丁路径必须是清单同目录下的文件名"
    )
    assert isinstance(expected_patch_hash, str) and SHA256_RE.fullmatch(expected_patch_hash), (
        "Mem0 补丁哈希格式无效"
    )
    patch_path = MEM0_SERVER / patch_name
    assert patch_path.is_file(), "Mem0 生产补丁不存在"
    patch_bytes = patch_path.read_bytes()
    assert hashlib.sha256(patch_bytes).hexdigest() == expected_patch_hash, (
        "Mem0 生产补丁哈希不匹配"
    )
    patch_text = patch_bytes.decode("utf-8")
    patch_paths = re.findall(r"^diff --git a/(.+) b/(.+)$", patch_text, re.MULTILINE)
    assert patch_paths, "Mem0 生产补丁没有文件变更"
    patch_indexes = re.findall(
        r"^index ([0-9a-f]{40})\.\.([0-9a-f]{40})(?: [0-7]{6})?$",
        patch_text,
        re.MULTILINE,
    )
    assert len(patch_indexes) == len(patch_paths), "Mem0 生产补丁必须为每个文件记录完整 blob ID"
    assert len({new_path for _, new_path in patch_paths}) == len(patch_paths), (
        "Mem0 生产补丁包含重复文件"
    )
    assert not any(line.startswith(" ") for line in patch_text.splitlines()), (
        "Mem0 生产补丁不得携带未修改的上游上下文"
    )
    for old_path, new_path in patch_paths:
        assert old_path == new_path, "Mem0 生产补丁不得包含重命名"
        path = PurePosixPath(old_path)
        assert not path.is_absolute() and ".." not in path.parts, "Mem0 生产补丁包含越界路径"
        assert old_path in {
            ".dockerignore",
            ".gitignore",
            "mem0/configs/vector_stores/pgvector.py",
            "mem0/memory/main.py",
            "mem0/memory/storage.py",
            "mem0/vector_stores/pgvector.py",
            "mem0-ts/src/oss/src/memory/index.ts",
            "mem0-ts/src/oss/src/storage/MemoryContract.test.ts",
            "mem0-ts/src/oss/src/storage/MemoryHistoryManager.ts",
            "mem0-ts/src/oss/src/storage/SQLiteManager.ts",
            "mem0-ts/src/oss/src/storage/base.ts",
            "mem0-ts/src/oss/src/utils/factory.ts",
            "mem0-ts/src/oss/src/vector_stores/pgvector.ts",
            "mem0-ts/src/oss/tests/pgvector.unit.test.ts",
            "tests/fixtures/memory_contract_v2.json",
            "tests/test_memory_contract_v2.py",
            "tests/vector_stores/test_pgvector.py",
        } or old_path.startswith(("server/", "openresty/")), (
            f"Mem0 生产补丁修改了未授权路径：{old_path}"
        )
    required_patch_paths = {
        "openresty/api-proxy.conf",
        "openresty/dashboard-proxy.conf",
        "openresty/mcp.conf",
        "server/alembic/versions/007_mcp_deletion_operations.py",
        "server/alembic/versions/008_api_key_purpose.py",
        "server/alembic/versions/011_platform_operations.py",
        "server/alembic/versions/012_memory_quality_model.py",
        "server/alembic/versions/013_retrieval_feedback.py",
        "server/alembic/versions/014_evaluation_v2.py",
        "server/alembic/versions/015_temporal_conflict_projection.py",
        "server/alembic/versions/016_retrieval_learning.py",
        "server/alembic/versions/017_governance_workflows.py",
        "server/alembic/versions/018_operations.py",
        "server/config.py",
        "server/disaster_recovery_service.py",
        "server/memory_quality_service.py",
        "server/mcp_scope.py",
        "server/observability.py",
        "server/pagination.py",
        "server/prod.Dockerfile",
        "server/requirements.lock",
        "server/scripts/backfill_memory_quality.py",
        "server/scripts/backfill_mcp_scope.py",
        "server/scripts/retry_memory_mutations.py",
        "server/test_main.py",
        "tests/fixtures/memory_contract_v2.json",
        "tests/test_memory_contract_v2.py",
        "mem0-ts/src/oss/src/storage/MemoryContract.test.ts",
        "mem0-ts/src/oss/src/vector_stores/pgvector.ts",
        "mem0-ts/src/oss/tests/pgvector.unit.test.ts",
        "mem0/configs/vector_stores/pgvector.py",
    }
    assert required_patch_paths <= {path for _, path in patch_paths}, (
        "Mem0 生产补丁缺少关键新增文件"
    )
    assert "120000" not in patch_text, "Mem0 生产补丁不得创建符号链接"
    for expected in (
        "MEM0_MCP_ROOT:-/data/mem0Mcp",
        "mcp_confirmation_secret",
        "mcp_project_scope_secret",
        "mem0_internal_service_key",
        "server/secrets",
    ):
        assert expected in patch_text, f"Mem0 生产补丁缺少 Secret 边界：{expected}"
    for expected in (
        "MEM0_INTERNAL_USER_ID:?必须设置 MEM0_INTERNAL_USER_ID",
        "MEM0_INTERNAL_OWNER:?必须设置 MEM0_INTERNAL_OWNER",
        "MEM0_FORWARDED_ALLOW_IPS:?必须设置 MEM0_FORWARDED_ALLOW_IPS",
        "REVERSE_PROXY_NETWORK_NAME:?必须设置 REVERSE_PROXY_NETWORK_NAME",
        "MODEL_GATEWAY_NETWORK_NAME:?必须设置 MODEL_GATEWAY_NETWORK_NAME",
        "mem0-api.example.com",
        "/etc/nginx/mem0",
        "validate_mcp_identity",
    ):
        assert expected in patch_text, f"Mem0 生产补丁缺少通用部署参数：{expected}"
    for forbidden in (
        "mem0-api.jiang.in",
        "mem0.jiang.in",
        "codex-primary",
        "1Panel-openresty",
        "sub2api-deploy",
        "/www/sites/",
        "/opt/1panel/",
        "192.168.144.",
        "192.168.64.",
        "192.168.112.",
        "192.168.128.",
    ):
        assert forbidden not in patch_text, f"Mem0 生产补丁泄露部署指纹：{forbidden}"
    for expected in (
        "openresty/dashboard-proxy.conf",
        "proxy_pass http://127.0.0.1:3111",
        "COPY --from=builder --chown=nextjs:nodejs /app/public",
        "FROM --platform=$BUILDPLATFORM node:22-alpine@sha256:",
        "c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS runner",
        "supportedArchitectures:",
        "    - arm64",
        "    - musl",
    ):
        assert expected in patch_text, f"Mem0 生产补丁缺少 Dashboard 部署资产：{expected}"

    materializer = materializer_path.read_text(encoding="utf-8")
    ast.parse(materializer)
    for expected in (
        "GIT_TERMINAL_PROMPT",
        "--depth",
        "--unidiff-zero",
        "--index",
        "apply",
        "--check",
        "reconfigure",
        "TimeoutExpired",
        "FETCH_GIT_TIMEOUT_SECONDS",
    ):
        assert expected in materializer, f"Mem0 物化脚本缺少安全门禁：{expected}"
    for expected in (
        "scripts/materialize_mem0.py",
        ".mem0-source/server/requirements.lock",
        "alembic downgrade 006",
        "verify-pgvector-filter-integration",
        "verify-backfill-integration",
        "prepare-restart",
        "verify-restart",
        "alembic upgrade 018",
        "test_memory_contract_v2.py",
        "MemoryContract.test.ts",
        "pgvector.unit.test.ts",
        "node-version: \"22\"",
        "pnpm install --frozen-lockfile",
        "pnpm run typecheck",
        "pnpm run build",
        "pnpm run test:e2e",
        "pnpm audit --prod --audit-level=high",
        "pip-audit==2.9.0",
        "Trivy 镜像门禁",
        "DOCKER_CONFIG=/tmp/docker-config",
        "verify-attestation",
        "needs.supply-chain-publish.outputs.api-image",
        "matrix:\n        component: [api, mcp, dashboard]",
        "timeout 20m docker run",
        'docker exec "$container" wget',
        "platforms: linux/arm64",
        "fetch-depth: 0",
        'GITLEAKS_VERSION: "8.30.1"',
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
        "git --redact --verbose --exit-code 1",
    ):
        assert expected in workflow, f"CI 缺少 Mem0 生产门禁：{expected}"
    for action_sha in (
        "actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38",
        "docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8",
        "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",
    ):
        assert action_sha in workflow, f"CI Action 未固定到审查 SHA：{action_sha}"
    trivy_ignore = (ROOT / ".trivyignore.yaml").read_text(encoding="utf-8")
    waiver_blocks = re.findall(
        r"(?ms)^  - id: (CVE-\d{4}-\d+)\n(.*?)(?=^  - id:|\Z)",
        trivy_ignore,
    )
    assert waiver_blocks, "Trivy 漏洞豁免清单为空"
    for vulnerability_id, block in waiver_blocks:
        statement = re.search(r"^    statement: (.+)$", block, re.MULTILINE)
        expiry = re.search(r"^    expired_at: (\d{4}-\d{2}-\d{2})$", block, re.MULTILINE)
        assert statement and statement.group(1).strip(), f"Trivy 豁免缺少原因：{vulnerability_id}"
        assert expiry and date.fromisoformat(expiry.group(1)) > date.today(), (
            f"Trivy 豁免已过期或缺少有效期：{vulnerability_id}"
        )
    assert "--ignorefile /workspace/.trivyignore.yaml" in workflow, "CI 未使用受控 Trivy 豁免清单"
    root_gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for ignored in (
        "/.mem0-source/",
        "/backups/",
        "/runtime.env",
        "/secrets/",
        "/services/mem0-mcp/secrets/",
    ):
        assert ignored in root_gitignore, f"仓库未忽略运行时敏感路径：{ignored}"

    runtime_contract = (PLUGIN / "SELF_HOSTED_RUNTIME.md").read_text(encoding="utf-8")
    assert "/auth/introspect" in runtime_contract and "用途为 MCP" in runtime_contract, (
        "运行时约定缺少 MCP 专用 Key 边界"
    )
    for name in MEM0_TOOLS:
        assert f"`{name}(" in runtime_contract, f"运行时约定缺少工具：{name}"
    assert "默认禁用" in runtime_contract and all(name in runtime_contract for name in BULK_TOOLS)
    print("验证通过：市场、MCP 14 工具、六类增强钩子、运行时边界和 16 个技能均有效")


if __name__ == "__main__":
    main()
