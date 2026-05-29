---
sidebar_position: 18
---

# Photon iMessage

Connect Hermes to **iMessage** through [Photon][photon], a managed
service that handles the Apple line allocation and abuse-prevention
layer so you don't have to run your own Mac relay.

The free tier uses Photon's shared iMessage line pool — different
recipients may see different sending numbers, but each conversation
stays stable. The paid Business tier gives every user the same
dedicated number; the plugin supports both, and the free tier is the
recommended starting point.

:::info Free to start
Photon's shared-line pool is free. No subscription is required to send
your first iMessage from Hermes — just a phone number we can bind to
your account.
:::

## Architecture

Inbound messages arrive as **signed webhooks**: Photon POSTs JSON with
an `X-Spectrum-Signature` header to a URL you register, and Hermes'
aiohttp listener verifies the HMAC-SHA256 signature before dispatching
the event into the agent.

Outbound replies go through a small supervised **Node sidecar** that
runs the `spectrum-ts` SDK on loopback. Photon does not currently
expose a public HTTP send-message endpoint — that's a roadmap item on
their side — so until then the sidecar is the only way to call
`Space.send(...)`. The Python plugin starts, supervises, and shuts
down the sidecar automatically. When Photon ships an HTTP send
endpoint we'll retire the sidecar in a follow-up release.

For shared iMessage lines, inbound webhooks and outbound SDK lookups
use slightly different identifiers. Webhooks deliver a canonical
Spectrum space id like `any;-;+<phone>`; the current iMessage SDK
helper resolves a direct-message send space by recipient address
(`+<phone>`). Hermes keeps the webhook `space.id` as the gateway
chat id, and the sidecar maps that id back to the recipient address
when it needs to resolve an uncached outbound space.

## Prerequisites

- A Photon account — sign up at [app.photon.codes][app]
- **Node.js 20.18.1 or newer** on PATH (`node --version`)
- Internet access to download a managed `cloudflared` binary if it is
  not already on PATH
- A phone number that can receive iMessage (used to bind your account)
- For production: a stable named Cloudflare Tunnel, ngrok domain, or
  your own gateway hostname

## First-time setup

```bash
# Set up Photon on this machine.
# Replace the placeholder with your E.164 number:
# + followed by country code and number, no spaces.
hermes photon quick-setup --phone '+<country-code><number>'
```

`quick-setup` is a reconciler. It:

1. Validates the Photon dashboard login, or runs device login when needed
2. Validates local Spectrum credentials before reuse
3. Reuses local project credentials, adopts one matching Photon project
   named `Hermes Agent`, or creates one when none exists
4. Calls the Spectrum `create-user` endpoint with `type: shared` so
   Photon allocates an iMessage line from the free pool
5. Adds the same `--phone` value to `PHOTON_ALLOWED_USERS` as the
   initial Hermes/Photon operator
6. Runs `npm install` inside the plugin's sidecar directory
7. Starts or reuses the gateway for the same Hermes home
8. Proves local `/healthz`
9. Starts or verifies the public webhook URL
10. Registers the current webhook and saves its signing secret
11. Restarts only the current-home gateway if runtime secrets changed
12. Waits until gateway runtime reports `photon=connected`

On success, Hermes prints the assigned iMessage number when Photon
returns one and a compact health summary. On failure, it prints the
specific invariant that failed with evidence such as Hermes home, env
path, service home, port owner, local health, public health, webhook
state, project id, and the last relevant gateway/Photon log lines.

For live diagnostics while setup waits for the gateway, tunnel, and
runtime status, use verbose mode:

```bash
hermes photon quick-setup -v --phone '+<country-code><number>'
```

Advanced/debug commands remain available:

```bash
hermes photon login
hermes photon status
```

Quick setup uses a local Cloudflare Quick Tunnel by default and registers
the public `trycloudflare.com` webhook URL with Photon.

