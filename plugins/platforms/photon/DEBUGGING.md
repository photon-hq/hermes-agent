# Photon Debugging Notes

This note captures the May 29, 2026 Photon tunnel debugging session. It is
intended for developers validating local Photon iMessage delivery through the
Hermes gateway.

## Expected Path

Photon inbound delivery has four moving pieces:

1. Photon has a registered webhook URL for the current project.
2. `cloudflared` owns the public Quick Tunnel hostname and forwards it to the
   local gateway.
3. The Hermes gateway listens locally for health checks and signed Photon
   webhooks.
4. The Photon sidecar handles outbound sends through `spectrum-ts`.

Expected endpoints:

| Layer | Endpoint | Expected result |
| --- | --- | --- |
| Local gateway health | `http://127.0.0.1:8788/healthz` | `200 OK` with body `ok` |
| Local Photon webhook | `http://127.0.0.1:8788/photon/webhook` | `POST` only; signed Photon payloads |
| Public health | `https://<quick-tunnel>.trycloudflare.com/healthz` | `200 OK` with body `ok` |
| Public Photon webhook | `https://<quick-tunnel>.trycloudflare.com/photon/webhook` | Registered in Photon |
| Sidecar health | `http://127.0.0.1:8789/healthz` | Internal `POST` with `X-Hermes-Sidecar-Token` |

Defaults can change through `PHOTON_WEBHOOK_PORT`, `PHOTON_WEBHOOK_PATH`, and
`PHOTON_SIDECAR_PORT`.

## Healthy Status

A clean setup should converge to:

```text
webhook public URL  : https://<current>.trycloudflare.com/photon/webhook
registered webhooks : current URL registered
managed tunnel      : running
public health       : reachable
next step           : gateway is running; send an iMessage to the Photon number
```

The gateway log should include:

```text
[photon] active Hermes home: ...
[photon] webhook public URL: https://<current>.trycloudflare.com/photon/webhook
[photon] reused/started managed webhook tunnel at ...
[photon] managed webhook URL already registered
[photon-sidecar] photon-sidecar: listening on 127.0.0.1:8789
[photon] connected - webhook at 0.0.0.0:8788/photon/webhook, sidecar on 127.0.0.1:8789
```

## What Happened

The first public-health failure was not caused by the local gateway being
blocked. The local checks later showed:

```bash
lsof -nP -iTCP:8788 -sTCP:LISTEN
curl -i http://127.0.0.1:8788/healthz
```

The gateway process was listening on `8788`, and `/healthz` returned `200 OK`.
That means the local receiver was healthy.

The Cloudflare log showed a different problem:

- `00:49:13` Cloudflare created
  `https://pointing-assuming-warnings-airline.trycloudflare.com`.
- `00:50:59` that tunnel shut down.
- Later starts received new Quick Tunnel URLs.
- Hermes still reported and registered the old `pointing-assuming...` URL.

The matching status error was:

```text
nodename nor servname provided, or not known
```

That means the saved public URL was no longer a resolvable Quick Tunnel
hostname. Restarting the gateway cannot repair a dead public hostname; the
managed tunnel must be stopped and started so Hermes saves and registers the
new current URL.

There was also a separate transient readiness race after a fresh tunnel start:

```text
HTTP Error 502: Bad Gateway
```

The cloudflared log showed:

```text
dial tcp 127.0.0.1:8788: connect: connection refused
```

This happened before the gateway finished bringing up Photon. A later status
check succeeded without another configuration change.

## Failure Modes

### Dead Quick Tunnel Hostname

Symptoms:

```text
public health : unreachable (... nodename nor servname provided ...)
public health : unreachable (... HTTP Error 530 ...)
```

Likely cause:

The saved `PHOTON_WEBHOOK_PUBLIC_URL` points at a Quick Tunnel hostname that
Cloudflare no longer serves.

Fix:

```bash
hermes photon webhook tunnel stop
hermes photon webhook tunnel start
hermes gateway restart
hermes photon status
```

### Gateway Not Ready Yet

Symptoms:

```text
public health : unreachable (... HTTP Error 502: Bad Gateway ...)
```

Likely cause:

Cloudflare can reach the tunnel, but the tunnel cannot reach the local gateway
yet. This is common immediately after `hermes photon webhook tunnel start` or
`hermes gateway restart`.

Fix:

```bash
hermes gateway restart
sleep 5
hermes photon status
```

