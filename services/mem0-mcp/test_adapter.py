import asyncio
import logging
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("MEM0_INTERNAL_SERVICE_KEY", "test-internal-key")
os.environ.setdefault("MCP_CONFIRMATION_SECRET", "test-confirmation-secret")

import server


TEST_API_KEY = "m0sk_test-valid-self-hosted-api-key-value"
TEST_ADMIN_API_KEY = "m0sk_test-admin-api-key-value"
TEST_UPSTREAM_FAILURE_API_KEY = "m0sk_test-upstream-failure-key-value"


class AdapterValidationTests(unittest.TestCase):
    def test_http_client_request_logs_are_disabled(self):
        self.assertGreaterEqual(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").getEffectiveLevel(), logging.WARNING)

    def test_text_and_messages_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "必须且只能"):
            server._messages("正文", [{"role": "user", "content": "消息"}])
        with self.assertRaisesRegex(ValueError, "必须且只能"):
            server._messages(None, None)

    def test_reserved_metadata_is_rejected_at_any_depth(self):
        with self.assertRaisesRegex(ValueError, "保留字段"):
            server._metadata({"nested": {"user_id": "attacker"}})

    def test_metadata_depth_and_size_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "嵌套深度"):
            server._metadata({"a": {"b": {"c": {"d": "too-deep"}}}})
        with self.assertRaisesRegex(ValueError, "超过"):
            server._metadata({"note": "x" * (server.MAX_METADATA_BYTES + 1)})

    def test_identity_filter_injection_is_rejected(self):
        for value in ({"user_id": "attacker"}, {"metadata": {"mcp_owner": "attacker"}}):
            with self.assertRaises(ValueError):
                server._filters(value)

    def test_invalid_memory_uuid_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "UUID"):
            server._memory_id("not-a-uuid")

    def test_filter_complexity_is_bounded(self):
        value = {"AND": [{"metadata": {"type": "x"}} for _ in range(17)]}
        with self.assertRaisesRegex(ValueError, "逻辑过滤"):
            server._filters(value)

    def test_exact_project_visibility_does_not_include_global(self):
        global_item = {
            "user_id": server.DEFAULT_USER_ID,
            "metadata": {"mcp_owner": server.OWNER, "scope": "global"},
        }
        self.assertFalse(server._visible(global_item, "project-a", include_global=False))
        self.assertTrue(server._visible(global_item, "project-a", include_global=True))

    def test_project_id_contract_accepts_leading_mark_characters(self):
        for value in (".project", "_project", "-project"):
            self.assertEqual(server._project(value), value)

    def test_confirmation_rejects_tamper_scope_and_expiry(self):
        expected = {"tool": "delete_all_memories", "project_id": "project-a", "run_id": None}
        with patch.object(server.time, "time", return_value=1000):
            token, _ = server._make_confirmation({**expected, "cutoff": "2026-08-05T00:00:00+00:00", "result_hash": "0" * 64, "count": 1})
            payload = server._verify_confirmation(token, expected)
            uuid.UUID(payload["jti"])
            with self.assertRaisesRegex(ValueError, "范围不匹配"):
                server._verify_confirmation(token, {**expected, "project_id": "project-b"})
            with self.assertRaisesRegex(ValueError, "篡改"):
                server._verify_confirmation(token[:-1] + ("A" if token[-1] != "A" else "B"), expected)
        with patch.object(server.time, "time", return_value=1000):
            token, _ = server._make_confirmation({**expected, "cutoff": "2026-08-05T00:00:00+00:00", "result_hash": "0" * 64, "count": 1})
        with patch.object(server.time, "time", return_value=1000 + server.CONFIRMATION_TTL_SECONDS + 1):
            with self.assertRaisesRegex(ValueError, "已过期"):
                server._verify_confirmation(token, expected)


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        return None


class AdapterAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.request_entered = asyncio.Event()
        self.request_release = asyncio.Event()

        async def handler(request):
            self.requests.append(request)
            if request.url.path == "/auth/introspect":
                supplied = request.headers.get("X-API-Key")
                if supplied == TEST_API_KEY:
                    return httpx.Response(
                        200,
                        json={
                            "subject": "00000000-0000-0000-0000-000000000001",
                            "purpose": "mcp",
                        },
                    )
                if supplied == TEST_ADMIN_API_KEY:
                    return httpx.Response(403, json={"detail": "用途不匹配"})
                if supplied == TEST_UPSTREAM_FAILURE_API_KEY:
                    return httpx.Response(503, json={"detail": "上游不可用"})
                return httpx.Response(401, json={"detail": "无效 API Key"})
            if request.url.path == "/slow":
                self.request_entered.set()
                await self.request_release.wait()
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/timeout":
                await asyncio.sleep(0.01)
                raise httpx.ReadTimeout("模拟上游读取超时", request=request)
            if request.url.path == "/bad":
                return httpx.Response(400, json={"detail": "不得泄露的上游正文"})
            if request.url.path == "/declared-large":
                return httpx.Response(200, headers={"content-length": "64"}, content=b"{}")
            if request.url.path == "/large":
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=_ChunkedStream([b'"', b"x" * 64, b'"']),
                )
            return httpx.Response(200, json={"status": "ok"})

        self.client = httpx.AsyncClient(
            base_url="http://mem0.test",
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(server.TIMEOUT, connect=min(5.0, server.TIMEOUT), pool=2.0),
        )
        server.HTTP_CLIENT = self.client
        server.UPSTREAM_SEMAPHORE = asyncio.Semaphore(2)

    async def asyncTearDown(self):
        server.HTTP_CLIENT = None
        server.UPSTREAM_SEMAPHORE = None
        await self.client.aclose()

    async def test_request_reuses_shared_client_and_hides_error_body(self):
        self.assertEqual((await server._request("GET", "/ok", internal=True))["status"], "ok")
        self.assertEqual((await server._request("GET", "/ok", internal=True))["status"], "ok")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[-1].headers["X-Mem0-Internal-Key"], "test-internal-key")
        with self.assertRaisesRegex(ValueError, "Mem0 拒绝请求") as raised:
            await server._request("GET", "/bad", internal=True)
        self.assertNotIn("不得泄露", str(raised.exception))

    async def test_mem0_api_key_verifier_accepts_only_mcp_keys(self):
        verifier = server.Mem0APIKeyVerifier()
        accepted = await verifier.verify_token(TEST_API_KEY)
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted.token, TEST_API_KEY)
        self.assertEqual(accepted.scopes, ["mem0:mcp"])
        self.assertIsNone(await verifier.verify_token(TEST_ADMIN_API_KEY))
        self.assertIsNone(await verifier.verify_token("m0sk_test-invalid-api-key-value"))
        with self.assertRaisesRegex(RuntimeError, "上游错误"):
            await verifier.verify_token(TEST_UPSTREAM_FAILURE_API_KEY)

    async def test_asgi_bearer_auth_distinguishes_invalid_key_from_upstream_failure(self):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "adapter-test", "version": "1.0"},
            },
        }
        transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Host": "mem0-api.jiang.in",
        }
        async with server.mcp.session_manager.run():
            async with httpx.AsyncClient(
                base_url="http://localhost",
                transport=transport,
            ) as client:
                accepted = await client.post(
                    "/mcp",
                    json=payload,
                    headers={**headers, "Authorization": f"Bearer {TEST_API_KEY}"},
                )
                rejected = await client.post(
                    "/mcp",
                    json=payload,
                    headers={**headers, "Authorization": f"Bearer {TEST_ADMIN_API_KEY}"},
                )
                unavailable = await client.post(
                    "/mcp",
                    json=payload,
                    headers={
                        **headers,
                        "Authorization": f"Bearer {TEST_UPSTREAM_FAILURE_API_KEY}",
                    },
                )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(rejected.status_code, 401)
        self.assertGreaterEqual(unavailable.status_code, 500)
        self.assertNotEqual(unavailable.status_code, 401)

    async def test_request_keeps_authentication_modes_separate(self):
        await server._request("GET", "/ok", internal=True)
        self.assertEqual(self.requests[-1].headers["X-Mem0-Internal-Key"], "test-internal-key")
        self.assertNotIn("X-API-Key", self.requests[-1].headers)
        await server._request("GET", "/ok", public=True)
        self.assertNotIn("X-API-Key", self.requests[-1].headers)
        self.assertNotIn("X-Mem0-Internal-Key", self.requests[-1].headers)
        await server._request("POST", "/auth/introspect", api_key=TEST_API_KEY)
        self.assertEqual(self.requests[-1].headers["X-API-Key"], TEST_API_KEY)
        self.assertNotIn("X-Mem0-Internal-Key", self.requests[-1].headers)
        with self.assertRaisesRegex(RuntimeError, "认证模式冲突"):
            await server._request("GET", "/ok")
        with self.assertRaisesRegex(RuntimeError, "认证模式冲突"):
            await server._request("GET", "/ok", internal=True, api_key=TEST_API_KEY)

    async def test_request_uses_client_default_and_explicit_read_timeout(self):
        await server._request("GET", "/ok", internal=True)
        default_timeout = self.requests[-1].extensions["timeout"]
        self.assertEqual(default_timeout["connect"], min(5.0, server.TIMEOUT))
        self.assertEqual(default_timeout["read"], server.TIMEOUT)
        self.assertEqual(default_timeout["write"], server.TIMEOUT)
        self.assertEqual(default_timeout["pool"], 2.0)

        await server._request("GET", "/ok", internal=True, timeout_seconds=server.READ_TIMEOUT)
        read_timeout = self.requests[-1].extensions["timeout"]
        self.assertEqual(read_timeout["connect"], min(3.0, server.READ_TIMEOUT))
        self.assertEqual(read_timeout["read"], server.READ_TIMEOUT)
        self.assertEqual(read_timeout["write"], server.READ_TIMEOUT)
        self.assertEqual(read_timeout["pool"], 1.0)

    async def test_timeout_releases_all_concurrency_permits(self):
        server.UPSTREAM_SEMAPHORE = asyncio.Semaphore(16)
        results = await asyncio.gather(
            *(server._request("GET", "/timeout", internal=True, timeout_seconds=0.01) for _ in range(16)),
            return_exceptions=True,
        )
        self.assertTrue(all(isinstance(result, RuntimeError) for result in results))
        self.assertEqual(server.UPSTREAM_SEMAPHORE._value, 16)
        self.assertEqual((await server._request("GET", "/ok", internal=True))["status"], "ok")

    async def test_request_rejects_chunked_response_over_limit(self):
        with self.assertRaisesRegex(RuntimeError, "超过大小限制"):
            await server._request("GET", "/large", public=True, max_response_bytes=32)

    async def test_request_rejects_declared_response_over_limit(self):
        with self.assertRaisesRegex(RuntimeError, "超过大小限制"):
            await server._request("GET", "/declared-large", public=True, max_response_bytes=32)

    async def test_request_rejects_when_concurrency_queue_is_full(self):
        server.UPSTREAM_SEMAPHORE = asyncio.Semaphore(1)
        first = asyncio.create_task(server._request("GET", "/slow", internal=True))
        await asyncio.wait_for(self.request_entered.wait(), timeout=1)
        try:
            with self.assertRaisesRegex(RuntimeError, "服务繁忙"):
                await server._request("GET", "/ok", internal=True)
        finally:
            self.request_release.set()
            await first

    async def test_search_pushes_filters_down_once(self):
        item = {
            "id": str(uuid.uuid4()),
            "user_id": server.DEFAULT_USER_ID,
            "memory": "测试",
            "score": 0.9,
            "metadata": {
                "mcp_owner": server.OWNER,
                "scope": "project",
                "project_id": "project-a",
                "kind": "decision",
            },
        }
        request = AsyncMock(return_value={"results": [item]})
        with patch.object(server, "_request", request):
            result = await server.search_memories(
                "测试",
                project_id="project-a",
                filters={"metadata": {"kind": "decision"}},
                rerank=True,
            )
        self.assertEqual([row["id"] for row in result["results"]], [item["id"]])
        self.assertEqual(request.await_count, 1)
        payload = request.await_args.kwargs["json_body"]
        self.assertEqual(payload["project_id"], "project-a")
        self.assertTrue(payload["rerank"])
        self.assertEqual(payload["filters"], {"metadata": {"kind": "decision"}})
        self.assertEqual(request.await_args.args, ("POST", "/internal/mcp/search"))
        self.assertTrue(request.await_args.kwargs["internal"])
        self.assertEqual(request.await_args.kwargs["timeout_seconds"], server.READ_TIMEOUT)

    async def test_internal_helpers_use_read_and_mutation_budgets(self):
        request = AsyncMock(return_value={})
        memory_id = str(uuid.uuid4())
        with patch.object(server, "_request", request):
            await server._internal_memory_action("get", memory_id, "project-a")
            self.assertEqual(request.await_args.kwargs["timeout_seconds"], server.READ_TIMEOUT)
            await server._internal_memory_action("update", memory_id, "project-a", text="测试")
            self.assertEqual(request.await_args.kwargs["timeout_seconds"], server.TIMEOUT)
            await server._internal_query(project_id="project-a")
            self.assertEqual(request.await_args.kwargs["timeout_seconds"], server.READ_TIMEOUT)
            await server._internal_entities(project_id="project-a", entity_type=None, show_expired=False)
            self.assertEqual(request.await_args.kwargs["timeout_seconds"], server.READ_TIMEOUT)

    async def test_add_uses_server_managed_atomic_endpoint(self):
        request = AsyncMock(return_value={"results": []})
        with patch.object(server, "_request", request):
            result = await server.add_memory(
                text="测试",
                project_id="project-a",
                metadata={"kind": "decision"},
                run_id="run-a",
                infer=True,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(request.await_args.args, ("POST", "/internal/mcp/add"))
        self.assertTrue(request.await_args.kwargs["internal"])
        self.assertEqual(request.await_args.kwargs["timeout_seconds"], server.TIMEOUT)
        payload = request.await_args.kwargs["json_body"]
        self.assertNotIn("user_id", payload)
        self.assertNotIn("agent_id", payload)
        self.assertEqual(payload["project_id"], "project-a")
        self.assertEqual(payload["metadata"], {"kind": "decision"})
        self.assertEqual(payload["run_id"], "run-a")
        self.assertTrue(payload["infer"])

    async def test_add_rejects_invalid_server_response(self):
        with patch.object(server, "_request", AsyncMock(return_value=None)):
            with self.assertRaisesRegex(RuntimeError, "新增记忆返回无效响应"):
                await server.add_memory(text="测试", project_id="project-a")

    def test_missing_filter_fields_follow_sql_three_value_logic(self):
        missing = {"metadata": {}}
        present = {"metadata": {"kind": "other"}}
        negative_filters = (
            {"metadata": {"kind": {"ne": "decision"}}},
            {"metadata": {"kind": {"nin": ["decision"]}}},
            {"NOT": {"metadata": {"kind": {"eq": "decision"}}}},
        )
        for value in negative_filters:
            self.assertFalse(server._matches_filter(missing, value))
            self.assertTrue(server._matches_filter(present, value))

    async def test_get_memories_caps_page_size(self):
        query = AsyncMock(return_value={"results": [], "count": 0, "partial": False})
        with patch.object(server, "_internal_query", query):
            await server.get_memories(project_id="project-a", page_size=200)
        self.assertEqual(query.await_args.kwargs["page_size"], server.MAX_PAGE_SIZE)

    async def test_list_entities_uses_one_aggregate_request(self):
        entities = AsyncMock(return_value={"results": [], "count": 0, "partial": False})
        with patch.object(server, "_internal_entities", entities):
            await server.list_entities(project_id="project-a")
        self.assertEqual(entities.await_count, 1)
        self.assertFalse(entities.await_args.kwargs["show_expired"])


if __name__ == "__main__":
    unittest.main()
