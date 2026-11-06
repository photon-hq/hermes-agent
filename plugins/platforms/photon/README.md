# Photon iMessage platform plugin

This plugin connects Hermes Agent to iMessage (and other Spectrum
interfaces) through [Photon][photon] — a managed service that handles
iMessage line allocation, delivery, and abuse-prevention so users don't
have to run their own Mac relay.

The free tier uses Photon's shared iMessage line pool and is the path we
recommend for everyone who doesn't already pay for a dedicated number.

## Architecture

Like Discord and Slack, Photon is a **persistent-connection** channel — no
public URL, no webhook, no signing secret. The `spectrum-ts` SDK holds a
long-lived **gRPC stream** to Photon for both directions. Because the SDK is
TypeScript-only, Hermes runs it inside a small supervised Node sidecar and
talks to it over loopback.

```
                         gRPC (spectrum-ts)
┌─────────────────────────┐ ◄───────────────► ┌──────────────────────┐
│  Photon Spectrum cloud  │   app.messages    │  Node sidecar        │
│  (iMessage line owner)  │   space.send()    │  (plugins/…/sidecar) │
└─────────────────────────┘                   └──────────┬───────────┘
                                       GET /inbound (NDJSON) │  ▲ POST /send
                                       inbound events        ▼  │ /send-richlink
                                                            │  │ /typing
                                              ┌──────────────────────┐
                                              │  PhotonAdapter        │
                                              │  (Python, in gateway) │
                                              └──────────────────────┘
```

- **Inbound**: the sidecar consumes the SDK's `app.messages` gRPC stream,
  normalizes each message, and streams it to the adapter over a loopback
  `GET /inbound` (NDJSON). The adapter dedupes on `messageId` and dispatches
  a `MessageEvent` to the gateway. It reconnects automatically if the stream
  drops; the sidecar owns the gRPC reconnect to Photon.
- **Outbound**: `send` / `send_typing` / reaction tapbacks are loopback POSTs
  to the sidecar (`/send`, `/send-richlink`, `/send-attachment`, `/typing`,
  `/react`, `/unreact`), authenticated with a shared
  `X-Hermes-Sidecar-Token`.

## First-time setup

```bash
# One-shot setup: device login (only when needed) + project + API key + iMessage line
hermes photon setup --phone +15551234567

# Start the gateway
hermes gateway start
```

Hermes runs on Photon 2.0 (`https://api.photon.codes/v1`). No account yet? Create one at
[app.beta.photon.codes][beta]. `hermes photon setup` does, in order:

1. **Project and API key.** When `PHOTON_PROJECT_ID` and `PHOTON_API_KEY` are already set and
   Photon accepts the key, setup reuses them and never opens a browser. Otherwise it signs you in
   with Photon's device login (RFC 8628: open `https://auth.photon.codes/device` on any device
   and enter the code), finds or creates the `Hermes Agent` project in your organization, and
   mints a project API key. A refused key is replaced and the previous Hermes key revoked.
2. **Assign a shared iMessage line** to your phone number (idempotent; reuses an existing
   assignment) and print the number you text to reach your agent.
3. **Wait for your first text.** A shared line can only reply to, or start a conversation
   with, a number that has texted it. On a terminal, setup waits up to five minutes for that
   message; otherwise `hermes photon status` shows when routing goes live.
4. **Enable the platform** in `config.yaml` and allowlist your number.

**No browser at all:** create a project API key in the dashboard and set the two variables in
`~/.hermes/.env` (or the process environment) before running setup, or instead of it:

```bash
PHOTON_PROJECT_ID=pho_prj_...
PHOTON_API_KEY=pho_sk_...
```

Setup then only needs the account for steps the key cannot do, such as taking a seat when the
key lacks `platforms:write`. The device login works headless too: it prints a URL and a code
to open on any other device.

**Coming from Photon Spectrum** (the platform before Photon 2.0, with `PHOTON_PROJECT_SECRET`):
create a Photon 2.0 account and run `hermes photon setup`. Setup moves the Spectrum
credentials out of `.env` into `auth.json` (`credential_pool.photon_legacy`), where
Photon's coming migration tool can pick them up, and connects a new Photon 2.0 project.

Re-running `setup` resumes where it left off. Run `hermes photon status` to see what's
configured.