`hermes setup gateway` runs the same guided Photon setup when you choose
Photon. Running setup again is safe: Hermes will not silently duplicate
a matching dashboard project. If multiple matching projects exist, the
setup stops and asks you to select one. To intentionally make a
replacement project, run `hermes photon setup --new-project --phone
'+<country-code><number>'`. To bind Hermes to an existing project, use
`hermes photon projects list` and `hermes photon projects select
<project-id>`.

To let another phone control Hermes later, run
`hermes photon allow-phone '+<country-code><number>'`.
If you register more phone numbers as Photon users, each user may be
assigned a different shared iMessage number. Use the number shown for
that user in the Photon dashboard when starting a new text thread.

Photon secrets are written to `~/.hermes/.env`. The dashboard token is
stored as `PHOTON_DASHBOARD_TOKEN`; the Spectrum project credentials
used by the gateway are stored as `PHOTON_PROJECT_ID` and
`PHOTON_PROJECT_SECRET`.

## Webhook tunnel

Quick setup uses Cloudflare Quick Tunnel by default:

```bash
hermes photon webhook tunnel start    # start/reuse tunnel and register webhook
hermes photon webhook tunnel status   # show pid, public URL, and state file
hermes photon webhook tunnel logs     # show recent cloudflared output
hermes photon webhook tunnel stop     # stop the local managed tunnel
```

The command installs or updates a managed `cloudflared` binary in the
active Hermes profile if one is not already on PATH. It then runs
`cloudflared tunnel --config /dev/null --url
http://127.0.0.1:8788 --no-autoupdate`, reads the
`https://*.trycloudflare.com` URL, appends `/photon/webhook`, registers
that URL with Photon, and saves both `PHOTON_WEBHOOK_SECRET` and
`PHOTON_WEBHOOK_PUBLIC_URL` in `~/.hermes/.env`. Runtime state and logs
live under `~/.hermes/photon/`.

Quick Tunnel URLs are temporary and can change after restart. This is
fine for local setup and testing. For production, use a named Cloudflare
Tunnel or another stable user-owned reverse proxy and register it
manually:

```bash
hermes photon webhook register https://YOUR-PUBLIC-URL/photon/webhook
hermes photon webhook list
hermes photon webhook delete <webhook-id>
```

The registration response includes a `signingSecret` — **Photon only
returns it once.** Hermes saves it to `~/.hermes/.env`:

```bash
PHOTON_WEBHOOK_SECRET=v0_64-char-hex...
PHOTON_WEBHOOK_PUBLIC_URL=https://YOUR-PUBLIC-URL/photon/webhook
```

The plugin verifies every inbound `POST` against this secret and
rejects deliveries with a timestamp drift greater than 5 minutes.
If the same URL is already registered and `PHOTON_WEBHOOK_SECRET` is set
locally, the command is a no-op. If the local secret is missing, delete
or recreate the webhook in the Photon dashboard and save the new signing
secret locally. The managed tunnel flow deletes stale
`trycloudflare.com` webhooks it created when the tunnel URL changes, but
leaves user-owned/manual webhook URLs alone. The gateway performs the
same cleanup when Photon connects, so the active gateway profile owns
the current managed webhook registration.

If the managed `cloudflared` install fails, Hermes prints manual install
instructions and the `hermes photon webhook register ...` fallback.

## Manual gateway runtime

`quick-setup` starts or reuses a gateway for the same Hermes home before
it returns success. For foreground debugging, run:

```bash
hermes gateway run -v
```

You'll see something like:

```
[photon] active Hermes home: /Users/you/.hermes
[photon] started managed webhook tunnel at https://...trycloudflare.com/photon/webhook
[photon] connected — webhook at 0.0.0.0:8788/photon/webhook, sidecar on 127.0.0.1:8789
```

Send an iMessage to your assigned number and Hermes will reply. If you
registered more than one phone, text the assigned number shown for that
specific user in the Photon dashboard.

