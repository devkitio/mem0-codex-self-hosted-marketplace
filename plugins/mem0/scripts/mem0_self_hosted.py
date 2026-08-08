#!/usr/bin/env python3
"""通过自托管 Mem0 MCP 执行 Codex 生命周期钩子。"""

from __future__ import annotations

import configparser
import hashlib
import ipaddress
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import IO, Any, Callable

if os.name == "nt":
    import msvcrt
else:
    import fcntl


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MCP_CONFIG_PATH = PLUGIN_ROOT / ".mcp.json"
PLUGIN_DATA = Path(
    os.environ.get("PLUGIN_DATA", Path.home() / ".codex" / "plugin-data" / "mem0-self-hosted")
)
LOG_PATH = PLUGIN_DATA / "mem0_self_hosted.log"
PROTOCOL_VERSION = "2025-03-26"
MCP_REQUEST_TIMEOUT = 15
MCP_MUTATION_TIMEOUT = 50
HOOK_TIME_BUDGETS = {
    "PreToolUse": 18.0,
    "SessionStart": 85.0,
    "UserPromptSubmit": 22.0,
    "PostToolUse": 18.0,
    "Stop": 55.0,
    "PreCompact": 55.0,
}
HOOK_CLEANUP_RESERVE = 2.0
MAX_MCP_RESPONSE_BYTES = 2_000_000
MAX_HOOK_INPUT_BYTES = 4_000_000
MAX_MEMORY_TEXT = 50_000
MAX_TRANSCRIPT_BYTES = 6_000_000
MAX_IMPORT_FILE_SIZE = 100_000
MAX_IMPORT_CHUNK_SIZE = 10_000
MIN_IMPORT_CHUNK_SIZE = 50
IMPORT_SEARCH_BATCH_SIZE = 20
IMPORT_FORMAT_VERSION = 3
SCHEMA_SNAPSHOT_PATH = PLUGIN_ROOT / "mcp-schema.snapshot.json"
TARGET_PROJECT_FILES = ("CLAUDE.md", "AGENTS.md", ".cursorrules", ".windsurfrules", "mem0.md")
DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_save": True,
    "auto_search": True,
    "search_limit": 5,
    "confidence_threshold": 0.25,
    "rerank": True,
    "debug": False,
    "session_retention_days": 90,
}
SETTING_ENV_VARS = {
    "auto_save": "MEM0_AUTO_SAVE",
    "auto_search": "MEM0_AUTO_SEARCH",
    "search_limit": "MEM0_SEARCH_LIMIT",
    "confidence_threshold": "MEM0_CONFIDENCE_THRESHOLD",
    "rerank": "MEM0_RERANK",
    "debug": "MEM0_DEBUG",
    "session_retention_days": "MEM0_SESSION_RETENTION_DAYS",
}
MEM0_MD_SECTIONS = {"settings", "search", "ignore", "identity", "categories", "retention"}
PROJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
MEM0_TOOL_NAMES = {
    "add_memory",
    "delete_all_memories",
    "delete_entities",
    "delete_memory",
    "search_memories",
    "get_memories",
    "get_memory",
    "get_memory_history",
    "list_entities",
    "update_memory",
}
MUTATING_TOOLS = {
    "add_memory",
    "update_memory",
    "delete_memory",
    "delete_all_memories",
    "delete_entities",
}
FILE_PATTERN = re.compile(
    r"[A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js|jsx|rs|go|rb|java|sh|ps1|yaml|yml|json|toml|md|sql|css|html)"
)
SYSTEM_TAG_PATTERN = re.compile(
    r"<(?:system-reminder|private|persisted-output|system_instruction)>.*?"
    r"</(?:system-reminder|private|persisted-output|system_instruction)>",
    re.DOTALL,
)
AUTH_SECRET_PATTERN = re.compile(
    r'''(?ix)(?P<prefix>["']?authorization["']?\s*[:=]\s*)'''
    r'''(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n]+)'''
)
AUTH_SCHEME_PATTERN = re.compile(
    r"(?i)\b(?:Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]+"
)
NAMED_SECRET_PATTERN = re.compile(
    r'''(?ix)(?P<prefix>["']?[A-Za-z0-9_.-]*'''
    r'''(?:api[_-]?key|access[_-]?key|token|password|passwd|secret)'''
    r'''[A-Za-z0-9_.-]*["']?\s*[:=]\s*)'''
    r'''(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)'''
)
URI_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@]+(?::[^\s/@]*)?@"
)
WINDOWS_USER_PATH_PATTERN = re.compile(
    r'''(?i)(?<![A-Za-z0-9])(?:[A-Z]:)?[\\/](?:Users|Documents and Settings)'''
    r'''[\\/][^\\/\r\n"']+'''
)
UNIX_USER_PATH_PATTERN = re.compile(r'''(?<![A-Za-z0-9])/(?:home|Users)/[^/\r\n"']+''')
IPV4_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
BRACKETED_IPV6_PATTERN = re.compile(r"\[[0-9A-Fa-f:.%]+\]")
BARE_IPV6_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}"
    r"(?:%[0-9A-Za-z_.-]+)?(?![0-9A-Za-z])"
)
PATCH_FILE_PATTERN = re.compile(
    r"(?m)^\s*(?:\*{3}\s+(?:Add|Update|Delete)\s+File:|---|\+\+\+)\s*(.+?)\s*$"
)
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
STANDALONE_SECRET_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"m0sk_[A-Za-z0-9_-]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}"
    r")(?![A-Za-z0-9])"
)
TRIVIAL_PROMPTS = {
    "ok",
    "okay",
    "可以",
    "好的",
    "好",
    "收到",
    "知道了",
    "明白",
    "明白了",
    "没问题",
    "谢谢",
    "继续",
}
CONTINUATION_PATTERN = re.compile(
    r"(?:继续(?:上次|之前|刚才)?|接着(?:上次|之前|刚才)?|恢复(?:上次|之前)?|从上次继续|continue)",
    re.IGNORECASE,
)
COMPLEX_PROMPT_PATTERN = re.compile(
    r"(?:实现|修复|优化|重构|迁移|部署|测试|排查|诊断|设计|方案|架构|为什么|区别|对比|决定|选择|"
    r"implement|fix|optimi[sz]e|refactor|migrat|deploy|test|debug|design|architecture)",
    re.IGNORECASE,
)
SUMMARY_SIGNAL_PATTERN = re.compile(
    r"(?:目标|需求|决定|确认|约定|偏好|实现|修复|完成|通过|验证|测试|部署|发布|回滚|阻塞|待办|下一步|"
    r"错误|失败|根因|风险|文件|分支|commit|目标版本|must|should|decided|implemented|fixed|verified|todo|blocked)",
    re.IGNORECASE,
)
ATOMIC_WRITE_LOCK = threading.Lock()
LOCK_HANDLES: dict[str, IO[bytes]] = {}
LOCK_HANDLES_GUARD = threading.Lock()
HOOK_DEADLINE: float | None = None


def hook_time_remaining(reserve: float = 0.0) -> float | None:
    if HOOK_DEADLINE is None:
        return None
    return HOOK_DEADLINE - time.monotonic() - reserve


def hook_budget_available(minimum: float = 0.0) -> bool:
    remaining = hook_time_remaining()
    return remaining is None or remaining > minimum


