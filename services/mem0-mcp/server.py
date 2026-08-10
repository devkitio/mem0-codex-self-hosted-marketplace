import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mem0-mcp")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _secret(name: str, minimum_length: int = 32, maximum_length: int = 512) -> str:
    file_value = os.environ.get(f"{name}_FILE", "").strip()
    if file_value:
        try:
            value = Path(file_value).read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"无法读取 {name} 的 secret 文件") from exc
        if len(value) > 8192:
            raise RuntimeError(f"{name} 的 secret 文件超过大小限制")
        value = value.strip()
    else:
        value = os.environ.get(name, "").strip()
    if len(value) > maximum_length:
        raise RuntimeError(f"{name} 不得超过 {maximum_length} 个字符")
    if value and len(value) < minimum_length:
        raise RuntimeError(f"{name} 必须至少包含 {minimum_length} 个字符")
    if value and any(not 33 <= ord(character) <= 126 for character in value):
        raise RuntimeError(f"{name} 必须仅包含可见 ASCII 字符")
    return value


def _validate_runtime_secrets(
    internal_key: str,
    confirmation_secret: str,
    project_scope_secret: str,
) -> None:
    secrets = (internal_key, confirmation_secret, project_scope_secret)
    if not all(secrets):
        raise RuntimeError(
            "必须配置 MEM0_INTERNAL_SERVICE_KEY、MCP_CONFIRMATION_SECRET 和 MCP_PROJECT_SCOPE_SECRET"
        )
    if any(
        hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
        for index, left in enumerate(secrets)
        for right in secrets[index + 1 :]
    ):
        raise RuntimeError("内部服务、删除确认与项目范围 Secret 必须两两不同")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc
    return max(minimum, min(value, maximum))


def _csv_env(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def _identity_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise RuntimeError(f"{name} 必须是有效且非空的内部身份")
    return value


MEM0_BASE_URL = os.environ.get("MEM0_BASE_URL", "http://mem0:8000").rstrip("/")
INTERNAL_SERVICE_KEY = _secret("MEM0_INTERNAL_SERVICE_KEY")
CONFIRMATION_SECRET = _secret("MCP_CONFIRMATION_SECRET")
PROJECT_SCOPE_SECRET = _secret("MCP_PROJECT_SCOPE_SECRET")
DEFAULT_USER_ID = _identity_env("MEM0_DEFAULT_USER_ID")
OWNER = _identity_env("MCP_OWNER")
TIMEOUT = max(5.0, min(float(os.environ.get("MEM0_TIMEOUT_SECONDS", "45")), 120.0))
READ_TIMEOUT = max(5.0, min(float(os.environ.get("MCP_READ_TIMEOUT_SECONDS", "12")), 30.0))
MAX_TEXT = _env_int("MCP_MAX_TEXT_LENGTH", 12000, 100, 50000)
MAX_QUERY = _env_int("MCP_MAX_QUERY_LENGTH", 2000, 100, 10000)
MAX_TOP_K = _env_int("MCP_MAX_TOP_K", 20, 1, 20)
MAX_PAGE_SIZE = _env_int("MCP_MAX_PAGE_SIZE", 20, 1, 20)
SCAN_LIMIT = _env_int("MCP_SCAN_LIMIT", 5000, 100, 10000)
MAX_METADATA_BYTES = _env_int("MCP_MAX_METADATA_BYTES", 8192, 512, 32768)
MAX_FILTER_BYTES = _env_int("MCP_MAX_FILTER_BYTES", 8192, 512, 32768)
MAX_UPSTREAM_RESPONSE_BYTES = _env_int("MCP_MAX_UPSTREAM_RESPONSE_BYTES", 1000000, 65536, 2000000)
MAX_CONCURRENT_UPSTREAM = _env_int("MCP_MAX_CONCURRENT_UPSTREAM", 16, 1, 64)
CONFIRMATION_TTL_SECONDS = _env_int("MCP_CONFIRMATION_TTL_SECONDS", 300, 60, 900)
PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CONFIRMATION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
RESERVED_METADATA = {"user_id", "agent_id", "app_id", "mcp_owner", "scope", "project_id", "source", "run_id"}
MEMORY_RESPONSE_FIELDS = {
    "id",
    "memory",
    "data",
    "user_id",
    "agent_id",
    "app_id",
    "run_id",
    "metadata",
    "categories",
    "hash",
    "score",
    "score_details",
    "created_at",
    "updated_at",
    "expiration_date",
    "event",
}
CLEAN_MEMORY_FIELDS = {
    "id",
    "memory",
    "user_id",
    "run_id",
    "created_at",
    "updated_at",
    "expiration_date",
    "score",
    "score_details",
}
FILTER_FIELDS = {"metadata", "run_id", "created_at", "updated_at", "expiration_date"}
FILTER_LOGICAL = {"AND", "OR", "NOT"}
FILTER_OPERATORS = {"eq", "ne", "in", "nin", "gt", "gte", "lt", "lte", "contains", "icontains"}
HTTP_CLIENT: httpx.AsyncClient | None = None
UPSTREAM_SEMAPHORE: asyncio.Semaphore | None = None

_validate_runtime_secrets(INTERNAL_SERVICE_KEY, CONFIRMATION_SECRET, PROJECT_SCOPE_SECRET)


class InvalidMem0APIKey(ValueError):
    pass


@asynccontextmanager
async def app_lifespan(_server) -> AsyncIterator[dict[str, Any]]:
    global HTTP_CLIENT, UPSTREAM_SEMAPHORE
    timeout = httpx.Timeout(TIMEOUT, connect=min(5.0, TIMEOUT), pool=2.0)
    limits = httpx.Limits(
        max_connections=MAX_CONCURRENT_UPSTREAM,
        max_keepalive_connections=min(8, MAX_CONCURRENT_UPSTREAM),
        keepalive_expiry=5.0,
    )
    async with httpx.AsyncClient(
        base_url=MEM0_BASE_URL,
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        HTTP_CLIENT = client
        UPSTREAM_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_UPSTREAM)
        try:
            yield {}
        finally:
            HTTP_CLIENT = None
            UPSTREAM_SEMAPHORE = None


class Mem0APIKeyVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not 16 <= len(token) <= 512:
            return None
        try:
            result = await _request(
                "POST",
                "/auth/introspect",
                api_key=token,
                timeout_seconds=min(5.0, READ_TIMEOUT),
                max_response_bytes=32768,
            )
        except InvalidMem0APIKey:
            return None
        subject_value = result.get("subject") if isinstance(result, dict) else None
        try:
            subject = str(uuid.UUID(subject_value))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("Mem0 API Key 内省返回无效响应") from exc
        if (
            not isinstance(result, dict)
            or result.get("purpose") != "mcp"
            or subject != subject_value
        ):
            raise RuntimeError("Mem0 API Key 内省返回无效响应")
        return AccessToken(
            token=token,
            client_id=f"mem0:{subject}",
            subject=subject,
            scopes=["mem0:mcp"],
        )


mcp = FastMCP(
    "Self-hosted Mem0",
    instructions="使用私有自托管 Mem0 提供长期记忆，并强制执行项目与所有者隔离。",
    token_verifier=Mem0APIKeyVerifier(),
    auth=AuthSettings(
        issuer_url=MEM0_BASE_URL,
        resource_server_url=None,
        required_scopes=["mem0:mcp"],
    ),
    stateless_http=True,
    json_response=True,
    max_request_body_size=1024 * 1024,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_csv_env(
            "MCP_ALLOWED_HOSTS",
            "127.0.0.1:*,localhost:*,mem0-mcp:*",
        ),
        allowed_origins=_csv_env("MCP_ALLOWED_ORIGINS", ""),
    ),
)


