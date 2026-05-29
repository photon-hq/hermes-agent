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

## Follow-up Update: Quick Setup Hardening

The same May 29 session exposed several separate states that looked similar in
the terminal because they all stopped `quick-setup` before message delivery.
They are not the same bug.

| Issue | Expected behavior | Actual behavior | Change made | Fixed? |
| --- | --- | --- | --- | --- |
| First-run setup after `reset --all` | One `hermes photon quick-setup` run should create/adopt the project, create the webhook/tunnel state, then start a gateway that loads the same `PHOTON_PROJECT_ID` and secret. | Reset removed `PHOTON_WEBHOOK_PUBLIC_URL` and `PHOTON_WEBHOOK_SECRET`. The gateway could restart before Photon had enough runtime config, log `No messaging platforms enabled`, and quick-setup could compare against stale Photon runtime status from an older gateway. | Quick setup now prepares the public webhook path and registers/saves the signing secret before requiring gateway local health. It then verifies that the running gateway loaded the reconciled Photon credentials. | Fixed for the quick-setup path. If stale platform state appears outside quick-setup, inspect `gateway_state.json` timestamps against the gateway start. |
| Dashboard project vs Spectrum project | Hermes should treat `PHOTON_PROJECT_ID` plus `PHOTON_PROJECT_SECRET` as the canonical runtime identity, and only use the dashboard to prove the logged-in account can see that project. | Selecting by display name alone was ambiguous because multiple dashboard projects can be named `Hermes Agent`. | Quick setup verifies that the dashboard project maps to the current runtime `PHOTON_PROJECT_ID`. The project id paired with the secret remains canonical. | Fixed as a guardrail. The dashboard id is not the runtime source of truth. |
| Existing phone user conflict | Creating a user for the selected project should either create the user in that project or verify the existing user in that same project. | Photon returned `409 Conflict` for a phone that had been used repeatedly, but the user was not visible in the selected project user list. | On conflict, quick setup now looks up the phone in the current project. If it is not there, setup stops with `phone exists, but not in the current Photon project`. | Diagnostic fixed. The underlying allocation/user ownership is a Photon state; use a fresh phone or attach the phone to the selected Photon project. |
| Active-home ownership | A Photon project should be owned by one Hermes home at a time so one home does not delete or mutate another home's webhook/tunnel state. | A custom test home was blocked because `/Users/raysmacbookair/.hermes/photon/active-home.json` still claimed the project for `/Users/raysmacbookair/.hermes-photon-test`. | No ownership rule change. Reset from the owning home removes the claim; a different home should not silently steal it. | Not a bug. The message is expected, but the repair should stay explicit: run setup from the owning home or reset Photon from that home first. |
| Custom `HERMES_HOME` with installed launchd service | If `HERMES_HOME` is exported, Photon quick setup should use that current home and avoid mutating another installed service. | The installed launchd gateway could point to a different home, causing quick setup to stop even though the exported test home was intentional. | Photon CLI now ignores a wrong-home installed service for quick setup and starts a temporary current-home gateway when the webhook port is free. It does not rewrite the launchd service. | Fixed for quick setup. Persistent service repair still requires `hermes gateway install --force` from the intended home. |
| Sidecar port conflict | The Photon sidecar port should be free, or already owned by the current-home gateway. | An orphan Node sidecar from an earlier test kept `127.0.0.1:8799` bound, so the new gateway could not connect Photon. | Quick setup now checks the sidecar port before starting the gateway. It stops a stale orphan Photon sidecar from another home, or fails with the owning pid and repair command. | Fixed for stale orphan Photon sidecars. Other active processes are reported, not killed automatically. |
| Forced gateway stop orphaned sidecars | If the gateway must be force-stopped, its adapter children should not keep Photon test ports busy. | A timed-out stop killed only the Python gateway process, leaving the Node sidecar alive as an orphan on the sidecar port. | Gateway forced-stop now kills the gateway process tree on POSIX. Windows already uses `taskkill /T /F`. | Fixed for forced gateway stop. |
| Public health DNS failure | If local health is OK, the public Quick Tunnel hostname should resolve through the Mac's system resolver and forward `/healthz` to the gateway. | Local health returned `200 OK`, but `curl` and Python could not resolve the `trycloudflare.com` hostname while direct DNS checks sometimes could. This is a system resolver / transient Quick Tunnel hostname state, not a gateway forwarding failure. | Public health now classifies resolver failures as `system DNS failed to resolve <host>` and quick setup reports `step: public webhook DNS` with concrete next steps. It does not bypass system DNS with an IP fallback. | Diagnostic fixed. The root resolver/tunnel propagation state is external; wait 30-60 seconds and rerun, then rotate the managed tunnel if it repeats. |

