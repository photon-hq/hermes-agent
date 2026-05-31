# Photon sidecar

Small Node helper that bridges Hermes Agent to Photon's Spectrum SDK
(`spectrum-ts`). Hermes is Python and `spectrum-ts` is TypeScript, so this
sidecar holds Photon's managed **gRPC** connection on Hermes' behalf. It is
the *only* transport for the channel — there is no webhook and no tunnel.

The sidecar:

- runs `Spectrum({ projectId, projectSecret, providers: [imessage.config()] })`
- streams inbound messages from the SDK's `app.messages` gRPC stream and emits
  one NDJSON event per message on **stdout**
- reads NDJSON commands (`send` / `typing` / `shutdown`) from **stdin** and
  dispatches them through the SDK
- logs only to **stderr**; stdout carries structured events exclusively
- suppresses its own outbound messages echoed back by the SDK so the agent
  never replies to itself

## Protocol (NDJSON over stdio)

stdout events (sidecar → Hermes):

```
{"type":"ready","protocol":1}
{"type":"message","id","spaceId","sender","platform","content":{...},"timestamp"}
{"type":"sent","cid","ok":true,"messageId"}
{"type":"error","cid"|null,"ok":false,"error","code"}
{"type":"fatal","error","code"}
```

stdin commands (Hermes → sidecar):

```
{"type":"send","cid","spaceId","text","replyTo"|null}
{"type":"typing","cid","spaceId"}
{"type":"shutdown"}
```

Closing stdin (EOF) triggers a graceful shutdown, so the sidecar dies with its
parent without any explicit signal.

## Install

```bash
cd plugins/platforms/photon/sidecar
npm install
```

The Hermes plugin's `hermes photon quick-setup` command runs `npm install`
here automatically when sidecar dependencies are missing.

## Run standalone

For debugging — type one NDJSON command per line on stdin:

```bash
PHOTON_PROJECT_ID=... PHOTON_PROJECT_SECRET=... node index.mjs
```

In normal use, the Python adapter supervises this process — start, restart on
crash, kill on shutdown — and never asks the user to run it by hand.

## Why a sidecar at all?

`spectrum-ts` is a TypeScript SDK and there is no Python equivalent. The
sidecar lets Hermes (Python) use Photon's managed gRPC gateway by speaking a
tiny line-delimited JSON protocol to a supervised Node process. The Python
side never imports any Node dependency.
