---
sidebar_position: 18
---

# Photon iMessage

Connect Hermes to **iMessage** through [Photon][photon] 2.0, a managed
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

Photon is a **persistent-connection** channel, like Discord or Slack —
**no webhook, no public URL, no signing secret to manage.** Everything runs
inside the gateway's Python process:

- **Inbound** — the adapter holds a Socket.IO connection to the project's
  event stream on `api.photon.codes` and handles each new message in order.
  It saves its place in the stream after every message, so a dropped
  connection or a gateway restart picks up exactly where it stopped
  instead of losing messages.
- **Outbound** — replies, images, typing indicators and read receipts are
  calls to Photon's REST API.

The Python packages it needs (the `photon` extra) install the first time
the adapter starts; the Docker image ships with them.

## Prerequisites

- A Photon 2.0 account — sign up at [app.beta.photon.codes][app]
- A phone number that can receive iMessage (used to bind your account)

That's it — there is no public URL or tunnel to set up.

## First-time setup

Either run the unified gateway wizard and pick **Photon iMessage**:

```bash
hermes gateway setup
```

…or run the Photon setup directly (the wizard calls the same flow):

```bash
hermes photon setup --phone +15551234567
```

The setup, in order:

1. **Project and API key.** If `PHOTON_PROJECT_ID` and `PHOTON_API_KEY` are already set and
   Photon accepts the key, setup uses them and never opens a browser. Otherwise it signs you
   in with Photon's device login (open `https://auth.photon.codes/device` on any device and
   enter the code), finds or creates the `Hermes Agent` project, and mints a project API key.
2. **Assigns a shared iMessage line** to your phone number (re-running reuses it) and prints
   the number you text to reach your agent.
3. **Waits for your first text**: a shared line can only message you after you have messaged
   it once. `hermes photon status` shows when routing is live.
4. **Enables the platform** and allowlists your number.

Runtime credentials are written to `~/.hermes/.env` (`PHOTON_PROJECT_ID`,
`PHOTON_API_KEY`), the same place every other channel keeps its token. Management metadata
(the device-login session, project and key ids) lives in `~/.hermes/auth.json` under
`credential_pool.photon` / `credential_pool.photon_project`.

**Without a browser:** create a project API key in the [Photon dashboard][app] and set
`PHOTON_PROJECT_ID=pho_prj_...` and `PHOTON_API_KEY=pho_sk_...` in `~/.hermes/.env` (or
the environment, e.g. in Docker). Setup and the gateway use them as they are.

:::note Coming from Photon Spectrum
Hermes now runs on Photon 2.0, and a Hermes still set up for Photon Spectrum
(a `PHOTON_PROJECT_SECRET` in `.env`) no longer connects. Create a Photon 2.0
account at [app.beta.photon.codes][app] and run `hermes photon setup`. It keeps
your Spectrum credentials in `auth.json` so your Spectrum projects can move over
once Photon's migration tool ships. Conversations have new ids on Photon 2.0, so
existing iMessage sessions start fresh, and setup points a `PHOTON_HOME_CHANNEL`
that held a Spectrum chat id at your number.
:::

## Authorizing users

Photon uses the same authorization model as every other Hermes
channel. Choose one approach:

**DM pairing (default).** When an unknown number messages your Photon
line, Hermes replies with a pairing code. Approve it with:

```bash
hermes pairing approve photon <CODE>
```

Use `hermes pairing list` to see pending codes and approved users.

**Pre-authorize specific numbers** (in `~/.hermes/.env`):

```bash
PHOTON_ALLOWED_USERS=+15551234567,+15559876543
```

**Open access** (dev only, in `~/.hermes/.env`):

```bash
PHOTON_ALLOW_ALL_USERS=true
```

When `PHOTON_ALLOWED_USERS` is set, unknown senders are silently
ignored rather than offered a pairing code (the allowlist signals you
deliberately restricted access).

### Require mentions in group chats

By default Hermes responds to every authorized DM and group message.
To make group chats opt-in, enable mention gating (DMs still always
work):

```yaml
gateway:
  platforms:
    photon:
      enabled: true
      require_mention: true
```

With `require_mention: true`, group-chat messages are ignored unless
they match a wake-word pattern. The defaults match `Hermes` and
`@Hermes agent` variants. For a custom agent name, set regex patterns:

