---
sidebar_position: 12
title: "iMessage"
description: "Set up Hermes Agent as an iMessage bot — local macOS mode or remote via HTTP proxy"
---

# iMessage Setup

Hermes connects to iMessage through two modes:

- **Local mode** (macOS only): Reads the native iMessage SQLite database (`~/Library/Messages/chat.db`) for inbound messages and sends via AppleScript. Pure Python, no external dependencies.
- **Remote mode** (any OS): Connects to an [advanced-imessage-http-proxy](https://github.com/photon-hq/advanced-imessage-http-proxy) server via HTTP polling. Supports sending files/images/audio, tapback reactions, typing indicators, message effects, message queries, chat history, attachment download, and iMessage availability checks.

:::info No Extra Python Dependencies
Both modes use only stdlib and `httpx` (already a core Hermes dependency). No additional packages are required.
:::

---

## Local Mode (macOS)

The simplest setup — Hermes runs directly on your Mac and talks to Messages.app.

### Prerequisites

- **macOS** with Messages.app signed in to iMessage
- **Full Disk Access** granted to your terminal application

### Step 1: Grant Full Disk Access

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Add your terminal application (Terminal, iTerm2, Warp, etc.)
3. Restart the terminal

### Step 2: Configure

Add to `~/.hermes/.env`:

```bash
IMESSAGE_ENABLED=true
IMESSAGE_ALLOWED_USERS=+15551234567,user@icloud.com
```

Or use `hermes gateway setup` and select **iMessage**.

### Step 3: Start

```bash
hermes gateway run
```

Hermes polls `chat.db` every 2 seconds for new messages and responds via AppleScript.

---

## Remote Mode

Run Hermes on any machine — iMessage is managed by [Photon](https://photon.codes).

### Configure

You only need your **Photon endpoint** and **API key** (provided by photon.codes). Add to `~/.hermes/.env`:

```bash
IMESSAGE_SERVER_URL=https://abc123.imsgd.photon.codes
IMESSAGE_API_KEY=your-photon-api-key
IMESSAGE_ALLOWED_USERS=+15551234567,user@icloud.com
```

Or run `hermes gateway setup` and select **iMessage**.

### Start

```bash
hermes gateway run
```

Hermes polls for new messages (default: every 2 seconds) and sends replies automatically.

---

## Access Control

DM access follows the standard Hermes pattern:

1. **`IMESSAGE_ALLOWED_USERS` set** — only those users can message
2. **No allowlist set** — unknown users get a DM pairing code
3. **`IMESSAGE_ALLOW_ALL_USERS=true`** — anyone can message

### Group Chat Policy

| Value | Behavior |
|-------|----------|
| `open` (default) | Process all group messages |
| `ignore` | Ignore groups, only respond to DMs |

```yaml
platforms:
  imessage:
    extra:
      group_policy: "ignore"
```

---

## Remote Mode Features

These features are available when using the [advanced-imessage-http-proxy](https://github.com/photon-hq/advanced-imessage-http-proxy):

### Messaging

| Feature | Description |
|---------|-------------|
| Send messages | Text with automatic multi-paragraph splitting |
| Reply to messages | Inline replies to specific messages |
| Message effects | Full-screen animations: `confetti`, `fireworks`, `balloon`, `heart`, `sparkles`, `echo`, `spotlight` |
| Message search | Full-text search across message history |
| Get message details | Retrieve a single message by ID |
| Chat history | List messages in a specific conversation |

### Reactions & Typing

| Feature | Description |
|---------|-------------|
| Tapback reactions | `love`, `like`, `dislike`, `laugh`, `emphasize`, `question` — add and remove |
| Typing indicators | Shown while generating a response |
| Mark read / unread | Manage read state on conversations |

Hermes reacts to incoming messages with a configurable tapback, then swaps it for a "done" tapback after replying:

```yaml
platforms:
  imessage:
    extra:
      react_tapback: "love"     # on receive (empty string to disable)
      done_tapback: ""          # after reply (empty string to disable)
```

### Attachments

| Feature | Description |
|---------|-------------|
| Send files | Images, documents, audio via proxy or AppleScript |
| Send audio messages | Audio files sent as voice messages (`audio=true`) |
| Download attachments | Retrieve received files and media |
| Attachment info | File name, size, MIME type metadata |

> **Note:** File uploads may timeout on some Kit servers behind the centralized proxy (Cloudflare 60s limit). If this happens, the adapter falls back to sending the URL or caption as text.

### Utilities

| Feature | Description |
|---------|-------------|
| Check iMessage availability | Verify if an address uses iMessage |
| Server info / health | OS version, server version, connectivity status |

---

## Full Configuration

```yaml
platforms:
  imessage:
    enabled: true
    extra:
      local: true                                    # false for remote mode
      server_url: ""                                 # Photon endpoint (remote only)
      poll_interval: 2.0                             # seconds between polls
      database_path: "~/Library/Messages/chat.db"    # local mode DB path
      group_policy: "open"                           # "open" or "ignore"
      react_tapback: "love"                          # tapback on inbound (remote)
      done_tapback: ""                               # tapback after reply (remote)
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **"iMessage database not found"** | Grant Full Disk Access to your terminal in System Settings |
| **"iMessage local mode requires macOS"** | Local mode only works on macOS — use remote mode on other OS |
| **Messages not received** | Check `IMESSAGE_ALLOWED_USERS` includes the sender |
| **AppleScript send failures** | Ensure Messages.app is signed in and recipient is a valid iMessage contact |
| **Remote health check fails** | Verify proxy URL: `curl https://your-proxy/health` |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `IMESSAGE_SERVER_URL` | Yes (remote) | Photon endpoint from photon.codes |
| `IMESSAGE_API_KEY` | Yes (remote) | Photon API key from photon.codes |
| `IMESSAGE_ENABLED` | Yes (local) | Set to `true` for local macOS mode (no Photon account needed) |
| `IMESSAGE_ALLOWED_USERS` | Recommended | Comma-separated phone numbers or Apple ID emails |
| `IMESSAGE_ALLOW_ALL_USERS` | No | Allow any user to interact |
| `IMESSAGE_HOME_CHANNEL` | No | Default delivery target for cron jobs |