def _project(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not PROJECT_RE.fullmatch(value):
        raise ValueError("project_id 必须为 1-64 位字母、数字、点、下划线或连字符")
    return value


def _repository_fingerprint(value: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ValueError("repository_fingerprint 必须是 64 位小写十六进制 SHA-256")
    return value


def _project_scope_id(subject: str, repository_fingerprint: str) -> str:
    if not isinstance(subject, str) or not subject:
        raise RuntimeError("MCP 认证上下文缺少有效主体")
    material = (
        b"mem0-project-scope-v1\0"
        + subject.encode("utf-8")
        + b"\0"
        + bytes.fromhex(_repository_fingerprint(repository_fingerprint))
    )
    return hmac.new(PROJECT_SCOPE_SECRET.encode("utf-8"), material, hashlib.sha256).hexdigest()


def _entity_id(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not ID_RE.fullmatch(value):
        raise ValueError(f"{field} 格式无效")
    return value


def _memory_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("memory_id 必须是有效 UUID") from exc


def _text(value: str, limit: int, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} 不能为空")
    if len(value) > limit:
        raise ValueError(f"{field} 超过 {limit} 个字符")
    return value


def _expiration(value: str | None, *, allow_clear: bool = False) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if allow_clear and value == "":
        return None
    if not value or len(value) > 64:
        raise ValueError("expiration_date 格式无效")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expiration_date 必须为 YYYY-MM-DD 日期") from exc
    if parsed.isoformat() != value:
        raise ValueError("expiration_date 必须为 YYYY-MM-DD 日期")
    return value


def _messages(text: str | None, messages: list[dict[str, str]] | None) -> list[dict[str, str]]:
    if (text is None) == (messages is None):
        raise ValueError("text 与 messages 必须且只能提供一个")
    if text is not None:
        return [{"role": "user", "content": _text(text, MAX_TEXT, "text")}]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 50:
        raise ValueError("messages 必须包含 1-50 条消息")
    cleaned: list[dict[str, str]] = []
    total = 0
    for item in messages:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ValueError("每条消息只能包含 role 与 content")
        role = item.get("role")
        if role not in {"user", "assistant", "system"}:
            raise ValueError("消息 role 只能是 user、assistant 或 system")
        content = _text(str(item.get("content", "")), MAX_TEXT, "message.content")
        total += len(content)
        if total > MAX_TEXT:
            raise ValueError(f"messages 总正文超过 {MAX_TEXT} 个字符")
        cleaned.append({"role": role, "content": content})
    return cleaned


def _json_bytes(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("参数必须是 JSON 可序列化值") from exc


def _reject_json_constant(_value: str) -> None:
    raise ValueError("不允许非标准 JSON 数值")


def _metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata 必须是对象")
    if _json_bytes(value) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata 超过 {MAX_METADATA_BYTES} 字节")
    key_count = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal key_count
        if depth > 3:
            raise ValueError("metadata 嵌套深度超过 3 层")
        if isinstance(item, dict):
            if len(item) > 32:
                raise ValueError("metadata 单层键数量超过 32")
            for key, child in item.items():
                key_count += 1
                if key_count > 64:
                    raise ValueError("metadata 总键数量超过 64")
                if not isinstance(key, str) or not key or len(key) > 64:
                    raise ValueError("metadata 键名格式无效")
                if key in RESERVED_METADATA:
                    raise ValueError(f"metadata 不允许覆盖保留字段: {key}")
                walk(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 20:
                raise ValueError("metadata 数组长度超过 20")
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str) and len(item) > 1000:
            raise ValueError("metadata 字符串超过 1000 个字符")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("metadata 包含不支持的值类型")

    walk(value, 0)
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _result_rows(data: Any, operation: str) -> list[dict[str, Any]]:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("results"), list)
        or any(not isinstance(item, dict) for item in data["results"])
    ):
        raise RuntimeError(f"Mem0 {operation}返回无效响应")
    return data["results"]


def _item_metadata(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    metadata = dict(value) if isinstance(value, dict) else {}
    for key, flattened_value in item.items():
        if key in RESERVED_METADATA and key in metadata and metadata[key] != flattened_value:
            raise RuntimeError("Mem0 返回冲突的保留 metadata")
        if key not in MEMORY_RESPONSE_FIELDS:
            metadata.setdefault(key, flattened_value)
    return metadata


def _managed(item: dict[str, Any]) -> bool:
    return (
        item.get("user_id") in (None, DEFAULT_USER_ID)
        and _item_metadata(item).get("mcp_owner") == OWNER
    )


def _visible(item: dict[str, Any], project_id: str | None, *, include_global: bool) -> bool:
    if not _managed(item):
        return False
    metadata = _item_metadata(item)
    item_project = metadata.get("project_id")
    expected_scope = "project" if item_project not in (None, "") else "global"
    if metadata.get("scope") != expected_scope:
        return False
    if project_id is None:
        return item_project in (None, "")
    if include_global:
        return item_project in (None, "", project_id)
    return item_project == project_id


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in item.items() if key in CLEAN_MEMORY_FIELDS}
    metadata = _item_metadata(item)
    if metadata or isinstance(item.get("metadata"), dict):
        cleaned["metadata"] = metadata
    return cleaned


def _validate_filter_condition(condition: Any) -> None:
    if condition is None:
        raise ValueError("过滤值不能为 null")
    if isinstance(condition, dict):
        if len(condition) != 1:
            raise ValueError("过滤比较只能包含一个操作符")
        operator, operand = next(iter(condition.items()))
        if operator not in FILTER_OPERATORS:
            raise ValueError(f"不允许的过滤操作符: {operator}")
        if operand is None:
            raise ValueError("过滤比较值不能为 null")
        if operator in {"gt", "gte", "lt", "lte"} and isinstance(operand, bool):
            raise ValueError("范围过滤值不能是布尔值")
        if operator in {"in", "nin"}:
            if not isinstance(operand, list) or not operand or len(operand) > 20:
                raise ValueError("in/nin 过滤必须包含 1-20 个值")
            for value in operand:
                if value is None or isinstance(value, (dict, list)) or (isinstance(value, str) and len(value) > 512):
                    raise ValueError("in/nin 过滤值无效")
        elif isinstance(operand, (dict, list)) or (isinstance(operand, str) and len(operand) > 512):
            raise ValueError("过滤比较值必须是长度受限的标量")
    elif isinstance(condition, list) or (isinstance(condition, str) and len(condition) > 512):
        raise ValueError("过滤值必须是长度受限的标量或比较对象")


def _filters(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise ValueError("filters 必须是非空对象")
    if _json_bytes(value) > MAX_FILTER_BYTES:
        raise ValueError(f"filters 超过 {MAX_FILTER_BYTES} 字节")
    state = {"nodes": 0}

    def walk(item: Any, depth: int) -> None:
        state["nodes"] += 1
        if depth > 4 or state["nodes"] > 32:
            raise ValueError("filters 复杂度超过限制")
        if not isinstance(item, dict) or not item:
            raise ValueError("过滤节点必须是非空对象")
        for key, condition in item.items():
            if key in FILTER_LOGICAL:
                children = condition if isinstance(condition, list) else [condition]
                if not children or len(children) > 16 or (key == "NOT" and len(children) != 1):
                    raise ValueError("逻辑过滤结构无效")
                for child in children:
                    walk(child, depth + 1)
                continue
            if key not in FILTER_FIELDS:
                raise ValueError(f"不允许的过滤字段: {key}")
            if key == "metadata":
                if not isinstance(condition, dict) or not condition or len(condition) > 16:
                    raise ValueError("metadata 过滤必须是非空对象")
                for metadata_key, metadata_condition in condition.items():
                    if (
                        not isinstance(metadata_key, str)
                        or not metadata_key
                        or len(metadata_key) > 64
                        or metadata_key in RESERVED_METADATA
                        or metadata_key in FILTER_LOGICAL
                    ):
                        raise ValueError("metadata 过滤包含保留或无效字段")
                    _validate_filter_condition(metadata_condition)
            else:
                _validate_filter_condition(condition)

    walk(value, 0)
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


_FILTER_MISSING = object()


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    return left == right


def _filter_and(values: list[bool | None]) -> bool | None:
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True


def _filter_or(values: list[bool | None]) -> bool | None:
    if any(value is True for value in values):
        return True
    if any(value is None for value in values):
        return None
    return False


def _compare_filter(actual: Any, condition: Any) -> bool | None:
    # 与 PostgreSQL WHERE 的三值逻辑保持一致：字段缺失或为 null 时结果未知。
    if actual is _FILTER_MISSING or actual is None:
        return None
    if not isinstance(condition, dict):
        return _json_equal(actual, condition)
    operator, expected = next(iter(condition.items()))
    if operator == "eq":
        return _json_equal(actual, expected)
    if operator == "ne":
        return not _json_equal(actual, expected)
    if operator == "in":
        return any(_json_equal(actual, item) for item in expected)
    if operator == "nin":
        return not any(_json_equal(actual, item) for item in expected)
    if operator in {"contains", "icontains"}:
        if isinstance(actual, list):
            return any(_json_equal(item, expected) for item in actual)
        if not isinstance(actual, str) or not isinstance(expected, str):
            return False
        return expected.lower() in actual.lower() if operator == "icontains" else expected in actual
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    try:
        if operator == "gt":
            return actual > expected
        if operator == "gte":
            return actual >= expected
        if operator == "lt":
            return actual < expected
        if operator == "lte":
            return actual <= expected
    except TypeError:
        return False
    return False


def _evaluate_filter(item: dict[str, Any], value: dict[str, Any]) -> bool | None:
    terms: list[bool | None] = []
    for key, condition in value.items():
        if key == "AND":
            children = condition if isinstance(condition, list) else [condition]
            terms.append(_filter_and([_evaluate_filter(item, child) for child in children]))
        elif key == "OR":
            children = condition if isinstance(condition, list) else [condition]
            terms.append(_filter_or([_evaluate_filter(item, child) for child in children]))
        elif key == "NOT":
            children = condition if isinstance(condition, list) else [condition]
            child = _filter_or([_evaluate_filter(item, item_filter) for item_filter in children])
            terms.append(None if child is None else not child)
        elif key == "metadata":
            metadata = _item_metadata(item)
            terms.append(
                _filter_and(
                    [
                        _compare_filter(metadata.get(name, _FILTER_MISSING), expected)
                        for name, expected in condition.items()
                    ]
                )
            )
        else:
            terms.append(_compare_filter(item.get(key, _FILTER_MISSING), condition))
    return _filter_and(terms)


def _matches_filter(item: dict[str, Any], value: dict[str, Any]) -> bool:
    return _evaluate_filter(item, value) is True


def _memory_rows(
    data: Any,
    operation: str,
    *,
    project_id: str | None,
    include_global: bool = False,
    all_projects: bool = False,
    run_id: str | None = None,
    filters: dict[str, Any] | None = None,
    maximum: int | None = None,
) -> list[dict[str, Any]]:
    rows = _result_rows(data, operation)
    if maximum is not None and len(rows) > maximum:
        raise RuntimeError(f"Mem0 {operation}返回无效响应")
    seen: set[str] = set()
    for item in rows:
        memory_id = item.get("id")
        try:
            normalized_id = str(uuid.UUID(memory_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Mem0 {operation}返回无效响应") from exc
        if normalized_id != memory_id or memory_id in seen:
            raise RuntimeError(f"Mem0 {operation}返回无效响应")
        seen.add(memory_id)
        in_scope = _managed(item) if all_projects else _visible(
            item,
            project_id,
            include_global=include_global,
        )
        if (
            not in_scope
            or (run_id is not None and item.get("run_id") != run_id)
            or (filters is not None and not _matches_filter(item, filters))
        ):
            raise RuntimeError(f"Mem0 {operation}返回越界结果")
    return rows


def _response_count(data: dict[str, Any], operation: str) -> int:
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise RuntimeError(f"Mem0 {operation}返回无效响应")
    return count


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    internal: bool = False,
    api_key: str | None = None,
    public: bool = False,
    timeout_seconds: float | None = None,
    max_response_bytes: int | None = None,
) -> Any:
    client = HTTP_CLIENT
    semaphore = UPSTREAM_SEMAPHORE
    if client is None or semaphore is None:
        raise RuntimeError("MCP 上游客户端尚未就绪")
    if sum((bool(internal), api_key is not None, bool(public))) != 1:
        raise RuntimeError("Mem0 上游认证模式冲突")
    headers = {"Accept": "application/json"}
    if internal:
        headers["X-Mem0-Internal-Key"] = INTERNAL_SERVICE_KEY
    elif api_key is not None:
        headers["X-API-Key"] = api_key
    limit = max_response_bytes or MAX_UPSTREAM_RESPONSE_BYTES
    stream_options: dict[str, Any] = {}
    if timeout_seconds is not None:
        stream_options["timeout"] = httpx.Timeout(
            timeout_seconds,
            connect=min(3.0, timeout_seconds),
            pool=1.0,
        )
    try:
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=0.25)
        except TimeoutError as exc:
            raise RuntimeError("MCP 服务繁忙，请稍后重试") from exc
        try:
            async with client.stream(
                method,
                path,
                headers=headers,
                params=params,
                json=json_body,
                **stream_options,
            ) as response:
                if response.status_code >= 400:
                    if response.status_code in (401, 403) and api_key is not None:
                        raise InvalidMem0APIKey("Mem0 API Key 无效或用途不匹配")
                    if response.status_code in (400, 404, 409, 422):
                        raise ValueError(f"Mem0 拒绝请求（{response.status_code}）")
                    raise RuntimeError(f"Mem0 上游错误（{response.status_code}）")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > limit:
                            raise RuntimeError("Mem0 上游响应超过大小限制")
                    except ValueError:
                        pass
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > limit:
                        raise RuntimeError("Mem0 上游响应超过大小限制")
                    body.extend(chunk)
                status_code = response.status_code
        finally:
            semaphore.release()
    except httpx.TimeoutException as exc:
        raise RuntimeError("Mem0 上游请求超时") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Mem0 上游不可用") from exc
    if status_code == 204 or not body:
        return None
    try:
        return json.loads(body, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Mem0 上游返回无效 JSON") from exc


async def _internal_memory_action(
    action: Literal["get", "update", "delete", "history"],
    memory_id: str,
    project_id: str | None,
    **changes: Any,
) -> dict[str, Any]:
    normalized_id = _memory_id(memory_id)
    payload = {
        "action": action,
        "memory_id": normalized_id,
        "project_id": project_id,
        **changes,
    }
    data = await _request(
        "POST",
        "/internal/mcp/memory",
        json_body=payload,
        internal=True,
        timeout_seconds=READ_TIMEOUT if action in {"get", "history"} else TIMEOUT,
    )
    if not isinstance(data, dict):
        raise RuntimeError("Mem0 内部记忆操作返回无效响应")
    if action in {"get", "update"}:
        memory = data.get("memory")
        if (
            not isinstance(memory, dict)
            or memory.get("id") != normalized_id
            or not _visible(memory, project_id, include_global=False)
        ):
            raise RuntimeError("Mem0 内部记忆操作返回无效响应")
    elif action == "delete":
        if data.get("deleted") is not True or data.get("memory_id") != normalized_id:
            raise RuntimeError("Mem0 内部删除返回无效响应")
    else:
        rows = _result_rows(data, "内部历史")
        if any(item.get("memory_id") not in (None, normalized_id) for item in rows):
            raise RuntimeError("Mem0 内部历史返回无效响应")
    return data


async def _owned(memory_id: str, project_id: str | None) -> dict[str, Any]:
    data = await _internal_memory_action("get", memory_id, project_id)
    return data["memory"]


async def _internal_query(
    *,
    project_id: str | None,
    include_global: bool = False,
    all_projects: bool = False,
    run_id: str | None = None,
    filters: dict[str, Any] | None = None,
    show_expired: bool = False,
    page: int = 1,
    page_size: int = 50,
    sort_by: str = "updated_at",
    sort_order: str = "desc",
    cutoff: str | None = None,
) -> dict[str, Any]:
    if all_projects and project_id is not None:
        raise RuntimeError("Mem0 内部查询范围无效")
    payload = {
        "project_id": project_id,
        "include_global": include_global,
        "all_projects": all_projects,
        "run_id": run_id,
        "filters": filters,
        "show_expired": show_expired,
        "scan_limit": SCAN_LIMIT,
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "cutoff": cutoff,
    }
    data = await _request(
        "POST",
        "/internal/mcp/query",
        json_body=payload,
        internal=True,
        timeout_seconds=READ_TIMEOUT,
    )
    if not isinstance(data, dict):
        raise RuntimeError("Mem0 内部查询返回无效响应")
    rows = _memory_rows(
        data,
        "内部查询",
        project_id=project_id,
        include_global=include_global,
        all_projects=all_projects,
        run_id=run_id,
        filters=filters,
        maximum=page_size,
    )
    count = _response_count(data, "内部查询")
    partial = data.get("partial")
    result_hash = data.get("result_hash")
    scanned = data.get("scanned")
    expected_next = page + 1 if page * page_size < count else None
    expected_previous = page - 1 if page > 1 and (page - 1) * page_size < count else None
    next_page = data.get("next")
    previous_page = data.get("previous")
    if (
        count < len(rows)
        or count > SCAN_LIMIT
        or not isinstance(partial, bool)
        or not isinstance(result_hash, str)
        or not HASH_RE.fullmatch(result_hash)
        or next_page is not None
        and (isinstance(next_page, bool) or not isinstance(next_page, int))
        or previous_page is not None
        and (isinstance(previous_page, bool) or not isinstance(previous_page, int))
        or next_page != expected_next
        or previous_page != expected_previous
        or (partial and scanned != SCAN_LIMIT)
        or (not partial and scanned is not None)
    ):
        raise RuntimeError("Mem0 内部查询返回无效响应")
    return data


async def _internal_entities(
    *,
    project_id: str | None,
    entity_type: Literal["project", "run"] | None,
    show_expired: bool,
) -> dict[str, Any]:
    data = await _request(
        "POST",
        "/internal/mcp/entities",
        json_body={
            "project_id": project_id,
            "entity_type": entity_type,
            "show_expired": show_expired,
            "scan_limit": SCAN_LIMIT,
        },
        internal=True,
        timeout_seconds=READ_TIMEOUT,
    )
    if not isinstance(data, dict):
        raise RuntimeError("Mem0 内部实体查询返回无效响应")
    rows = _result_rows(data, "内部实体查询")
    count = _response_count(data, "内部实体查询")
    partial = data.get("partial")
    seen: set[tuple[str, str, str | None]] = set()
    for item in rows:
        item_type = item.get("type")
        item_id = item.get("id")
        item_project = item.get("project_id")
        memory_count = item.get("memory_count")
        if item_type not in {"project", "run"} or (
            entity_type is not None and item_type != entity_type
        ):
            raise RuntimeError("Mem0 内部实体查询返回无效响应")
        try:
            if item_type == "project":
                if _project(item_id) is None or item_project not in (None, ""):
                    raise ValueError
                normalized_project = item_id
            else:
                if _entity_id(item_id, "entity_id") is None:
                    raise ValueError
                normalized_project = _project(item_project)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Mem0 内部实体查询返回无效响应") from exc
        if project_id is not None and normalized_project != project_id:
            raise RuntimeError("Mem0 内部实体查询返回越界结果")
        identity = (item_type, item_id, normalized_project)
        if (
            identity in seen
            or isinstance(memory_count, bool)
            or not isinstance(memory_count, int)
            or memory_count < 1
            or item.get("updated_at") is not None
            and not isinstance(item.get("updated_at"), str)
        ):
            raise RuntimeError("Mem0 内部实体查询返回无效响应")
        seen.add(identity)
    if count != len(rows) or count > SCAN_LIMIT or not isinstance(partial, bool):
        raise RuntimeError("Mem0 内部实体查询返回无效响应")
    return data


def _token_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _make_confirmation(payload: dict[str, Any]) -> tuple[str, str]:
    expires_at = int(time.time()) + CONFIRMATION_TTL_SECONDS
    value = dict(payload)
    value.update({"exp": expires_at, "jti": str(uuid.uuid4())})
    encoded = _token_encode(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _token_encode(hmac.new(CONFIRMATION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    expires_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
    return f"{encoded}.{signature}", expires_iso


def _verify_confirmation(token: str, expected: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(token, str)
        or len(token) > 4096
        or not CONFIRMATION_TOKEN_RE.fullmatch(token)
    ):
        raise ValueError("确认令牌格式无效")
    encoded, supplied_signature = token.split(".", 1)
    expected_signature = _token_encode(
        hmac.new(CONFIRMATION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ValueError("确认令牌已被篡改")
    try:
        payload = json.loads(_token_decode(encoded))
    except Exception as exc:
        raise ValueError("确认令牌格式无效") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("exp"), int):
        raise ValueError("确认令牌内容无效")
    try:
        uuid.UUID(str(payload.get("jti", "")))
    except (TypeError, ValueError) as exc:
        raise ValueError("确认令牌内容无效") from exc
    now = time.time()
    if payload["exp"] <= now:
        raise ValueError("确认令牌已过期")
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError("确认令牌与当前删除范围不匹配")
    return payload


async def _bulk_delete(
    tool_name: str,
    project_id: str,
    run_id: str | None,
    confirmation_token: str | None,
) -> dict[str, Any]:
    project_id = _project(project_id)
    if project_id is None:
        raise ValueError("批量删除必须指定 project_id，禁止全局用户清空")
    run_id = _entity_id(run_id, "run_id")
    scope = {"tool": tool_name, "project_id": project_id, "run_id": run_id}
    if not confirmation_token:
        cutoff = datetime.now(timezone.utc).isoformat()
        preview = await _internal_query(
            project_id=project_id,
            include_global=False,
            run_id=run_id,
            show_expired=True,
            page=1,
            page_size=20,
            cutoff=cutoff,
        )
        if preview.get("partial"):
            raise ValueError("删除范围达到扫描上限，无法生成安全确认令牌")
        count = preview.get("count", 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RuntimeError("Mem0 内部查询返回无效预览数量")
        result_hash = str(preview.get("result_hash", ""))
        if not HASH_RE.fullmatch(result_hash):
            raise RuntimeError("Mem0 内部查询缺少有效预览摘要")
        samples = []
        for item in preview["results"][:20]:
            memory = item.get("memory")
            samples.append(
                {
                    "id": item.get("id"),
                    "preview": (memory[:160] + "…") if isinstance(memory, str) and len(memory) > 160 else memory,
                }
            )
        if count == 0:
            return {"ok": True, "phase": "preview", "scope": scope, "count": 0, "preview": [], "confirmation_required": False}
        payload = {**scope, "cutoff": cutoff, "result_hash": result_hash, "count": count}
        token, expires_at = _make_confirmation(payload)
        return {
            "ok": True,
            "phase": "preview",
            "scope": scope,
            "count": count,
            "preview": samples,
            "cutoff": cutoff,
            "expires_at": expires_at,
            "confirmation_token": token,
            "confirmation_required": True,
        }

    payload = _verify_confirmation(confirmation_token, scope)
    result_hash = payload.get("result_hash")
    cutoff = payload.get("cutoff")
    expected_count = payload.get("count")
    if (
        not isinstance(result_hash, str)
        or not HASH_RE.fullmatch(result_hash)
        or not isinstance(cutoff, str)
        or isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
    ):
        raise ValueError("确认令牌缺少删除预览信息")
    result = await _request(
        "POST",
        "/internal/mcp/delete",
        json_body={
            "project_id": project_id,
            "run_id": run_id,
            "cutoff": cutoff,
            "expected_hash": result_hash,
            "expected_count": expected_count,
            "operation_id": payload["jti"],
            "operation_expires_at": int(payload["exp"]),
        },
        internal=True,
        timeout_seconds=TIMEOUT,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Mem0 内部删除返回无效响应")
    status = result.get("status")
    deleted = result.get("deleted")
    failed_ids = result.get("failed_ids")
    replayed = result.get("replayed")
    if (
        status not in {"completed", "partial"}
        or isinstance(deleted, bool)
        or not isinstance(deleted, int)
        or deleted < 0
        or not isinstance(failed_ids, list)
        or any(not isinstance(memory_id, str) or not memory_id for memory_id in failed_ids)
        or len(set(failed_ids)) != len(failed_ids)
        or not isinstance(replayed, bool)
        or result.get("result_hash") != result_hash
        or deleted + len(failed_ids) != expected_count
        or (status == "completed") == bool(failed_ids)
    ):
        raise RuntimeError("Mem0 内部删除返回无效响应")
    return {
        "ok": not failed_ids,
        "phase": "partial" if failed_ids else "executed",
        "scope": scope,
        "deleted": deleted,
        "failed_ids": failed_ids,
        "operation_id": payload["jti"],
        "replayed": replayed,
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
)
async def add_memory(
    text: str | None = None,
    project_id: str | None = None,
    infer: bool = False,
    messages: list[dict[str, str]] | None = None,
    metadata: dict[str, Any] | None = None,
    run_id: str | None = None,
    expiration_date: str | None = None,
) -> dict[str, Any]:
    """保存长期记忆。text 与 messages 二选一；expiration_date 使用 YYYY-MM-DD；省略 project_id 表示全局记忆。"""
    project_id = _project(project_id)
    run_id = _entity_id(run_id, "run_id")
    payload: dict[str, Any] = {
        "messages": _messages(text, messages),
        "project_id": project_id,
        "metadata": _metadata(metadata),
        "infer": bool(infer),
    }
    if run_id:
        payload["run_id"] = run_id
    if expiration_date is not None:
        payload["expiration_date"] = _expiration(expiration_date)
    data = await _request(
        "POST",
        "/internal/mcp/add",
        json_body=payload,
        internal=True,
        timeout_seconds=TIMEOUT,
    )
    rows = _memory_rows(
        data,
        "新增记忆",
        project_id=project_id,
        run_id=run_id,
    )
    return {"ok": True, "results": [_clean(item) for item in rows]}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
)
async def resolve_project_scope(repository_fingerprint: str) -> dict[str, str]:
    """为当前认证主体和 Git 仓库指纹解析私有、稳定的项目范围。"""
    repository_fingerprint = _repository_fingerprint(repository_fingerprint)
    access_token = get_access_token()
    subject = access_token.subject if access_token is not None else None
    if not isinstance(subject, str) or not subject:
        raise RuntimeError("MCP 认证上下文缺少有效主体")
    return {"project_id": _project_scope_id(subject, repository_fingerprint)}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
)
async def search_memories(
    query: str,
    project_id: str | None = None,
    top_k: int = 10,
    threshold: float | None = None,
    filters: dict[str, Any] | None = None,
    rerank: bool = False,
    explain: bool = False,
    show_expired: bool = False,
) -> dict[str, Any]:
    """语义搜索受管记忆；项目搜索同时包含当前项目与全局记忆。"""
    query = _text(query, MAX_QUERY, "query")
    project_id = _project(project_id)
    caller_filters = _filters(filters)
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    payload: dict[str, Any] = {
        "query": query,
        "project_id": project_id,
        "top_k": top_k,
        "rerank": bool(rerank),
        "explain": bool(explain),
        "show_expired": bool(show_expired),
        "filters": caller_filters,
    }
    if threshold is not None:
        if not 0 <= float(threshold) <= 1:
            raise ValueError("threshold 必须在 0 到 1 之间")
        payload["threshold"] = float(threshold)
    data = await _request(
        "POST",
        "/internal/mcp/search",
        json_body=payload,
        internal=True,
        timeout_seconds=READ_TIMEOUT,
    )
    rows = _memory_rows(
        data,
        "内部搜索",
        project_id=project_id,
        include_global=bool(project_id),
        filters=caller_filters,
        maximum=top_k,
    )
    rows.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return {"results": [_clean(item) for item in rows]}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
)
async def get_memories(
    project_id: str | None = None,
    limit: int = 50,
    page: int = 1,
    page_size: int | None = None,
    filters: dict[str, Any] | None = None,
    sort_by: Literal["created_at", "updated_at", "expiration_date"] = "updated_at",
    sort_order: Literal["asc", "desc"] = "desc",
    show_expired: bool = False,
) -> dict[str, Any]:
    """分页列出受管记忆；保留 limit 作为旧调用的单页大小别名。"""
    project_id = _project(project_id)
    page = max(1, int(page))
    size = limit if page_size is None else page_size
    size = max(1, min(int(size), MAX_PAGE_SIZE))
    caller_filters = _filters(filters)
    data = await _internal_query(
        project_id=project_id,
        include_global=bool(project_id),
        filters=caller_filters,
        show_expired=bool(show_expired),
        page=page,
        page_size=size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return {
        "results": [_clean(item) for item in data["results"]],
        "count": data["count"],
        "next": data.get("next"),
        "previous": data.get("previous"),
        "partial": data["partial"],
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
)
async def get_memory(memory_id: str, project_id: str | None = None) -> dict[str, Any]:
    """按 ID 读取受管记忆，并要求精确匹配项目范围。"""
    return _clean(await _owned(memory_id, _project(project_id)))


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
)
async def update_memory(
    memory_id: str,
    text: str | None = None,
    project_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    expiration_date: str | None = None,
) -> dict[str, Any]:
    """更新正文、非保留 metadata 或 YYYY-MM-DD 过期日期；传空字符串可清除过期时间。"""
    project_id = _project(project_id)
    if text is None and metadata is None and expiration_date is None:
        raise ValueError("text、metadata 或 expiration_date 至少提供一个")
    payload: dict[str, Any] = {}
    if text is not None:
        payload["text"] = _text(text, MAX_TEXT, "text")
    if metadata is not None:
        payload["metadata"] = _metadata(metadata)
    if expiration_date is not None:
        payload["expiration_date"] = _expiration(expiration_date, allow_clear=True)
    data = await _internal_memory_action("update", memory_id, project_id, **payload)
    return {"ok": True, "memory": _clean(data["memory"])}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
)
async def delete_memory(memory_id: str, project_id: str | None = None) -> dict[str, Any]:
    """永久删除一条受管记忆；调用前必须向用户确认精确 ID。"""
    project_id = _project(project_id)
    data = await _internal_memory_action("delete", memory_id, project_id)
    return {"ok": True, "memory_id": data["memory_id"]}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
)
async def get_memory_history(memory_id: str, project_id: str | None = None) -> dict[str, Any]:
    """读取记忆历史；读取历史前先验证当前记忆的所有权与精确项目范围。"""
    project_id = _project(project_id)
    data = await _internal_memory_action("history", memory_id, project_id)
    return {"results": data["results"]}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
)
async def list_entities(
    entity_type: Literal["project", "run"] | None = None,
    project_id: str | None = None,
    show_expired: bool = False,
) -> dict[str, Any]:
    """从受管记忆推导项目与运行实体，不暴露固定用户或服务器其他实体。"""
    project_id = _project(project_id)
    data = await _internal_entities(
        project_id=project_id,
        entity_type=entity_type,
        show_expired=bool(show_expired),
    )
    return {
        "results": data["results"],
        "count": data["count"],
        "partial": data["partial"],
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
)
async def delete_all_memories(
    project_id: str,
    run_id: str | None = None,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """按项目或项目内运行批量删除；首次调用仅预览，第二次携带 5 分钟确认令牌执行。"""
    return await _bulk_delete("delete_all_memories", project_id, run_id, confirmation_token)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
)
async def delete_entities(
    entity_type: Literal["project", "run"],
    entity_id: str,
    project_id: str | None = None,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """删除推导出的项目或运行实体；采用预览与 5 分钟确认令牌。"""
    if entity_type == "project":
        if project_id not in (None, ""):
            raise ValueError("删除 project 实体时不要另传 project_id")
        target_project = _project(entity_id)
        target_run = None
    else:
        target_project = _project(project_id)
        if target_project is None:
            raise ValueError("删除 run 实体时必须指定 project_id")
        target_run = _entity_id(entity_id, "entity_id")
    if target_project is None:
        raise ValueError("批量删除必须指定项目范围")
    return await _bulk_delete("delete_entities", target_project, target_run, confirmation_token)


@mcp.custom_route("/livez", methods=["GET"])
async def livez(_request):
    return JSONResponse({"status": "ok"})


async def _readiness_response() -> JSONResponse:
    try:
        data = await _request(
            "GET",
            "/api/health",
            public=True,
            timeout_seconds=3.0,
            max_response_bytes=32768,
        )
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise RuntimeError("Mem0 上游未就绪")
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/readyz", methods=["GET"])
async def readyz(_request):
    return await _readiness_response()


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return await _readiness_response()


@asynccontextmanager
async def asgi_lifespan(_app):
    async with app_lifespan(mcp):
        async with mcp.session_manager.run():
            yield


transport_app = mcp.streamable_http_app()
app = Starlette(
    routes=[Mount("/", app=transport_app)],
    lifespan=asgi_lifespan,
)
