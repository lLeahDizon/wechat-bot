"""Loopback-only model rewrite proxy for a trusted Caddy IP matcher."""

import argparse
import asyncio
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web
from multidict import CIMultiDict
from yarl import URL

LOG = logging.getLogger("ip_model_router")
ROUTE_HEADER = "X-Sub2API-Route-Client-IP"
HTTP_PATHS = frozenset(
    {
        "/v1/responses",
        "/v1/responses/compact",
        "/v1/chat/completions",
        "/v1/messages",
        "/v1/alpha/search",
    }
)
HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
SESSION = web.AppKey("session", aiohttp.ClientSession)
CONFIG = web.AppKey("config", object)
WS_HANDLERS = web.AppKey("websocket_handlers", set)


@dataclass(frozen=True)
class Config:
    source_ip: str
    target_model: str
    upstream: str = "http://127.0.0.1:8080"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8091
    max_body_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        ipaddress.ip_address(self.source_ip)
        if not ipaddress.ip_address(self.listen_host).is_loopback:
            raise ValueError("listener_must_be_loopback")
        upstream = URL(self.upstream)
        if (
            upstream.scheme != "http"
            or not upstream.host
            or not ipaddress.ip_address(upstream.host).is_loopback
            or upstream.user is not None
            or upstream.path not in ("", "/")
            or upstream.query_string
            or upstream.fragment
        ):
            raise ValueError("upstream_must_be_loopback_http_origin")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}", self.target_model):
            raise ValueError("invalid_target_model")
        if not 0 < self.listen_port < 65536 or self.max_body_bytes < 1024:
            raise ValueError("invalid_listener_or_body_limit")


class InvalidPayload(ValueError):
    pass


def is_conversation_model(model):
    name = model.lower().rsplit("/", 1)[-1]
    if any(
        part in name for part in ("image", "audio", "realtime", "tts", "transcribe")
    ):
        return False
    return name.startswith(("gpt-", "codex-")) or bool(
        re.match(r"^o[1-9](?:$|[-.])", name)
    )


def rewrite_model(payload, target, *, websocket=False):
    """Change only the top-level model value, preserving every other input byte."""
    try:
        text = payload.decode("utf-8")
        decoder = json.JSONDecoder()
        pos = 0

        def skip_space(index):
            while index < len(text) and text[index] in " \t\r\n":
                index += 1
            return index

        pos = skip_space(pos)
        if text[pos : pos + 1] != "{":
            raise InvalidPayload("json_object_required")
        pos = skip_space(pos + 1)
        model = None
        model_span = None
        event_type = None
        guarded_keys = set()
        while text[pos : pos + 1] != "}":
            key, pos = decoder.raw_decode(text, pos)
            if not isinstance(key, str):
                raise InvalidPayload("invalid_json_key")
            pos = skip_space(pos)
            if text[pos : pos + 1] != ":":
                raise InvalidPayload("invalid_json_separator")
            start = skip_space(pos + 1)
            value, pos = decoder.raw_decode(text, start)
            if key.lower() == "model" or (websocket and key.lower() == "type"):
                if key != key.lower() or key in guarded_keys:
                    raise InvalidPayload("ambiguous_model_or_event_type")
                guarded_keys.add(key)
            if key == "model":
                if not isinstance(value, str):
                    raise InvalidPayload("model_must_be_string")
                model, model_span = value, (start, pos)
            elif key == "type":
                event_type = value
            pos = skip_space(pos)
            if text[pos : pos + 1] == "}":
                break
            if text[pos : pos + 1] != ",":
                raise InvalidPayload("invalid_json_separator")
            pos = skip_space(pos + 1)
            if text[pos : pos + 1] == "}":
                raise InvalidPayload("invalid_trailing_comma")
        if skip_space(pos + 1) != len(text):
            raise InvalidPayload("invalid_json_suffix")
        if websocket and event_type != "response.create":
            return payload, False
        if not model or not is_conversation_model(model) or model == target:
            return payload, False
        start, end = model_span
        return (text[:start] + json.dumps(target) + text[end:]).encode("utf-8"), True
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, InvalidPayload):
            raise
        raise InvalidPayload("invalid_json") from None