## Credentials

Runtime credentials live in `~/.hermes/.env` (the same place every other channel keeps its
token), and the adapter reads them from the environment:

```bash
PHOTON_PROJECT_ID=pho_prj_...   # Photon 2.0 project id
PHOTON_API_KEY=pho_sk_...       # project API key (shown once, when it is created)
```

Management metadata lives in `~/.hermes/auth.json` under `credential_pool`. No API key is
stored there:

```jsonc
{
  "credential_pool": {
    "photon": [          // device-login session, used by setup only (the refresh token rotates)
      { "access_token": "...", "refresh_token": "...", "expires_at": 0, "email": "you@example.com" }
    ],
    "photon_project": [
      { "project_id": "pho_prj_...", "organization_id": "pho_org_...", "api_key_id": "...", "name": "Hermes Agent" }
    ],
    "photon_user": [
      { "phone_number": "+15551234567", "assigned_phone_number": "+1628...", "assigned_at": "..." }
    ],
    "photon_legacy": [   // only after moving off Photon Spectrum
      { "spectrum_project_id": "...", "project_secret": "..." }
    ]
  }
}
```

## Configuration knobs

All env vars are documented in `plugin.yaml`. The most important:

| Env var                   | Default                    | Meaning                              |
|---------------------------|----------------------------|--------------------------------------|
| `PHOTON_PROJECT_ID`       | from .env (set by setup)   | Photon 2.0 project id (`pho_prj_...`)|
| `PHOTON_API_KEY`          | from .env (set by setup)   | Project API key (`pho_sk_...`)       |
| `PHOTON_SIDECAR_PORT`     | 8789                       | Loopback port for the sidecar        |
| `PHOTON_SIDECAR_AUTOSTART`| true                       | Spawn the sidecar on connect         |
| `PHOTON_API_HOST`         | https://api.photon.codes   | Photon API host                      |
| `PHOTON_HOME_CHANNEL`     | your number (set by setup) | Default space for cron delivery — a space id, or a bare E.164 number (resolved to a DM) |
| `PHOTON_ALLOWED_USERS`    | your number (set by setup) | Comma-separated E.164 allowlist      |
| `PHOTON_REQUIRE_MENTION`  | false                      | Gate group chats on a wake word      |
| `PHOTON_MAX_INLINE_ATTACHMENT_BYTES` | 20 MB           | Max inbound attachment size the sidecar reads & inlines |
| `PHOTON_TELEMETRY`        | false                      | Spectrum SDK telemetry — toggle with `hermes photon telemetry on\|off` (restart the gateway to apply) |
| `PHOTON_MARKDOWN`         | true                       | Send agent replies as markdown (iMessage renders natively). `false` strips formatting to plain text |
| `PHOTON_REACTIONS`        | false                      | Tapback 👀/👍/👎 as processing status; tapbacks on bot messages reach the agent as `reaction:added:<emoji>` |

## Attachments & limitations

- **Inbound attachments and voice notes are downloaded.** The sidecar reads
  the bytes (`content.read()`) and base64-inlines them on the NDJSON event; the
  adapter caches them to the shared media cache and populates `media_urls` /
  `media_types`, so the agent sees the real image/file or can transcribe the
  voice note — parity with the BlueBubbles iMessage channel. Mixed iMessage
  bubbles that contain both text and attachments are normalized as a grouped
  payload so the user's typed text is preserved alongside the cached media.
  Media larger than `PHOTON_MAX_INLINE_ATTACHMENT_BYTES` (default 20 MB), or
  any byte read that fails, falls back to a text marker (`[Photon attachment
  received: …]` or `[Photon voice received: …]`) so the agent still knows
  something arrived. If Spectrum emits a `richlink` content object, Hermes
  preserves its URL plus any title/summary metadata Spectrum already exposed;
  current Spectrum versions may still deliver ordinary inbound links as plain
  `text`. iMessage may also emit rich-link preview artwork as
  `.pluginPayloadAttachment` images immediately after the URL; Hermes coalesces
  those artifacts so the agent receives one link message instead of a follow-up
  `(attachment)` prompt.
- **Outbound attachments are supported.** Images, voice notes, video, and
  documents are sent via `space.send(attachment(...))` /
  `space.send(voice(...))` through the sidecar's `/send-attachment`
  endpoint; a caption is delivered as a separate text bubble after the media.