The active Hermes home must be the same profile where you ran
`hermes photon quick-setup`. If quick setup used a custom `HERMES_HOME`
but `hermes gateway run -v` logs `/Users/you/.hermes`, the gateway is
reading the wrong `.env`.

For always-on local use, install the launchd service and start it:

```bash
hermes gateway install --force
hermes gateway start
```

## Detailed commands

```bash
# First-run setup.
hermes photon quick-setup --phone '+<country-code><number>'
hermes photon quick-setup -v --phone '+<country-code><number>'

# Separate setup steps for debugging or advanced installs.
hermes photon login
hermes photon projects list
hermes photon projects select <dashboard-or-spectrum-project-id>
hermes photon setup --phone '+<country-code><number>'
hermes photon setup --new-project --phone '+<country-code><number>'
hermes photon allow-phone '+<country-code><number>'
hermes photon install-sidecar

# Webhooks.
hermes photon webhook tunnel start
hermes photon webhook tunnel status
hermes photon webhook tunnel logs
hermes photon webhook tunnel stop
hermes photon webhook register https://YOUR-PUBLIC-URL/photon/webhook
hermes photon webhook list
hermes photon webhook delete <webhook-id>

# Readiness and runtime.
hermes photon status
hermes gateway run -v
hermes gateway restart
```

## Status & troubleshooting

```bash
hermes photon status
```

Prints:

```
Photon iMessage status
──────────────────────
  device token        : ✓ stored
  project id          : 3c90c3cc-0d44-4b50-...
  project key         : ✓ stored
  webhook key         : ✓ set
  Hermes home         : /Users/you/.hermes
  env path            : /Users/you/.hermes/.env
  dashboard auth      : ✓ valid
  Spectrum creds      : ✓ valid
  Photon owner        : this Hermes home
  gateway service     : launchd installed; running; home=/Users/you/.hermes
  gateway runtime     : pid 12345; photon=connected
  local health        : ✓ reachable (http://127.0.0.1:8788/healthz)
  node binary         : /usr/bin/node
  sidecar deps        : ✓ installed
  authorized phones   : 1 configured
  webhook public URL  : https://...
  registered webhooks : ✓ 1 registered; current URL registered
  managed tunnel      : ✓ running (pid 12345)
  public health       : ✓ reachable (https://.../healthz)
  next step           : gateway is running; send an iMessage to the Photon number
  docs                : plugins/platforms/photon/README.md; website/docs/user-guide/messaging/photon.md
```

Common issues:

- **`sidecar deps : ✗ run hermes photon install-sidecar`** — Node is
  installed but `spectrum-ts` isn't. Run the suggested command.
- **`webhook key : ⚠ unset — verification disabled`** — the
  plugin will accept ANY POST to the webhook URL, which is unsafe.
  Re-run `hermes photon webhook tunnel start` or
  `hermes photon webhook register` and store the secret.
- **`managed tunnel : ✗ stopped`** — run
  `hermes photon webhook tunnel start`. Quick Tunnel URLs can change
  after restart, so Hermes will update the registered Photon webhook.
- **`public health : ✗ unreachable (... HTTP Error 502: Bad Gateway)`** —
  Cloudflare reached the tunnel before the local gateway was ready.
  `hermes photon status` checks local health and service identity before
  suggesting a repair.
- **`public health : ✗ unreachable (... nodename nor servname provided ...)`
  or `HTTP Error 530`** — the saved Quick Tunnel hostname is not usable.
  Run `hermes photon webhook tunnel stop && hermes photon webhook tunnel start`.
- **`registered webhooks : ... unowned stale managed`** — old managed
  URLs are visible in Photon but are not the first thing to fix when
  the current URL is registered. Public health and gateway connection
  are the blocking checks; delete old webhook IDs manually later with
  `hermes photon webhook list` if needed.
- **`gateway runtime project identity` failed** — quick setup validated
  one Photon project, but the running gateway loaded a different
  `PHOTON_PROJECT_ID`. Restart the current-home gateway so it reloads
  the reconciled `.env`, then rerun quick setup.