def clean_headers(headers, *, request=False, websocket=False):
    blocked = set(HOP_HEADERS)
    blocked.update(
        item.strip().lower() for item in headers.get("Connection", "").split(",")
    )
    if request:
        blocked.add(ROUTE_HEADER.lower())
        # aiohttp computes framing from the actual, possibly rewritten body.
        blocked.add("content-length")
    if websocket:
        blocked.update(
            {
                "sec-websocket-key",
                "sec-websocket-version",
                "sec-websocket-accept",
                "sec-websocket-extensions",
                "sec-websocket-protocol",
            }
        )
    return CIMultiDict((k, v) for k, v in headers.items() if k.lower() not in blocked)


def matches_source(request, config):
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if not peer:
        return False
    try:
        return ipaddress.ip_address(peer[0]).is_loopback and ipaddress.ip_address(
            request.headers.get(ROUTE_HEADER, "")
        ) == ipaddress.ip_address(config.source_ip)
    except ValueError:
        return False


def error_response(status, code):
    return web.json_response(
        {"error": {"type": "ip_model_router_error", "message": code}}, status=status
    )


async def forward_http(request, config, route):
    headers = clean_headers(request.headers, request=True)
    if route and request.method == "POST" and request.path in HTTP_PATHS:
        if request.headers.get("Content-Encoding", "identity").lower() != "identity":
            return error_response(415, "compressed_request_body_not_supported")
        try:
            payload, changed = rewrite_model(await request.read(), config.target_model)
        except InvalidPayload as exc:
            return error_response(400, str(exc))
        except web.HTTPRequestEntityTooLarge:
            return error_response(413, "request_body_too_large")
        if changed:
            LOG.info(
                "model_route_applied transport=http target=%s", config.target_model
            )
        data = payload
    else:
        data = (
            request.content.iter_chunked(64 * 1024) if request.can_read_body else None
        )
    response = None
    try:
        async with request.app[SESSION].request(
            request.method,
            URL(config.upstream.rstrip("/") + request.raw_path, encoded=True),
            headers=headers,
            data=data,
            allow_redirects=False,
        ) as upstream:
            response = web.StreamResponse(
                status=upstream.status,
                reason=upstream.reason,
                headers=clean_headers(upstream.headers),
            )
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
        LOG.warning("proxy_failure transport=http code=%s", type(exc).__name__)
        if response is not None and response.prepared:
            # Do not turn a truncated SSE stream into a clean, successful EOF.
            if request.transport:
                request.transport.close()
            return response
        return error_response(502, "upstream_connection_failed")


async def relay_websocket(source, destination, target=None):
    while True:
        message = await source.receive()
        if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
            data = (
                message.data.encode("utf-8")
                if message.type == WSMsgType.TEXT
                else message.data
            )
            if target is not None:
                data, changed = rewrite_model(data, target, websocket=True)
                if changed:
                    LOG.info(
                        "model_route_applied transport=websocket target=%s", target
                    )
            if message.type == WSMsgType.TEXT:
                await destination.send_str(data.decode("utf-8"))
            else:
                await destination.send_bytes(data)
        elif message.type == WSMsgType.PING:
            await destination.ping(message.data)
        elif message.type == WSMsgType.PONG:
            await destination.pong(message.data)
        elif message.type == WSMsgType.CLOSE:
            reason = (
                str(message.extra)
                .encode("utf-8")[:123]
                .decode("utf-8", "ignore")
                .encode("utf-8")
            )
            await destination.close(code=message.data, message=reason)
            return
        elif message.type in (WSMsgType.CLOSED, WSMsgType.CLOSING):
            return
        else:
            await destination.close(code=1011, message=b"websocket_connection_failed")
            return


