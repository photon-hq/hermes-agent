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

Photon is a first-class channel like Telegram or Discord: one persistent
connection for the lifetime of the gateway, with **no webhook and no
public tunnel** to configure.

Because the `spectrum-ts` SDK is TypeScript and Hermes is Python, a small
supervised **Node sidecar** holds Photon's managed **gRPC** connection on
Hermes' behalf:

- **Inbound** messages arrive on the SDK's `app.messages` gRPC stream
  inside the sidecar. The sidecar emits one newline-delimited JSON
  (NDJSON) event per message on its stdout; the Python adapter parses
  each line, dedupes by message id, and dispatches it to the agent.
- **Outbound** replies are NDJSON commands the Python adapter writes to
  the sidecar's stdin; the sidecar calls `space.send(...)` through the
  SDK.

The Python plugin starts, supervises, and shuts down the sidecar
automatically. If the sidecar crashes, the gateway's reconnect watcher
recreates the channel, exactly like other platforms. Only one gateway
per Spectrum project may run at a time (enforced by a runtime lock).

For shared iMessage lines, Spectrum carries a canonical space id like
`any;-;+<phone>` for direct messages and `any;+;<chat-guid>` for groups.
Hermes keeps that `space.id` as the gateway chat id; the sidecar caches
inbound `Space` objects and resolves an uncached outbound DM by recipient
address.

## Prerequisites

- A Photon account — sign up at [app.photon.codes][app]
- **Node.js 20.18.1 or newer** on PATH (`node --version`)
- A phone number that can receive iMessage (used to bind your account)

There is nothing to expose to the public internet — no tunnel,
reverse proxy, or open port is required.

## First-time setup

```bash
# Set up Photon on this machine.
# Replace the placeholder with your E.164 number:
# + followed by country code and number, no spaces.
hermes photon quick-setup --phone '+<country-code><number>'
```

`quick-setup` is an idempotent reconciler. It:

1. Validates the Photon dashboard login, or runs device login
   (`client_id=photon-cli`) when needed.
2. Reuses local Spectrum credentials, adopts one matching Photon project
   named `Hermes Agent`, or creates one when none exists, and stores
   `PHOTON_PROJECT_ID` / `PHOTON_PROJECT_SECRET`.
3. Creates the shared iMessage user for `--phone` (`type: shared`, from
   the free pool) only if one does not already exist, and authorizes that
   sender in `PHOTON_ALLOWED_USERS`.
4. Runs `npm install` inside the plugin's sidecar directory if needed.
5. Enables `platforms.photon` and starts (or restarts) the current-home
   gateway.
6. Waits until gateway runtime reports `photon=connected`, then prints
   the assigned iMessage number.

On success, Hermes prints the assigned iMessage number when Photon
returns one. On failure, it prints the specific invariant that failed
with evidence such as Hermes home, env path, gateway service identity,
project id, and the last relevant gateway/Photon log lines.

For live diagnostics while setup waits for the gateway and runtime
status, use verbose mode:

```bash
hermes photon quick-setup -v --phone '+<country-code><number>'
```

:::note Testing with a custom Hermes home

If you export `HERMES_HOME` to test Photon in an isolated home,
`quick-setup` uses that exported home as the current home. An installed
gateway service for another home is ignored, not rewritten — Hermes
starts a temporary gateway for the current home. The per-project runtime
lock still prevents two gateways from streaming the same Photon project.

:::

`hermes setup gateway` runs the same guided Photon setup when you choose
Photon. Running setup again is safe: Hermes will not silently duplicate a
matching dashboard project. If multiple matching projects exist, setup
stops and asks you to select one. To intentionally make a replacement
project, run `hermes photon quick-setup --new-project --phone
'+<country-code><number>'`. To bind Hermes to an existing project, use
`hermes photon projects list` and `hermes photon projects select
<project-id>`.

To let another phone control Hermes later, run
`hermes photon allow-phone '+<country-code><number>'`. If you register
more phone numbers as Photon users, each user may be assigned a different
shared iMessage number. Use the number shown for that user in the Photon
dashboard when starting a new text thread.

Photon secrets are written to `~/.hermes/.env`. The dashboard token is
stored as `PHOTON_DASHBOARD_TOKEN`; the Spectrum project credentials used
by the gateway are stored as `PHOTON_PROJECT_ID` and
`PHOTON_PROJECT_SECRET`.