- **Markdown is rendered.** Replies go out via spectrum-ts' `markdown()`
  builder; iMessage renders bold/italics/lists/code natively and other
  Spectrum platforms degrade to readable plain text. URL-only replies go out
  via spectrum-ts' `richlink()` builder so iMessage can render a native link
  preview card. `PHOTON_MARKDOWN=false` reverts to stripped plain text and
  disables rich-link routing.
- **Reactions (tapbacks) are supported** behind `PHOTON_REACTIONS` (default
  off): the adapter tapbacks 👀 while processing and swaps it for 👍/👎 on
  completion, and a user tapback on a bot-sent message is routed to the agent
  as a synthetic `reaction:added:<emoji>` event. Removal after a sidecar
  restart is best-effort — the live reaction handle is lost, so a stale
  tapback heals when the next reaction replaces it. Group spaces stay
  reachable across restarts via spectrum-ts' `space.get` rehydration.
- **Read receipts are supported.** The sidecar marks an inbound iMessage read
  after forwarding it to Hermes, so the sender sees `Read` without waiting for
  a model/tool turn. Inbound receipts for Hermes-sent messages are consumed as
  presence telemetry and never create an agent turn. Set
  `PHOTON_READ_RECEIPTS=false` to keep messages at `Delivered`.
- **Native polls are supported.** Hermes posts poll content through
  `spectrum-ts`' `poll(...)` builder via the sidecar's `/send-poll` endpoint.
- **Message effects are supported.** Text can be sent with native iMessage
  bubble/screen effects through `spectrum-ts`' iMessage `effect(...)` builder
  via the sidecar's `/send-effect` endpoint.
- **Cron/standalone sends require a running gateway.** Processes outside
  the gateway (cron subprocesses, `hermes send`) cannot spawn the sidecar;
  they authenticate to the gateway's live sidecar via the runtime record at
  `<hermes-home>/photon-sidecar.json` (written after the sidecar's
  `/healthz` readiness check, `0600`, removed on stop/failed start). Also
  note that shared/free-tier Photon lines cannot INITIATE conversations
  with numbers that never texted the line — that's Photon-side policy, not
  a Hermes limitation.

## Upgrading spectrum-ts

`spectrum-ts` is pinned to an **exact version** in `sidecar/package.json`
(no `^` range) and installed with `npm ci`, because the SDK ships breaking
majors (v2 removed `defineFusorPlatform`; v3 reworked space construction; v5
split it into `@spectrum-ts/*` packages, with `spectrum-ts` as the umbrella
that re-exports them; v8 made `richlink` primarily outbound, so many inbound
links now arrive as plain `text`). A floating range or `npm install spectrum-ts@latest`
would let a breaking release take down fresh setups silently. Upgrades are
deliberate:

1. Read the [SDK release notes](https://github.com/photon-hq/spectrum-ts/releases)
   for every version between the current pin and the target.
2. Bump the exact pin in `sidecar/package.json`, then run `npm install`
   inside `sidecar/` to regenerate `package-lock.json`. Commit both.
3. Migrate `sidecar/index.mjs` against the new typings. `spectrum-ts` re-exports
   `@spectrum-ts/core` (the framework: `Spectrum`, content builders,
   `Space`/`Message`) and `@spectrum-ts/imessage` (the provider), so the source
   of truth is `sidecar/node_modules/@spectrum-ts/{core,imessage}/dist/*.d.ts`
   (the hosted docs can lag).
4. Re-validate `sidecar/patch-spectrum-mixed-attachments.mjs`. It rewrites the
   compiled iMessage inbound mappers in `@spectrum-ts/imessage/dist/index.js`
   so a bubble with both text and attachments keeps its typed text; the anchors
   are tied to that build's output. `npm install` runs it via `postinstall` and
   fails loudly if the anchors no longer match — update them to the new output
   (`test_spectrum_patch.py` covers the patch).
5. Run `pytest tests/plugins/platforms/photon/`.
6. Verify end-to-end: `hermes photon status`, a DM and a group roundtrip,
   and an agent reply into a group right after a gateway restart (exercises
   `space.get` rehydration).

[photon]: https://photon.codes/
[beta]: https://app.beta.photon.codes/
