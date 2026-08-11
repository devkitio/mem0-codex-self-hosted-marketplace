from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "plugins" / "mem0" / "scripts" / "mem0_self_hosted.py"
SPEC = importlib.util.spec_from_file_location("mem0_self_hosted", SCRIPT)
assert SPEC and SPEC.loader
mem0 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mem0)
DOC_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "mem0"
    / "skills"
    / "mem0"
    / "scripts"
    / "mem0_doc_search.py"
)
DOC_SPEC = importlib.util.spec_from_file_location("mem0_doc_search", DOC_SCRIPT)
assert DOC_SPEC and DOC_SPEC.loader
mem0_docs = importlib.util.module_from_spec(DOC_SPEC)
DOC_SPEC.loader.exec_module(mem0_docs)


class Mem0SelfHostedTests(unittest.TestCase):
    def capture_json(self, function, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            function(*args)
        return json.loads(output.getvalue())

    def test_连接只读取自部署_Mem0_API_Key(self):
        with mock.patch.dict(
            os.environ,
            {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-self-hosted-key"},
        ):
            url, token = mem0.load_connection()

        self.assertEqual(url, "https://mem0-api.jiang.in/mcp")
        self.assertEqual(token, "m0sk_test-self-hosted-key")

    def test_MCP_响应兼容_JSON_批次和_SSE(self):
        json_batch = json.dumps(
            [
                {"jsonrpc": "2.0", "id": 2, "result": {"ignored": True}},
                {"jsonrpc": "2.0", "id": 1, "result": {"状态": "通过"}},
            ],
            ensure_ascii=False,
        )
        self.assertEqual(
            mem0.parse_mcp_response(json_batch, "application/json")["result"]["状态"],
            "通过",
        )

        sse = (
            'event: message\n'
            'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            'event: message\n'
            'data: [{"jsonrpc":"2.0","id":1,"result":{"状态":"通过"}}]\n\n'
        )
        self.assertEqual(
            mem0.parse_mcp_response(sse, "text/event-stream; charset=utf-8")["result"]["状态"],
            "通过",
        )

    def test_钩子失败日志不记录异常正文(self):
        output = io.StringIO()
        with mock.patch.object(mem0.sys, "argv", [str(SCRIPT)]), mock.patch.object(
            mem0.sys,
            "stdin",
            io.StringIO("{}"),
        ), mock.patch.object(
            mem0,
            "handle_event",
            side_effect=RuntimeError("Authorization: Bearer secret-value"),
        ), mock.patch.object(mem0, "log_error") as log, redirect_stdout(output):
            status = mem0.main()

        self.assertEqual(status, 0)
        self.assertNotIn("secret-value", log.call_args.args[0])
        self.assertEqual(json.loads(output.getvalue()), {})

    def test_自检失败会脱敏日志和标准错误(self):
        error = io.StringIO()
        with mock.patch.object(mem0.sys, "argv", [str(SCRIPT), "--check"]), mock.patch.object(
            mem0,
            "self_test",
            side_effect=RuntimeError("Authorization: Bearer sentinel-secret"),
        ), mock.patch.object(mem0, "log_error") as log, redirect_stderr(error):
            status = mem0.main()

        self.assertEqual(status, 1)
        self.assertNotIn("sentinel-secret", error.getvalue())
        self.assertNotIn("sentinel-secret", log.call_args.args[0])
        self.assertIn("[已脱敏]", json.loads(error.getvalue())["错误"])

    def test_MCP_响应限制大小(self):
        response = mock.MagicMock()
        response.read.return_value = b"x" * (mem0.MAX_MCP_RESPONSE_BYTES + 1)
        context = mock.MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with mock.patch.object(mem0, "load_connection", return_value=("https://mem0.test/mcp", "token")), mock.patch.object(
            mem0,
            "open_mcp_request",
            return_value=context,
        ):
            with self.assertRaisesRegex(RuntimeError, "响应超过大小限制"):
                mem0.mcp_request("tools/list", {})

        response.read.assert_called_once_with(mem0.MAX_MCP_RESPONSE_BYTES + 1)

    def test_MCP_错误正文不会透传(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"message": "Authorization: Bearer sentinel-secret"},
            }
        ).encode("utf-8")
        response.headers.get_content_type.return_value = "application/json"
        context = mock.MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with mock.patch.object(mem0, "load_connection", return_value=("https://mem0.test/mcp", "token")), mock.patch.object(
            mem0,
            "open_mcp_request",
            return_value=context,
        ):
            with self.assertRaisesRegex(RuntimeError, "^MCP 返回错误$") as raised:
                mem0.mcp_request("tools/list", {})

        self.assertNotIn("sentinel-secret", str(raised.exception))

    def test_MCP_读写请求使用不同超时(self):
        with mock.patch.object(
            mem0,
            "mcp_request",
            return_value={"isError": False},
        ) as request:
            mem0.call_tool("search_memories", {"query": "测试"})
            self.assertEqual(request.call_args.args[2], mem0.MCP_REQUEST_TIMEOUT)

            mem0.call_tool("add_memory", {"text": "测试"})
            self.assertEqual(request.call_args.args[2], mem0.MCP_MUTATION_TIMEOUT)

    def test_Hook总预算约束请求和锁等待(self):
        with mock.patch.object(mem0, "HOOK_DEADLINE", 110.0), mock.patch.object(
            mem0.time,
            "monotonic",
            return_value=100.0,
        ), mock.patch.object(
            mem0,
            "mcp_request",
            return_value={"isError": False},
        ) as request:
            mem0.call_tool("add_memory", {"text": "测试"})
        self.assertEqual(request.call_args.args[2], 8.0)

        with mock.patch.object(mem0, "HOOK_DEADLINE", 101.0), mock.patch.object(
            mem0.time,
            "monotonic",
            return_value=100.0,
        ), mock.patch.object(mem0, "mcp_request") as request:
            with self.assertRaisesRegex(TimeoutError, "总时间预算"):
                mem0.call_tool("add_memory", {"text": "测试"})
        request.assert_not_called()

        with mock.patch.object(mem0, "HOOK_DEADLINE", 100.05), mock.patch.object(
            mem0,
            "acquire_lock",
            return_value=False,
        ) as acquire, mock.patch.object(mem0.time, "monotonic", side_effect=[100.0, 100.1]):
            self.assertFalse(mem0.wait_for_lock(Path("never.lock"), timeout=20))
        acquire.assert_not_called()

    def test_Hook入口设置并恢复事件总预算(self):
        observed = []
        with mock.patch.object(mem0, "HOOK_DEADLINE", None), mock.patch.object(
            mem0.time,
            "monotonic",
            return_value=100.0,
        ), mock.patch.object(
            mem0,
            "_handle_event",
            side_effect=lambda _value: observed.append(mem0.HOOK_DEADLINE),
        ):
            mem0.handle_event({"hook_event_name": "Stop"})
            self.assertIsNone(mem0.HOOK_DEADLINE)

        self.assertEqual(observed, [100.0 + mem0.HOOK_TIME_BUDGETS["Stop"]])

    def test_自动导入预算耗尽前保留待恢复状态(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            (root / "AGENTS.md").write_text("目标：验证预算恢复。\n" * 10, encoding="utf-8")
            clock = [100.0]

            def fake_call(name, _arguments):
                if name == "add_memory":
                    clock[0] = 109.0
                    return {}
                return {"structuredContent": {"results": []}}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0,
                "git_root",
                return_value=root,
            ), mock.patch.object(mem0, "resolve_branch", return_value=""), mock.patch.object(
                mem0,
                "HOOK_DEADLINE",
                110.0,
            ), mock.patch.object(
                mem0.time,
                "monotonic",
                side_effect=lambda: clock[0],
            ), mock.patch.object(mem0, "call_tool", side_effect=fake_call):
                mem0._auto_import_project_files(str(root), "demo-project")

            state = mem0.load_json_file(data / "auto_import_state.json", {})
            scope = mem0.import_scope_key(root, "demo-project")
            self.assertTrue(state[scope]["AGENTS.md"]["pending"])
            self.assertEqual(state[scope]["AGENTS.md"]["memory_ids"], [])

    def test_MCP_地址和重定向不会泄漏令牌(self):
        for url in (
            "http://10.20.30.40/mcp",
            "ftp://mem0.test/mcp",
            "https://user:password@mem0.test/mcp",
            "https://mem0.test/mcp#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                mem0.validate_mcp_url(url)

        mem0.validate_mcp_url("https://mem0.test/mcp")
        mem0.validate_mcp_url("http://127.0.0.1:8080/mcp")
        mem0.validate_mcp_url("http://[::1]:8080/mcp")

        request = mem0.urllib.request.Request(
            "https://mem0.test/mcp",
            data=b"{}",
            method="POST",
        )
        request.add_unredirected_header("Authorization", "Bearer sentinel-secret")
        handler = mem0.SameOriginRedirectHandler()
        with self.assertRaisesRegex(RuntimeError, "跨源重定向"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://other.test/mcp",
            )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://mem0.test/next",
        )
        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.get_header("Authorization"), "Bearer sentinel-secret")
        self.assertNotIn("Authorization", redirected.headers)

    def test_钩子输入限制大小(self):
        with mock.patch.object(mem0, "MAX_HOOK_INPUT_BYTES", 32):
            with self.assertRaisesRegex(ValueError, "超过大小限制"):
                mem0.load_hook_input(io.StringIO('{"prompt":"' + "x" * 64 + '"}'))

    def test_JSON_形式敏感信息会脱敏(self):
        original = json.dumps(
            {
                "token": "token-secret",
                "password": "password-secret",
                "authorization": "Bearer auth-secret",
            }
        )
        redacted = mem0.redact_sensitive(original)

        self.assertNotIn("token-secret", redacted)
        self.assertNotIn("password-secret", redacted)
        self.assertNotIn("auth-secret", redacted)
        self.assertEqual(redacted.count("[已脱敏]"), 3)

    def test_跨平台路径地址和通用凭据会脱敏(self):
        original = (
            "AWS_SECRET_ACCESS_KEY=aws-secret "
            "DATABASE_URL=postgres://alice:db-secret@db.internal/app "
            "C:/Users/Alice/private/file.py "
            "/home/bob/private/file.py "
            "/Users/carol/private/file.py "
            "10.20.30.40 [2001:db8::1] 2001:db8::2\n"
            "Error: Bearer bare-bearer-secret；Token bare-token-secret\n"
            "Authorization: Basic dXNlcjpwYXNz"
        )
        redacted = mem0.redact_sensitive(original)
        for secret in (
            "aws-secret",
            "alice:db-secret",
            "C:/Users/Alice",
            "/home/bob",
            "/Users/carol",
            "10.20.30.40",
            "2001:db8::1",
            "2001:db8::2",
            "bare-bearer-secret",
            "bare-token-secret",
            "dXNlcjpwYXNz",
        ):
            self.assertNotIn(secret, redacted)

        context = mem0.format_context(
            {
                "structuredContent": {
                    "results": [{"id": "memory-1", "memory": original}]
                }
            }
        )
        self.assertNotIn("aws-secret", context)
        self.assertNotIn("10.20.30.40", context)
        self.assertIn("[mem0:memory-1]", context)

    def test_独立粘贴的常见凭据会脱敏(self):
        secrets = (
            "m0sk_" + "A" * 48,
            "sk-proj-" + "B" * 40,
            "ghp_" + "c" * 36,
            "AKIA" + "D" * 16,
        )
        redacted = mem0.redact_sensitive("，".join(secrets))

        for secret in secrets:
            self.assertNotIn(secret, redacted)
        self.assertEqual(redacted.count("[凭据已脱敏]"), len(secrets))
        self.assertIn("m0sk_示例", mem0.redact_sensitive("m0sk_示例"))

        json_text = json.dumps({"value": secrets[0]}, ensure_ascii=False)
        self.assertNotIn(secrets[0], mem0.redact_sensitive(json_text))
        self.assertIn("M0SK_" + "A" * 48, mem0.redact_sensitive("M0SK_" + "A" * 48))

    def test_原子写入并发使用独立临时文件(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            values = [{"value": index} for index in range(24)]
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(mem0.atomic_write_json, path, value)
                    for value in values
                ]
                for future in futures:
                    future.result()

            self.assertIn(mem0.load_json_file(path, {}), values)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_自动导入会合并短章节并限制分块数量(self):
        short_sections = "\n".join(
            f"## 规则 {index}\n必须执行第 {index} 项验证。" for index in range(8)
        )
        chunks = mem0.split_import_content(short_sections, True)
        self.assertTrue(chunks)
        self.assertIn("规则 0", "\n".join(chunks))
        self.assertIn("规则 7", "\n".join(chunks))

        many_sections = "\n".join(
            f"## 规则 {index}\n" + "必须测试。" * 10 for index in range(60)
        )
        many_chunks = mem0.split_import_content(many_sections, True)
        self.assertLessEqual(len(many_chunks), 50)
        self.assertTrue(all(len(chunk) <= mem0.MAX_IMPORT_CHUNK_SIZE for chunk in many_chunks))

    def test_pretool_自动补齐项目范围(self):
        value = self.capture_json(
            mem0.handle_pre_tool,
            {
                "tool_name": "mcp__plugin_mem0_mem0__search_memories",
                "tool_input": {"query": "代理启动"},
            },
            "demo-project",
        )
        specific = value["hookSpecificOutput"]
        self.assertEqual(specific["permissionDecision"], "allow")
        self.assertEqual(specific["updatedInput"]["project_id"], "demo-project")

    def test_pretool_为新增记忆补齐官方兼容元数据(self):
        value = self.capture_json(
            mem0.handle_pre_tool,
            {
                "tool_name": "mcp__plugin_mem0_mem0__add_memory",
                "tool_input": {"text": "记录一次任务经验"},
                "session_id": "session-1",
            },
            "demo-project",
        )
        updated = value["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["project_id"], "demo-project")
        self.assertEqual(
            updated["metadata"],
            {
                "type": "task_learning",
                "confidence": 0.7,
                "codex_origin": "tool",
                "session_id": "session-1",
            },
        )

        explicit = self.capture_json(
            mem0.handle_pre_tool,
            {
                "tool_name": "mcp__mem0__add_memory",
                "tool_input": {
                    "messages": [{"role": "user", "content": "采用新架构"}],
                    "project_id": "demo-project",
                    "metadata": {"type": "decision", "confidence": 1.0},
                },
            },
            "demo-project",
        )["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(explicit["metadata"]["type"], "decision")
        self.assertEqual(explicit["metadata"]["confidence"], 1.0)
        self.assertEqual(explicit["metadata"]["codex_origin"], "tool")
        self.assertFalse(explicit["infer"])

    def test_pretool_按管理工具语义补齐项目范围(self):
        delete_all = self.capture_json(
            mem0.handle_pre_tool,
            {
                "tool_name": "mcp__plugin_mem0_mem0__delete_all_memories",
                "tool_input": {"run_id": "run-1"},
            },
            "demo-project",
        )
        self.assertEqual(
            delete_all["hookSpecificOutput"]["updatedInput"]["project_id"],
            "demo-project",
        )

        delete_run = self.capture_json(
            mem0.handle_pre_tool,
            {
                "tool_name": "mcp__mem0__delete_entities",
                "tool_input": {"entity_type": "run", "entity_id": "run-1"},
            },
            "demo-project",
        )
        self.assertEqual(
            delete_run["hookSpecificOutput"]["updatedInput"]["project_id"],
            "demo-project",
        )

    def test_pretool_实体枚举和项目实体删除不自动限缩(self):
        for tool_input in (
            {"entity_type": "project"},
            {"entity_type": "project", "entity_id": "other-project"},
        ):
            tool_name = "list_entities" if "entity_id" not in tool_input else "delete_entities"
            value = self.capture_json(
                mem0.handle_pre_tool,
                {
                    "tool_name": f"mcp__plugin_mem0_mem0__{tool_name}",
                    "tool_input": tool_input,
                },
                "demo-project",
            )
            self.assertEqual(value, {})

        resolver = self.capture_json(
            mem0.handle_pre_tool,
            {
                "tool_name": "mcp__plugin_mem0_mem0__resolve_project_scope",
                "tool_input": {"repository_fingerprint": "a" * 64},
            },
            "demo-project",
        )
        self.assertEqual(resolver, {})

    def test_pretool_保护托管记忆文件(self):
        value = self.capture_json(
            mem0.handle_pre_tool,
            {
                "tool_name": "apply_patch",
                "tool_input": {"patch": "*** Update File: .codex/memories/MEMORY.md\n"},
            },
            "demo-project",
        )
        specific = value["hookSpecificOutput"]
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertIn("add_memory", specific["permissionDecisionReason"])

        managed_paths = (
            r"C:\Users\Alice\.codex\memories\长期 记忆.md",
            r"\\server\share\.codex\memories\MEMORY.md",
            "/home/alice/.codex/memories/长期 记忆.md",
            "/Users/alice/.codex/memories/MEMORY.md",
        )
        for path in managed_paths:
            with self.subTest(path=path):
                self.assertEqual(mem0.tool_paths({"file_path": path}), [path])
                denied = self.capture_json(
                    mem0.handle_pre_tool,
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": path},
                    },
                    "demo-project",
                )
                reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertNotIn(path, reason)
                self.assertEqual(
                    denied["hookSpecificOutput"]["permissionDecision"],
                    "deny",
                )

        patch_path = "/Users/alice/.codex/memories/长期 记忆.md"
        self.assertIn(
            patch_path,
            mem0.tool_paths({"patch": f"*** Update File: {patch_path}\n"}),
        )

    def test_命令错误签名会脱敏(self):
        signature = mem0.error_signature(
            {"command": "python app.py"},
            "Authorization: Bearer abc123\nTraceback (most recent call last)\nValueError: bad",
        )
        self.assertIn("Traceback", signature)
        self.assertNotIn("abc123", signature)

        bare = mem0.error_signature(
            {"command": "python app.py"},
            "Error: Bearer standalone-secret 无法通过认证" * 3,
        )
        self.assertIn("Error", bare)
        self.assertNotIn("standalone-secret", bare)

    def test_错误响应扁平化保持线性性能(self):
        started = time.perf_counter()
        flattened = mem0.flatten_text(["x"] * 30_000, 30_000)
        elapsed = time.perf_counter() - started

        self.assertLessEqual(len(flattened), 30_000)
        self.assertGreater(len(flattened), 29_000)
        self.assertLess(elapsed, 2.0)

    def test_读文件前检索相关历史(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "service.py"
            source.parent.mkdir()
            source.write_text("x = 1\n" * 300, encoding="utf-8")
            result = {
                "structuredContent": {
                    "results": [{"id": "memory-123", "memory": "此前修复过空指针"}]
                }
            }
            with mock.patch.object(mem0, "git_root", return_value=root), mock.patch.object(
                mem0, "call_tool", return_value=result
            ) as call:
                context = mem0.file_context(str(source), str(root), "demo-project")
            self.assertIn("此前修复过空指针", context)
            self.assertEqual(call.call_args.args[0], "search_memories")
            self.assertEqual(call.call_args.args[1]["project_id"], "demo-project")

    def test_项目文件按哈希导入替换并清理已删除文件(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            agents = root / "AGENTS.md"
            agents.write_text(
                "## 约定\nTOKEN=secret-value\n"
                "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----\n"
                + "必须运行测试。\n" * 20,
                encoding="utf-8",
            )
            calls: list[tuple[str, dict]] = []
            next_id = 0
            memories: dict[str, str] = {}
            fail_search = False

            def fake_call(name, arguments):
                nonlocal next_id
                calls.append((name, arguments))
                if name == "add_memory":
                    next_id += 1
                    memories[f"m-{next_id}"] = arguments["text"]
                    return {}
                if name == "search_memories":
                    if fail_search:
                        raise RuntimeError("临时查询失败")
                    return {
                        "structuredContent": {
                            "results": [
                                {"id": memory_id, "memory": text}
                                for memory_id, text in memories.items()
                            ]
                        }
                    }
                if name == "delete_memory":
                    memories.pop(arguments["memory_id"], None)
                return {}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "resolve_branch", return_value="main"), mock.patch.object(
                mem0, "call_tool", side_effect=fake_call
            ):
                mem0.auto_import_project_files(str(root), "demo-project")
                first_add_count = sum(name == "add_memory" for name, _ in calls)
                imported_text = "\n".join(
                    arguments["text"] for name, arguments in calls if name == "add_memory"
                )
                self.assertNotIn("secret-value", imported_text)
                self.assertNotIn("private-material", imported_text)
                self.assertIn("[已脱敏]", imported_text)
                mem0.auto_import_project_files(str(root), "demo-project")
                self.assertEqual(sum(name == "add_memory" for name, _ in calls), first_add_count)
                memories.clear()
                mem0.auto_import_project_files(str(root), "demo-project")
                self.assertGreater(sum(name == "add_memory" for name, _ in calls), first_add_count)
                agents.write_text("## 约定\n" + "必须运行全部测试。\n" * 20, encoding="utf-8")
                mem0.auto_import_project_files(str(root), "demo-project")
                agents.unlink()
                fail_search = True
                mem0.auto_import_project_files(str(root), "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                scope = mem0.import_scope_key(root, "demo-project")
                self.assertIn("AGENTS.md", state.get(scope, {}))

                fail_search = False
                mem0.auto_import_project_files(str(root), "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                self.assertNotIn("AGENTS.md", state.get(scope, {}))

            self.assertGreater(sum(name == "add_memory" for name, _ in calls), first_add_count)
            deletes = [arguments for name, arguments in calls if name == "delete_memory"]
            self.assertTrue(deletes)
            self.assertTrue(all(arguments["memory_id"].startswith("m-") for arguments in deletes))
            self.assertFalse(memories)

    def test_自动导入失败可续跑且空文件会清理旧记忆(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            agents = root / "AGENTS.md"
            agents.write_text("## 规则\n" + "必须验证旧流程。\n" * 20, encoding="utf-8")
            memories: dict[str, str] = {}
            add_count = 0
            fail_search = True
            fail_delete = False
            fail_add = False

            def fake_call(name, arguments):
                nonlocal add_count
                if name == "add_memory":
                    if fail_add:
                        raise RuntimeError("临时新增失败")
                    add_count += 1
                    memories[f"m-{add_count}"] = arguments["text"]
                    return {}
                if name == "search_memories":
                    if fail_search:
                        raise RuntimeError("临时查询失败")
                    return {
                        "structuredContent": {
                            "results": [
                                {"id": memory_id, "memory": text}
                                for memory_id, text in memories.items()
                            ]
                        }
                    }
                if name == "delete_memory":
                    if fail_delete:
                        raise RuntimeError("临时删除失败")
                    memories.pop(arguments["memory_id"], None)
                return {}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "resolve_branch", return_value="main"), mock.patch.object(
                mem0, "call_tool", side_effect=fake_call
            ):
                mem0.auto_import_project_files(str(root), "demo-project")
                scope = mem0.import_scope_key(root, "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                self.assertTrue(state[scope]["AGENTS.md"]["pending"])
                self.assertEqual(add_count, 1)

                fail_search = False
                mem0.auto_import_project_files(str(root), "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                self.assertNotIn("pending", state[scope]["AGENTS.md"])
                self.assertEqual(add_count, 1)

                fail_search = True
                mem0.auto_import_project_files(str(root), "demo-project")
                self.assertEqual(add_count, 1)

                fail_search = False
                old_hash = state[scope]["AGENTS.md"]["sha256"]
                agents.write_text("## 规则\n" + "必须验证新流程。\n" * 20, encoding="utf-8")
                fail_add = True
                mem0.auto_import_project_files(str(root), "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                self.assertNotEqual(state[scope]["AGENTS.md"]["sha256"], old_hash)
                self.assertEqual(state[scope]["AGENTS.md"]["previous_hash"], old_hash)
                self.assertTrue(state[scope]["AGENTS.md"]["pending"])
                self.assertEqual(add_count, 1)
                self.assertEqual(len(memories), 1)

                fail_add = False
                fail_delete = True
                mem0.auto_import_project_files(str(root), "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                self.assertEqual(state[scope]["AGENTS.md"]["previous_hash"], old_hash)
                self.assertTrue(state[scope]["AGENTS.md"]["pending"])
                self.assertEqual(add_count, 2)
                self.assertEqual(len(memories), 2)

                fail_delete = False
                mem0.auto_import_project_files(str(root), "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                self.assertNotEqual(state[scope]["AGENTS.md"]["sha256"], old_hash)
                self.assertEqual(add_count, 2)
                self.assertNotIn("pending", state[scope]["AGENTS.md"])
                self.assertNotIn("previous_hash", state[scope]["AGENTS.md"])
                self.assertEqual(len(memories), 1)

                agents.write_text("", encoding="utf-8")
                mem0.auto_import_project_files(str(root), "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                self.assertNotIn("AGENTS.md", state.get(scope, {}))
                self.assertFalse(memories)

    def test_自动导入按分块序号验证完整性(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            agents = root / "AGENTS.md"
            agents.write_text("## 规则\n" + "必须验证完整分块。\n" * 1200, encoding="utf-8")
            file_hash = hashlib.sha256(agents.read_bytes()).hexdigest()
            old_hash = "old-hash"
            memories = {
                "current-a": (
                    "[mem0:auto-import]\n项目：demo-project\n来源文件：AGENTS.md\n"
                    f"内容哈希：{file_hash}\n导入格式：{mem0.IMPORT_FORMAT_VERSION}\n"
                    "分块：1/2\n\n第一块"
                ),
                "current-b": (
                    "[mem0:auto-import]\n项目：demo-project\n来源文件：AGENTS.md\n"
                    f"内容哈希：{file_hash}\n导入格式：{mem0.IMPORT_FORMAT_VERSION}\n"
                    "分块：1/2\n\n重复第一块"
                ),
                "foreign-2": (
                    "[mem0:auto-import]\n项目：demo-project-other\n来源文件：AGENTS.md\n"
                    f"内容哈希：{file_hash}\n导入格式：{mem0.IMPORT_FORMAT_VERSION}\n"
                    "分块：2/2\n\n其他项目的第二块"
                ),
                "old-1": (
                    "[mem0:auto-import]\n项目：demo-project\n来源文件：AGENTS.md\n"
                    f"内容哈希：{old_hash}\n导入格式：2\n分块：1/1\n\n旧内容"
                ),
            }
            scope = mem0.import_scope_key(root, "demo-project")
            mem0.atomic_write_json(
                data / "auto_import_state.json",
                {
                    scope: {
                        "AGENTS.md": {
                            "sha256": file_hash,
                            "format_version": mem0.IMPORT_FORMAT_VERSION,
                            "memory_ids": ["current-a", "current-b"],
                            "chunks": 2,
                            "pending": True,
                            "previous_hash": old_hash,
                            "previous_format_version": 2,
                            "previous_memory_ids": ["old-1"],
                        }
                    }
                },
            )

            def fake_call(name, arguments):
                if name == "search_memories":
                    return {
                        "structuredContent": {
                            "results": [
                                {"id": memory_id, "memory": text}
                                for memory_id, text in memories.items()
                            ]
                        }
                    }
                if name == "delete_memory":
                    memories.pop(arguments["memory_id"], None)
                    return {}
                if name == "add_memory":
                    raise RuntimeError("阻止本次重建")
                return {}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "resolve_branch", return_value="main"), mock.patch.object(
                mem0, "call_tool", side_effect=fake_call
            ):
                mem0.auto_import_project_files(str(root), "demo-project")

            state = mem0.load_json_file(data / "auto_import_state.json", {})
            self.assertTrue(state[scope]["AGENTS.md"]["pending"])
            self.assertIn("old-1", memories)
            self.assertNotIn("current-a", memories)
            self.assertNotIn("current-b", memories)
            self.assertIn("foreign-2", memories)

    def test_自动导入分批清理会保留已验证分块(self):
        file_hash = "current-hash"
        header = (
            "[mem0:auto-import]\n项目：demo-project\n来源文件：AGENTS.md\n"
            f"内容哈希：{file_hash}\n导入格式：{mem0.IMPORT_FORMAT_VERSION}\n"
        )
        canonical_ids = ["current-1", "current-2"]
        memories = {
            "current-1": f"{header}分块：1/2\n\n当前第一块",
            "current-2": f"{header}分块：2/2\n\n当前第二块",
            **{
                f"extra-{index}": f"{header}分块：1/2\n\n重复块 {index}"
                for index in range(1, 26)
            },
        }
        delete_calls: list[str] = []
        search_count = 0

        def fake_call(name, arguments):
            nonlocal search_count
            if name == "search_memories":
                search_count += 1
                batch = list(memories.items())[: mem0.IMPORT_SEARCH_BATCH_SIZE]
                return {
                    "structuredContent": {
                        "results": [
                            {"id": memory_id, "memory": text}
                            for memory_id, text in batch
                        ]
                    }
                }
            if name == "delete_memory":
                memory_id = arguments["memory_id"]
                delete_calls.append(memory_id)
                memories.pop(memory_id)
            return {}

        with mock.patch.object(mem0, "call_tool", side_effect=fake_call):
            succeeded = mem0.delete_import_memories(
                "AGENTS.md",
                file_hash,
                "demo-project",
                exclude_ids=canonical_ids,
            )

        self.assertTrue(succeeded)
        self.assertEqual(set(memories), set(canonical_ids))
        self.assertEqual(set(delete_calls), {f"extra-{index}" for index in range(1, 26)})
        self.assertGreaterEqual(search_count, 3)

    def test_自动导入清理遇到固定搜索批次会安全停止(self):
        memory = (
            "[mem0:auto-import]\n项目：demo-project\n来源文件：AGENTS.md\n"
            f"内容哈希：fixed-hash\n导入格式：{mem0.IMPORT_FORMAT_VERSION}\n"
            "分块：1/1\n\n固定结果"
        )
        delete_calls: list[str] = []

        def fake_call(name, arguments):
            if name == "search_memories":
                return {
                    "structuredContent": {
                        "results": [{"id": "fixed-1", "memory": memory}]
                    }
                }
            if name == "delete_memory":
                delete_calls.append(arguments["memory_id"])
            return {}

        with mock.patch.object(mem0, "call_tool", side_effect=fake_call), mock.patch.object(
            mem0, "log_error"
        ) as log:
            succeeded = mem0.delete_import_memories(
                "AGENTS.md",
                "fixed-hash",
                "demo-project",
            )

        self.assertFalse(succeeded)
        self.assertEqual(delete_calls, ["fixed-1"])
        log.assert_called_with("自动导入清理搜索结果未能收敛")

    def test_自动导入部分清理会持久化剩余旧ID(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            agents = root / "AGENTS.md"
            agents.write_text("## 新规则\n" + "必须验证清理恢复。\n" * 20, encoding="utf-8")
            new_hash = hashlib.sha256(agents.read_bytes()).hexdigest()
            old_hash = "legacy-hash"
            memories = {
                "old-1": (
                    "[mem0:auto-import]\n项目：demo-project\n来源文件：AGENTS.md\n"
                    f"内容哈希：{old_hash}\n导入格式：2\n分块：1/2\n\n旧块一"
                ),
                "old-2": (
                    "[mem0:auto-import]\n项目：demo-project\n来源文件：AGENTS.md\n"
                    f"内容哈希：{old_hash}\n导入格式：2\n分块：2/2\n\n旧块二"
                ),
            }
            scope = mem0.import_scope_key(root, "demo-project")
            mem0.atomic_write_json(
                data / "auto_import_state.json",
                {
                    scope: {
                        "AGENTS.md": {
                            "sha256": old_hash,
                            "format_version": 2,
                            "memory_ids": ["old-1", "old-2"],
                            "chunks": 2,
                        }
                    }
                },
            )
            delete_calls: list[str] = []
            failed_once = False
            next_id = 0

            def fake_call(name, arguments):
                nonlocal failed_once, next_id
                if name == "add_memory":
                    next_id += 1
                    memories[f"new-{next_id}"] = arguments["text"]
                    return {}
                if name == "search_memories":
                    return {
                        "structuredContent": {
                            "results": [
                                {"id": memory_id, "memory": text}
                                for memory_id, text in memories.items()
                            ]
                        }
                    }
                if name == "delete_memory":
                    memory_id = arguments["memory_id"]
                    delete_calls.append(memory_id)
                    if memory_id == "old-2" and not failed_once:
                        failed_once = True
                        raise RuntimeError("临时删除失败")
                    if memory_id not in memories:
                        raise RuntimeError("记忆不存在")
                    memories.pop(memory_id)
                    return {}
                return {}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "resolve_branch", return_value="main"), mock.patch.object(
                mem0, "call_tool", side_effect=fake_call
            ):
                mem0.auto_import_project_files(str(root), "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                self.assertEqual(
                    state[scope]["AGENTS.md"]["previous_memory_ids"],
                    ["old-2"],
                )
                mem0.auto_import_project_files(str(root), "demo-project")

            state = mem0.load_json_file(data / "auto_import_state.json", {})
            self.assertNotIn("pending", state[scope]["AGENTS.md"])
            self.assertNotIn("previous_memory_ids", state[scope]["AGENTS.md"])
            self.assertEqual(delete_calls.count("old-1"), 1)
            self.assertEqual(delete_calls.count("old-2"), 2)
            self.assertNotIn("old-1", memories)
            self.assertNotIn("old-2", memories)
            self.assertEqual(state[scope]["AGENTS.md"]["sha256"], new_hash)

    def test_删除项目文件不会删除标记不匹配的状态ID(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            scope = mem0.import_scope_key(root, "demo-project")
            mem0.atomic_write_json(
                data / "auto_import_state.json",
                {
                    scope: {
                        "AGENTS.md": {
                            "sha256": "old-hash",
                            "format_version": mem0.IMPORT_FORMAT_VERSION,
                            "memory_ids": ["old-1"],
                            "chunks": 1,
                        }
                    }
                },
            )
            deleted: list[str] = []

            def fake_call(name, arguments):
                if name == "search_memories":
                    return {
                        "structuredContent": {
                            "results": [
                                {
                                    "id": "old-1",
                                    "memory": (
                                        "[mem0:auto-import]\n项目：other-project\n"
                                        "来源文件：AGENTS.md\n内容哈希：old-hash\n"
                                        f"导入格式：{mem0.IMPORT_FORMAT_VERSION}\n分块：1/1"
                                    ),
                                }
                            ]
                        }
                    }
                if name == "delete_memory":
                    deleted.append(arguments["memory_id"])
                return {}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "call_tool", side_effect=fake_call):
                mem0.auto_import_project_files(str(root), "demo-project")

            state = mem0.load_json_file(data / "auto_import_state.json", {})
            self.assertEqual(deleted, [])
            self.assertNotIn("AGENTS.md", state.get(scope, {}))

    def test_远端未返回的旧ID不会调用删除且状态会收敛(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            scope = mem0.import_scope_key(root, "demo-project")
            mem0.atomic_write_json(
                data / "auto_import_state.json",
                {
                    scope: {
                        "AGENTS.md": {
                            "sha256": "old-hash",
                            "format_version": mem0.IMPORT_FORMAT_VERSION,
                            "memory_ids": ["missing-1"],
                            "chunks": 1,
                        }
                    }
                },
            )
            delete_calls: list[str] = []

            def fake_call(name, arguments):
                if name == "search_memories":
                    return {"structuredContent": {"results": []}}
                if name == "delete_memory":
                    delete_calls.append(arguments["memory_id"])
                    raise RuntimeError("记忆不存在")
                return {}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "call_tool", side_effect=fake_call):
                mem0.auto_import_project_files(str(root), "demo-project")

            state = mem0.load_json_file(data / "auto_import_state.json", {})
            self.assertEqual(delete_calls, [])
            self.assertNotIn("AGENTS.md", state.get(scope, {}))

    def test_项目文件变得过大后会分批清理全部旧导入(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            agents = root / "AGENTS.md"
            agents.write_text("## 规则\n" + "必须保持规则最新。\n" * 20, encoding="utf-8")
            memories: dict[str, str] = {}
            delete_calls: list[str] = []
            search_calls: list[dict] = []
            next_id = 0

            def fake_call(name, arguments):
                nonlocal next_id
                if name == "add_memory":
                    next_id += 1
                    memories[f"m-{next_id}"] = arguments["text"]
                    return {}
                if name == "search_memories":
                    search_calls.append(arguments)
                    batch = list(memories.items())[: mem0.IMPORT_SEARCH_BATCH_SIZE]
                    return {
                        "structuredContent": {
                            "results": [
                                {"id": memory_id, "memory": text}
                                for memory_id, text in batch
                            ]
                        }
                    }
                if name == "delete_memory":
                    memory_id = arguments["memory_id"]
                    delete_calls.append(memory_id)
                    memories.pop(memory_id)
                return {}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "resolve_branch", return_value="main"), mock.patch.object(
                mem0, "call_tool", side_effect=fake_call
            ):
                mem0.auto_import_project_files(str(root), "demo-project")
                scope = mem0.import_scope_key(root, "demo-project")
                state = mem0.load_json_file(data / "auto_import_state.json", {})
                imported_id = state[scope]["AGENTS.md"]["memory_ids"][0]
                state[scope]["AGENTS.md"]["memory_ids"].append("state-only-id")
                mem0.atomic_write_json(data / "auto_import_state.json", state)
                imported_text = memories[imported_id]
                foreign_text = imported_text.replace("项目：demo-project", "项目：other-project")
                memories["foreign-1"] = foreign_text
                for index in range(2, 27):
                    memories[f"m-{index}"] = imported_text

                agents.write_bytes(b"x" * (mem0.MAX_IMPORT_FILE_SIZE + 1))
                mem0.auto_import_project_files(str(root), "demo-project")

            state = mem0.load_json_file(data / "auto_import_state.json", {})
            self.assertEqual(set(delete_calls), {f"m-{index}" for index in range(1, 27)})
            self.assertEqual(len(delete_calls), 26)
            self.assertNotIn("state-only-id", delete_calls)
            self.assertNotIn("foreign-1", delete_calls)
            self.assertEqual(memories, {"foreign-1": foreign_text})
            self.assertNotIn("AGENTS.md", state.get(scope, {}))
            self.assertGreaterEqual(len(search_calls), 4)
            self.assertTrue(
                all(call["top_k"] == mem0.IMPORT_SEARCH_BATCH_SIZE for call in search_calls)
            )

    def test_未变化的完整导入不会重写状态(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            agents = root / "AGENTS.md"
            agents.write_text("## 规则\n" + "必须保持状态稳定。\n" * 20, encoding="utf-8")
            file_hash = hashlib.sha256(agents.read_bytes()).hexdigest()
            memory = (
                "[mem0:auto-import]\n项目：demo-project\n来源文件：AGENTS.md\n"
                f"内容哈希：{file_hash}\n导入格式：{mem0.IMPORT_FORMAT_VERSION}\n"
                "分块：1/1\n\n已有内容"
            )
            scope = mem0.import_scope_key(root, "demo-project")
            mem0.atomic_write_json(
                data / "auto_import_state.json",
                {
                    scope: {
                        "AGENTS.md": {
                            "sha256": file_hash,
                            "format_version": mem0.IMPORT_FORMAT_VERSION,
                            "memory_ids": ["current-1"],
                            "chunks": 1,
                        }
                    }
                },
            )

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(
                mem0,
                "call_tool",
                return_value={
                    "structuredContent": {
                        "results": [{"id": "current-1", "memory": memory}]
                    }
                },
            ), mock.patch.object(mem0, "save_import_scope_state") as save_state:
                mem0._auto_import_project_files(str(root), "demo-project")

            save_state.assert_not_called()

    def test_自动导入状态并发合并不同项目(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "plugin-data"
            scopes = [f"scope-{index}" for index in range(12)]
            with mock.patch.object(mem0, "PLUGIN_DATA", data), ThreadPoolExecutor(
                max_workers=6
            ) as executor:
                futures = [
                    executor.submit(
                        mem0.save_import_scope_state,
                        scope,
                        {"AGENTS.md": {"sha256": scope}},
                    )
                    for scope in scopes
                ]
                for future in futures:
                    future.result()

            state = mem0.load_json_file(data / "auto_import_state.json", {})
            self.assertEqual(set(state), set(scopes))
            self.assertTrue(all(state[scope]["AGENTS.md"]["sha256"] == scope for scope in scopes))

    def test_相同内容的不同项目文件会分别更新(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            agents = root / "AGENTS.md"
            claude = root / "CLAUDE.md"
            agents.write_text("## 旧规则\n" + "保留旧规则。\n" * 20, encoding="utf-8")
            memories: dict[str, str] = {}
            next_id = 0

            def fake_call(name, arguments):
                nonlocal next_id
                if name == "add_memory":
                    next_id += 1
                    memories[f"m-{next_id}"] = arguments["text"]
                elif name == "search_memories":
                    return {
                        "structuredContent": {
                            "results": [
                                {"id": memory_id, "memory": text}
                                for memory_id, text in memories.items()
                            ]
                        }
                    }
                elif name == "delete_memory":
                    memories.pop(arguments["memory_id"], None)
                return {}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "resolve_branch", return_value="main"), mock.patch.object(
                mem0, "call_tool", side_effect=fake_call
            ):
                mem0.auto_import_project_files(str(root), "demo-project")
                shared = "## 共享规则\n" + "必须执行共享验证。\n" * 20
                agents.write_text(shared, encoding="utf-8")
                claude.write_text(shared, encoding="utf-8")
                mem0.auto_import_project_files(str(root), "demo-project")

            state = mem0.load_json_file(data / "auto_import_state.json", {})
            scope_state = state[mem0.import_scope_key(root, "demo-project")]
            self.assertEqual(scope_state["AGENTS.md"]["sha256"], scope_state["CLAUDE.md"]["sha256"])
            imported = "\n".join(memories.values())
            self.assertIn("来源文件：AGENTS.md", imported)
            self.assertIn("来源文件：CLAUDE.md", imported)
            self.assertNotIn("保留旧规则", imported)

    def test_会话统计并发更新不会丢失(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            mem0,
            "PLUGIN_DATA",
            Path(directory) / "data",
        ), ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(mem0.update_session_stats, "session-1", "search_memories")
                for _ in range(40)
            ]
            for future in futures:
                future.result()

            state = mem0.load_session_stats("session-1")
            self.assertEqual(state["operations"]["search_memories"], 40)

    def test_项目范围映射可持久保存和恢复(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(mem0, "PLUGIN_DATA", root / "data"), mock.patch.object(
                mem0, "git_root", return_value=root
            ):
                self.assertEqual(mem0.resolve_project_id(str(root)), root.name)
                self.assertEqual(mem0.set_project_mapping(str(root), "shared-project"), "shared-project")
                self.assertEqual(mem0.resolve_project_id(str(root)), "shared-project")
                self.assertEqual(mem0.set_project_mapping(str(root), None), root.name)
                self.assertEqual(mem0.resolve_project_id(str(root)), root.name)

    def test_服务端项目范围可在不同机器和克隆间共享(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source-copy"
            clone = base / "renamed-clone"
            remotes = (
                "git@github.com:team/shared-project.git",
                "https://github.com/team/shared-project.git",
            )
            for root, remote in zip((source, clone), remotes):
                git_directory = root / ".git"
                git_directory.mkdir(parents=True)
                (git_directory / "HEAD").write_text(
                    "ref: refs/heads/main\n",
                    encoding="utf-8",
                )
                (git_directory / "config").write_text(
                    f'[remote "origin"]\n\turl = {remote}\n',
                    encoding="utf-8",
                )

            fingerprints = []

            def resolve_scope(name, arguments):
                self.assertEqual(name, "resolve_project_scope")
                fingerprint = arguments["repository_fingerprint"]
                fingerprints.append(fingerprint)
                return {"structuredContent": {"project_id": fingerprint}}

            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.dict(
                    mem0.os.environ,
                    {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-shared-user"},
                ), mock.patch.object(mem0, "call_tool", side_effect=resolve_scope):
                    with mock.patch.object(mem0, "PLUGIN_DATA", base / "machine-a"):
                        source_id = mem0.sync_project_scope(str(source))
                        source_status = mem0.project_scope_status(str(source))
                    with mock.patch.object(mem0, "PLUGIN_DATA", base / "machine-b"):
                        clone_id = mem0.sync_project_scope(str(clone))
                        clone_status = mem0.project_scope_status(str(clone))

                self.assertEqual(source_id, clone_id)
                self.assertEqual(fingerprints, [source_id, clone_id])
                self.assertEqual(source_status["source"], "服务端同步范围")
                self.assertEqual(clone_status["source"], "服务端同步范围")
                self.assertTrue(source_status["sync_available"])
                self.assertTrue(source_status["synchronized"])
                self.assertFalse((source / ".mem0" / "project.json").exists())
            finally:
                mem0._cached_git_root.cache_clear()

    def test_远端身份忽略协议默认端口(self):
        expected = "github.com/team/shared-project"
        for remote in (
            "git@github.com:team/shared-project.git",
            "ssh://git@github.com:22/team/shared-project.git",
            "https://github.com/team/shared-project.git",
            "https://github.com:443/team/shared-project.git",
            "http://github.com:80/team/shared-project.git",
            "git://github.com:9418/team/shared-project.git",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(mem0._normalized_remote_identity(remote), expected)
        self.assertEqual(
            mem0._normalized_remote_identity("ssh://git@github.com:2222/team/shared-project.git"),
            "github.com:2222/team/shared-project",
        )

    def test_新仓库首次启动自动同步且缓存命中不重复请求(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "demo-project"
            git_directory = root / ".git"
            git_directory.mkdir(parents=True)
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git_directory / "config").write_text(
                '[remote "origin"]\n\turl = git@github.com:team/demo-project.git\n',
                encoding="utf-8",
            )
            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", base / "data"), mock.patch.dict(
                    mem0.os.environ,
                    {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-auto-sync"},
                ), mock.patch.object(
                    mem0,
                    "call_tool",
                    return_value={"structuredContent": {"project_id": "server-project"}},
                ) as call:
                    initial_status = mem0.project_scope_status(str(root))
                    self.assertEqual(initial_status["source"], "自动识别")
                    self.assertFalse(initial_status["migration_required"])
                    self.assertEqual(mem0.maybe_auto_sync_project_scope(str(root)), "")
                    self.assertEqual(mem0.resolve_project_id(str(root)), "server-project")
                    self.assertEqual(mem0.maybe_auto_sync_project_scope(str(root)), "")
                    status = mem0.project_scope_status(str(root))
                    cache_text = mem0.server_project_scopes_path().read_text(encoding="utf-8")

                call.assert_called_once()
                self.assertEqual(status["source"], "服务端同步范围")
                self.assertTrue(status["synchronized"])
                self.assertFalse(status["migration_required"])
                self.assertNotIn("m0sk_test-auto-sync", cache_text)
            finally:
                mem0._cached_git_root.cache_clear()

    def test_项目范围缓存按连接凭据隔离(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "demo-project"
            git_directory = root / ".git"
            git_directory.mkdir(parents=True)
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git_directory / "config").write_text(
                '[remote "origin"]\n\turl = https://github.com/team/demo-project.git\n',
                encoding="utf-8",
            )

            def resolve_scope(_name, _arguments):
                token = mem0.os.environ["MEM0_SELF_HOSTED_API_KEY"]
                return {
                    "structuredContent": {
                        "project_id": "project-a" if token.endswith("-a") else "project-b"
                    }
                }

            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", base / "data"), mock.patch.object(
                    mem0,
                    "call_tool",
                    side_effect=resolve_scope,
                ) as call:
                    with mock.patch.dict(
                        mem0.os.environ,
                        {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-user-a"},
                    ):
                        self.assertEqual(mem0.sync_project_scope(str(root)), "project-a")
                    with mock.patch.dict(
                        mem0.os.environ,
                        {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-user-b"},
                    ):
                        self.assertEqual(mem0.sync_project_scope(str(root)), "project-b")
                        self.assertEqual(mem0.resolve_project_id(str(root)), "project-b")
                    with mock.patch.dict(
                        mem0.os.environ,
                        {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-user-a"},
                    ):
                        self.assertEqual(mem0.resolve_project_id(str(root)), "project-a")

                    cache = mem0.load_json_file(mem0.server_project_scopes_path(), {})
                    cache_text = mem0.server_project_scopes_path().read_text(encoding="utf-8")

                self.assertEqual(call.call_count, 2)
                self.assertEqual(cache["version"], 2)
                self.assertEqual(len(cache["scopes"]), 2)
                self.assertNotIn("m0sk_test-user-a", cache_text)
                self.assertNotIn("m0sk_test-user-b", cache_text)
            finally:
                mem0._cached_git_root.cache_clear()

    def test_旧项目不会自动切换但手动同步后解除迁移提示(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "demo-project"
            git_directory = root / ".git"
            git_directory.mkdir(parents=True)
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git_directory / "config").write_text(
                '[remote "origin"]\n\turl = https://github.com/team/demo-project.git\n',
                encoding="utf-8",
            )
            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", base / "data"), mock.patch.dict(
                    mem0.os.environ,
                    {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-user-a"},
                ), mock.patch.object(
                    mem0,
                    "call_tool",
                    return_value={"structuredContent": {"project_id": "server-project"}},
                ) as call:
                    identity = mem0._repository_identity_key(root)
                    mem0.atomic_write_json(
                        mem0.project_claims_path(),
                        {
                            "version": 1,
                            "claims": {
                                identity: {
                                    "project_id": "demo-project",
                                    "legacy_project_id": "demo-project",
                                    "collision": False,
                                }
                            },
                        },
                    )
                    self.assertEqual(mem0.resolve_project_id(str(root)), "demo-project")
                    notice = mem0.maybe_auto_sync_project_scope(str(root))
                    status = mem0.project_scope_status(str(root))
                    call.assert_not_called()

                    self.assertIn("已有本机项目范围", notice)
                    self.assertTrue(status["migration_required"])
                    self.assertEqual(mem0.sync_project_scope(str(root)), "server-project")
                    self.assertFalse(mem0.project_scope_status(str(root))["migration_required"])
            finally:
                mem0._cached_git_root.cache_clear()

    def test_自动同步失败后即使生成本机范围仍会重试(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "demo-project"
            git_directory = root / ".git"
            git_directory.mkdir(parents=True)
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git_directory / "config").write_text(
                '[remote "origin"]\n\turl = https://github.com/team/demo-project.git\n',
                encoding="utf-8",
            )
            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", base / "data"), mock.patch.dict(
                    mem0.os.environ,
                    {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-retry"},
                ), mock.patch.object(
                    mem0,
                    "call_tool",
                    side_effect=[
                        RuntimeError("temporary failure"),
                        {"structuredContent": {"project_id": "server-project"}},
                    ],
                ) as call:
                    notice = mem0.maybe_auto_sync_project_scope(str(root))
                    self.assertIn("后续启动会自动重试", notice)
                    self.assertEqual(mem0.resolve_project_id(str(root)), "demo-project")
                    self.assertTrue(mem0.project_scope_sync_pending(str(root)))
                    self.assertEqual(mem0.maybe_auto_sync_project_scope(str(root)), "")
                    self.assertEqual(mem0.resolve_project_id(str(root)), "server-project")
                    self.assertFalse(mem0.project_scope_sync_pending(str(root)))

                self.assertEqual(call.call_count, 2)
            finally:
                mem0._cached_git_root.cache_clear()

    def test_项目范围待同步时阻止所有自动写入(self):
        policy = {
            "settings": {},
            "search": [],
            "ignore": [],
            "identity": [],
            "categories": [],
            "retention": {},
        }
        settings = {**mem0.DEFAULT_SETTINGS, "auto_search": False}
        with mock.patch.object(mem0, "parse_mem0_md", return_value=policy), mock.patch.object(
            mem0,
            "load_settings",
            return_value=settings,
        ), mock.patch.object(
            mem0,
            "maybe_auto_sync_project_scope",
            return_value="自动获取跨机器项目范围失败；记忆写入已暂停。",
        ), mock.patch.object(
            mem0,
            "resolve_project_id",
            return_value="demo-project",
        ), mock.patch.object(
            mem0,
            "project_scope_sync_pending",
            return_value=True,
        ), mock.patch.object(
            mem0,
            "project_scope_notice",
            return_value="",
        ), mock.patch.object(mem0, "auto_import_project_files") as auto_import, mock.patch.object(
            mem0,
            "save_summary",
        ) as save:
            startup = self.capture_json(
                mem0.handle_event,
                {
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                    "cwd": "demo",
                },
            )
            self.capture_json(
                mem0.handle_event,
                {
                    "hook_event_name": "Stop",
                    "cwd": "demo",
                    "last_assistant_message": "已经完成关键架构决定。",
                },
            )
            denied = self.capture_json(
                mem0.handle_event,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__plugin_mem0_mem0__add_memory",
                    "tool_input": {"text": "不应写入本机临时范围"},
                    "cwd": "demo",
                },
            )

        auto_import.assert_not_called()
        save.assert_not_called()
        self.assertIn("记忆写入已暂停", json.dumps(startup, ensure_ascii=False))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("尚未同步", denied["hookSpecificOutput"]["permissionDecisionReason"])

    def test_会话启动执行自动项目同步并注入迁移提示(self):
        policy = {
            "settings": {},
            "search": [],
            "ignore": [],
            "identity": [],
            "categories": [],
            "retention": {},
        }
        settings = {**mem0.DEFAULT_SETTINGS, "auto_search": False}
        with mock.patch.object(
            mem0,
            "parse_mem0_md",
            return_value=policy,
        ), mock.patch.object(
            mem0,
            "load_settings",
            return_value=settings,
        ), mock.patch.object(
            mem0,
            "maybe_auto_sync_project_scope",
            return_value="需要确认迁移旧项目范围。",
        ) as synchronize, mock.patch.object(
            mem0,
            "resolve_project_id",
            return_value="demo-project",
        ), mock.patch.object(
            mem0,
            "project_scope_notice",
            return_value="",
        ):
            output = self.capture_json(
                mem0.handle_event,
                {
                    "hook_event_name": "SessionStart",
                    "source": "resume",
                    "cwd": "demo",
                },
            )

        synchronize.assert_called_once_with("demo", True)
        self.assertIn("需要确认迁移旧项目范围", json.dumps(output, ensure_ascii=False))

    def test_无远端时同步失败并保留自动范围(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-project"
            git_directory = root / ".git"
            git_directory.mkdir(parents=True)
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", Path(directory) / "data"), mock.patch.object(
                    mem0, "call_tool"
                ) as call:
                    status = mem0.project_scope_status(str(root))
                    with self.assertRaisesRegex(ValueError, "没有可识别的远端地址"):
                        mem0.sync_project_scope(str(root))

                self.assertEqual(status["project_id"], "demo-project")
                self.assertEqual(status["source"], "自动识别")
                self.assertFalse(status["sync_available"])
                self.assertFalse(status["synchronized"])
                call.assert_not_called()
            finally:
                mem0._cached_git_root.cache_clear()

    def test_显式映射优先且清除时同时删除服务端缓存(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-project"
            git_directory = root / ".git"
            git_directory.mkdir(parents=True)
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git_directory / "config").write_text(
                '[remote "origin"]\n\turl = git@github.com:team/demo-project.git\n',
                encoding="utf-8",
            )
            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", Path(directory) / "data"), mock.patch.dict(
                    mem0.os.environ,
                    {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-user-a"},
                ), mock.patch.object(
                    mem0,
                    "call_tool",
                    return_value={"structuredContent": {"project_id": "server-project"}},
                ):
                    self.assertEqual(mem0.sync_project_scope(str(root)), "server-project")
                    self.assertEqual(mem0.set_project_mapping(str(root), "explicit-project"), "explicit-project")
                    self.assertEqual(mem0.project_scope_status(str(root))["source"], "本机显式映射")
                    self.assertEqual(mem0.set_project_mapping(str(root), None), "demo-project")
                    status = mem0.project_scope_status(str(root))
                    cache = mem0.load_json_file(mem0.server_project_scopes_path(), {})

                self.assertEqual(status["source"], "自动识别")
                self.assertFalse(status["synchronized"])
                self.assertEqual(cache["scopes"], {})
            finally:
                mem0._cached_git_root.cache_clear()

    def test_同步项目命令返回服务端范围和最终优先级(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-project"
            git_directory = root / ".git"
            git_directory.mkdir(parents=True)
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git_directory / "config").write_text(
                '[remote "origin"]\n\turl = https://github.com/team/demo-project.git\n',
                encoding="utf-8",
            )
            output = io.StringIO()
            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", Path(directory) / "data"), mock.patch.dict(
                    mem0.os.environ,
                    {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-command"},
                ), mock.patch.object(
                    mem0,
                    "call_tool",
                    return_value={"structuredContent": {"project_id": "server-project"}},
                ), mock.patch.object(
                    mem0.sys,
                    "argv",
                    [str(SCRIPT), "--sync-project", "--cwd", str(root)],
                ), redirect_stdout(output):
                    self.assertEqual(mem0.main(), 0)

                result = json.loads(output.getvalue())
                self.assertEqual(result["状态"], "已同步服务端项目范围")
                self.assertEqual(result["project_id"], "server-project")
                self.assertEqual(result["server_project_id"], "server-project")
                self.assertEqual(result["source"], "服务端同步范围")
                self.assertTrue(result["synchronized"])
            finally:
                mem0._cached_git_root.cache_clear()

    def test_同步项目命令错误会脱敏且不输出回溯(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-project"
            git_directory = root / ".git"
            git_directory.mkdir(parents=True)
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git_directory / "config").write_text(
                '[remote "origin"]\n\turl = https://github.com/team/demo-project.git\n',
                encoding="utf-8",
            )
            output = io.StringIO()
            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", Path(directory) / "data"), mock.patch.dict(
                    mem0.os.environ,
                    {"MEM0_SELF_HOSTED_API_KEY": "m0sk_test-error"},
                ), mock.patch.object(
                    mem0,
                    "call_tool",
                    side_effect=RuntimeError("Authorization: Bearer private-sync-token"),
                ), mock.patch.object(
                    mem0.sys,
                    "argv",
                    [str(SCRIPT), "--sync-project", "--cwd", str(root)],
                ), redirect_stderr(output):
                    self.assertEqual(mem0.main(), 2)

                error = output.getvalue()
                result = json.loads(error)
                self.assertEqual(result["状态"], "失败")
                self.assertNotIn("private-sync-token", error)
                self.assertNotIn("Traceback", error)
            finally:
                mem0._cached_git_root.cache_clear()

    def test_同名仓库按远端身份隔离且只有旧范围提示迁移(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data = base / "plugin-data"

            def create_repo(parent, remote):
                root = base / parent / "shared-name"
                git_directory = root / ".git"
                git_directory.mkdir(parents=True)
                (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
                (git_directory / "config").write_text(
                    f'[remote "origin"]\n\turl = {remote}\n',
                    encoding="utf-8",
                )
                return root

            first = create_repo("first", "git@github.com:team/first.git")
            second = create_repo("second", "https://github.com/team/second.git")
            clone = create_repo("clone", "ssh://git@github.com/team/first.git")
            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", data):
                    first_id = mem0.resolve_project_id(str(first))
                    second_id = mem0.resolve_project_id(str(second))
                    clone_id = mem0.resolve_project_id(str(clone))

                    self.assertEqual(first_id, "shared-name")
                    self.assertNotEqual(second_id, first_id)
                    self.assertRegex(second_id, r"^shared-name-[0-9a-f]{12}$")
                    self.assertEqual(clone_id, first_id)
                    automatic_notice = mem0.project_scope_notice(str(second))
                    self.assertFalse(mem0.project_scope_status(str(second))["migration_required"])
                    self.assertIn(second_id, automatic_notice)
                    self.assertIn(first_id, automatic_notice)
                    self.assertIn("无需手动迁移", automatic_notice)
                    self.assertNotIn("switch-project", automatic_notice)

                    repository_fingerprint = mem0._repository_remote_fingerprint(second)
                    self.assertIsNotNone(repository_fingerprint)
                    mem0._set_project_sync_mode(repository_fingerprint, "legacy")
                    migration_notice = mem0.project_scope_notice(str(second))
                    self.assertTrue(mem0.project_scope_status(str(second))["migration_required"])
                    self.assertIn("switch-project", migration_notice)

                    self.assertEqual(
                        mem0.set_project_mapping(str(second), "shared-team-project"),
                        "shared-team-project",
                    )
                    self.assertEqual(mem0.project_scope_notice(str(second)), "")
            finally:
                mem0._cached_git_root.cache_clear()

    def test_同名无远端仓库不提示无法执行的同步(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data = base / "plugin-data"
            first = base / "first" / "shared-name"
            second = base / "second" / "shared-name"
            for root in (first, second):
                (root / ".git").mkdir(parents=True)
                (root / ".git" / "HEAD").write_text(
                    "ref: refs/heads/main\n",
                    encoding="utf-8",
                )
            mem0._cached_git_root.cache_clear()
            try:
                with mock.patch.object(mem0, "PLUGIN_DATA", data):
                    self.assertEqual(mem0.resolve_project_id(str(first)), "shared-name")
                    second_id = mem0.resolve_project_id(str(second))
                    notice = mem0.project_scope_notice(str(second))

                    self.assertNotEqual(second_id, "shared-name")
                    self.assertIn("没有可识别的 Git 远端", notice)
                    self.assertNotIn("switch-project", notice)
                    self.assertFalse(mem0.project_scope_status(str(second))["sync_available"])
            finally:
                mem0._cached_git_root.cache_clear()

    def test_项目标识与生产契约一致(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "plugin-data"
            root = Path(directory) / "中文 项目"
            root.mkdir()
            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ):
                generated = mem0.resolve_project_id(str(root))
                self.assertRegex(generated, r"^[A-Za-z0-9._-]{1,64}$")
                self.assertEqual(generated, mem0.default_project_id(root))
                output = io.StringIO()
                with mock.patch.object(
                    mem0.sys,
                    "argv",
                    [str(SCRIPT), "--current-project", "--cwd", str(root)],
                ), redirect_stdout(output):
                    self.assertEqual(mem0.main(), 0)
                self.assertEqual(json.loads(output.getvalue())["project_id"], generated)
                self.assertEqual(
                    mem0.set_project_mapping(str(root), "team.alpha_1-beta"),
                    "team.alpha_1-beta",
                )
                for invalid in ("", "my project", "中文项目", "x" * 65, "team/project"):
                    with self.subTest(project_id=invalid), self.assertRaises(ValueError):
                        mem0.set_project_mapping(str(root), invalid)

                mem0.atomic_write_json(
                    mem0.project_mapping_path(),
                    {mem0._project_mapping_key(root): "旧版 非法映射"},
                )
                self.assertEqual(mem0.resolve_project_id(str(root)), generated)

    def test_项目映射并发更新不会丢失(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data = base / "plugin-data"
            roots = [base / f"workspace-{index}" for index in range(12)]
            for root in roots:
                root.mkdir()
            barrier = threading.Barrier(len(roots))
            original_atomic_write = mem0.atomic_write_json

            def slow_atomic_write(path, value):
                time.sleep(0.02)
                original_atomic_write(path, value)

            def update(index):
                barrier.wait()
                return mem0.set_project_mapping(str(roots[index]), f"project-{index}")

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", side_effect=lambda cwd: Path(cwd).resolve()
            ), mock.patch.object(
                mem0, "atomic_write_json", side_effect=slow_atomic_write
            ), ThreadPoolExecutor(max_workers=len(roots)) as executor:
                futures = [executor.submit(update, index) for index in range(len(roots))]
                for future in futures:
                    future.result()

            mappings = mem0.load_json_file(data / "project_mappings.json", {})
            self.assertEqual(len(mappings), len(roots))
            for index, root in enumerate(roots):
                self.assertEqual(
                    mappings[mem0._project_mapping_key(root.resolve())],
                    f"project-{index}",
                )

    def test_git根目录查询在单次进程内缓存(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            nested = root / "src" / "module"
            nested.mkdir(parents=True)
            git_directory = root / ".git"
            git_directory.mkdir()
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            mem0._cached_git_root.cache_clear()
            try:
                self.assertEqual(mem0.git_root(str(nested)), root)
                (git_directory / "HEAD").unlink()
                git_directory.rmdir()
                self.assertEqual(mem0.git_root(str(nested)), root)
                mem0._cached_git_root.cache_clear()
                self.assertEqual(mem0.git_root(str(nested)), nested)
            finally:
                mem0._cached_git_root.cache_clear()

    def test_git工作树指针和分支只用标准库解析(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "workspace"
            nested = root / "src"
            git_directory = base / "git-metadata"
            nested.mkdir(parents=True)
            git_directory.mkdir()
            (root / ".git").write_text("gitdir: ../git-metadata\n", encoding="utf-8")
            (git_directory / "HEAD").write_text(
                "ref: refs/heads/feature/windows-safe\n",
                encoding="utf-8",
            )
            mem0._cached_git_root.cache_clear()
            try:
                self.assertEqual(mem0.git_root(str(nested)), root)
                self.assertEqual(mem0.resolve_branch(str(nested)), "feature/windows-safe")
                (git_directory / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
                self.assertEqual(mem0.resolve_branch(str(nested)), "")
            finally:
                mem0._cached_git_root.cache_clear()

    @unittest.skipIf(os.name == "nt", "Windows 测试环境不保证允许创建符号链接")
    def test_git目录符号链接可被只读解析(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "workspace"
            nested = root / "src"
            git_directory = base / "git-metadata"
            nested.mkdir(parents=True)
            git_directory.mkdir()
            (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (root / ".git").symlink_to(git_directory, target_is_directory=True)
            mem0._cached_git_root.cache_clear()
            try:
                self.assertEqual(mem0.git_root(str(nested)), root)
                self.assertEqual(mem0.resolve_branch(str(nested)), "main")
            finally:
                mem0._cached_git_root.cache_clear()

    def test_git元数据读取受大小和引用格式限制(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            git_directory = root / ".git"
            git_directory.mkdir()
            (git_directory / "HEAD").write_text("ref: refs/heads/" + "x" * 5000, encoding="utf-8")
            mem0._cached_git_root.cache_clear()
            try:
                self.assertEqual(mem0.git_root(str(root)), root)
                self.assertEqual(mem0.resolve_branch(str(root)), "")
                (git_directory / "HEAD").write_text(
                    "ref: refs/heads/feature/../../config\n",
                    encoding="utf-8",
                )
                self.assertEqual(mem0.resolve_branch(str(root)), "")
            finally:
                mem0._cached_git_root.cache_clear()

    def test_文件锁不会按_mtime_抢占且进程退出后自动释放(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "state.lock"
            self.assertTrue(mem0.acquire_lock(lock_path, stale_after=1))
            os.utime(lock_path, (0, 0))
            self.assertFalse(mem0.acquire_lock(lock_path, stale_after=0))
            mem0.release_lock(lock_path)

            ready_path = Path(directory) / "ready"
            child_code = (
                "import importlib.util,pathlib,sys,time;"
                "spec=importlib.util.spec_from_file_location('child_mem0',sys.argv[1]);"
                "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);"
                "lock=pathlib.Path(sys.argv[2]);ready=pathlib.Path(sys.argv[3]);"
                "assert module.acquire_lock(lock);ready.write_text('ready',encoding='utf-8');"
                "time.sleep(30)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", child_code, str(SCRIPT), str(lock_path), str(ready_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            acquired = False
            try:
                deadline = time.monotonic() + 5
                while not ready_path.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready_path.exists())
                self.assertFalse(mem0.acquire_lock(lock_path))
                process.terminate()
                process.wait(timeout=5)
                acquired = mem0.wait_for_lock(lock_path, timeout=3)
                self.assertTrue(acquired)
                self.assertTrue(lock_path.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if acquired:
                    mem0.release_lock(lock_path)

    def test_项目状态键遵循当前系统的路径大小写语义(self):
        first = Path("workspace") / "Project"
        second = Path("workspace") / "project"
        expected_same = os.path.normcase(str(first)) == os.path.normcase(str(second))

        self.assertEqual(
            mem0.import_scope_key(first, "demo-project")
            == mem0.import_scope_key(second, "demo-project"),
            expected_same,
        )
        with mock.patch.object(mem0, "git_root", return_value=first):
            first_mapping = mem0.project_mapping_key(str(first))
        with mock.patch.object(mem0, "git_root", return_value=second):
            second_mapping = mem0.project_mapping_key(str(second))
        self.assertEqual(first_mapping == second_mapping, expected_same)

    def test_转录提取最近消息和触达文件(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "turn.jsonl"
            entries = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "修复登录问题"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "apply_patch",
                        "arguments": {"file_path": "src/auth.py"},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "已修复并通过测试"}],
                    },
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries),
                encoding="utf-8",
            )
            exchange, files = mem0.extract_transcript(str(transcript))
        self.assertIn("用户：修复登录问题", exchange)
        self.assertIn("助手：已修复并通过测试", exchange)
        self.assertIn("src/auth.py", files)

    def test_旧版未脱敏导入会被替换(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            agents = root / "AGENTS.md"
            agents.write_text("## 规则\nTOKEN=legacy-secret\n" + "需要测试。\n" * 20, encoding="utf-8")
            file_hash = hashlib.sha256(agents.read_bytes()).hexdigest()
            memories = {
                "legacy-1": (
                    "[mem0:auto-import]\n项目：demo-project\n来源文件：AGENTS.md\n"
                    f"内容哈希：{file_hash}\n分块：1/1\n\nTOKEN=legacy-secret"
                )
            }
            calls: list[tuple[str, dict]] = []
            state = {
                mem0.import_scope_key(root, "demo-project"): {
                    "AGENTS.md": {"sha256": file_hash, "memory_ids": ["legacy-1"], "chunks": 1}
                }
            }
            mem0.atomic_write_json(data / "auto_import_state.json", state)

            def fake_call(name, arguments):
                calls.append((name, arguments))
                if name == "add_memory":
                    memories["current-1"] = arguments["text"]
                    return {}
                if name == "search_memories":
                    return {
                        "structuredContent": {
                            "results": [
                                {"id": memory_id, "memory": text}
                                for memory_id, text in memories.items()
                            ]
                        }
                    }
                if name == "delete_memory":
                    memories.pop(arguments["memory_id"], None)
                return {}

            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "resolve_branch", return_value="main"), mock.patch.object(
                mem0, "call_tool", side_effect=fake_call
            ):
                mem0.auto_import_project_files(str(root), "demo-project")

            self.assertNotIn("legacy-1", memories)
            self.assertIn("current-1", memories)
            self.assertNotIn("legacy-secret", memories["current-1"])
            self.assertIn(f"导入格式：{mem0.IMPORT_FORMAT_VERSION}", memories["current-1"])

    def test_相同总结跨停止和压缩事件不会重复写入(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            hook_input = {"session_id": "session-1", "cwd": directory}
            with mock.patch.object(mem0, "PLUGIN_DATA", Path(directory) / "data"), mock.patch.object(
                mem0, "resolve_branch", return_value="main"
            ), mock.patch.object(mem0, "call_tool", side_effect=lambda *args: calls.append(args) or {}):
                text = "用户确认需要保留这个架构决定。" * 10
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(
                            mem0.save_summary,
                            text,
                            "demo-project",
                            kind,
                            hook_input,
                            ["src/app.py"],
                        )
                        for kind in ("本轮会话总结", "上下文压缩前总结")
                    ]
                    for future in futures:
                        future.result()
                mem0.save_summary(
                    text,
                    "other-project",
                    "本轮会话总结",
                    hook_input,
                    ["src/app.py"],
                )
            self.assertEqual(len(calls), 2)

    def test_文档搜索只允许官方主机(self):
        with mock.patch.object(mem0_docs, "fetch_url") as fetch:
            result = mem0_docs.fetch_page("http://127.0.0.1:8080/private")
            invalid_port = mem0_docs.fetch_page("https://docs.mem0.ai:invalid/page")
            invalid_ipv6 = mem0_docs.fetch_page("https://[bad")
        self.assertIn("error", result)
        self.assertIn("error", invalid_port)
        self.assertIn("error", invalid_ipv6)
        fetch.assert_not_called()

    def test_文档搜索拒绝跨主机重定向(self):
        request = mem0_docs.urllib.request.Request("https://docs.mem0.ai/start")
        handler = mem0_docs.DocsRedirectHandler()
        with self.assertRaises(mem0_docs.urllib.error.HTTPError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://127.0.0.1:8080/private",
            )

        with mock.patch.object(mem0_docs, "open_docs_request") as open_request:
            result = mem0_docs.fetch_url("https://example.com/private")
        self.assertIn("只允许", result)
        open_request.assert_not_called()

    def test_文档搜索限制响应大小(self):
        response = mock.MagicMock()
        response.read.return_value = b"x" * (mem0_docs.MAX_RESPONSE_BYTES + 1)
        context = mock.MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with mock.patch.object(mem0_docs, "open_docs_request", return_value=context):
            result = mem0_docs.fetch_url("https://docs.mem0.ai/llms.txt")

        self.assertIn("超过大小限制", result)
        response.read.assert_called_once_with(mem0_docs.MAX_RESPONSE_BYTES + 1)

    def test_文档搜索错误不会透传网络详情(self):
        with mock.patch.object(
            mem0_docs,
            "open_docs_request",
            side_effect=mem0_docs.urllib.error.URLError(
                "proxy://user:network-secret@internal.example"
            ),
        ):
            result = mem0_docs.fetch_url("https://docs.mem0.ai/llms.txt")

        self.assertEqual(result, "URL 请求失败")
        self.assertNotIn("network-secret", result)

    def test_文档索引只解析官方链接并去重(self):
        content = """
说明文字与 `MemoryClient` 代码不应被计为页面。
- [Graph Memory](https://docs.mem0.ai/open-source/features/graph-memory) [OSS]: 图记忆说明
- [Graph Memory 重复](https://docs.mem0.ai/open-source/features/graph-memory)
- [LLM 配置](https://docs.mem0.ai/components/llms/config) [OSS]: 模型配置
- [外部链接](https://example.com/private)
"""
        with mock.patch.object(mem0_docs, "fetch_url", return_value=content):
            index = mem0_docs.get_index()
            result = mem0_docs.search_docs("配置", section="open-source")

        self.assertEqual(index["total_pages"], 2)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["title"], "LLM 配置")
        self.assertEqual(
            result["matching_urls"],
            ["https://docs.mem0.ai/components/llms/config"],
        )
        self.assertTrue(all(mem0_docs.is_allowed_docs_url(url) for url in index["urls"]))

    def test_文档搜索限制结构化结果数量(self):
        content = "\n".join(
            f"- [Memory {index}](https://docs.mem0.ai/platform/page-{index})"
            for index in range(mem0_docs.MAX_SEARCH_RESULTS + 5)
        )
        with mock.patch.object(mem0_docs, "fetch_url", return_value=content):
            result = mem0_docs.search_docs("memory", section="platform")

        self.assertEqual(len(result["results"]), mem0_docs.MAX_SEARCH_RESULTS)
        self.assertEqual(len(result["matching_urls"]), mem0_docs.MAX_SEARCH_RESULTS)

    def test_文档搜索无结果时给出明确提示(self):
        content = "- [Graph Memory](https://docs.mem0.ai/platform/features/graph-memory)"
        output = io.StringIO()
        with mock.patch.object(mem0_docs, "fetch_url", return_value=content), mock.patch.object(
            mem0_docs.sys,
            "argv",
            [str(DOC_SCRIPT), "--query", "不存在的查询"],
        ), redirect_stdout(output):
            mem0_docs.main()

        text = output.getvalue()
        self.assertIn("查询：不存在的查询", text)
        self.assertIn("未找到匹配的官方文档", text)
        self.assertIn("可读取具体 URL", text)

    def test_文档索引错误不会伪装成搜索结果(self):
        with mock.patch.object(mem0_docs, "fetch_url", return_value="URL 请求失败"):
            result = mem0_docs.search_docs("请求")
            index = mem0_docs.get_index()

        self.assertEqual(result["error"], "URL 请求失败")
        self.assertNotIn("results", result)
        self.assertEqual(index, {"error": "URL 请求失败"})

        with mock.patch.object(mem0_docs, "fetch_url", return_value="格式已经改变"):
            malformed_result = mem0_docs.search_docs("memory")
            malformed_index = mem0_docs.get_index()
        self.assertEqual(malformed_result["error"], "文档索引格式无效")
        self.assertEqual(malformed_index, {"error": "文档索引格式无效"})

    def test_文档搜索在非_UTF8_控制台仍输出_UTF8(self):
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "cp1252"
        completed = subprocess.run(
            [sys.executable, str(DOC_SCRIPT), "--section", "api"],
            check=False,
            capture_output=True,
            env=environment,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        output = completed.stdout.decode("utf-8")
        self.assertIn("区段：api", output)
        self.assertIn("https://docs.mem0.ai/api-reference", output)

    def test_压缩后会话启动提取并保存真实摘要(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "compact.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "isCompactSummary": True,
                            "message": {
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "已完成权限隔离，并通过回归测试。" * 5,
                                    }
                                ]
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            calls: list[tuple[str, dict]] = []

            def fake_call(name, arguments):
                calls.append((name, arguments))
                return {"structuredContent": {"results": []}}

            policy = {
                "settings": {},
                "search": [],
                "ignore": [],
                "identity": [],
                "categories": [],
                "retention": {},
            }
            with mock.patch.object(mem0, "PLUGIN_DATA", root / "data"), mock.patch.object(
                mem0, "resolve_project_id", return_value="demo-project"
            ), mock.patch.object(mem0, "git_root", return_value=root), mock.patch.object(
                mem0, "resolve_branch", return_value="main"
            ), mock.patch.object(mem0, "parse_mem0_md", return_value=policy), mock.patch.object(
                mem0, "load_settings", return_value=dict(mem0.DEFAULT_SETTINGS)
            ), mock.patch.object(mem0, "call_tool", side_effect=fake_call):
                value = self.capture_json(
                    mem0.handle_event,
                    {
                        "hook_event_name": "SessionStart",
                        "source": "compact",
                        "transcript_path": str(transcript),
                        "session_id": "session-compact",
                        "cwd": str(root),
                    },
                )

        additions = [arguments for name, arguments in calls if name == "add_memory"]
        self.assertEqual(len(additions), 1)
        self.assertIn("已完成权限隔离", additions[0]["messages"][0]["content"])
        self.assertEqual(additions[0]["metadata"]["type"], "compact_summary")
        self.assertEqual(value, {})

    def test_mem0_md_解析六类原生配置(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mem0.md").write_text(
                "# 项目记忆\r\n\r\n"
                "## Settings\r\n- auto_save: false\r\n- search_limit = 7\r\n"
                "## Search\r\n- 安全边界\r\n- 发布验收\r\n"
                "## Ignore\r\n- node_modules\r\n"
                "## Identity\r\n- 这是桌面应用\r\n"
                "## Categories\r\n- 决定：长期架构决定\r\n"
                "## Retention\r\n- days: 45\r\n- exclude: 临时日志\r\n",
                encoding="utf-8",
            )
            with mock.patch.object(mem0, "git_root", return_value=root):
                policy = mem0.parse_mem0_md(str(root))

        self.assertEqual(policy["settings"]["auto_save"], "false")
        self.assertEqual(policy["settings"]["search_limit"], "7")
        self.assertEqual(policy["search"], ["安全边界", "发布验收"])
        self.assertEqual(policy["ignore"], ["node_modules"])
        self.assertEqual(policy["identity"], ["这是桌面应用"])
        self.assertEqual(policy["categories"], ["决定：长期架构决定"])
        self.assertEqual(policy["retention"]["days"], "45")
        self.assertEqual(policy["retention"]["exclude"], "临时日志")

    def test_设置按项目本机环境优先级合并并限制范围(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            (root / "mem0.md").write_text(
                "## Settings\n- auto_save: false\n- search_limit: 2\n"
                "## Retention\n- retention_session_days: 60\n",
                encoding="utf-8",
            )
            data.mkdir()
            (data / "settings.json").write_text(
                json.dumps(
                    {
                        "auto_save": True,
                        "auto_search": False,
                        "search_limit": 200,
                        "confidence_threshold": -1,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.dict(mem0.os.environ, {}, clear=False):
                for name in mem0.SETTING_ENV_VARS.values():
                    mem0.os.environ.pop(name, None)
                mem0.os.environ["MEM0_AUTO_SEARCH"] = "true"
                mem0.os.environ["MEM0_SEARCH_LIMIT"] = "7"
                mem0.os.environ["MEM0_RERANK"] = "false"
                policy = mem0.parse_mem0_md(str(root))
                settings = mem0.load_settings(str(root), policy)

        self.assertTrue(settings["auto_save"])
        self.assertTrue(settings["auto_search"])
        self.assertEqual(settings["search_limit"], 7)
        self.assertEqual(settings["confidence_threshold"], 0.0)
        self.assertEqual(settings["session_retention_days"], 60)
        self.assertFalse(settings["rerank"])

    def test_项目文件不能控制自动范围同步(self):
        settings = dict(mem0.DEFAULT_SETTINGS)
        with mock.patch.object(mem0, "log_error"):
            mem0._apply_settings_layer(
                settings,
                {"auto_sync_project": False},
                "mem0.md",
            )
        self.assertTrue(settings["auto_sync_project"])

        mem0._apply_settings_layer(
            settings,
            {"auto_sync_project": False},
            "settings.json",
        )
        self.assertFalse(settings["auto_sync_project"])

    def test_设置命令可初始化并显示安全默认值(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "plugin-data"
            with mock.patch.object(mem0, "PLUGIN_DATA", data), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0.sys, "argv", [str(mem0.SCRIPT if hasattr(mem0, "SCRIPT") else SCRIPT), "--init-settings", "--cwd", str(root)]), mock.patch.dict(
                mem0.os.environ, {}, clear=False
            ):
                for name in mem0.SETTING_ENV_VARS.values():
                    mem0.os.environ.pop(name, None)
                output = io.StringIO()
                with redirect_stdout(output):
                    status = mem0.main()
                initialized = json.loads(output.getvalue())

                self.assertEqual(status, 0)
                self.assertEqual(initialized["状态"], "已创建")
                self.assertTrue(initialized["设置"]["rerank"])
                self.assertEqual(initialized["设置"]["session_retention_days"], 90)
                self.assertTrue((data / "settings.json").is_file())

                with mock.patch.object(
                    mem0.sys,
                    "argv",
                    [str(SCRIPT), "--show-settings", "--cwd", str(root)],
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        status = mem0.main()
                    shown = json.loads(output.getvalue())

        self.assertEqual(status, 0)
        self.assertEqual(shown["状态"], "当前设置")
        self.assertEqual(shown["设置"], initialized["设置"])

    def test_提示检索门禁和多查询去重(self):
        policy = {
            "settings": {},
            "search": ["安全边界", "回归测试"],
            "ignore": ["node_modules"],
            "identity": [],
            "categories": [],
            "retention": {},
        }
        settings = dict(mem0.DEFAULT_SETTINGS)
        self.assertFalse(mem0.should_search_prompt("好的。", settings, policy))
        self.assertFalse(mem0.should_search_prompt("检查 node_modules", settings, policy))
        self.assertTrue(mem0.should_search_prompt("继续上次的部署工作", settings, policy))

        disabled = {**settings, "auto_search": False}
        with mock.patch.object(mem0, "resolve_project_id", return_value="demo-project"), mock.patch.object(
            mem0, "parse_mem0_md", return_value=policy
        ), mock.patch.object(mem0, "load_settings", return_value=disabled), mock.patch.object(
            mem0, "call_tool"
        ) as call:
            value = self.capture_json(
                mem0.handle_event,
                {"hook_event_name": "UserPromptSubmit", "prompt": "修复部署问题"},
            )
        self.assertEqual(value, {})
        call.assert_not_called()

        queries = mem0.build_search_queries(
            "继续上次工作，修复 src/service.py 的部署问题，并完成测试和验证。" * 3,
            policy,
        )
        self.assertGreaterEqual(len(queries), 3)
        self.assertLessEqual(len(queries), 4)
        calls: list[dict] = []

        def fake_call(name, arguments):
            calls.append(arguments)
            return {
                "structuredContent": {
                    "results": [
                        {"id": "same", "memory": "重复记忆"},
                        {"id": f"m-{len(calls)}", "memory": f"记忆 {len(calls)}"},
                    ]
                }
            }

        with mock.patch.object(mem0, "call_tool", side_effect=fake_call):
            result = mem0.search_queries(queries, "demo-project", settings)
        memories = mem0.structured_results(result)
        self.assertEqual(sum(item["id"] == "same" for item in memories), 1)
        self.assertTrue(all(call["top_k"] == 5 for call in calls))
        self.assertTrue(all(call["threshold"] == 0.25 for call in calls))
        self.assertTrue(all(call["rerank"] is True for call in calls))

    def test_自动保存质量门禁分类元数据和保留期(self):
        with tempfile.TemporaryDirectory() as directory:
            calls: list[tuple[str, dict]] = []
            hook_input = {"session_id": "session-settings", "cwd": directory}
            policy = {
                "identity": ["桌面端项目"],
                "categories": ["决定：长期架构决定"],
                "retention": {"session_summary": "14d", "decision": "forever"},
            }
            settings = {**mem0.DEFAULT_SETTINGS, "session_retention_days": 7}
            with mock.patch.object(mem0, "PLUGIN_DATA", Path(directory) / "data"), mock.patch.object(
                mem0, "resolve_branch", return_value="main"
            ), mock.patch.object(mem0, "call_tool", side_effect=lambda *args: calls.append(args) or {}):
                mem0.save_summary(
                    "用户：确认采用新的隔离架构。\n\n助手：已完成实现并通过全部回归测试。" * 4,
                    "demo-project",
                    "本轮会话总结",
                    hook_input,
                    ["src/app.py"],
                    settings,
                    policy,
                )
            self.assertEqual(len(calls), 1)
            arguments = calls[0][1]
            self.assertIn("决定：长期架构决定", arguments["messages"][0]["content"])
            self.assertEqual(arguments["messages"][0]["role"], "assistant")
            self.assertEqual(arguments["metadata"]["type"], "session_summary")
            self.assertEqual(arguments["metadata"]["confidence"], 0.8)
            self.assertEqual(arguments["metadata"]["memory_kind"], "session_summary")
            expiration = datetime.fromisoformat(arguments["expiration_date"]).date()
            self.assertEqual(
                (expiration - datetime.now(timezone.utc).date()).days,
                14,
            )

            decision_hook = {"session_id": "session-decision", "cwd": directory}
            with mock.patch.object(mem0, "PLUGIN_DATA", Path(directory) / "data"), mock.patch.object(
                mem0,
                "call_tool",
                side_effect=lambda *args: calls.append(args) or {},
            ):
                mem0.save_summary(
                    "用户：确认该架构决定永久有效。\n\n助手：已记录决定并完成验证。" * 4,
                    "demo-project",
                    "架构决定",
                    decision_hook,
                    [],
                    settings,
                    policy,
                    memory_type="decision",
                )
            decision = calls[-1][1]
            self.assertEqual(decision["metadata"]["type"], "decision")
            self.assertNotIn("expiration_date", decision)

            disabled = {**settings, "auto_save": False}
            with mock.patch.object(mem0, "call_tool") as call:
                mem0.save_summary(
                    "用户：确认采用新架构。" * 10,
                    "demo-project",
                    "本轮会话总结",
                    hook_input,
                    [],
                    disabled,
                    policy,
                )
            call.assert_not_called()

    def test_总结元数据只保留项目内相对路径(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            inside = root / "src" / "app.py"
            outside = base / "private.py"
            calls: list[tuple[str, dict]] = []
            with mock.patch.object(mem0, "PLUGIN_DATA", base / "data"), mock.patch.object(
                mem0, "git_root", return_value=root
            ), mock.patch.object(mem0, "resolve_branch", return_value="main"), mock.patch.object(
                mem0, "call_tool", side_effect=lambda *args: calls.append(args) or {}
            ):
                mem0.save_summary(
                    "用户：确认修复路径泄漏问题。\n\n助手：已完成修复并通过测试。" * 4,
                    "demo-project",
                    "本轮会话总结",
                    {"session_id": "session-path", "cwd": str(root)},
                    [str(inside), "src/app.py", str(outside), ".env"],
                )

        arguments = calls[0][1]
        self.assertEqual(arguments["metadata"]["files_touched"], ["src/app.py"])
        self.assertNotIn(str(base), arguments["messages"][0]["content"])

    def test_契约快照锁定十一工具和关键枚举(self):
        snapshot = json.loads(mem0.SCHEMA_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(snapshot["tools"]), mem0.MEM0_TOOL_NAMES)
        self.assertEqual(
            snapshot["tools"]["get_memories"]["enums"]["sort_order"],
            ["asc", "desc"],
        )
        self.assertTrue(
            snapshot["tools"]["delete_all_memories"]["annotations"]["destructiveHint"]
        )


if __name__ == "__main__":
    unittest.main()