If it persists, verify local health:

```bash
lsof -nP -iTCP:8788 -sTCP:LISTEN
curl -i http://127.0.0.1:8788/healthz
tail -n 80 ~/.hermes/logs/gateway.log
tail -n 80 ~/.hermes/photon/cloudflared.log
```

### Wrong Gateway Service

Symptoms:

- `hermes photon status` uses one `HERMES_HOME`, but the gateway service reads
  another.
- `hermes gateway status` shows an unexpected Python path, checkout path, or
  log path.
- Photon owner does not match the active Hermes home.

Checks:

```bash
hermes gateway status
hermes photon status
```

On macOS, verify the launchd service points at the intended checkout and home:

```bash
launchctl print gui/$(id -u)/ai.hermes.gateway
```

Fix:

```bash
hermes gateway stop
hermes gateway install --force
hermes gateway start
hermes gateway status
```

This is a broader gateway-service identity problem, not a Photon-specific
webhook registration problem.

### Sidecar Port Conflict

Symptoms in `gateway.log`:

```text
Error: listen EADDRINUSE: address already in use 127.0.0.1:8789
photon failed to connect
```

Likely cause:

Another Photon sidecar is still bound to `8789`.

Checks:

```bash
lsof -nP -iTCP:8789 -sTCP:LISTEN
tail -n 80 ~/.hermes/logs/gateway.log
```

Fix the duplicate process, or use a different `PHOTON_SIDECAR_PORT` for an
isolated test profile.

### Stale Managed Webhooks

Symptoms:

```text
registered webhooks : current URL registered; N unowned stale managed
```

Meaning:

Photon has old `trycloudflare.com` webhooks from another Hermes home or an old
test run. These are cleanup noise when the current URL is registered and public
health is reachable.

Owned stale webhooks can be cleaned by:

```bash
hermes photon webhook tunnel start
```

Unowned stale webhooks require manual review:

```bash
hermes photon webhook list
hermes photon webhook delete <webhook-id>
```

Do not treat unowned stale cleanup as the first fix when public health is
failing. Fix the public-health failure first.

## Files To Inspect

| File | Purpose |
| --- | --- |
| `~/.hermes/.env` | Photon project credentials, webhook secret, public webhook URL |
| `~/.hermes/photon/tunnel.json` | Managed tunnel PID, public URL, webhook URL, owned webhook IDs |
| `~/.hermes/photon/active-home.json` | Photon active-home owner record |
| `~/.hermes/photon/cloudflared.log` | Quick Tunnel lifecycle and origin connection errors |
| `~/.hermes/logs/gateway.log` | Gateway startup, Photon adapter, webhook delivery, sidecar logs |
| `~/Library/LaunchAgents/ai.hermes.gateway.plist` | macOS gateway service definition |

For isolated tests, set both `HERMES_HOME` and `PHOTON_ACTIVE_HOME_FILE` so the
test profile does not collide with the user's normal Photon owner record.

## Commands Used During Debugging

```bash
hermes photon status
hermes photon webhook list
hermes photon webhook tunnel status
hermes photon webhook tunnel logs
hermes gateway status
lsof -nP -iTCP:8788 -sTCP:LISTEN
curl -i http://127.0.0.1:8788/healthz
tail -f ~/.hermes/logs/gateway.log
tail -f ~/.hermes/photon/cloudflared.log
```

## Fixes In This Patch

The Photon CLI now prioritizes public-health failures over stale webhook
cleanup:

- Public-health details are passed into the status next-step decision.
- Transient `502 Bad Gateway` checks are retried briefly before surfacing a
  failure.
- DNS and `530` failures for managed Quick Tunnel URLs suggest tunnel stop/start.
- Persistent gateway-origin public-health failures suggest `hermes gateway
  restart`.
- Unowned stale managed webhooks remain visible, but they no longer become the
  main next step when the current URL is registered.

Documentation now calls out that unowned stale URLs are usually optional cleanup,
while public health and gateway readiness are the blocking checks.

## Not Fixed Here

This work does not solve every duplicate-Hermes scenario. If multiple local
Hermes checkouts have installed the same launchd service label, the gateway can
still be started from an unexpected checkout or home. That needs a broader
gateway-service identity hardening pass.

This work also does not make Cloudflare Quick Tunnel URLs durable. Quick Tunnel
URLs are ephemeral by design; when one dies, the correct repair is to create and
register the new current tunnel URL.
