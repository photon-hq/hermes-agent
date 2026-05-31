// Hermes Agent — Photon Spectrum sidecar
//
// Spawned by `plugins/platforms/photon/adapter.py` to bridge Hermes <-> Photon
// Spectrum (iMessage) through the `spectrum-ts` SDK's managed gRPC gateway.
// This sidecar is the SOLE transport for the channel — there are no webhooks:
//
//   - Inbound messages arrive on the SDK's `app.messages` async stream and are
//     emitted to stdout as newline-delimited JSON (NDJSON) events.
//   - Outbound commands arrive on stdin as NDJSON and are dispatched through
//     the SDK (`space.send`, typing).
//
// Protocol (NDJSON — one JSON object per line, UTF-8):
//   stdout events (sidecar -> Hermes):
//     {"type":"ready","protocol":1}
//     {"type":"message","id","spaceId","sender","platform","content":{...},"timestamp"}
//     {"type":"sent","cid","ok":true,"messageId"}
//     {"type":"error","cid"|null,"ok":false,"error","code"}
//     {"type":"fatal","error","code"}            (emitted before a startup exit)
//   stdin commands (Hermes -> sidecar):
//     {"type":"send","cid","spaceId","text","replyTo"|null}
//     {"type":"typing","cid","spaceId"}
//     {"type":"shutdown"}
//
// Discipline: stdout carries ONLY NDJSON events; every diagnostic goes to
// stderr (`console.error`). Closing stdin (EOF) triggers graceful shutdown, so
// the sidecar dies with its parent without any explicit signal.
//
// Env vars (required): PHOTON_PROJECT_ID, PHOTON_PROJECT_SECRET
// Optional:            PHOTON_API_HOST (passed through if spectrum-ts honours it)

import readline from "node:readline";

const PROTOCOL_VERSION = 1;

const projectId = process.env.PHOTON_PROJECT_ID;
const projectSecret = process.env.PHOTON_PROJECT_SECRET;