## Manual gateway runtime

`quick-setup` starts (or restarts) a gateway for the current Hermes home
before it returns success. For foreground debugging, run:

```bash
hermes gateway run -v
```

You'll see something like:

```
[photon] connected via spectrum-ts gRPC stream (project 3c90c3cc-..., home /Users/you/.hermes)
```

Send an iMessage to your assigned number and Hermes will reply. If you
registered more than one phone, text the assigned number shown for that
specific user in the Photon dashboard.

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
hermes photon allow-phone '+<country-code><number>'
hermes photon reset
hermes photon reset --all

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
  Hermes home         : /Users/you/.hermes
  env path            : /Users/you/.hermes/.env
  dashboard auth      : ✓ valid
  Spectrum creds      : ✓ valid
  gateway service     : launchd installed; running; home=/Users/you/.hermes
  gateway runtime     : pid 12345; photon=connected
  node binary         : /usr/bin/node
  sidecar deps        : ✓ installed (spectrum-ts 1.7.2)
  authorized phones   : 1 configured
  next step           : gateway is running; send an iMessage to the Photon number
  docs                : plugins/platforms/photon/README.md; website/docs/user-guide/messaging/photon.md
```

Common issues:

- **`sidecar deps : ✗ ... quick-setup ...`** — Node is installed but
  `spectrum-ts` is not runnable. Re-run quick setup so Hermes can repair it.
- **`gateway runtime : photon=fatal` or stuck not connected** — check
  `~/.hermes/logs/gateway.log` for `[photon-sidecar]` lines. A
  `SPECTRUM_TS_MISSING` fatal means the sidecar dependencies aren't
  installed; a `SPECTRUM_INIT_FAILED` fatal usually means the project
  credentials are wrong or revoked.
- **`gateway runtime` shows a different project id** — quick setup
  validated one Photon project, but the running gateway loaded a
  different `PHOTON_PROJECT_ID`. Restart the current-home gateway so it
  reloads the reconciled `.env`, then rerun quick setup.
- **`photon_lock` fatal** — another gateway (often a different
  `HERMES_HOME`) is already streaming this Photon project. Stop it first,
  or use a different project.
- **`unable to resolve space id any;-;+...`** — the sidecar could not
  resolve an outbound DM space. For groups, Hermes can only send into a
  group it has already received a message from in this session.

## Limits today

- **Attachments are metadata-only.** Inbound events carry the filename +
  MIME type. The SDK exposes attachment bytes, so an on-demand fetch is a
  possible follow-up.
- **Outbound attachments not wired yet.** Easy to add in the sidecar
  once the agent has reason to send them.
- **Threaded replies not wired yet.** Hermes can carry a `replyTo` id
  internally, but Photon replies require the SDK `reply(...)` builder plus
  the original message object, so the sidecar currently sends plain
  outbound text.
- **Photon's free quotas:** 5,000 messages per server per day, 50
  new-conversation initiations per shared line per day. Increases
  available — email `help@photon.codes`.

## Env vars

| Variable                  | Default            | Notes                                      |
|---------------------------|--------------------|--------------------------------------------|
| `PHOTON_DASHBOARD_TOKEN`  | (unset)            | Set by `hermes photon login`               |
| `PHOTON_PROJECT_ID`       | (unset)            | Set by `hermes photon quick-setup`         |
| `PHOTON_PROJECT_SECRET`   | (unset)            | Set by `hermes photon quick-setup`         |
| `PHOTON_SIDECAR_AUTOSTART`| `true`             | Whether the adapter spawns the sidecar     |
| `PHOTON_NODE_BIN`         | `which node`       | Override the Node binary path              |
| `PHOTON_API_HOST`         | `https://spectrum.photon.codes` | Spectrum API host             |
| `PHOTON_DASHBOARD_HOST`   | `https://app.photon.codes` | Dashboard API host                 |
| `PHOTON_HOME_CHANNEL`     | (unset)            | Default space ID for cron / notifications  |
| `PHOTON_ALLOWED_USERS`    | (unset)            | Comma-separated E.164 allowlist; setup seeds `--phone` |
| `PHOTON_ALLOW_ALL_USERS`  | `false`            | Dev only — accept any sender               |

[photon]: https://photon.codes/
[app]: https://app.photon.codes/
