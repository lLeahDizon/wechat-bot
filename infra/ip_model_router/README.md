# IP model routing

This optional loopback proxy lets Caddy select a public source IP and rewrite
GPT/Codex conversation model aliases before forwarding to the existing Sub2API
gateway. It preserves the original API key, reasoning parameters, request fields,
and Sub2API authentication and billing path. It does not store credentials,
request bodies, or response bodies. Logs contain fixed event codes and the
configured target model only.

The proxy covers POST `/v1/responses`, `/v1/responses/compact`,
`/v1/chat/completions`, `/v1/messages`, `/v1/alpha/search`, and every
`response.create` frame on GET `/v1/responses` WebSockets. Non-GPT models,
image/audio/realtime models, and other paths pass through. SSE is streamed without
buffering the complete answer; there is no total generation or idle read timeout.
WebSocket text/binary messages, subprotocol negotiation, ping/pong, close, and
omitted-model follow-up turns are supported. The proxy never retries generation
requests or follows redirects. It rejects ambiguous model keys and compressed
HTTP generation request bodies instead of allowing the policy to be bypassed.

## Upstream requirements

Sub2API still performs account/channel mappings after this rewrite. Before
enabling a rule, verify that **every schedulable account in the affected keys'
groups maps the target model to itself**, and that no channel mapping changes it.
Verify an actual completed request's `upstream_model`; a rewritten request body or
the client's selected model alone does not prove the final model. Recheck this
invariant when changing accounts, groups, channels, or model mappings. The
operator must disable the IP route if this invariant no longer holds.

Use Caddy's real TCP peer matcher when clients connect directly. If a CDN or
another reverse proxy is introduced, first establish an explicit trusted-proxy
chain and revalidate real client IP handling. Never trust arbitrary caller
`X-Forwarded-For` or the private routing header. Shared NAT/proxy egress IPs are
shared routing identities.

## Install and enable

Keep the real config, Caddy backup, deployment receipts, and runtime artifacts
outside Git. Copy `router.py`, `requirements.txt`, and the service file to a new
`/opt/sub2api-ip-model-router` directory. Create a dedicated Python 3.12+ venv and
install `requirements.txt` there; do not change the bot's Python environment.
Use `config.example.json` as a template. The source IP and target model are
nonsecret config values; no API key is stored in this component.

The systemd unit uses a dynamic unprivileged identity, read-only filesystem,
loopback listener/upstream, and a 512 MiB memory limit. Source/config files must
be root-owned and readable by the service (for example mode 0644 in a 0755
directory). Install the unit in `/etc/systemd/system`, enable/start it, and check
`http://127.0.0.1:8091/healthz` before changing Caddy.

Inside the existing API site's gateway handler, replace the ordinary
`reverse_proxy` with this ordered route. Substitute the same real source IP used
by the private proxy config:

```caddyfile
route {
    @ip_model_route {
        remote_ip 203.0.113.7
        path /v1/responses /v1/responses/compact /v1/chat/completions /v1/messages /v1/alpha/search
    }
    reverse_proxy @ip_model_route 127.0.0.1:8091 {
        header_up X-Sub2API-Route-Client-IP {http.request.remote.host}
    }
    reverse_proxy 127.0.0.1:8080
}
```

Other source IPs go directly to the original upstream. The private header is
overwritten by Caddy and stripped before Sub2API. The component only trusts it
from loopback peers and does not open a public port. Validate the complete Caddy
config, inspect the adapted route order, then hot-reload Caddy. Existing streams
may finish on the old route; new requests/connections use the new rule. No
Sub2API or bot restart is needed. Failure of this component fails matched calls
rather than silently using the original model.

## Verify and roll back

Run deterministic tests with no real keys, model calls, or external network:

```sh
python -m unittest discover -s infra/ip_model_router/tests -v
ruff check infra/ip_model_router
```

After deployment, check the component and Sub2API health, the Caddy matcher/order,
unauthenticated HTTP/WebSocket rejection, and new natural traffic for the target
IP. Inspect only request timestamps and requested/upstream model summaries; do
not retain bodies, keys, or raw authentication headers. Confirm other IPs still
use the original route. A current matching-IP HTTP/WS receipt and completed usage
record are separate verification steps; mark either pending if it has not
occurred yet.

To roll back, restore the saved Caddyfile and validate/hot-reload it first. Then
stop/disable `sub2api-ip-model-router`. Do not stop the proxy while Caddy still
depends on it. Keep the original Sub2API configuration, accounts, data, and
containers intact.
