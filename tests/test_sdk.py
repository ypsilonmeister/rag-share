"""Smoke tests against the installed MCP SDK; no model or Drive credentials."""
import json
import unittest
from unittest.mock import patch

import server


class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tools_and_argument_schemas(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertEqual(set(tools), {"memory_search", "memory_read"})
        self.assertEqual(tools["memory_search"].input_schema["required"], ["query"])
        self.assertEqual(tools["memory_read"].input_schema["required"], ["file_id"])

    async def test_real_sdk_dispatches_search_and_read(self):
        page = {"fileId": "note", "text": "日本語", "next_offset": None}
        with patch.object(server.INDEX, "search", return_value=[]) as search, \
             patch.object(server.INDEX, "read", return_value=page) as read, \
             patch.object(server, "access_log"):
            result = await server.mcp.call_tool("memory_search", {"query": "日本語"})
            self.assertFalse(result.is_error)
            self.assertEqual(json.loads(result.content[0].text),
                             {"query": "日本語", "hits": []})
            search.assert_called_once_with("日本語", 5)
            result = await server.mcp.call_tool(
                "memory_read", {"file_id": "note", "limit": 3})
            self.assertFalse(result.is_error)
            self.assertEqual(json.loads(result.content[0].text), page)
            read.assert_called_once_with("note", 0, 3)

    async def test_transport_app_and_bearer_gate(self):
        app = server.mcp.streamable_http_app(
            stateless_http=True, json_response=True,
            transport_security=server.TRANSPORT_SECURITY)
        app = server.OAuthRouter(server.BearerAuthMiddleware(app))
        messages = []

        async def receive():
            raise AssertionError("unauthenticated request must not reach the SDK")

        async def send(message):
            messages.append(message)

        with patch.object(server, "verify_any_token", return_value=None), \
             patch.object(server, "access_log"):
            await app({"type": "http", "method": "POST", "path": "/mcp",
                       "headers": []}, receive, send)
        self.assertEqual(messages[0]["status"], 401)
        headers = {key.lower(): value for key, value in messages[0]["headers"]}
        self.assertIn(b"www-authenticate", headers)