def log_error(message: str) -> None:
    """仅记录诊断信息，绝不记录令牌或记忆正文。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat()
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def atomic_write_json(path: Path, value: Any) -> None:
    """原子写入插件状态，避免钩子并发导致半截 JSON。"""
    with ATOMIC_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            assert temporary is not None
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass


def load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_hook_input(stream: Any) -> Any:
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        raw = binary.read(MAX_HOOK_INPUT_BYTES + 1)
        if len(raw) > MAX_HOOK_INPUT_BYTES:
            raise ValueError("钩子输入超过大小限制")
        return json.loads(raw.decode("utf-8"))
    text = stream.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(text.encode("utf-8")) > MAX_HOOK_INPUT_BYTES:
        raise ValueError("钩子输入超过大小限制")
    return json.loads(text)


def http_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("MCP 地址格式无效") from exc
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not host:
        raise RuntimeError("MCP 地址必须是有效的 HTTP(S) URL")
    return scheme, host, port or (443 if scheme == "https" else 80)


def validate_mcp_url(url: str) -> None:
    scheme, host, _ = http_origin(url)
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise RuntimeError("MCP 地址不得包含凭据或片段")
    if scheme == "https":
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise RuntimeError("MCP 地址必须使用 HTTPS；仅回环地址允许 HTTP")


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        validate_mcp_url(new_url)
        if http_origin(request.full_url) != http_origin(new_url):
            raise RuntimeError("MCP 拒绝跨源重定向")
        redirected = super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )
        if redirected is not None:
            authorization = request.get_header("Authorization")
            redirected.remove_header("Authorization")
            if authorization:
                redirected.add_unredirected_header("Authorization", authorization)
        return redirected


def open_mcp_request(
    request: urllib.request.Request,
    timeout_seconds: float = MCP_REQUEST_TIMEOUT,
) -> Any:
    opener = urllib.request.build_opener(SameOriginRedirectHandler())
    return opener.open(request, timeout=timeout_seconds)


def load_connection() -> tuple[str, str]:
    """从插件自身的 MCP 配置读取地址和令牌环境变量。"""
    with MCP_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    server = config.get("mcpServers", {}).get("mem0", {})
    url = str(server.get("url", "")).strip()
    token_name = str(server.get("bearer_token_env_var", "MEM0_SELF_HOSTED_API_KEY")).strip()
    token = os.environ.get(token_name, "").strip()
    if not url:
        raise RuntimeError("插件未配置 mcpServers.mem0.url")
    validate_mcp_url(url)
    if not token:
        raise RuntimeError(f"未设置令牌环境变量 {token_name}")
    return url, token


def parse_mcp_response(body: str, content_type: str, request_id: int = 1) -> dict[str, Any]:
    payloads: list[Any] = []
    if content_type.split(";", 1)[0].strip().casefold() == "text/event-stream":
        data_lines: list[str] = []
        for line in [*body.splitlines(), ""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                payloads.append(json.loads("\n".join(data_lines)))
                data_lines = []
    else:
        payloads.append(json.loads(body))

    for payload in payloads:
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("id") == request_id
                and ("result" in message or "error" in message)
            ):
                return message
    raise RuntimeError("MCP 响应缺少匹配当前请求的结果")


def mcp_request(
    method: str,
    params: dict[str, Any],
    timeout_seconds: float = MCP_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """使用无状态 Streamable HTTP 调用自托管 MCP。"""
    url, token = load_connection()
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        ensure_ascii=False,
    ).encode("utf-8", errors="replace")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": "codex-mem0-self-hosted-hook/2.0",
        },
    )
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    with open_mcp_request(request, timeout_seconds) as response:
        raw = response.read(MAX_MCP_RESPONSE_BYTES + 1)
        if len(raw) > MAX_MCP_RESPONSE_BYTES:
            raise RuntimeError("MCP 响应超过大小限制")
        result = parse_mcp_response(
            raw.decode("utf-8"),
            response.headers.get_content_type(),
        )
    if result.get("error"):
        raise RuntimeError("MCP 返回错误")
    return result.get("result", {})


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    timeout_seconds = MCP_MUTATION_TIMEOUT if name in MUTATING_TOOLS else MCP_REQUEST_TIMEOUT
    remaining = hook_time_remaining(HOOK_CLEANUP_RESERVE)
    if remaining is not None:
        if remaining <= 0.1:
            raise TimeoutError("Hook 总时间预算已耗尽")
        timeout_seconds = min(timeout_seconds, remaining)
    result = mcp_request(
        "tools/call",
        {"name": name, "arguments": arguments},
        timeout_seconds,
    )
    if result.get("isError"):
        raise RuntimeError(f"MCP 工具 {name} 返回错误")
    return result


def _read_small_text(path: Path, max_bytes: int = 4096) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return ""
    if len(raw) > max_bytes:
        return ""
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def _git_directory(root: Path) -> Path | None:
    marker = root / ".git"
    if marker.is_dir():
        try:
            return marker.resolve()
        except OSError:
            return None
    if not marker.is_file():
        return None
    value = _read_small_text(marker)
    if not value.lower().startswith("gitdir:"):
        return None
    candidate = Path(value.split(":", 1)[1].strip())
    if not candidate.is_absolute():
        candidate = marker.parent / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    return candidate if candidate.is_dir() and (candidate / "HEAD").is_file() else None


def _git_config_path(git_directory: Path) -> Path:
    common_value = _read_small_text(git_directory / "commondir")
    if common_value:
        common = Path(common_value)
        if not common.is_absolute():
            common = git_directory / common
        try:
            common = common.resolve()
        except OSError:
            common = git_directory
        if common.is_dir():
            return common / "config"
    return git_directory / "config"


def _normalized_remote_identity(value: str) -> str:
    remote = value.strip()
    if not remote:
        return ""
    if "://" not in remote:
        match = re.fullmatch(r"(?:[^@/\\\s]+@)?([A-Za-z0-9.-]+):(.+)", remote)
        if not match or (len(match.group(1)) == 1 and match.group(2).startswith(("/", "\\"))):
            return ""
        host = match.group(1).casefold()
        path = match.group(2)
        port = ""
    else:
        try:
            parsed = urllib.parse.urlsplit(remote)
            if parsed.scheme.casefold() not in {"http", "https", "ssh", "git"} or not parsed.hostname:
                return ""
            host = parsed.hostname.casefold()
            port = f":{parsed.port}" if parsed.port is not None else ""
            path = urllib.parse.unquote(parsed.path)
        except ValueError:
            return ""
    path = path.replace("\\", "/").strip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    return f"{host}{port}/{path}" if path else ""


def _repository_identity_key(root: Path) -> str:
    git_directory = _git_directory(root)
    if git_directory is not None:
        config_text = _read_small_text(_git_config_path(git_directory), max_bytes=65_536)
        if config_text:
            parser = configparser.RawConfigParser(interpolation=None, strict=False)
            try:
                parser.read_string(config_text)
            except configparser.Error:
                parser = configparser.RawConfigParser(interpolation=None, strict=False)
            remote_sections = [
                section for section in parser.sections() if section.casefold().startswith('remote "')
            ]
            remote_sections.sort(key=lambda section: (section.casefold() != 'remote "origin"', section))
            for section in remote_sections:
                identity = _normalized_remote_identity(parser.get(section, "url", fallback=""))
                if identity:
                    return hashlib.sha256(b"remote\0" + identity.encode("utf-8")).hexdigest()
    material = os.fsencode(os.path.normcase(str(root.resolve())))
    return hashlib.sha256(b"path\0" + material).hexdigest()


@lru_cache(maxsize=16)
def _cached_git_root(working_dir: str) -> Path:
    current = Path(working_dir).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if _git_directory(candidate) is not None:
            return candidate
    return current


def git_root(cwd: str | None) -> Path:
    working_dir = Path(cwd or os.getcwd()).resolve()
    return _cached_git_root(str(working_dir))


def project_mapping_path() -> Path:
    return PLUGIN_DATA / "project_mappings.json"


def project_claims_path() -> Path:
    return PLUGIN_DATA / "project_claims.json"


def project_scope_lock_path() -> Path:
    return PLUGIN_DATA / "project_scope.lock"


def _project_mapping_key(root: Path) -> str:
    return hashlib.sha256(os.fsencode(os.path.normcase(str(root)))).hexdigest()


def project_mapping_key(cwd: str | None) -> str:
    return _project_mapping_key(git_root(cwd))


def validate_project_id(project_id: str) -> str:
    value = project_id.strip()
    if not PROJECT_ID_PATTERN.fullmatch(value):
        raise ValueError("project_id 必须由 1～64 位字母、数字、点、下划线或连字符组成")
    return value


def default_project_id(root: Path) -> str:
    name = root.name or "default"
    if PROJECT_ID_PATTERN.fullmatch(name):
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"project-{digest}"


def _mapped_project_id(root: Path, mappings: Any) -> str | None:
    if not isinstance(mappings, dict):
        return None
    value = mappings.get(_project_mapping_key(root))
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return validate_project_id(value)
    except ValueError:
        log_error("忽略不符合生产契约的项目映射")
        return None


def _collision_project_id(legacy_id: str, identity_key: str, claimed: set[str]) -> str:
    for suffix_length in (12, 16, 24, 32):
        suffix = identity_key[:suffix_length]
        prefix = legacy_id[: 63 - suffix_length].rstrip("._-") or "project"
        candidate = f"{prefix}-{suffix}"
        if candidate not in claimed:
            return candidate
    return f"project-{identity_key[:56]}"


def _automatic_project_id(root: Path) -> str:
    identity_key = _repository_identity_key(root)
    lock_path = project_scope_lock_path()
    if not wait_for_lock(lock_path, timeout=5):
        raise TimeoutError("等待项目范围锁超时")
    try:
        mappings = load_json_file(project_mapping_path(), {})
        mapped = _mapped_project_id(root, mappings)
        if mapped is not None:
            return mapped
        state = load_json_file(project_claims_path(), {})
        if not isinstance(state, dict) or state.get("version") != 1:
            state = {"version": 1, "claims": {}}
        claims = state.get("claims")
        if not isinstance(claims, dict):
            claims = {}
            state["claims"] = claims
        existing = claims.get(identity_key)
        if isinstance(existing, dict):
            project_id = existing.get("project_id")
            if isinstance(project_id, str):
                try:
                    return validate_project_id(project_id)
                except ValueError:
                    pass
        claimed = {
            record.get("project_id")
            for record in claims.values()
            if isinstance(record, dict) and isinstance(record.get("project_id"), str)
        }
        if isinstance(mappings, dict):
            claimed.update(value for value in mappings.values() if isinstance(value, str))
        legacy_id = default_project_id(root)
        collision = legacy_id in claimed
        project_id = (
            _collision_project_id(legacy_id, identity_key, claimed)
            if collision
            else legacy_id
        )
        claims[identity_key] = {
            "project_id": project_id,
            "legacy_project_id": legacy_id,
            "collision": collision,
        }
        atomic_write_json(project_claims_path(), state)
        return project_id
    finally:
        release_lock(lock_path)


def resolve_project_id(cwd: str | None) -> str:
    """优先使用显式映射，否则使用带本机冲突保护的自动范围。"""
    root = git_root(cwd)
    mappings = load_json_file(project_mapping_path(), {})
    mapped = _mapped_project_id(root, mappings)
    return mapped if mapped is not None else _automatic_project_id(root)


def project_scope_notice(cwd: str | None) -> str:
    root = git_root(cwd)
    if _mapped_project_id(root, load_json_file(project_mapping_path(), {})) is not None:
        return ""
    state = load_json_file(project_claims_path(), {})
    claims = state.get("claims", {}) if isinstance(state, dict) else {}
    record = claims.get(_repository_identity_key(root)) if isinstance(claims, dict) else None
    if not isinstance(record, dict) or not record.get("collision"):
        return ""
    project_id = str(record.get("project_id", ""))
    legacy_id = str(record.get("legacy_project_id", ""))
    return (
        f"检测到同名但身份不同的 Git 仓库；当前工作区已隔离为 project_id `{project_id}`。"
        f"旧范围 `{legacy_id}` 中可能存在此前混合的记忆，插件不会自动迁移或删除；"
        "跨机器共享请使用 `mem0:switch-project` 为各副本设置同一个明确的 project_id。"
    )


def set_project_mapping(cwd: str | None, project_id: str | None) -> str:
    root = git_root(cwd)
    path = project_mapping_path()
    lock_path = project_scope_lock_path()
    if not wait_for_lock(lock_path, timeout=5):
        raise TimeoutError("等待项目映射锁超时")
    try:
        mappings = load_json_file(path, {})
        if not isinstance(mappings, dict):
            mappings = {}
        key = _project_mapping_key(root)
        if project_id is None:
            changed = key in mappings
            mappings.pop(key, None)
            resolved = None
        else:
            resolved = validate_project_id(project_id)
            changed = mappings.get(key) != resolved
            mappings[key] = resolved
        if changed:
            atomic_write_json(path, mappings)
    finally:
        release_lock(lock_path)
    return resolve_project_id(str(root)) if resolved is None else resolved


def resolve_branch(cwd: str | None) -> str:
    git_directory = _git_directory(git_root(cwd))
    if git_directory is None:
        return ""
    value = _read_small_text(git_directory / "HEAD")
    prefix = "ref: refs/heads/"
    if not value.startswith(prefix):
        return ""
    branch = value[len(prefix) :]
    forbidden = set(" ~^:?*[\\")
    if (
        not branch
        or len(branch) > 200
        or any(character in forbidden or ord(character) < 32 for character in branch)
        or ".." in branch
        or "@{" in branch
        or "//" in branch
        or branch.startswith(("/", "."))
        or branch.endswith(("/", ".", ".lock"))
    ):
        return ""
    return branch


def settings_path() -> Path:
    return PLUGIN_DATA / "settings.json"


def initialize_settings() -> bool:
    path = settings_path()
    if path.exists():
        return False
    atomic_write_json(path, DEFAULT_SETTINGS)
    return True


def _markdown_item(line: str) -> str:
    value = re.sub(r"^\s*(?:(?:[-*+])|(?:\d+[.)]))\s+", "", line).strip()
    if value.startswith("`") and value.endswith("`") and len(value) > 1:
        value = value[1:-1].strip()
    return value


def _key_values(lines: list[str]) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    unmatched: list[str] = []
    for line in lines:
        item = _markdown_item(line)
        if not item or item.startswith("<!--") or item.startswith("```"):
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]{0,63})\s*[:=]\s*(.+)$", item)
        if not match:
            unmatched.append(item)
            continue
        key = match.group(1).replace("-", "_").casefold()
        values[key] = match.group(2).strip()
    return values, unmatched


def _list_values(lines: list[str]) -> list[str]:
    values: list[str] = []
    for line in lines:
        item = _markdown_item(line)
        if not item or item.startswith("<!--") or item.startswith("```"):
            continue
        values.append(item[:500])
    return values[:50]


def parse_mem0_md(cwd: str | None) -> dict[str, Any]:
    """解析项目级 mem0.md；缺失或格式异常时返回空策略。"""
    empty: dict[str, Any] = {
        "settings": {},
        "search": [],
        "ignore": [],
        "identity": [],
        "categories": [],
        "retention": {},
    }
    root = git_root(cwd)
    current = Path(cwd or root).resolve()
    candidates = list(dict.fromkeys((current / "mem0.md", root / "mem0.md")))
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        return empty
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_IMPORT_FILE_SIZE + 1)
    except OSError:
        return empty
    if len(raw) > MAX_IMPORT_FILE_SIZE:
        log_error("忽略过大的 mem0.md")
        return empty

    sections: dict[str, list[str]] = {name: [] for name in MEM0_MD_SECTIONS}
    current_section = ""
    for line in raw.decode("utf-8", errors="replace").splitlines():
        heading = re.match(r"^\s*##\s+(.+?)\s*#*\s*$", line)
        if heading:
            name = heading.group(1).strip().casefold()
            current_section = name if name in MEM0_MD_SECTIONS else ""
            continue
        if current_section:
            sections[current_section].append(line)

    settings, unmatched_settings = _key_values(sections["settings"])
    retention, unmatched_retention = _key_values(sections["retention"])
    if unmatched_settings:
        log_error(f"忽略无法解析的 mem0.md Settings 条目 count={len(unmatched_settings)}")
    if unmatched_retention:
        retention["rules"] = unmatched_retention[:20]
    return {
        "settings": settings,
        "search": _list_values(sections["search"]),
        "ignore": _list_values(sections["ignore"]),
        "identity": _list_values(sections["identity"]),
        "categories": _list_values(sections["categories"]),
        "retention": retention,
    }


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on", "启用", "是"}:
            return True
        if normalized in {"0", "false", "no", "off", "禁用", "否"}:
            return False
    return None


def _apply_settings_layer(settings: dict[str, Any], raw: Any, source: str) -> None:
    if not isinstance(raw, dict):
        return
    aliases = {
        "top_k": "search_limit",
        "threshold": "confidence_threshold",
        "retention_days": "session_retention_days",
        "retention_session_days": "session_retention_days",
        "days": "session_retention_days",
    }
    unknown = 0
    invalid = 0
    for original_key, value in raw.items():
        key = str(original_key).replace("-", "_").casefold()
        key = aliases.get(key, key)
        if key not in DEFAULT_SETTINGS:
            unknown += 1
            continue
        try:
            if key in {"auto_save", "auto_search", "rerank", "debug"}:
                parsed = _boolean(value)
                if parsed is None:
                    raise ValueError
                settings[key] = parsed
            elif key == "search_limit":
                settings[key] = min(20, max(1, int(value)))
            elif key == "confidence_threshold":
                parsed_float = float(value)
                if not math.isfinite(parsed_float):
                    raise ValueError
                settings[key] = min(1.0, max(0.0, parsed_float))
            elif key == "session_retention_days":
                settings[key] = min(3650, max(0, int(value)))
        except (TypeError, ValueError):
            invalid += 1
    if unknown:
        log_error(f"忽略未知设置 source={source} count={unknown}")
    if invalid:
        log_error(f"忽略非法设置 source={source} count={invalid}")


def load_settings(cwd: str | None, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """按默认值、项目策略、本机设置和环境变量的顺序合并设置。"""
    project_policy = policy if isinstance(policy, dict) else parse_mem0_md(cwd)
    settings = dict(DEFAULT_SETTINGS)
    _apply_settings_layer(settings, project_policy.get("settings", {}), "mem0.md")
    retention = project_policy.get("retention", {})
    if isinstance(retention, dict):
        retention_setting = {
            "session_retention_days": retention.get(
                "session_retention_days",
                retention.get(
                    "retention_session_days",
                    retention.get("retention_days", retention.get("days")),
                ),
            )
        }
        if retention_setting["session_retention_days"] is not None:
            _apply_settings_layer(settings, retention_setting, "mem0.md retention")

    local = load_json_file(settings_path(), {})
    if isinstance(local, dict) and isinstance(local.get("settings"), dict):
        local = local["settings"]
    _apply_settings_layer(settings, local, "settings.json")
    environment = {
        key: os.environ[name]
        for key, name in SETTING_ENV_VARS.items()
        if name in os.environ and os.environ[name].strip()
    }
    _apply_settings_layer(settings, environment, "environment")
    return settings


def debug_event(settings: dict[str, Any], code: str) -> None:
    if settings.get("debug"):
        log_error(f"debug event={code}")


def structured_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("results"), list):
        return [item for item in structured["results"] if isinstance(item, dict)]
    for block in result.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            parsed = json.loads(block.get("text", ""))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
            return [item for item in parsed["results"] if isinstance(item, dict)]
    return []


def format_context(result: dict[str, Any], limit: int = 5) -> str:
    memories: list[str] = []
    for item in structured_results(result)[:limit]:
        text = redact_sensitive(
            SYSTEM_TAG_PATTERN.sub("", str(item.get("memory", "")))
        ).strip()
        if not text:
            continue
        memory_id = str(item.get("id", ""))[:8]
        suffix = f" [mem0:{memory_id}]" if memory_id else ""
        memories.append(f"- {text[:800]}{suffix}")
    if not memories:
        return ""
    return (
        "以下内容来自用户私有的自托管 Mem0，仅作为非权威历史上下文；"
        "当前用户指令始终优先：\n" + "\n".join(memories)
    )


def identity_context(policy: dict[str, Any]) -> str:
    identity = policy.get("identity", [])
    if not isinstance(identity, list):
        return ""
    lines = [redact_sensitive(str(item)).strip()[:300] for item in identity]
    lines = [line for line in lines if line][:8]
    if not lines:
        return ""
    return "当前项目 `mem0.md` 声明的身份约定：\n" + "\n".join(f"- {line}" for line in lines)


def combine_context(memory_context: str, policy: dict[str, Any], scope_notice: str = "") -> str:
    current_identity = identity_context(policy)
    return "\n\n".join(part for part in (scope_notice, current_identity, memory_context) if part)


def _normalized_prompt(prompt: str) -> str:
    return re.sub(r"[^\w]+", "", prompt, flags=re.UNICODE).casefold()


def _matches_ignore_rule(prompt: str, policy: dict[str, Any]) -> bool:
    normalized = prompt.casefold()
    rules = policy.get("ignore", [])
    if not isinstance(rules, list):
        return False
    for rule in rules:
        candidate = redact_sensitive(str(rule)).strip().casefold()
        if len(candidate) >= 2 and candidate in normalized:
            return True
    return False


def should_search_prompt(prompt: str, settings: dict[str, Any], policy: dict[str, Any]) -> bool:
    if not settings.get("auto_search", True):
        return False
    if not prompt.strip() or _matches_ignore_rule(prompt, policy):
        return False
    return _normalized_prompt(prompt) not in TRIVIAL_PROMPTS


def build_search_queries(prompt: str, policy: dict[str, Any]) -> list[str]:
    """为复杂提示生成少量确定性互补查询，避免无界扩张。"""
    clean = redact_sensitive(prompt).strip()[:2_000]
    if not clean:
        return []
    queries: list[str] = []
    if CONTINUATION_PATTERN.search(clean):
        queries.append("项目目标、最近完成事项、未完成工作、当前阻塞和下一步")
    queries.append(clean)

    file_paths = list(dict.fromkeys(FILE_PATTERN.findall(clean)))[:3]
    if file_paths:
        queries.append(f"文件 {'、'.join(file_paths)} 的历史修改、问题、决定和验证结果")

    signals = COMPLEX_PROMPT_PATTERN.findall(clean)
    is_complex = len(clean) >= 120 or clean.count("\n") >= 2 or len(signals) >= 2
    if is_complex:
        queries.append(f"与当前请求相关的既有架构决定、约束、风险和验证结果：{clean}")

    search_rules = policy.get("search", [])
    if isinstance(search_rules, list) and search_rules:
        hints = "；".join(redact_sensitive(str(item)).strip()[:200] for item in search_rules[:6])
        if hints:
            queries.append(f"项目重点：{hints}\n当前请求：{clean}")

    unique: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(query[:2_000])
    return unique[:4]


def search_queries(
    queries: list[str],
    project_id: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    if not queries:
        return {"structuredContent": {"results": []}}
    arguments = {
        "project_id": project_id,
        "top_k": int(settings["search_limit"]),
        "threshold": float(settings["confidence_threshold"]),
        "rerank": bool(settings.get("rerank", True)),
    }

    ordered: dict[int, dict[str, Any]] = {}
    if len(queries) == 1:
        try:
            ordered[0] = call_tool("search_memories", {"query": queries[0], **arguments})
        except Exception as exc:
            log_error(f"自动检索失败 {type(exc).__name__}")
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(queries))) as executor:
            futures = {
                executor.submit(call_tool, "search_memories", {"query": query, **arguments}): index
                for index, query in enumerate(queries)
            }
            for future in as_completed(futures):
                try:
                    ordered[futures[future]] = future.result()
                except Exception as exc:
                    log_error(f"自动检索失败 {type(exc).__name__}")

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in sorted(ordered):
        for item in structured_results(ordered[index]):
            memory_id = str(item.get("id", "")).strip()
            memory_text = str(item.get("memory", "")).strip()
            key = memory_id or hashlib.sha256(memory_text.encode("utf-8")).hexdigest()
            if not memory_text or key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= int(settings["search_limit"]):
                return {"structuredContent": {"results": merged}}
    return {"structuredContent": {"results": merged}}


def emit(event: str, context: str = "") -> None:
    output: dict[str, Any] = {}
    if context:
        output["hookSpecificOutput"] = {
            "hookEventName": event,
            "additionalContext": context,
        }
    print(json.dumps(output, ensure_ascii=True))


def emit_pretool_updated(tool_input: dict[str, Any]) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": tool_input,
                }
            },
            ensure_ascii=True,
        )
    )


def emit_pretool_denied(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=True,
        )
    )


def tail_jsonl(path: str, max_bytes: int = MAX_TRANSCRIPT_BYTES) -> list[dict[str, Any]]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines()[1 if size > max_bytes else 0 :]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def redact_sensitive(text: str) -> str:
    def replace_assignment(match: re.Match[str]) -> str:
        value = match.group("value")
        quote = value[0] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else ""
        return f"{match.group('prefix')}{quote}[已脱敏]{quote}"

    def replace_ip(match: re.Match[str]) -> str:
        candidate = match.group(0).strip("[]").split("%", 1)[0]
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return match.group(0)
        return "[IP已脱敏]"

    value = PRIVATE_KEY_PATTERN.sub("[私钥已脱敏]", text)
    value = AUTH_SECRET_PATTERN.sub(replace_assignment, value)
    value = AUTH_SCHEME_PATTERN.sub("[认证信息已脱敏]", value)
    value = NAMED_SECRET_PATTERN.sub(replace_assignment, value)
    value = URI_CREDENTIAL_PATTERN.sub(r"\g<scheme>[凭据已脱敏]@", value)
    value = STANDALONE_SECRET_PATTERN.sub("[凭据已脱敏]", value)
    for home in {str(Path.home()), str(Path.home()).replace("\\", "/")}:
        if home:
            value = re.sub(re.escape(home), "[用户目录]", value, flags=re.IGNORECASE)
    value = WINDOWS_USER_PATH_PATTERN.sub("[用户目录]", value)
    value = UNIX_USER_PATH_PATTERN.sub("[用户目录]", value)
    value = IPV4_PATTERN.sub(replace_ip, value)
    value = BRACKETED_IPV6_PATTERN.sub(replace_ip, value)
    value = BARE_IPV6_PATTERN.sub(replace_ip, value)
    return value


def message_from_entry(entry: dict[str, Any]) -> tuple[str, str] | None:
    payload = entry.get("payload")
    if entry.get("type") == "response_item" and isinstance(payload, dict):
        if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
            return None
        parts = [
            str(block.get("text", ""))
            for block in payload.get("content", [])
            if isinstance(block, dict) and block.get("type") in {"input_text", "output_text", "text"}
        ]
        text = "\n".join(part for part in parts if part).strip()
        return (str(payload["role"]), text) if text else None
    if entry.get("type") not in {"user", "assistant"}:
        return None
    message = entry.get("message", {})
    content = message.get("content", []) if isinstance(message, dict) else []
    if isinstance(content, str):
        text = content.strip()
    else:
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part).strip()
    return (str(entry["type"]), text) if text else None


def collect_file_paths(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"file_path", "path"} and isinstance(child, str):
                candidate = child.strip()
                if candidate:
                    found.add(candidate[:4096])
            elif key in {"command", "patch"} and isinstance(child, str):
                found.update(FILE_PATTERN.findall(child))
                for match in PATCH_FILE_PATTERN.finditer(child):
                    candidate = match.group(1).strip().strip("\"'")
                    if candidate and candidate != "/dev/null":
                        found.add(candidate[:4096])
            else:
                collect_file_paths(child, found)
    elif isinstance(value, list):
        for child in value:
            collect_file_paths(child, found)


def extract_transcript(path: str) -> tuple[str, list[str]]:
    """提取最近对话和工具触达文件，兼容 Codex 与常见 JSONL 格式。"""
    messages: list[tuple[str, str]] = []
    files: set[str] = set()
    for entry in tail_jsonl(path):
        message = message_from_entry(entry)
        if message:
            role, text = message
            clean = redact_sensitive(SYSTEM_TAG_PATTERN.sub("", text)).strip()
            if clean:
                messages.append((role, clean))
        collect_file_paths(entry, files)
    labels = {"user": "用户", "assistant": "助手"}
    exchange = "\n\n".join(
        f"{labels[role]}：{text}" for role, text in messages[-12:]
    )[-MAX_MEMORY_TEXT:]
    return exchange, sorted(files)[:30]


def extract_compact_summary(path: str) -> str:
    """读取压缩完成后写入转录的最近一条摘要。"""
    for entry in reversed(tail_jsonl(path)):
        payload = entry.get("payload")
        candidates = [entry]
        if isinstance(payload, dict):
            candidates.append(payload)
        compact = next(
            (
                candidate
                for candidate in candidates
                if candidate.get("isCompactSummary") or candidate.get("is_compact_summary")
            ),
            None,
        )
        if not isinstance(compact, dict):
            continue
        message = compact.get("message", compact)
        content = message.get("content", []) if isinstance(message, dict) else []
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(block.get("text", block.get("content", "")))
                for block in content
                if isinstance(block, dict)
                and block.get("type") in {"text", "input_text", "output_text"}
            )
        else:
            text = ""
        clean = redact_sensitive(SYSTEM_TAG_PATTERN.sub("", text)).strip()
        if clean:
            return clean[:MAX_MEMORY_TEXT]
    return ""


def safe_project_files(files: list[str], cwd: str | None) -> list[str]:
    """只保留项目内相对路径，避免把用户目录写入记忆。"""
    root = git_root(cwd).resolve()
    current = Path(cwd or root)
    safe: list[str] = []
    for value in files:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = current / candidate
        try:
            relative_path = candidate.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        parts = [part.casefold() for part in relative_path.parts]
        if any(
            part in {".git", "node_modules", "target", "vendor"} or part.startswith(".env")
            for part in parts
        ):
            continue
        relative = str(relative_path).replace("\\", "/")
        if relative and relative not in safe:
            safe.append(relative)
    return safe[:20]


def session_state_path(session_id: str) -> Path:
    digest = hashlib.sha256((session_id or "unknown").encode("utf-8")).hexdigest()[:24]
    return PLUGIN_DATA / "sessions" / f"{digest}.json"


def load_session_stats(session_id: str) -> dict[str, Any]:
    value = load_json_file(session_state_path(session_id), {})
    return value if isinstance(value, dict) else {}


def update_session_state(
    session_id: str,
    update: Callable[[dict[str, Any]], None],
) -> bool:
    if not session_id:
        return False
    path = session_state_path(session_id)
    lock_path = path.with_suffix(".lock")
    if not wait_for_lock(lock_path, timeout=2, stale_after=10):
        log_error("等待会话状态锁超时")
        return False
    try:
        state = load_session_stats(session_id)
        update(state)
        atomic_write_json(path, state)
        return True
    finally:
        release_lock(lock_path)


def update_session_stats(session_id: str, operation: str) -> None:
    if not session_id:
        return

    def increment(state: dict[str, Any]) -> None:
        operations = state.setdefault("operations", {})
        if not isinstance(operations, dict):
            operations = {}
            state["operations"] = operations
        operations[operation] = int(operations.get(operation, 0)) + 1
        state.setdefault("started", datetime.now(timezone.utc).isoformat())

    update_session_state(session_id, increment)


def summary_was_saved(session_id: str, kind: str, digest: str) -> bool:
    if not session_id:
        return False
    state = load_session_stats(session_id)
    summaries = state.get("summaries", {})
    return isinstance(summaries, dict) and summaries.get(kind) == digest


def mark_summaries_saved(session_id: str, digests: dict[str, str]) -> None:
    if not session_id:
        return

    def mark(state: dict[str, Any]) -> None:
        summaries = state.setdefault("summaries", {})
        if not isinstance(summaries, dict):
            summaries = {}
            state["summaries"] = summaries
        summaries.update(digests)

    update_session_state(session_id, mark)


def should_save_summary(text: str, files: list[str], policy: dict[str, Any]) -> bool:
    if len(text) < 120:
        return False
    message_bodies = [
        _normalized_prompt(line.split("：", 1)[-1])
        for line in text.splitlines()
        if line.strip()
    ]
    meaningful = [body for body in message_bodies if body and body not in TRIVIAL_PROMPTS]
    if not meaningful:
        return False

    retention = policy.get("retention", {})
    if isinstance(retention, dict):
        exclusions: list[str] = []
        for key in ("exclude", "ignore", "rules"):
            value = retention.get(key)
            if isinstance(value, str):
                exclusions.extend(part.strip() for part in re.split(r"[,;，；]", value))
            elif isinstance(value, list):
                exclusions.extend(str(part).strip() for part in value)
        lowered = text.casefold()
        if any(len(rule) >= 2 and rule.casefold() in lowered for rule in exclusions):
            return False

    if files or SUMMARY_SIGNAL_PATTERN.search(text):
        return True
    return len(text) >= 400 and "用户：" in text and "助手：" in text


def summary_categories(policy: dict[str, Any]) -> str:
    categories = policy.get("categories", [])
    if not isinstance(categories, list):
        return ""
    values = [redact_sensitive(str(item)).strip()[:200] for item in categories[:10]]
    values = [value for value in values if value]
    if not values:
        return ""
    return (
        "项目定义了以下长期记忆类别；只在内容确实匹配时使用相应 `[类别]` 前缀：\n"
        + "\n".join(f"- {value}" for value in values)
    )


def retention_days_for(
    memory_type: str,
    settings: dict[str, Any],
    policy: dict[str, Any],
) -> int:
    retention = policy.get("retention", {})
    value = retention.get(memory_type) if isinstance(retention, dict) else None
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"forever", "永久", "永远"}:
            return 0
        match = re.fullmatch(r"(\d+)\s*d(?:ays?)?", normalized)
        if match:
            return min(3650, int(match.group(1)))
    if isinstance(value, int) and not isinstance(value, bool):
        return min(3650, max(0, value))
    return int(settings.get("session_retention_days", 0))


def save_summary(
    text: str,
    project_id: str,
    kind: str,
    hook_input: dict[str, Any],
    files: list[str],
    settings: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    memory_type: str = "session_summary",
    force: bool = False,
) -> None:
    active_settings = settings or dict(DEFAULT_SETTINGS)
    active_policy = policy or {
        "identity": [],
        "categories": [],
        "retention": {},
    }
    if not active_settings.get("auto_save", True):
        debug_event(active_settings, "auto_save_disabled")
        return
    clean = redact_sensitive(text).strip()
    files = safe_project_files(files, hook_input.get("cwd"))
    if not force and not should_save_summary(clean, files, active_policy):
        debug_event(active_settings, "summary_quality_gate")
        return
    session_id = str(hook_input.get("session_id", ""))
    branch = resolve_branch(hook_input.get("cwd"))
    stats = load_session_stats(session_id).get("operations", {})
    details = [f"项目：{project_id}"]
    if branch:
        details.append(f"分支：{branch}")
    if files:
        details.append(f"涉及文件：{', '.join(files[:20])}")
    if isinstance(stats, dict) and stats:
        details.append(
            "本会话 Mem0 操作：" + ", ".join(f"{key}={value}" for key, value in sorted(stats.items()))
        )
    instructions = [
        "请提取并保存适合跨会话复用的长期记忆：用户目标、关键决定、已完成事项、"
        "验证结果、未完成工作和稳定偏好；忽略寒暄、临时日志和敏感凭据。"
    ]
    current_identity = identity_context(active_policy)
    if current_identity:
        instructions.append(current_identity)
    categories = summary_categories(active_policy)
    if categories:
        instructions.append(categories)
    prompt = (
        f"[{kind}]\n" + "\n".join(details) + "\n\n"
        + "\n\n".join(instructions)
        + f"\n\n{clean[:MAX_MEMORY_TEXT]}"
    )[:MAX_MEMORY_TEXT]
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    content_digest = hashlib.sha256(
        (clean + "\0" + "\0".join(files)).encode("utf-8")
    ).hexdigest()
    project_scope = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    kind_key = f"{project_scope}:{kind}"
    content_key = f"{project_scope}:content"
    if summary_was_saved(session_id, kind_key, digest) or summary_was_saved(
        session_id,
        content_key,
        content_digest,
    ):
        return
    metadata: dict[str, Any] = {
        "type": memory_type,
        "confidence": 0.8,
        "codex_hook": "session_start_compact"
        if memory_type == "compact_summary"
        else ("pre_compact" if kind == "上下文压缩前总结" else "stop"),
        "memory_kind": memory_type,
    }
    if session_id:
        metadata["session_id"] = session_id[:200]
    if branch:
        metadata["branch"] = branch
    if files:
        metadata["files_touched"] = files
    arguments: dict[str, Any] = {
        "messages": [{"role": "assistant", "content": prompt}],
        "project_id": project_id,
        "infer": True,
        "metadata": metadata,
    }
    retention_days = retention_days_for(memory_type, active_settings, active_policy)
    if retention_days > 0:
        expiration = datetime.now(timezone.utc) + timedelta(days=retention_days)
        arguments["expiration_date"] = expiration.date().isoformat()
    summary_lock = session_state_path(session_id).with_suffix(".summary.lock") if session_id else None
    if summary_lock and not wait_for_lock(summary_lock, timeout=20, stale_after=30):
        log_error("等待会话摘要锁超时")
        return
    try:
        if session_id and (
            summary_was_saved(session_id, kind_key, digest)
            or summary_was_saved(session_id, content_key, content_digest)
        ):
            return
        call_tool("add_memory", arguments)
        mark_summaries_saved(
            session_id,
            {
                kind_key: digest,
                content_key: content_digest,
            },
        )
    finally:
        if summary_lock:
            release_lock(summary_lock)


def split_import_content(content: str, markdown: bool) -> list[str]:
    raw_chunks: list[str] = []
    if markdown:
        current: list[str] = []
        for line in content.splitlines(keepends=True):
            if line.startswith("## ") and current:
                raw_chunks.append("".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            raw_chunks.append("".join(current).strip())
    else:
        raw_chunks = [content]

    normalized = "\n\n".join(raw.strip() for raw in raw_chunks if raw.strip())
    if len(normalized) < MIN_IMPORT_CHUNK_SIZE:
        return []
    chunk_count = math.ceil(len(normalized) / MAX_IMPORT_CHUNK_SIZE)
    chunk_size = math.ceil(len(normalized) / chunk_count)
    chunks = [
        normalized[start : start + chunk_size].strip()
        for start in range(0, len(normalized), chunk_size)
    ]
    return [chunk for chunk in chunks if chunk]


def import_state_path() -> Path:
    return PLUGIN_DATA / "auto_import_state.json"


def import_scope_key(root: Path, project_id: str) -> str:
    value = (
        os.fsencode(os.path.normcase(str(root)))
        + b"\0"
        + project_id.encode("utf-8")
    )
    return hashlib.sha256(value).hexdigest()


def delete_memories(
    memory_ids: list[str],
    project_id: str,
    progress: Callable[[list[str]], None] | None = None,
) -> bool:
    remaining = list(dict.fromkeys(memory_ids))
    succeeded = True
    for memory_id in list(remaining):
        if not hook_budget_available(HOOK_CLEANUP_RESERVE + 0.1):
            log_error("自动导入清理达到 Hook 总时间预算")
            return False
        try:
            call_tool("delete_memory", {"memory_id": memory_id, "project_id": project_id})
        except Exception as exc:
            succeeded = False
            log_error(f"清理旧导入记忆失败 {type(exc).__name__}")
            continue
        remaining.remove(memory_id)
        if progress is not None:
            progress(list(remaining))
    return succeeded


def find_import_memories(
    filename: str,
    file_hash: str,
    project_id: str,
    format_version: int | None = IMPORT_FORMAT_VERSION,
) -> list[tuple[str, str]]:
    """只返回正文带有精确插件标记的导入记忆。"""
    result = call_tool(
        "search_memories",
        {
            "query": (
                f"[mem0:auto-import] 项目 {project_id} 来源文件 {filename} 内容哈希 {file_hash}"
                + (f" 导入格式 {format_version}" if format_version is not None else "")
            ),
            "project_id": project_id,
            "top_k": IMPORT_SEARCH_BATCH_SIZE,
            "threshold": 0.0,
        },
    )
    memories: list[tuple[str, str]] = []
    for item in structured_results(result):
        text = str(item.get("memory", ""))
        lines = set(text.splitlines())
        memory_id = item.get("id")
        if (
            isinstance(memory_id, str)
            and "[mem0:auto-import]" in lines
            and f"项目：{project_id}" in lines
            and f"来源文件：{filename}" in lines
            and f"内容哈希：{file_hash}" in lines
            and (format_version is None or f"导入格式：{format_version}" in lines)
        ):
            memories.append((memory_id, text))
    return list(dict.fromkeys(memories))


def find_import_memory_ids(
    filename: str,
    file_hash: str,
    project_id: str,
    format_version: int | None = IMPORT_FORMAT_VERSION,
) -> list[str]:
    return [
        memory_id
        for memory_id, _ in find_import_memories(
            filename,
            file_hash,
            project_id,
            format_version,
        )
    ]


def complete_import_chunk_ids(
    memories: list[tuple[str, str]],
    expected_chunks: int,
) -> list[str]:
    """按分块序号验证完整覆盖，重复序号不能冒充缺失分块。"""
    by_index: dict[int, str] = {}
    for memory_id, text in memories:
        marker = re.search(r"(?m)^分块：(\d+)/(\d+)\s*$", text)
        if not marker or int(marker.group(2)) != expected_chunks:
            continue
        index = int(marker.group(1))
        if 1 <= index <= expected_chunks:
            by_index.setdefault(index, memory_id)
    if set(by_index) != set(range(1, expected_chunks + 1)):
        return []
    return [by_index[index] for index in range(1, expected_chunks + 1)]


def try_find_import_memories(
    filename: str,
    file_hash: str,
    project_id: str,
    format_version: int | None = IMPORT_FORMAT_VERSION,
) -> list[tuple[str, str]] | None:
    try:
        return find_import_memories(filename, file_hash, project_id, format_version)
    except Exception as exc:
        log_error(f"核对自动导入记忆失败 {type(exc).__name__}")
        return None


def try_find_import_memory_ids(
    filename: str,
    file_hash: str,
    project_id: str,
    format_version: int | None = IMPORT_FORMAT_VERSION,
) -> list[str] | None:
    memories = try_find_import_memories(filename, file_hash, project_id, format_version)
    if memories is None:
        return None
    return [memory_id for memory_id, _ in memories]


def delete_import_memories(
    filename: str,
    file_hash: str,
    project_id: str,
    format_version: int | None = IMPORT_FORMAT_VERSION,
    exclude_ids: list[str] | None = None,
    progress: Callable[[list[str]], None] | None = None,
) -> bool:
    """分批搜索并删除精确匹配的导入记忆，直到远端结果收敛。"""
    excluded = set(exclude_ids or [])
    seen_batches: set[tuple[str, ...]] = set()
    while hook_budget_available(HOOK_CLEANUP_RESERVE + 0.1):
        found_ids = try_find_import_memory_ids(
            filename,
            file_hash,
            project_id,
            format_version=format_version,
        )
        if found_ids is None:
            return False
        memory_ids = list(
            dict.fromkeys(memory_id for memory_id in found_ids if memory_id not in excluded)
        )
        if not memory_ids:
            return True
        batch = tuple(memory_ids)
        if batch in seen_batches:
            log_error("自动导入清理搜索结果未能收敛")
            return False
        seen_batches.add(batch)
        if progress is not None:
            progress(memory_ids)
        if not delete_memories(memory_ids, project_id, progress=progress):
            return False
    log_error("自动导入清理达到 Hook 总时间预算")
    return False


def add_import_chunk(text: str, project_id: str) -> None:
    call_tool("add_memory", {"text": text, "project_id": project_id, "infer": False})


def _lock_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _try_lock(handle: IO[bytes]) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: IO[bytes]) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_lock(path: Path, stale_after: int = 120) -> bool:
    """使用操作系统文件锁；进程退出时由系统自动释放。"""
    del stale_after
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _lock_key(path)
    with LOCK_HANDLES_GUARD:
        if key in LOCK_HANDLES:
            return False
    handle: IO[bytes] | None = None
    try:
        handle = path.open("a+b")
        if os.name == "nt" and path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        _try_lock(handle)
    except OSError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        return False
    with LOCK_HANDLES_GUARD:
        if key not in LOCK_HANDLES:
            LOCK_HANDLES[key] = handle
            return True
    try:
        _unlock(handle)
    finally:
        handle.close()
    return False


def wait_for_lock(path: Path, timeout: float, stale_after: int = 120) -> bool:
    deadline = time.monotonic() + timeout
    if HOOK_DEADLINE is not None:
        deadline = min(deadline, HOOK_DEADLINE)
    while time.monotonic() < deadline:
        if acquire_lock(path, stale_after):
            return True
        time.sleep(0.05)
    return False


def release_lock(path: Path) -> None:
    key = _lock_key(path)
    with LOCK_HANDLES_GUARD:
        handle = LOCK_HANDLES.pop(key, None)
    if handle is None:
        return
    try:
        _unlock(handle)
    except OSError:
        pass
    finally:
        handle.close()


def save_import_scope_state(scope: str, scope_state: dict[str, Any]) -> None:
    """合并写入单个项目范围，避免并发覆盖其他项目状态。"""
    lock_path = PLUGIN_DATA / "auto_import_state.lock"
    if not wait_for_lock(lock_path, timeout=5):
        raise TimeoutError("等待自动导入状态锁超时")
    try:
        state = load_json_file(import_state_path(), {})
        if not isinstance(state, dict):
            state = {}
        if scope_state:
            state[scope] = scope_state
        else:
            state.pop(scope, None)
        atomic_write_json(import_state_path(), state)
    finally:
        release_lock(lock_path)


def state_memory_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            memory_id
            for memory_id in value
            if isinstance(memory_id, str) and memory_id
        )
    )


def save_import_id_progress(
    scope: str,
    scope_state: dict[str, Any],
    filename: str,
    field: str,
    memory_ids: list[str],
) -> None:
    entry = scope_state.get(filename)
    if not isinstance(entry, dict):
        return
    entry[field] = memory_ids
    save_import_scope_state(scope, scope_state)


def _auto_import_project_files(cwd: str | None, project_id: str) -> None:
    """导入声明式项目文件，并用本地哈希避免重复写入。"""
    root = git_root(cwd)
    current = Path(cwd or root).resolve()
    search_dirs = [current]
    if root != current:
        search_dirs.append(root)
    state = load_json_file(import_state_path(), {})
    if not isinstance(state, dict):
        state = {}
    scope = import_scope_key(root, project_id)
    scope_state = state.setdefault(scope, {})
    if not isinstance(scope_state, dict):
        scope_state = {}
        state[scope] = scope_state
    changed = False

    for filename in TARGET_PROJECT_FILES:
        if not hook_budget_available(HOOK_CLEANUP_RESERVE + 0.25):
            log_error("自动导入达到 Hook 总时间预算，将在下次启动继续")
            break
        path = next((directory / filename for directory in search_dirs if (directory / filename).is_file()), None)
        previous = scope_state.get(filename, {})
        had_previous = filename in scope_state
        previous_hash = previous.get("sha256", "") if isinstance(previous, dict) else ""
        previous_format = previous.get("format_version") if isinstance(previous, dict) else None
        previous_ids = state_memory_ids(previous.get("memory_ids", [])) if isinstance(previous, dict) else []
        pending = bool(previous.get("pending")) if isinstance(previous, dict) else False
        fallback_hash = previous.get("previous_hash", "") if pending else ""
        fallback_format = previous.get("previous_format_version") if pending else None
        fallback_ids = (
            state_memory_ids(previous.get("previous_memory_ids", []))
            if pending and isinstance(previous, dict)
            else []
        )
        raw: bytes | None = None
        if path is not None:
            try:
                with path.open("rb") as handle:
                    raw = handle.read(MAX_IMPORT_FILE_SIZE + 1)
            except OSError:
                continue
        too_large = raw is not None and len(raw) > MAX_IMPORT_FILE_SIZE
        if path is None or too_large:
            if too_large:
                log_error(f"停止导入过大的 {filename}")
            cleanup_targets = [
                (file_hash, format_version, field)
                for file_hash, format_version, field in (
                    (previous_hash, previous_format, "memory_ids"),
                    (
                        fallback_hash,
                        fallback_format,
                        "previous_memory_ids",
                    ),
                )
                if isinstance(file_hash, str) and file_hash
            ]
            cleanup_succeeded = (
                not isinstance(previous, dict) or not previous or bool(cleanup_targets)
            )
            for old_hash, old_format, field in cleanup_targets:
                if not delete_import_memories(
                    filename,
                    old_hash,
                    project_id,
                    format_version=old_format if isinstance(old_format, int) else None,
                    progress=lambda remaining, field=field, filename=filename: save_import_id_progress(
                        scope,
                        scope_state,
                        filename,
                        field,
                        remaining,
                    ),
                ):
                    cleanup_succeeded = False
            if cleanup_succeeded and had_previous:
                scope_state.pop(filename, None)
                changed = True
            continue
        if raw is None:
            continue
        file_hash = hashlib.sha256(raw).hexdigest()
        cleanup_hash = ""
        cleanup_format: int | None = None
        cleanup_ids: list[str] = []
        if (
            isinstance(previous, dict)
            and previous.get("sha256") == file_hash
            and previous.get("format_version") == IMPORT_FORMAT_VERSION
        ):
            existing_memories = try_find_import_memories(filename, file_hash, project_id)
            if existing_memories is None:
                continue
            existing_ids = [memory_id for memory_id, _ in existing_memories]
            expected_chunks = previous.get("chunks", 1)
            if not isinstance(expected_chunks, int) or expected_chunks < 1:
                expected_chunks = 1
            complete_ids = complete_import_chunk_ids(existing_memories, expected_chunks)
            if complete_ids:
                extra_ids = [
                    memory_id for memory_id in existing_ids if memory_id not in complete_ids
                ]
                if previous.get("memory_ids") != complete_ids:
                    previous["memory_ids"] = complete_ids
                    save_import_scope_state(scope, scope_state)
                if extra_ids and not delete_import_memories(
                    filename,
                    file_hash,
                    project_id,
                    exclude_ids=complete_ids,
                ):
                    continue
                if (
                    isinstance(fallback_hash, str)
                    and fallback_hash
                    and not delete_import_memories(
                        filename,
                        fallback_hash,
                        project_id,
                        format_version=fallback_format if isinstance(fallback_format, int) else None,
                        exclude_ids=complete_ids,
                        progress=lambda remaining, filename=filename: save_import_id_progress(
                            scope,
                            scope_state,
                            filename,
                            "previous_memory_ids",
                            remaining,
                        ),
                    )
                ):
                    continue
                if previous.get("memory_ids") != complete_ids or previous.get("pending"):
                    previous["memory_ids"] = complete_ids
                    previous.pop("pending", None)
                    previous.pop("previous_hash", None)
                    previous.pop("previous_format_version", None)
                    previous.pop("previous_memory_ids", None)
                    changed = True
                continue
            if not delete_import_memories(
                filename,
                file_hash,
                project_id,
                progress=lambda remaining, filename=filename: save_import_id_progress(
                    scope,
                    scope_state,
                    filename,
                    "memory_ids",
                    remaining,
                ),
            ):
                continue
            if isinstance(fallback_hash, str) and fallback_hash:
                cleanup_hash = fallback_hash
                cleanup_format = fallback_format if isinstance(fallback_format, int) else None
                cleanup_ids = list(fallback_ids)
        else:
            if pending and isinstance(previous_hash, str) and previous_hash:
                if not delete_import_memories(
                    filename,
                    previous_hash,
                    project_id,
                    format_version=previous_format if isinstance(previous_format, int) else None,
                    progress=lambda remaining, filename=filename: save_import_id_progress(
                        scope,
                        scope_state,
                        filename,
                        "memory_ids",
                        remaining,
                    ),
                ):
                    continue
                cleanup_hash = fallback_hash if isinstance(fallback_hash, str) else ""
                cleanup_format = fallback_format if isinstance(fallback_format, int) else None
                cleanup_ids = list(fallback_ids)
            elif isinstance(previous_hash, str) and previous_hash:
                cleanup_hash = previous_hash
                cleanup_format = previous_format if isinstance(previous_format, int) else None
                cleanup_ids = list(previous_ids)
                if cleanup_hash == file_hash and cleanup_format is None:
                    old_ids = try_find_import_memory_ids(
                        filename,
                        cleanup_hash,
                        project_id,
                        format_version=None,
                    )
                    if old_ids is None:
                        continue
                    cleanup_ids = list(dict.fromkeys(old_ids))
        content = redact_sensitive(raw.decode("utf-8", errors="replace"))
        chunks = split_import_content(content, filename.lower().endswith(".md"))
        if (
            not chunks
            and cleanup_hash
            and not delete_import_memories(
                filename,
                cleanup_hash,
                project_id,
                format_version=cleanup_format,
                progress=lambda remaining, filename=filename: save_import_id_progress(
                    scope,
                    scope_state,
                    filename,
                    "memory_ids",
                    remaining,
                ),
            )
        ):
            continue
        if not chunks:
            if filename in scope_state:
                scope_state.pop(filename, None)
                changed = True
            continue
        branch = resolve_branch(str(root))
        memories = [
            "\n".join(
                filter(
                    None,
                    [
                        "[mem0:auto-import]",
                        f"项目：{project_id}",
                        f"来源文件：{filename}",
                        f"内容哈希：{file_hash}",
                        f"导入格式：{IMPORT_FORMAT_VERSION}",
                        f"分块：{index}/{len(chunks)}",
                        f"分支：{branch}" if branch else "",
                        "",
                        chunk,
                    ],
                )
            )
            for index, chunk in enumerate(chunks, 1)
        ]
        scope_state[filename] = {
            "sha256": file_hash,
            "format_version": IMPORT_FORMAT_VERSION,
            "memory_ids": [],
            "chunks": len(chunks),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pending": True,
        }
        if cleanup_hash:
            scope_state[filename]["previous_hash"] = cleanup_hash
            if cleanup_format is not None:
                scope_state[filename]["previous_format_version"] = cleanup_format
            if cleanup_ids:
                scope_state[filename]["previous_memory_ids"] = cleanup_ids
        save_import_scope_state(scope, scope_state)
        with ThreadPoolExecutor(max_workers=min(4, len(memories))) as executor:
            futures = [executor.submit(add_import_chunk, memory, project_id) for memory in memories]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    log_error(f"自动导入 {filename} 失败 {type(exc).__name__}")
        new_memories = try_find_import_memories(filename, file_hash, project_id)
        if new_memories is None:
            continue
        all_new_ids = [memory_id for memory_id, _ in new_memories]
        new_ids = complete_import_chunk_ids(new_memories, len(memories))
        if not new_ids:
            log_error(f"自动导入 {filename} 未能验证全部分块")
            continue
        extra_ids = [memory_id for memory_id in all_new_ids if memory_id not in new_ids]
        scope_state[filename]["memory_ids"] = new_ids
        save_import_scope_state(scope, scope_state)
        if extra_ids and not delete_import_memories(
            filename,
            file_hash,
            project_id,
            exclude_ids=new_ids,
        ):
            continue
        if (
            cleanup_hash
            and not delete_import_memories(
                filename,
                cleanup_hash,
                project_id,
                format_version=cleanup_format,
                exclude_ids=new_ids,
                progress=lambda remaining, filename=filename: save_import_id_progress(
                    scope,
                    scope_state,
                    filename,
                    "previous_memory_ids",
                    remaining,
                ),
            )
        ):
            continue
        scope_state[filename] = {
            "sha256": file_hash,
            "format_version": IMPORT_FORMAT_VERSION,
            "memory_ids": list(dict.fromkeys(new_ids)),
            "chunks": len(chunks),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        changed = True

    if changed:
        save_import_scope_state(scope, scope_state)


def auto_import_project_files(cwd: str | None, project_id: str) -> None:
    scope = import_scope_key(git_root(cwd), project_id)[:24]
    lock_path = PLUGIN_DATA / f"auto_import.{scope}.lock"
    if not acquire_lock(lock_path):
        return
    try:
        _auto_import_project_files(cwd, project_id)
    finally:
        release_lock(lock_path)


def normalized_tool_name(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1]


def tool_paths(tool_input: Any) -> list[str]:
    found: set[str] = set()
    collect_file_paths(tool_input, found)
    return sorted(found)


def is_managed_memory_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    parts = [part for part in normalized.split("/") if part]
    for marker in (".codex", ".claude"):
        if marker not in parts:
            continue
        marker_index = parts.index(marker)
        tail = parts[marker_index + 1 :]
        if "memory" in tail or "memories" in tail or (tail and tail[-1] == "memory.md"):
            return True
    return False


def file_context(
    path: str,
    cwd: str | None,
    project_id: str,
    settings: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> str:
    active_settings = settings or dict(DEFAULT_SETTINGS)
    active_policy = policy or {"ignore": []}
    if not active_settings.get("auto_search", True):
        return ""
    if not path:
        return ""
    root = git_root(cwd).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(cwd or root) / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
        if not resolved.is_file() or resolved.stat().st_size < 1500:
            return ""
    except (OSError, ValueError):
        return ""
    relative = str(resolved.relative_to(root)).replace("\\", "/")
    if any(
        part.casefold() in {".git", "node_modules", "target", "vendor"}
        for part in resolved.parts
    ):
        return ""
    if resolved.name.casefold().startswith(".env"):
        return ""
    if _matches_ignore_rule(relative, active_policy):
        return ""
    file_settings = dict(active_settings)
    file_settings["search_limit"] = min(5, int(active_settings["search_limit"]))
    file_settings["confidence_threshold"] = max(
        0.3,
        float(active_settings["confidence_threshold"]),
    )
    result = search_queries(
        [f"文件 {relative} {resolved.name} 的历史修改、问题和决定"],
        project_id,
        file_settings,
    )
    context = format_context(result, limit=int(file_settings["search_limit"]))
    if not context:
        return ""
    return f"读取 `{relative}` 前找到相关历史：\n{context}"


def flatten_text(value: Any, limit: int = 30_000) -> str:
    parts: list[str] = []
    remaining = max(0, limit)
    stack = [value]
    while stack and remaining > 0:
        item = stack.pop()
        if isinstance(item, str):
            separator = 1 if parts else 0
            if remaining <= separator:
                break
            part = item[: remaining - separator]
            parts.append(part)
            remaining -= len(part) + separator
        elif isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))
    return "\n".join(parts)


def error_signature(tool_input: Any, tool_response: Any) -> str:
    command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""
    if re.search(r"\bgit\s+(?:commit|merge|rebase)\b", command):
        return ""
    response = redact_sensitive(flatten_text(tool_response))
    if len(response) < 50:
        return ""
    patterns = (
        r"Traceback \(most recent call last\)",
        r"\bpanic:\s*.+",
        r"\bFATAL:\s*.+",
        r"\berror\[E\d+\].+",
        r"\b(?:Error|Exception|FAIL):\s*.+",
        r"(?:Exit code|exit status|process exited with code)\s*[:=]?\s*[1-9]\d*",
    )
    for pattern in patterns:
        match = re.search(pattern, response, re.IGNORECASE)
        if match:
            line_start = response.rfind("\n", 0, match.start()) + 1
            line_end = response.find("\n", match.end())
            line = response[line_start : line_end if line_end >= 0 else None].strip()
            return line[:400]
    return ""


def handle_pre_tool(
    hook_input: dict[str, Any],
    project_id: str,
    settings: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> None:
    tool_name = str(hook_input.get("tool_name", ""))
    tool_input = hook_input.get("tool_input", {})
    operation = normalized_tool_name(tool_name)
    if operation in MEM0_TOOL_NAMES and isinstance(tool_input, dict):
        if operation == "add_memory":
            updated = dict(tool_input)
            changed = False
            if not updated.get("project_id"):
                updated["project_id"] = project_id
                changed = True
            metadata = updated.get("metadata")
            if metadata is None:
                metadata = {}
            if isinstance(metadata, dict):
                normalized_metadata = dict(metadata)
                defaults: dict[str, Any] = {
                    "type": "task_learning",
                    "confidence": 0.7,
                    "codex_origin": "tool",
                }
                session_id = str(hook_input.get("session_id", "")).strip()
                if session_id:
                    defaults["session_id"] = session_id[:200]
                for key, value in defaults.items():
                    if key not in normalized_metadata:
                        normalized_metadata[key] = value
                        changed = True
                if normalized_metadata != metadata:
                    updated["metadata"] = normalized_metadata
                if normalized_metadata.get("confidence") == 1.0 and "infer" not in updated:
                    updated["infer"] = False
                    changed = True
            if changed:
                emit_pretool_updated(updated)
            else:
                emit("PreToolUse")
            return
        should_add_project = operation not in {"list_entities", "delete_entities"}
        if operation == "delete_entities":
            should_add_project = tool_input.get("entity_type") == "run"
        if should_add_project and not tool_input.get("project_id"):
            updated = dict(tool_input)
            updated["project_id"] = project_id
            emit_pretool_updated(updated)
        else:
            emit("PreToolUse")
        return
    if tool_name in {"apply_patch", "Edit", "Write", "MultiEdit"}:
        blocked = next((path for path in tool_paths(tool_input) if is_managed_memory_path(path)), "")
        if blocked:
            emit_pretool_denied(
                "禁止直接写入托管记忆文件；请使用自托管 Mem0 的 add_memory 工具保存长期记忆。"
            )
        else:
            emit("PreToolUse")
        return
    if tool_name == "Read" or normalized_tool_name(tool_name) in {"read_file", "read_text_file"}:
        path = tool_paths(tool_input)
        emit(
            "PreToolUse",
            file_context(
                path[0] if path else "",
                hook_input.get("cwd"),
                project_id,
                settings,
                policy,
            ),
        )
        return
    emit("PreToolUse")


def handle_post_tool(
    hook_input: dict[str, Any],
    project_id: str,
    settings: dict[str, Any] | None = None,
) -> None:
    active_settings = settings or dict(DEFAULT_SETTINGS)
    tool_name = str(hook_input.get("tool_name", ""))
    operation = normalized_tool_name(tool_name)
    if operation in MEM0_TOOL_NAMES:
        update_session_stats(str(hook_input.get("session_id", "")), operation)
        emit("PostToolUse")
        return
    if tool_name != "Bash":
        emit("PostToolUse")
        return
    signature = error_signature(hook_input.get("tool_input", {}), hook_input.get("tool_response", {}))
    if not signature or not active_settings.get("auto_search", True):
        emit("PostToolUse")
        return
    error_settings = dict(active_settings)
    error_settings["search_limit"] = min(3, int(active_settings["search_limit"]))
    result = search_queries([signature], project_id, error_settings)
    history = format_context(result, limit=3)
    context = f"检测到命令错误：{signature}"
    if history:
        context += f"\n\n可能相关的历史解决记录：\n{history}"
    emit("PostToolUse", context)


def _handle_event(hook_input: dict[str, Any]) -> None:
    event = str(hook_input.get("hook_event_name", ""))
    project_id = resolve_project_id(hook_input.get("cwd"))
    policy = parse_mem0_md(hook_input.get("cwd"))
    settings = load_settings(hook_input.get("cwd"), policy)

    if event == "PreToolUse":
        handle_pre_tool(hook_input, project_id, settings, policy)
        return

    if event == "PostToolUse":
        handle_post_tool(hook_input, project_id, settings)
        return

    if event == "SessionStart":
        source = str(hook_input.get("source", ""))
        scope_notice = project_scope_notice(hook_input.get("cwd"))
        if source in {"startup", "clear"}:
            try:
                auto_import_project_files(hook_input.get("cwd"), project_id)
            except Exception as exc:
                log_error(f"自动导入项目资料失败 {type(exc).__name__}")
        if source == "compact" and settings.get("auto_save", True):
            transcript = str(hook_input.get("transcript_path", "")).strip()
            compact_summary = extract_compact_summary(transcript) if transcript else ""
            if compact_summary:
                save_summary(
                    compact_summary,
                    project_id,
                    "上下文压缩总结",
                    hook_input,
                    [],
                    settings,
                    policy,
                    memory_type="compact_summary",
                    force=True,
                )
        if not settings.get("auto_search", True):
            debug_event(settings, "session_search_disabled")
            emit(event, combine_context("", policy, scope_notice))
            return
        queries = build_search_queries(
            "项目目标、关键架构决定、最近完成事项、待办和用户稳定偏好",
            policy,
        )
        result = search_queries(queries, project_id, settings)
        update_session_stats(str(hook_input.get("session_id", "")), "search_memories")
        emit(
            event,
            combine_context(
                format_context(result, limit=int(settings["search_limit"])),
                policy,
                scope_notice,
            ),
        )
        return

    if event == "UserPromptSubmit":
        prompt = redact_sensitive(str(hook_input.get("prompt", ""))).strip()
        if not should_search_prompt(prompt, settings, policy):
            debug_event(settings, "prompt_search_skipped")
            emit(event)
            return
        result = search_queries(build_search_queries(prompt, policy), project_id, settings)
        update_session_stats(str(hook_input.get("session_id", "")), "search_memories")
        emit(event, format_context(result, limit=int(settings["search_limit"])))
        return

    if event in {"Stop", "PreCompact"}:
        transcript = str(hook_input.get("transcript_path", "")).strip()
        exchange, files = extract_transcript(transcript) if transcript else ("", [])
        if not exchange:
            exchange = redact_sensitive(str(hook_input.get("last_assistant_message", ""))).strip()
        kind = "本轮会话总结" if event == "Stop" else "上下文压缩前总结"
        save_summary(exchange, project_id, kind, hook_input, files, settings, policy)
        emit(event)
        return

    emit(event or "SessionStart")


def handle_event(hook_input: dict[str, Any]) -> None:
    global HOOK_DEADLINE
    event = str(hook_input.get("hook_event_name", ""))
    budget = HOOK_TIME_BUDGETS.get(event)
    previous_deadline = HOOK_DEADLINE
    if budget is not None:
        deadline = time.monotonic() + budget
        HOOK_DEADLINE = min(previous_deadline, deadline) if previous_deadline is not None else deadline
    try:
        _handle_event(hook_input)
    finally:
        HOOK_DEADLINE = previous_deadline


def _schema_values(schema: Any, key: str) -> list[Any]:
    values: list[Any] = []
    if isinstance(schema, dict):
        value = schema.get(key)
        if key == "type" and isinstance(value, str):
            values.append(value)
        elif key == "enum" and isinstance(value, list):
            values.extend(value)
        for child_key in ("anyOf", "oneOf", "allOf"):
            children = schema.get(child_key, [])
            if isinstance(children, list):
                for child in children:
                    values.extend(_schema_values(child, key))
    return list(dict.fromkeys(values))


def canonical_tool_contract(tools: list[dict[str, Any]]) -> dict[str, Any]:
    contract: dict[str, Any] = {}
    annotation_names = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    for tool in tools:
        name = str(tool.get("name", "")).strip()
        if not name:
            continue
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if not isinstance(properties, dict):
            properties = {}
        annotations = tool.get("annotations", {})
        if not isinstance(annotations, dict):
            annotations = {}
        enums: dict[str, list[Any]] = {}
        types: dict[str, list[str]] = {}
        defaults: dict[str, Any] = {}
        for property_name, property_schema in properties.items():
            enum_values = _schema_values(property_schema, "enum")
            if enum_values:
                enums[str(property_name)] = enum_values
            type_values = _schema_values(property_schema, "type")
            if type_values:
                types[str(property_name)] = sorted(str(value) for value in type_values)
            if isinstance(property_schema, dict) and "default" in property_schema:
                defaults[str(property_name)] = property_schema["default"]
        required = schema.get("required", []) if isinstance(schema, dict) else []
        contract[name] = {
            "properties": sorted(str(property_name) for property_name in properties),
            "required": sorted(str(property_name) for property_name in required)
            if isinstance(required, list)
            else [],
            "types": dict(sorted(types.items())),
            "defaults": dict(sorted(defaults.items())),
            "enums": dict(sorted(enums.items())),
            "annotations": {annotation: annotations.get(annotation) for annotation in annotation_names},
        }
    return dict(sorted(contract.items()))


def contract_differences(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    differences: list[str] = []
    expected_names = set(expected)
    actual_names = set(actual)
    if expected_names - actual_names:
        differences.append("缺少工具=" + ",".join(sorted(expected_names - actual_names)))
    if actual_names - expected_names:
        differences.append("新增工具=" + ",".join(sorted(actual_names - expected_names)))
    for name in sorted(expected_names & actual_names):
        for field in ("properties", "required", "types", "defaults", "enums", "annotations"):
            if expected[name].get(field) != actual[name].get(field):
                differences.append(f"{name}.{field}")
    return differences


def self_test() -> None:
    result = mcp_request("tools/list", {})
    tools = [tool for tool in result.get("tools", []) if isinstance(tool, dict)]
    available = {str(tool.get("name")) for tool in tools}
    missing = sorted(MEM0_TOOL_NAMES - available)
    if missing:
        raise RuntimeError(f"自托管 MCP 缺少工具：{', '.join(missing)}")
    snapshot = load_json_file(SCHEMA_SNAPSHOT_PATH, {})
    expected = snapshot.get("tools", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(expected, dict) or not expected:
        raise RuntimeError("插件缺少有效的 MCP 契约快照")
    actual = canonical_tool_contract(tools)
    differences = contract_differences(expected, actual)
    if differences:
        raise RuntimeError("生产 MCP 契约漂移：" + "；".join(differences))
    capabilities: dict[str, list[str]] = {}
    for tool in tools:
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        capabilities[str(tool.get("name"))] = sorted(properties) if isinstance(properties, dict) else []
    load_connection()
    print(
        json.dumps(
            {
                "状态": "通过",
                "地址": "已安全配置",
                "工具": sorted(MEM0_TOOL_NAMES),
                "参数": capabilities,
                "契约": "与快照一致",
            },
            ensure_ascii=True,
        )
    )


def main() -> int:
    if "--init-settings" in sys.argv or "--show-settings" in sys.argv:
        try:
            cwd = None
            if "--cwd" in sys.argv:
                index = sys.argv.index("--cwd")
                cwd = sys.argv[index + 1]
            created = initialize_settings() if "--init-settings" in sys.argv else False
            policy = parse_mem0_md(cwd)
            print(
                json.dumps(
                    {
                        "状态": "已创建" if created else "当前设置",
                        "设置": load_settings(cwd, policy),
                    },
                    ensure_ascii=True,
                )
            )
            return 0
        except IndexError:
            print(
                json.dumps({"状态": "失败", "错误": "--cwd 缺少路径"}, ensure_ascii=True),
                file=sys.stderr,
            )
            return 2

    if "--set-project" in sys.argv or "--clear-project" in sys.argv or "--current-project" in sys.argv:
        try:
            cwd = None
            if "--cwd" in sys.argv:
                index = sys.argv.index("--cwd")
                cwd = sys.argv[index + 1]
            if "--set-project" in sys.argv:
                index = sys.argv.index("--set-project")
                project_id = set_project_mapping(cwd, sys.argv[index + 1])
                action = "已设置"
            elif "--clear-project" in sys.argv:
                project_id = set_project_mapping(cwd, None)
                action = "已恢复自动识别"
            else:
                project_id = resolve_project_id(cwd)
                action = "当前范围"
            print(json.dumps({"状态": action, "project_id": project_id}, ensure_ascii=True))
            return 0
        except (IndexError, OSError, TimeoutError, ValueError) as exc:
            error = redact_sensitive(str(exc)) or type(exc).__name__
            print(json.dumps({"状态": "失败", "错误": error}, ensure_ascii=True), file=sys.stderr)
            return 2

    if "--check" in sys.argv:
        try:
            self_test()
            return 0
        except Exception as exc:
            error = f"{type(exc).__name__}: {redact_sensitive(str(exc))}"
            log_error(error)
            print(
                json.dumps(
                    {"状态": "失败", "错误": error},
                    ensure_ascii=True,
                ),
                file=sys.stderr,
            )
            return 1

    try:
        hook_input = load_hook_input(sys.stdin)
        if not isinstance(hook_input, dict):
            raise ValueError("钩子输入必须是 JSON 对象")
        handle_event(hook_input)
        return 0
    except Exception as exc:
        log_error(f"钩子执行失败 {type(exc).__name__}")
        event = ""
        try:
            event = str(locals().get("hook_input", {}).get("hook_event_name", ""))
        except Exception:
            pass
        emit(event or "Stop")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
