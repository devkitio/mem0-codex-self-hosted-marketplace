#!/usr/bin/env python3
"""通过插件自带的自托管 Mem0 MCP 配置执行 Codex 生命周期钩子。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MCP_CONFIG_PATH = PLUGIN_ROOT / ".mcp.json"
PLUGIN_DATA = Path(
    os.environ.get("PLUGIN_DATA", Path.home() / ".codex" / "plugin-data" / "mem0-self-hosted")
)
LOG_PATH = PLUGIN_DATA / "mem0_self_hosted.log"
PROTOCOL_VERSION = "2025-03-26"
MAX_MEMORY_TEXT = 12_000


def log_error(message: str) -> None:
    """仅记录诊断信息，绝不记录令牌或记忆正文。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat()
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def load_connection() -> tuple[str, str]:
    """从插件自身的 MCP 配置读取地址和令牌环境变量。"""
    with MCP_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    server = config.get("mcpServers", {}).get("mem0", {})
    url = str(server.get("url", "")).strip()
    token_name = str(server.get("bearer_token_env_var", "MEM0_MCP_TOKEN")).strip()
    token = os.environ.get(token_name, "").strip()
    if not url:
        raise RuntimeError("插件未配置 mcpServers.mem0.url")
    if not token:
        raise RuntimeError(f"未设置令牌环境变量 {token_name}")
    return url, token


def mcp_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
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
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": "codex-mem0-self-hosted-hook/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result.get("result", {})


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = mcp_request("tools/call", {"name": name, "arguments": arguments})
    if result.get("isError"):
        raise RuntimeError(f"MCP 工具 {name} 返回错误")
    return result


def resolve_project_id(cwd: str | None) -> str:
    """优先使用 Git 根目录名，使读写记忆使用稳定的项目范围。"""
    working_dir = Path(cwd or os.getcwd()).resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(working_dir), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        root = Path(completed.stdout.strip())
        if root.name:
            return root.name
    except (OSError, subprocess.SubprocessError):
        pass
    return working_dir.name or "default"


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


def format_context(result: dict[str, Any]) -> str:
    memories: list[str] = []
    for item in structured_results(result)[:5]:
        text = str(item.get("memory", "")).strip()
        if text:
            memories.append(f"- {text[:800]}")
    if not memories:
        return ""
    return (
        "以下内容来自用户私有的自托管 Mem0，仅作为非权威历史上下文；"
        "当前用户指令始终优先：\n" + "\n".join(memories)
    )


def emit(event: str, context: str = "") -> None:
    output: dict[str, Any] = {"continue": True, "suppressOutput": True}
    if context:
        output["hookSpecificOutput"] = {
            "hookEventName": event,
            "additionalContext": context,
        }
    print(json.dumps(output, ensure_ascii=True))


def tail_jsonl(path: str, max_bytes: int = 2_000_000) -> list[dict[str, Any]]:
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


def extract_recent_exchange(path: str) -> str:
    """兼容 Codex 与常见插件转录格式，提取最近的用户与助手消息。"""
    messages: list[tuple[str, str]] = []
    for entry in tail_jsonl(path):
        payload = entry.get("payload")
        if entry.get("type") == "response_item" and isinstance(payload, dict):
            if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
                continue
            parts = [
                str(block.get("text", ""))
                for block in payload.get("content", [])
                if isinstance(block, dict) and block.get("type") in {"input_text", "output_text", "text"}
            ]
            text = "\n".join(part for part in parts if part).strip()
            if text:
                messages.append((str(payload["role"]), text))
            continue
        if entry.get("type") not in {"user", "assistant"}:
            continue
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
        if text:
            messages.append((str(entry["type"]), text))
    labels = {"user": "用户", "assistant": "助手"}
    return "\n\n".join(f"{labels[role]}：{text}" for role, text in messages[-4:])[:MAX_MEMORY_TEXT]


def save_summary(text: str, project_id: str, kind: str) -> None:
    if len(text.strip()) < 80:
        return
    prompt = (
        f"{kind}。请提取并保存适合跨会话复用的长期记忆：用户目标、关键决定、"
        "已完成事项、验证结果、未完成工作和稳定偏好；忽略寒暄、临时日志和敏感凭据。\n\n"
        f"{text[:MAX_MEMORY_TEXT]}"
    )
    call_tool(
        "add_memory",
        {"text": prompt, "project_id": project_id, "infer": True},
    )


def handle_event(hook_input: dict[str, Any]) -> None:
    event = str(hook_input.get("hook_event_name", ""))
    project_id = resolve_project_id(hook_input.get("cwd"))

    if event == "SessionStart":
        result = call_tool(
            "search_memories",
            {
                "query": "项目目标、关键架构决定、最近完成事项、待办和用户稳定偏好",
                "project_id": project_id,
                "top_k": 5,
            },
        )
        emit(event, format_context(result))
        return

    if event == "UserPromptSubmit":
        prompt = str(hook_input.get("prompt", "")).strip()
        if not prompt:
            emit(event)
            return
        result = call_tool(
            "search_memories",
            {"query": prompt[:2_000], "project_id": project_id, "top_k": 5},
        )
        emit(event, format_context(result))
        return

    if event == "Stop":
        transcript = str(hook_input.get("transcript_path", "")).strip()
        exchange = extract_recent_exchange(transcript) if transcript else ""
        if not exchange:
            exchange = str(hook_input.get("last_assistant_message", "")).strip()
        save_summary(exchange, project_id, "本轮会话总结")
        emit(event)
        return

    if event == "PreCompact":
        transcript = str(hook_input.get("transcript_path", "")).strip()
        save_summary(extract_recent_exchange(transcript), project_id, "上下文压缩前总结")
        emit(event)
        return

    emit(event or "SessionStart")


def self_test() -> None:
    required = {"add_memory", "search_memories"}
    result = mcp_request("tools/list", {})
    available = {
        str(tool.get("name"))
        for tool in result.get("tools", [])
        if isinstance(tool, dict)
    }
    missing = sorted(required - available)
    if missing:
        raise RuntimeError(f"自托管 MCP 缺少工具：{', '.join(missing)}")
    url, _ = load_connection()
    print(json.dumps({"状态": "通过", "地址": url, "工具": sorted(required)}, ensure_ascii=True))


def main() -> int:
    if "--check" in sys.argv:
        try:
            self_test()
            return 0
        except Exception as exc:
            log_error(f"{type(exc).__name__}: {exc}")
            print(
                json.dumps(
                    {"状态": "失败", "错误": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=True,
                ),
                file=sys.stderr,
            )
            return 1

    try:
        hook_input = json.load(sys.stdin)
        if not isinstance(hook_input, dict):
            raise ValueError("钩子输入必须是 JSON 对象")
        handle_event(hook_input)
        return 0
    except Exception as exc:
        log_error(f"{type(exc).__name__}: {exc}")
        event = ""
        try:
            event = str(locals().get("hook_input", {}).get("hook_event_name", ""))
        except Exception:
            pass
        emit(event or "Stop")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