- **`PHOTON_WEBHOOK_PORT` already in use** — set a different port via
  `~/.hermes/.env`.
- **Webhook reachable from localhost but Photon can't deliver** —
  Photon needs a public hostname. Cloudflare Tunnel is the easiest
  free option.
- **`photon-sidecar: observed SDK inbound ...` appears but no
  `[photon] webhook delivery received` follows** — the sidecar saw the
  SDK message stream, but Hermes did not receive the signed webhook it
  uses for inbound dispatch. Check that `HERMES_HOME` matches setup,
  then run `hermes photon status`.
- **`unable to resolve space id any;-;+...`** — the sidecar is using
  the webhook `space.id` directly instead of resolving the iMessage
  DM by phone number. Update the Photon plugin; current versions cache
  inbound `Space` objects and fall back to the phone-number lookup.
- **`TypeError: c.build is not a function` while sending** — an old
  sidecar called `space.send(text, { replyTo })`. The SDK expects
  content builders such as `text(...)`; current versions send plain
  text with `space.send(text(...))` and do not wire threaded replies
  yet.

## Webhook management

```bash
hermes photon webhook list                  # show registered hooks
hermes photon webhook delete <webhook-id>   # remove one
```

## Limits today

- **Attachments are metadata-only.** Inbound webhooks carry the
  filename + MIME type but no download URL — Photon documents an
  attachment retrieval endpoint as roadmap.
- **Outbound attachments not wired yet.** Easy to add in the sidecar
  once the agent has reason to send them.
- **Threaded replies not wired yet.** Hermes can carry a `replyTo`
  id internally, but Photon replies require the SDK `reply(...)`
  builder plus the original message object, so the sidecar currently
  sends plain outbound text.
- **Photon's free quotas:** 5,000 messages per server per day,
  50 new-conversation initiations per shared line per day. Increases
  available — email `help@photon.codes`.

## Env vars

| Variable                  | Default            | Notes                                      |
|---------------------------|--------------------|--------------------------------------------|
| `PHOTON_DASHBOARD_TOKEN`  | (unset)            | Set by `hermes photon login`               |
| `PHOTON_PROJECT_ID`       | (unset)            | Set by `hermes photon setup`               |
| `PHOTON_PROJECT_SECRET`   | (unset)            | Set by `hermes photon setup`               |
| `PHOTON_WEBHOOK_SECRET`   | (unset)            | From webhook registration                  |
| `PHOTON_WEBHOOK_PUBLIC_URL` | (unset)          | Registered public webhook URL              |
| `PHOTON_WEBHOOK_TUNNEL_AUTOSTART` | `true`    | Gateway starts/registers managed tunnel for trycloudflare URLs |
| `PHOTON_WEBHOOK_TUNNEL_STOP_ON_DISCONNECT` | `true` | Stop managed tunnel on gateway shutdown |
| `PHOTON_WEBHOOK_PORT`     | `8788`             | Local port for the aiohttp listener        |
| `PHOTON_WEBHOOK_PATH`     | `/photon/webhook`  | Path under which the listener mounts       |
| `PHOTON_WEBHOOK_BIND`     | `0.0.0.0`          | Bind address for the listener              |
| `PHOTON_SIDECAR_PORT`     | `8789`             | Loopback port for sidecar control          |
| `PHOTON_SIDECAR_AUTOSTART`| `true`             | Whether the adapter spawns the sidecar     |
| `PHOTON_NODE_BIN`         | `which node`       | Override the Node binary path              |
| `PHOTON_HOME_CHANNEL`     | (unset)            | Default space ID for cron / notifications  |
| `PHOTON_ALLOWED_USERS`    | (unset)            | Comma-separated E.164 allowlist; setup seeds `--phone` |
| `PHOTON_ALLOW_ALL_USERS`  | `false`            | Dev only — accept any sender               |

[photon]: https://photon.codes/
[app]: https://app.photon.codes/