```yaml
gateway:
  platforms:
    photon:
      require_mention: true
      mention_patterns:
        - '(?<![\w@])@?amos\b[,:\-]?'
```

Both keys also accept env vars (`PHOTON_REQUIRE_MENTION`,
`PHOTON_MENTION_PATTERNS`). This is the same mention-gating model the
BlueBubbles iMessage channel uses.

## Start the gateway

```bash
hermes gateway start
```

You'll see something like:

```
[photon] connected — streaming project pho_prj_... events after cursor 0
```

Send an iMessage to your assigned number and Hermes will reply.

## Status & troubleshooting

```bash
hermes photon status
```

Prints whether you're signed in to Photon (only setup needs it), the
project id, whether an API key is stored, your registered number, the
assigned iMessage line, and — checked live against Photon — whether
routing works yet, which needs one text from your number to the line.

Common issues:

- **`routing : ✗ not live`** — text your assigned line once from your
  registered number, then re-check.
- **The gateway reports `AUTH_REJECTED`** — Photon no longer accepts the
  project API key (revoked, or the project was deleted). Re-run
  `hermes photon setup`; it replaces the key.
- **The gateway reports `SPECTRUM_LEGACY`** — this Hermes is still set up
  for Photon Spectrum; see the note under [First-time setup](#first-time-setup).
- **The gateway reports `MISSING_DEP`** — the `photon` extra could not be
  installed (for example, no network). Run `hermes pm install --extra photon`.

## What works today

Photon 2.0 sends **text and images** over iMessage. Concretely:

- **Text** goes out as markdown, which iMessage renders. If Photon refuses a
  reply's markdown, Hermes resends it as plain text. `PHOTON_MARKDOWN=false`
  always sends plain text.
- **Inbound attachments are downloaded.** Images, files and voice notes reach
  the agent as real media (up to 64 MB each); anything larger arrives as a
  short text marker so the agent still knows something came in.
- **Outbound images** are sent natively, with an optional caption. Other
  files, video and voice notes can't be sent on Photon 2.0 yet.
- **Threaded replies** to one of Hermes's messages reach the agent with the
  quoted text.
- **Typing indicator and read receipts.** Set `PHOTON_READ_RECEIPTS=false` to
  keep messages at `Delivered`.
- **Not yet on Photon 2.0:** tapbacks, polls and message effects.
- **A shared line works in direct conversations only**, and only with people
  who have texted it since their line was assigned. Group chats get no
  replies on a shared line, and a cron or standalone send to someone who has
  never texted the line fails with that reason.
- **Cron and standalone sends don't need the gateway running.** They call
  Photon directly with the same credentials. The target is a phone number of
  someone who has texted the line, or a Photon space id (`pho_spc_...`).

## Env vars

| Variable                  | Default            | Notes                                      |
|---------------------------|--------------------|--------------------------------------------|
| `PHOTON_PROJECT_ID`       | from `.env`        | Photon 2.0 project id (`pho_prj_...`); set by setup |
| `PHOTON_API_KEY`          | from `.env`        | Project API key (`pho_sk_...`); set by setup |
| `PHOTON_HOME_CHANNEL`     | your number        | Default target for cron / notifications: a phone number that has texted the line, or a space id |
| `PHOTON_HOME_CHANNEL_NAME`| (unset)            | Human label for the home channel           |
| `PHOTON_ALLOWED_USERS`    | your number        | Comma-separated E.164 allowlist            |
| `PHOTON_ALLOW_ALL_USERS`  | `false`            | Dev only — accept any sender               |
| `PHOTON_READ_RECEIPTS`    | `true`             | Mark inbound messages read once Hermes has them |
| `PHOTON_MARKDOWN`         | `true`             | Send replies as markdown; `false` sends plain text |
| `PHOTON_REQUIRE_MENTION`  | `false`            | Require a wake word before responding in groups |
| `PHOTON_MENTION_PATTERNS` | Hermes wake words  | JSON list / comma / newline regex patterns for group mentions |
| `PHOTON_API_HOST`         | `api.photon.codes` | Override the Photon API host               |

[photon]: https://photon.codes/
[app]: https://app.beta.photon.codes/