async def forward_websocket(request, config, route):
    protocols = tuple(
        p.strip()
        for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if p.strip()
    )
    try:
        upstream = await request.app[SESSION].ws_connect(
            URL(config.upstream.rstrip("/") + request.raw_path, encoded=True),
            headers=clean_headers(request.headers, request=True, websocket=True),
            protocols=protocols,
            autoping=False,
            autoclose=False,
            timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=2),
            max_msg_size=config.max_body_bytes,
        )
    except aiohttp.WSServerHandshakeError as exc:
        return error_response(
            exc.status if 400 <= exc.status < 600 else 502,
            "upstream_websocket_handshake_failed",
        )
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
        LOG.warning("proxy_failure transport=websocket code=%s", type(exc).__name__)
        return error_response(502, "upstream_connection_failed")
    downstream = web.WebSocketResponse(
        protocols=(upstream.protocol,) if upstream.protocol else (),
        autoping=False,
        autoclose=False,
        compress=False,
        timeout=2,
        max_msg_size=config.max_body_bytes,
    )
    tasks = []
    handler = asyncio.current_task()
    request.app[WS_HANDLERS].add(handler)
    try:
        await downstream.prepare(request)
        tasks = [
            asyncio.create_task(
                relay_websocket(
                    downstream, upstream, config.target_model if route else None
                )
            ),
            asyncio.create_task(relay_websocket(upstream, downstream)),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except InvalidPayload:
        await downstream.close(code=1007, message=b"invalid_json_payload")
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
        LOG.warning("proxy_failure transport=websocket code=%s", type(exc).__name__)
        if downstream.prepared:
            await downstream.close(code=1011, message=b"websocket_connection_failed")
    finally:

        async def cleanup():
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(
                upstream.close(), downstream.close(), return_exceptions=True
            )

        closing = asyncio.create_task(cleanup())
        cancelled = False
        try:
            # Shutdown/disconnect cancellation must not interrupt socket release.
            # Both the app shutdown hook and the HTTP runner can cancel us.
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    cancelled = True
            closing.result()
            if cancelled:
                raise asyncio.CancelledError
        finally:
            request.app[WS_HANDLERS].discard(handler)
    return downstream


async def proxy(request):
    config = request.app[CONFIG]
    route = matches_source(request, config)
    if (
        request.method == "GET"
        and request.path == "/v1/responses"
        and request.headers.get("Upgrade", "").lower() == "websocket"
    ):
        return await forward_websocket(request, config, route)
    return await forward_http(request, config, route)


async def health(request):
    return web.json_response({"status": "ok"})


def create_app(config):
    app = web.Application(
        client_max_size=config.max_body_bytes, handler_args={"auto_decompress": False}
    )
    app[CONFIG] = config
    app[WS_HANDLERS] = set()

    async def shutdown_websockets(application):
        handlers = list(application[WS_HANDLERS])
        for handler in handlers:
            handler.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)

    app.on_shutdown.append(shutdown_websockets)

    async def session_context(application):
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None),
            connector=aiohttp.TCPConnector(limit=0),
            auto_decompress=False,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            skip_auto_headers={
                "Accept",
                "Accept-Encoding",
                "User-Agent",
                "Content-Type",
            },
            max_field_size=65536,
        ) as session:
            application[SESSION] = session
            yield

    app.cleanup_ctx.append(session_context)
    app.router.add_get("/healthz", health)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = Config(**json.loads(Path(args.config).read_text()))
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    # Access/error exception dumps could contain authentication or request data.
    logging.getLogger("aiohttp.server").setLevel(logging.CRITICAL)
    web.run_app(
        create_app(config),
        host=config.listen_host,
        port=config.listen_port,
        access_log=None,
        print=None,
        handler_cancellation=True,
    )


if __name__ == "__main__":
    main()
