import asyncio
import gc
import gzip
import json
import sys
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from router import (  # noqa: E402
    CONFIG,
    ROUTE_HEADER,
    SESSION,
    WS_HANDLERS,
    Config,
    InvalidPayload,
    clean_headers,
    create_app,
    matches_source,
    rewrite_model,
)

SOURCE = "203.0.113.7"
TARGET = "gpt-target"
AUTH = "Bearer offline-test-placeholder"


class RewriteTests(unittest.TestCase):
    def test_only_model_bytes_change(self):
        payload = b' { "model" : "gpt-source", "reasoning":{"effort":"max"}, "n":0.123456789012345678901, "input":"\\u4f60" }\n'
        actual, changed = rewrite_model(payload, TARGET)
        self.assertTrue(changed)
        self.assertEqual(actual, payload.replace(b'"gpt-source"', b'"gpt-target"'))

    def test_media_and_other_provider_models_are_preserved(self):
        for model in [
            "gpt-image-2",
            "gpt-4o-audio-preview",
            "gpt-realtime",
            "claude-example",
        ]:
            with self.subTest(model=model):
                payload = json.dumps({"model": model}).encode()
                self.assertEqual(rewrite_model(payload, TARGET), (payload, False))

    def test_codex_reasoning_and_provider_prefix_aliases(self):
        for model in [
            "gpt-source",
            "openai/gpt-source",
            "o3",
            "o4-mini",
            "codex-auto-review",
        ]:
            with self.subTest(model=model):
                data, changed = rewrite_model(
                    json.dumps({"model": model}).encode(), TARGET
                )
                self.assertTrue(changed)
                self.assertEqual(json.loads(data)["model"], TARGET)

    def test_ambiguous_model_and_invalid_json_are_rejected(self):
        for payload in [
            b'{"model":"gpt-a","model":"gpt-b"}',
            b'{"model":"gpt-a","MODEL":"gpt-b"}',
            b'{"Model":"gpt-a"}',
            b'{"model":123}',
            b'{"model":"gpt-a",}',
            b"[]",
            b"{",
            b"{} trailing",
            b"\xff",
        ]:
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidPayload):
                    rewrite_model(payload, TARGET)

    def test_missing_model_and_target_identity_are_unchanged(self):
        for payload in [b"{}", b'{"input":[]}', b'{"model":"gpt-target"}']:
            self.assertEqual(rewrite_model(payload, TARGET), (payload, False))

    def test_websocket_only_changes_response_create(self):
        payload = b'{"type":"session.update","model":"gpt-source"}'
        self.assertEqual(
            rewrite_model(payload, TARGET, websocket=True), (payload, False)
        )
        with self.assertRaises(InvalidPayload):
            rewrite_model(
                b'{"type":"x","type":"response.create"}', TARGET, websocket=True
            )

    def test_configuration_rejects_external_listener_and_upstream(self):
        for extra in [
            {"listen_host": "0.0.0.0"},
            {"upstream": "http://example.com"},
            {"upstream": "http://user:password@127.0.0.1:8080"},
            {"upstream": "http://127.0.0.1:8080/path"},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                Config(SOURCE, TARGET, **extra)

    def test_headers_preserve_auth_and_remove_private_hop_headers(self):
        headers = CIMultiDict(
            {
                "Authorization": AUTH,
                "Connection": "Keep-Alive, X-Hop",
                "X-Hop": "private",
                "Content-Length": "10",
                ROUTE_HEADER: SOURCE,
                "OpenAI-Beta": "responses=experimental",
            }
        )
        clean = clean_headers(headers, request=True)
        self.assertEqual(clean["Authorization"], AUTH)
        self.assertEqual(clean["OpenAI-Beta"], headers["OpenAI-Beta"])
        self.assertFalse(
            {"Connection", "X-Hop", "Content-Length", ROUTE_HEADER} & clean.keys()
        )

    def test_external_peer_cannot_supply_routing_header(self):
        request = SimpleNamespace(
            transport=SimpleNamespace(get_extra_info=lambda key: ("8.8.8.8", 1000)),
            headers={ROUTE_HEADER: SOURCE},
        )
        self.assertFalse(matches_source(request, Config(SOURCE, TARGET)))


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.warning_context = warnings.catch_warnings(record=True)
        self.resource_warnings = self.warning_context.__enter__()
        warnings.simplefilter("always", ResourceWarning)
        self.received = []
        self.first_chunk = asyncio.Event()
        self.finish_stream = asyncio.Event()
        self.backend_app = web.Application(handler_args={"auto_decompress": False})
        self.backend_app.router.add_route("*", "/{path:.*}", self.backend)
        self.backend_server = TestServer(self.backend_app)
        await self.backend_server.start_server()
        self.config = Config(
            SOURCE, TARGET, upstream=str(self.backend_server.make_url(""))
        )
        self.proxy_app = create_app(self.config)
        self.proxy_server = TestServer(self.proxy_app)
        self.client = TestClient(self.proxy_server)
        await self.client.start_server()

    async def asyncTearDown(self):
        self.finish_stream.set()
        await self.client.close()
        await self.backend_server.close()
        gc.collect()
        self.warning_context.__exit__(None, None, None)
        self.assertEqual(
            [
                str(item.message)
                for item in self.resource_warnings
                if issubclass(item.category, ResourceWarning)
            ],
            [],
        )

    def headers(self, source=SOURCE, **extra):
        return {ROUTE_HEADER: source, "Authorization": AUTH, **extra}

    async def backend(self, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if request.headers.get("X-Test-Unauthorized"):
                return web.Response(status=401)
            ws = web.WebSocketResponse(autoping=False, protocols=("response-test",))
            await ws.prepare(request)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    self.received.append(
                        {
                            "body": message.data.encode(),
                            "headers": dict(request.headers),
                        }
                    )
                    await ws.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    self.received.append(
                        {"body": message.data, "headers": dict(request.headers)}
                    )
                    await ws.send_bytes(message.data)
                elif message.type == WSMsgType.PING:
                    await ws.pong(message.data)
            return ws
        self.received.append(
            {
                "body": await request.read(),
                "headers": dict(request.headers),
                "path": request.raw_path,
                "method": request.method,
            }
        )
        if request.headers.get("X-Test-Stream"):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            self.first_chunk.set()
            await self.finish_stream.wait()
            await response.write(b"data: last\n\n")
            await response.write_eof()
            return response
        if request.headers.get("X-Test-Truncate"):
            response = web.StreamResponse(headers={"Content-Length": "1000"})
            await response.prepare(request)
            await response.write(b"incomplete")
            request.transport.close()
            return response
        if request.headers.get("X-Test-Redirect"):
            return web.Response(
                status=307, headers={"Location": "/must-not-be-followed"}
            )
        if request.headers.get("X-Test-Compressed"):
            return web.Response(
                body=gzip.compress(b"response bytes"),
                headers={"Content-Encoding": "gzip"},
            )
        return web.Response(
            status=200, body=b"ok", headers={"X-Upstream-Evidence": "present"}
        )

    async def test_target_http_keeps_key_reasoning_and_original_nonmodel_bytes(self):
        payload = b'{"model":"gpt-source","reasoning":{"effort":"max"},"input":"hello"}'
        response = await self.client.post(
            "/v1/responses?test=1", data=payload, headers=self.headers()
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Upstream-Evidence"], "present")
        row = self.received[-1]
        self.assertEqual(row["body"], payload.replace(b"gpt-source", b"gpt-target"))
        self.assertEqual(row["headers"]["Authorization"], AUTH)
        self.assertNotIn(ROUTE_HEADER, row["headers"])
        self.assertEqual(row["path"], "/v1/responses?test=1")

    async def test_other_ip_and_spoofed_forwarded_ip_are_unchanged(self):
        payload = b'{"model":"gpt-source"}'
        for headers in [self.headers("203.0.113.8"), {"X-Forwarded-For": SOURCE}]:
            response = await self.client.post(
                "/v1/responses", data=payload, headers=headers
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(self.received[-1]["body"], payload)

    async def test_media_and_non_generation_paths_are_unchanged(self):
        payload = b'{"model":"gpt-source"}'
        response = await self.client.post(
            "/v1/images/generations", data=payload, headers=self.headers()
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.received[-1]["body"], payload)
        response = await self.client.get("/v1/models", headers=self.headers())
        self.assertEqual(response.status, 200)
        self.assertEqual(self.received[-1]["method"], "GET")

    async def test_chat_compact_messages_and_search_are_routed(self):
        for path in [
            "/v1/chat/completions",
            "/v1/responses/compact",
            "/v1/messages",
            "/v1/alpha/search",
        ]:
            response = await self.client.post(
                path, json={"model": "gpt-source"}, headers=self.headers()
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(self.received[-1]["body"])["model"], TARGET)

    async def test_stream_first_chunk_arrives_before_generation_finishes(self):
        response = await self.client.post(
            "/v1/responses",
            json={"model": "gpt-source", "stream": True},
            headers=self.headers(**{"X-Test-Stream": "yes"}),
        )
        first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 2)
        self.assertEqual(first, b"data: first\n\n")
        self.assertFalse(self.finish_stream.is_set())
        self.finish_stream.set()
        self.assertEqual(await response.read(), b"data: last\n\n")
        self.assertIsNone(self.proxy_app[SESSION].timeout.total)
        self.assertIsNone(self.proxy_app[SESSION].timeout.sock_read)

    async def test_truncated_stream_remains_a_client_error(self):
        with self.assertLogs("ip_model_router", level="WARNING"):
            response = await self.client.post(
                "/v1/responses",
                json={"model": "gpt-source"},
                headers=self.headers(**{"X-Test-Truncate": "yes"}),
            )
            with self.assertRaises(aiohttp.ClientPayloadError):
                await response.read()

    async def test_redirect_not_replayed_and_auth_not_sent_again(self):
        response = await self.client.post(
            "/v1/responses",
            json={"model": "gpt-source"},
            allow_redirects=False,
            headers=self.headers(**{"X-Test-Redirect": "yes"}),
        )
        self.assertEqual(response.status, 307)
        self.assertEqual(len(self.received), 1)

    async def test_compressed_nonmatching_request_is_unchanged(self):
        payload = gzip.compress(b'{"model":"gpt-source"}')
        response = await self.client.post(
            "/v1/responses",
            data=payload,
            headers=self.headers("203.0.113.8", **{"Content-Encoding": "gzip"}),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.received[-1]["body"], payload)

    async def test_invalid_or_compressed_target_request_does_not_reach_backend(self):
        response = await self.client.post(
            "/v1/responses", data=b'{"model":1}', headers=self.headers()
        )
        self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/v1/responses",
            data=b"compressed",
            headers=self.headers(**{"Content-Encoding": "gzip"}),
        )
        self.assertEqual(response.status, 415)
        self.assertEqual(self.received, [])

    async def test_websocket_text_binary_omitted_model_and_ping(self):
        async with self.client.ws_connect(
            "/v1/responses",
            headers=self.headers(),
            protocols=("response-test",),
            autoping=False,
        ) as ws:
            self.assertEqual(ws.protocol, "response-test")
            for binary in [False, True]:
                payload = b'{"type":"response.create","model":"gpt-source","reasoning":{"effort":"max"}}'
                if binary:
                    await ws.send_bytes(payload)
                else:
                    await ws.send_str(payload.decode())
                reply = await asyncio.wait_for(ws.receive(), 2)
                actual = reply.data if binary else reply.data.encode()
                self.assertEqual(actual, payload.replace(b"gpt-source", b"gpt-target"))
                self.assertEqual(self.received[-1]["headers"]["Authorization"], AUTH)
            omitted = '{"type":"response.create","input":[]}'
            await ws.send_str(omitted)
            self.assertEqual((await asyncio.wait_for(ws.receive(), 2)).data, omitted)
            await ws.ping(b"check")
            reply = await asyncio.wait_for(ws.receive(), 2)
            self.assertEqual(reply.type, WSMsgType.PONG)
            self.assertEqual(reply.data, b"check")

    async def test_nonmatching_websocket_is_unchanged(self):
        async with self.client.ws_connect(
            "/v1/responses", headers=self.headers("203.0.113.8")
        ) as ws:
            payload = '{"type":"response.create","model":"gpt-source"}'
            await ws.send_str(payload)
            self.assertEqual((await asyncio.wait_for(ws.receive(), 2)).data, payload)

    async def test_websocket_invalid_model_closes_without_forwarding(self):
        async with self.client.ws_connect(
            "/v1/responses", headers=self.headers()
        ) as ws:
            await ws.send_str('{"type":"response.create","model":1}')
            reply = await asyncio.wait_for(ws.receive(), 2)
            self.assertEqual(reply.type, WSMsgType.CLOSE)
            self.assertEqual(reply.data, 1007)
            self.assertEqual(self.received, [])

    async def test_websocket_auth_error_is_not_accepted(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as error:
            await self.client.ws_connect(
                "/v1/responses", headers=self.headers(**{"X-Test-Unauthorized": "yes"})
            )
        self.assertEqual(error.exception.status, 401)

    async def test_shutdown_releases_active_websocket_handlers(self):
        ws = await self.client.ws_connect("/v1/responses", headers=self.headers())
        await ws.send_str('{"type":"response.create","model":"gpt-source"}')
        await asyncio.wait_for(ws.receive(), 2)
        self.assertEqual(len(self.proxy_app[WS_HANDLERS]), 1)
        await self.proxy_app.shutdown()
        self.assertEqual(self.proxy_app[WS_HANDLERS], set())
        self.assertEqual(
            (await asyncio.wait_for(ws.receive(), 2)).type, WSMsgType.CLOSE
        )
        await ws.close()

    async def test_repeated_cancellation_cannot_interrupt_socket_cleanup(self):
        ws = await self.client.ws_connect("/v1/responses", headers=self.headers())
        await ws.send_str('{"type":"response.create","model":"gpt-source"}')
        await asyncio.wait_for(ws.receive(), 2)
        entered = asyncio.Event()
        finish = asyncio.Event()
        original_close = aiohttp.ClientWebSocketResponse.close

        async def controlled_close(socket, *args, **kwargs):
            if socket is not ws:
                entered.set()
                await finish.wait()
            return await original_close(socket, *args, **kwargs)

        handler = next(iter(self.proxy_app[WS_HANDLERS]))
        with patch.object(aiohttp.ClientWebSocketResponse, "close", controlled_close):
            handler.cancel()
            await asyncio.wait_for(entered.wait(), 2)
            handler.cancel()
            finish.set()
            await asyncio.wait_for(asyncio.gather(handler, return_exceptions=True), 3)
        self.assertEqual(self.proxy_app[WS_HANDLERS], set())
        await ws.close()

    async def test_local_health_does_not_call_upstream(self):
        response = await self.client.get("/healthz")
        self.assertEqual(await response.json(), {"status": "ok"})
        self.assertEqual(self.received, [])
        self.assertEqual(self.proxy_app[CONFIG].source_ip, SOURCE)


if __name__ == "__main__":
    unittest.main()