Focused verification after the latest Photon CLI changes:

```bash
venv/bin/python -m py_compile gateway/status.py hermes_cli/gateway.py plugins/platforms/photon/cli.py plugins/platforms/photon/tunnel.py
git diff --check -- gateway/status.py hermes_cli/gateway.py plugins/platforms/photon/cli.py plugins/platforms/photon/tunnel.py plugins/platforms/photon/README.md plugins/platforms/photon/DEBUGGING.md website/docs/user-guide/messaging/photon.md tests/gateway/test_status_process_tree.py tests/hermes_cli/test_gateway_service.py tests/plugins/platforms/photon/test_cli_service_identity.py tests/plugins/platforms/photon/test_tunnel.py
./scripts/run_tests.sh tests/gateway/test_status_process_tree.py tests/plugins/platforms/photon
venv/bin/python -m pytest tests/hermes_cli/test_gateway_service.py::TestLaunchdServiceRecovery::test_wait_for_gateway_exit_force_kills_process_tree -q
```

Result: focused Photon/gateway tests passed.

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
- `quick-setup` says the installed gateway service belongs to a different home
  and starts a temporary gateway for the current home.

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

Current `quick-setup` behavior:

- The exported/current `HERMES_HOME` is treated as authoritative.
- An installed service for a different home is ignored for setup purposes.
- `quick-setup` may start a temporary current-home gateway instead of mutating
  or reusing the wrong installed service.
- The installed service should still be repaired before normal long-running use.

### Sidecar Port Conflict

Symptoms in `gateway.log`:

```text
Error: listen EADDRINUSE: address already in use 127.0.0.1:8789
photon failed to connect
```

Likely cause:

Another Photon sidecar is still bound to `8789`.

This can happen when `hermes gateway stop` has to escalate to a force kill. A
normal graceful gateway stop lets the Photon adapter stop its Node sidecar. A
forced parent kill skips adapter cleanup, so the sidecar can become an orphan
with parent PID `1`.

Checks:

```bash
lsof -nP -iTCP:8789 -sTCP:LISTEN
tail -n 80 ~/.hermes/logs/gateway.log
```

Fix the duplicate process, or use a different `PHOTON_SIDECAR_PORT` for an
isolated test profile.

Current `quick-setup` behavior:

- If the sidecar port is free, setup continues.
- If the port is owned by the current-home running gateway, setup continues.
- If the port is owned by a stale orphan Photon sidecar from another home,
  setup stops it automatically.
- If ownership is ambiguous, setup fails with the owning PID and a repair
  command instead of surfacing a later `EADDRINUSE` crash.

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

The Photon CLI and gateway stop path now prioritize concrete setup blockers
over stale/noisy state:

- Public-health details are passed into the status next-step decision.
- Transient `502 Bad Gateway` checks are retried briefly before surfacing a
  failure.
- DNS and `530` failures for managed Quick Tunnel URLs suggest tunnel stop/start.
- Persistent gateway-origin public-health failures suggest `hermes gateway
  restart`.
- Unowned stale managed webhooks remain visible, but they no longer become the
  main next step when the current URL is registered.
- Custom `HERMES_HOME` quick-setup runs ignore installed gateway services that
  belong to another home and use a temporary current-home gateway instead.
- Sidecar port ownership is checked before gateway startup.
- Forced gateway stop kills the process tree so Photon sidecars do not remain
  orphaned on the sidecar port.

Documentation now calls out that unowned stale URLs are usually optional cleanup,
while public health and gateway readiness are the blocking checks.

## Not Fixed Here

This work does not repair an installed launchd/systemd service that points at
the wrong checkout or home. `quick-setup` can avoid reusing that service, but
normal long-running gateway use should still reinstall or repair the service.

This work also does not make Cloudflare Quick Tunnel URLs durable. Quick Tunnel
URLs are ephemeral by design; when one dies, the correct repair is to create and
register the new current tunnel URL.

This work does not prove whether Spectrum allows the same user phone to be
cleanly re-created across many Photon projects. Use a fresh phone number when
validating first-run setup, and treat repeated `409 Conflict` user creation as a
separate Photon/Spectrum account-state investigation.
