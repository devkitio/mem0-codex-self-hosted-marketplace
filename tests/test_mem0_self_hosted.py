from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
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
            mem0.urllib.request,
            "urlopen",
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
            mem0.urllib.request,
            "urlopen",
            return_value=context,
        ):
            with self.assertRaisesRegex(RuntimeError, "^MCP 返回错误$") as raised:
                mem0.mcp_request("tools/list", {})

        self.assertNotIn("sentinel-secret", str(raised.exception))

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

    def test_命令错误签名会脱敏(self):
        signature = mem0.error_signature(
            {"command": "python app.py"},
            "Authorization: Bearer abc123\nTraceback (most recent call last)\nValueError: bad",
        )
        self.assertIn("Traceback", signature)
        self.assertNotIn("abc123", signature)

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
            self.assertEqual(len(calls), 1)

    def test_文档搜索只允许官方主机(self):
        with mock.patch.object(mem0_docs, "fetch_url") as fetch:
            result = mem0_docs.fetch_page("http://127.0.0.1:8080/private")
            invalid_port = mem0_docs.fetch_page("https://docs.mem0.ai:invalid/page")
        self.assertIn("error", result)
        self.assertIn("error", invalid_port)
        fetch.assert_not_called()

    def test_文档搜索限制响应大小(self):
        response = mock.MagicMock()
        response.read.return_value = b"x" * (mem0_docs.MAX_RESPONSE_BYTES + 1)
        context = mock.MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with mock.patch.object(mem0_docs.urllib.request, "urlopen", return_value=context):
            result = mem0_docs.fetch_url("https://docs.mem0.ai/llms.txt")

        self.assertIn("超过大小限制", result)
        response.read.assert_called_once_with(mem0_docs.MAX_RESPONSE_BYTES + 1)

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

    def test_契约快照锁定十工具和关键枚举(self):
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