function emit(obj) {
  // The single sink for stdout — only ever a structured event line.
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function log(...args) {
  console.error("photon-sidecar:", ...args);
}

if (!projectId || !projectSecret) {
  log("PHOTON_PROJECT_ID and PHOTON_PROJECT_SECRET must both be set.");
  emit({ type: "fatal", error: "missing project credentials", code: "MISSING_CREDENTIALS" });
  process.exit(2);
}

// Lazy-load spectrum-ts so a missing install fails with a clear, structured
// signal instead of a cryptic module-resolution error during import.
let Spectrum, imessage, spectrumText;
try {
  ({ Spectrum, text: spectrumText } = await import("spectrum-ts"));
  ({ imessage } = await import("spectrum-ts/providers/imessage"));
} catch (e) {
  log(
    "spectrum-ts is not installed. Run `npm install` inside " +
      "plugins/platforms/photon/sidecar/. Original error: " +
      (e && e.stack ? e.stack : String(e))
  );
  emit({ type: "fatal", error: "spectrum-ts not installed", code: "SPECTRUM_TS_MISSING" });
  process.exit(3);
}

let app;
try {
  app = await Spectrum({
    projectId,
    projectSecret,
    providers: [imessage.config()],
  });
} catch (e) {
  log("Spectrum() initialization failed: " + (e && e.stack ? e.stack : String(e)));
  emit({ type: "fatal", error: String((e && e.message) || e), code: "SPECTRUM_INIT_FAILED" });
  process.exit(4);
}

// ---------------------------------------------------------------------------
// Space resolution / caching

const cachedSpaces = new Map();

function cacheSpace(space) {
  if (space && typeof space.id === "string" && typeof space.send === "function") {
    cachedSpaces.set(space.id, space);
  }
}

function dmAddressFromSpaceId(spaceId) {
  if (typeof spaceId !== "string") return null;
  // Spectrum carries canonical space ids such as `any;-;+<phone>` for DMs.
  // The iMessage helper resolves uncached DMs by recipient address.
  if (spaceId.startsWith("any;-;")) return spaceId.slice("any;-;".length);
  if (spaceId.startsWith("+")) return spaceId;
  return null;
}

async function resolveSpace(spaceId) {
  const cached = cachedSpaces.get(spaceId);
  if (cached) return cached;

  const im = imessage(app);
  if (typeof im.space === "function") {
    const dmAddress = dmAddressFromSpaceId(spaceId);
    if (dmAddress) {
      const space = await im.space(dmAddress);
      cacheSpace(space);
      return space;
    }
  }
  throw new Error(`unable to resolve space id ${spaceId}`);
}

// ---------------------------------------------------------------------------
// Self-echo suppression
//
// spectrum-ts may re-deliver our own outbound on `app.messages` (iMessage
// shows sent messages in the conversation). Track the ids we just sent and
// drop them from the inbound stream so the agent never replies to itself.

const recentlySent = new Map(); // messageId -> epoch ms
const SENT_TTL_MS = 5 * 60 * 1000;

function rememberSent(id) {
  if (id) recentlySent.set(id, Date.now());
}

function wasSelfSent(id) {
  if (!id) return false;
  const cutoff = Date.now() - SENT_TTL_MS;
  for (const [key, ts] of recentlySent) {
    if (ts < cutoff) recentlySent.delete(key);
  }
  return recentlySent.has(id);
}

// ---------------------------------------------------------------------------
// Inbound normalization

function normalizeContent(content) {
  if (!content || typeof content !== "object") {
    return { type: "unknown" };
  }
  if (content.type === "text") {
    return { type: "text", text: typeof content.text === "string" ? content.text : "" };
  }
  if (content.type === "attachment") {
    return {
      type: "attachment",
      name: content.name ?? null,
      mimeType: content.mimeType ?? null,
      size: typeof content.size === "number" ? content.size : null,
      attachmentId: content.id ?? content.attachmentId ?? null,
    };
  }
  return { type: content.type || "unknown" };
}

function messageToEvent(space, message) {
  let timestamp = null;
  const ts = message && message.timestamp;
  if (ts instanceof Date) timestamp = ts.toISOString();
  else if (typeof ts === "string") timestamp = ts;
  else if (typeof ts === "number") timestamp = new Date(ts).toISOString();

  return {
    type: "message",
    id: (message && message.id) ?? null,
    spaceId: (space && space.id) ?? (message && message.space && message.space.id) ?? null,
    sender: (message && message.sender && message.sender.id) ?? null,
    platform: (message && message.platform) ?? "imessage",
    content: normalizeContent(message && message.content),
    timestamp,
    fromMe: Boolean(
      (message && (message.fromMe ?? message.isFromMe ?? message.outgoing)) || false
    ),
  };
}

// Consume the inbound gRPC stream. Each event is emitted to stdout; the Python
// adapter normalizes it into a MessageEvent and dispatches it to the gateway.
(async () => {
  try {
    for await (const [space, message] of app.messages) {
      cacheSpace(space);
      try {
        const event = messageToEvent(space, message);
        if (event.fromMe || wasSelfSent(event.id)) {
          continue; // our own outbound echoed back — never re-dispatch it
        }
        emit(event);
      } catch (e) {
        log("failed to normalize inbound message: " + (e && e.stack ? e.stack : String(e)));
      }
    }
    log("inbound stream ended");
  } catch (e) {
    log("inbound stream errored: " + (e && e.stack ? e.stack : String(e)));
    emit({
      type: "error",
      cid: null,
      ok: false,
      error: String((e && e.message) || e),
      code: "STREAM_ERROR",
    });
  }
})();

// Signal readiness once Spectrum is initialized and we are listening.
emit({ type: "ready", protocol: PROTOCOL_VERSION });
log("ready — streaming spectrum-ts inbound, awaiting stdin commands");

// ---------------------------------------------------------------------------
// Outbound command handling

async function handleSend(cmd) {
  const cid = cmd.cid ?? null;
  const { spaceId, text } = cmd;
  if (!spaceId || typeof text !== "string") {
    emit({ type: "error", cid, ok: false, error: "spaceId and text are required", code: "BAD_COMMAND" });
    return;
  }
  if (cmd.replyTo) {
    log("replyTo ignored for outbound text; sending plain message");
  }
  try {
    const space = await resolveSpace(spaceId);
    const result = await space.send(spectrumText(text));
    const messageId = (result && (result.id || result.messageId)) || null;
    rememberSent(messageId);
    emit({ type: "sent", cid, ok: true, messageId });
  } catch (e) {
    log("send failed: " + (e && e.stack ? e.stack : String(e)));
    emit({ type: "error", cid, ok: false, error: String((e && e.message) || e), code: "SEND_FAILED" });
  }
}

async function handleTyping(cmd) {
  const cid = cmd.cid ?? null;
  const { spaceId } = cmd;
  if (!spaceId) {
    emit({ type: "error", cid, ok: false, error: "spaceId is required", code: "BAD_COMMAND" });
    return;
  }
  try {
    const space = await resolveSpace(spaceId);
    if (typeof space.startTyping === "function") await space.startTyping();
    else if (typeof space.typing === "function") await space.typing();
    else if (typeof space.setTyping === "function") await space.setTyping(true);
    emit({ type: "sent", cid, ok: true, messageId: null });
  } catch (e) {
    log("typing failed: " + (e && e.stack ? e.stack : String(e)));
    emit({ type: "error", cid, ok: false, error: String((e && e.message) || e), code: "TYPING_FAILED" });
  }
}

const rl = readline.createInterface({ input: process.stdin });

rl.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let cmd;
  try {
    cmd = JSON.parse(trimmed);
  } catch (e) {
    log("ignoring non-JSON stdin line");
    return;
  }
  switch (cmd && cmd.type) {
    case "send":
      handleSend(cmd);
      break;
    case "typing":
      handleTyping(cmd);
      break;
    case "shutdown":
      shutdown("shutdown-command");
      break;
    default:
      emit({
        type: "error",
        cid: (cmd && cmd.cid) ?? null,
        ok: false,
        error: `unknown command type ${cmd && cmd.type}`,
        code: "UNKNOWN_COMMAND",
      });
  }
});

rl.on("close", () => shutdown("stdin-eof"));

// ---------------------------------------------------------------------------
// Shutdown

let shuttingDown = false;

async function shutdown(reason) {
  if (shuttingDown) return;
  shuttingDown = true;
  log(`received ${reason}, stopping...`);
  try {
    await Promise.race([
      app.stop(),
      new Promise((resolve) => setTimeout(resolve, 3000)),
    ]);
  } catch (e) {
    log("app.stop() failed: " + String(e));
  }
  process.exit(0);
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
