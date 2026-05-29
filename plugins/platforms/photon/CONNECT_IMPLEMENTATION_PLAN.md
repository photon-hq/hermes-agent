# Photon Quick Setup Implementation Plan

This document consolidates the recent Photon setup work into a simpler,
durable implementation plan for a single reconciliation command:

```bash
hermes photon quick-setup --phone '+15551234567'
```

The goal is to preserve the current happy-path command name while changing its
semantics. `quick-setup` should not be a step runner that prints follow-up
commands. It should reconcile Photon for this machine and prove runtime
readiness before returning control to the user.

## Research Inputs

This plan is based on:

- Current Photon implementation in `plugins/platforms/photon/`.
- Runtime findings in `plugins/platforms/photon/DEBUGGING.md`.
- Current user-facing docs in `plugins/platforms/photon/README.md` and
  `website/docs/user-guide/messaging/photon.md`.
- The last 20 commits authored by `raysun12142006@gmail.com`.

## Recent Commit Lessons

The last 20 authored commits show the setup surface has been evolving around
the same themes: idempotency, credential validation, managed tunnel lifecycle,
webhook ownership, active Hermes home ownership, and better diagnostics.

| Commit | Date | Subject | Design lesson |
| --- | --- | --- | --- |
| `859213085` | 2026-05-29 | fix(photon): clarify stale webhook status | Split owned stale webhooks from unowned stale webhooks; public health should dominate cleanup noise. |
| `84c0ffad8` | 2026-05-28 | fix(photon): guard interactive setup reconfigure | Avoid re-running setup when the profile already has enough valid Photon state. |
| `6e010a58a` | 2026-05-28 | chore(photon): remove adapter runtime test file | Keep runtime behavior covered by focused unit tests, not brittle integration scaffolding. |
| `e39415a9f` | 2026-05-28 | chore: keep Photon PR scoped to Photon | Avoid broad gateway/setup refactors inside a Photon-specific patch unless they are required. |
| `e112217af` | 2026-05-28 | fix(photon): gate startup to active Hermes home | A Photon project/tunnel must be claimed by one active Hermes home to avoid split-brain delivery. |
| `27fc10160` | 2026-05-28 | fix(photon): protect managed webhook ownership | Only delete managed webhooks that this Hermes profile recorded as owned. |
| `e9a5d054a` | 2026-05-28 | Improve Photon setup onboarding | Setup status must account for webhook secret, allowed phones, tunnel state, and sidecar readiness. |
| `e72d28a1b` | 2026-05-28 | Add Photon webhook diagnostics | Health checks must cover both local and public webhook paths. |
| `5265b54f1` | 2026-05-28 | Remove Photon CLI test coverage | Removed tests created a gap later restored by focused CLI/status tests. |
| `73561f8c2` | 2026-05-28 | Improve Photon setup docs and tunnel install | Managed `cloudflared` should be installed profile-locally with checksum verification. |
| `1e46148bc` | 2026-05-28 | Improve Photon setup auth and gateway access | Dashboard token, Spectrum credentials, and sender allowlist are separate states. |
| `81282c962` | 2026-05-28 | Debug Photon dashboard auth token validation | Stored dashboard tokens must be validated against project APIs, not merely session lookup. |
| `f81a1d872` | 2026-05-28 | removed legacy generated auth tokon in auth.json | Photon secrets belong in Hermes env, not legacy generated auth files. |
| `6aa735460` | 2026-05-27 | Require Photon login before quick setup | Avoid creating remote resources from an ambiguous auth state. |
| `58273b081` | 2026-05-27 | Clarify incomplete Photon setup guidance | Partial setup guidance needs exact next actions, not generic docs pointers. |
| `9b7d76160` | 2026-05-27 | Add Photon quick setup tunnel flow | The all-in-one flow reduced friction but still left gateway startup outside setup. |
| `19d46b2f4` | 2026-05-27 | Store Photon project credentials in Hermes env | Runtime credentials must be available to gateway processes through the canonical Hermes env path. |
| `641a79d4a` | 2026-05-27 | Make Photon setup idempotent | Reuse/adopt/create is the correct shape, but reuse must mean verified reuse. |
| `358eaf31e` | 2026-05-27 | Merge pull request #1 from photon-hq/ray/photon-e2e-setup-fixes | Sidecar package lock and e2e setup fixes made local setup reproducible. |
| `8128eb1bf` | 2026-05-26 | Remove Photon setup QA notes | Ad hoc QA notes should be promoted into maintained debugging and implementation docs. |

## Problem Statement

The current setup UX is split across too many commands:

```bash
hermes photon login
hermes photon quick-setup --phone ...
hermes photon status
hermes gateway run -v
hermes gateway restart
hermes photon webhook tunnel start
```

That shape creates two systemic problems.

First, it treats stored state as valid state. A saved project id, project
secret, webhook URL, webhook secret, tunnel pid, or launchd plist can all be
stale or pointed at another Hermes home.

Second, it lets Photon provisioning finish before the runtime path is proven.
Photon is not usable unless the gateway for the same Hermes home is running,
serving local health, reachable through the public tunnel, and reporting the
Photon adapter as connected.

## Target Command Surface

Primary first-run path:

```bash
hermes photon quick-setup --phone '+15551234567'
```

There is no new primary command name. The implementation should make
`quick-setup` the reconciler.

`hermes photon login` should remain as an explicit auth command for users who
want to authenticate separately. `hermes photon status` and
`hermes photon reset` remain support commands, not required happy-path steps.

Advanced/debug escape hatches should remain available but not dominate first-run
docs:

```bash
hermes photon project select <id>
hermes photon webhook list
hermes photon webhook delete <id>
hermes photon tunnel logs
```

## Final State Invariants

`hermes photon quick-setup` should return success only when all of these are
true:

1. Dashboard auth is present and valid for Photon project APIs.
2. A compatible Spectrum/iMessage project exists.
3. Stored `PHOTON_PROJECT_ID` and `PHOTON_PROJECT_SECRET` validate against the
   Spectrum API.
4. The operator phone exists as a Spectrum shared iMessage user, or the command
   has just created it.
5. The operator phone is authorized in Hermes sender access.
6. Node and sidecar dependencies are installed and runnable.
7. The gateway is running from the same Hermes home as the Photon state.
8. Local webhook health works at `http://127.0.0.1:<port>/healthz`.
9. A managed or user-owned public URL forwards to the local gateway.
10. Public health works at `https://<public-host>/healthz`.
11. Photon has the current webhook URL registered.
12. The local webhook signing secret matches the registered current webhook.
13. Managed stale webhooks created by this profile are cleaned or marked for
    cleanup.
14. Unowned webhooks are never deleted automatically.
15. Gateway runtime status reports `photon=connected`.
16. The user is shown the assigned iMessage number when Photon returns it.

## Reconciler Design

The command should be implemented as a state reconciler, not a linear script.
Each step should follow this pattern:

```python
def ensure_x(ctx):
    observed = inspect_x(ctx)
    if observed.valid:
        return observed
    repair_x(ctx, observed)
    verified = inspect_x(ctx)
    if not verified.valid:
        raise FailedInvariant(
            step="x",
            summary="specific invariant that failed",
            observed=verified,
            evidence={
                "hermes_home": ctx.hermes_home,
                "env_path": ctx.env_path,
                "service_home": verified.service_home,
                "port_owner": verified.port_owner,
                "local_health": verified.local_health,
                "public_health": verified.public_health,
            },
            repair="smallest safe repair action",
        )
    return verified
```

No step should trust cached state without validation. No step should print a
"next command" as a substitute for verification.

Failures should be specific enough that the user does not get trapped in a
`status` / `gateway restart` loop. Every failed invariant should include:

- The step that failed, in user-facing language.
- What was expected.
- What was observed.
- The active Hermes home and env path.
- Any conflicting Hermes home or service definition.
- Relevant local process evidence, such as port owner and pid.
- Relevant remote evidence, such as HTTP status, registered webhook ids, or
  Photon API response class.
- Whether automatic repair was skipped because it would affect another Hermes
  home, delete unowned remote state, or rotate a secret.
- The smallest safe repair action.

For example, a gateway service mismatch should not raise a generic
`GatewayNotReady` error. It should report that the current Photon setup belongs
to one Hermes home, the installed service points at another Hermes home, and the
local webhook port is not being served by the expected gateway.

## Proposed Flow

1. Resolve the active Hermes home and env path.
2. Validate the dashboard token.
3. If the token is missing or invalid, run device login.
4. Validate stored Spectrum credentials.
5. If stored Spectrum credentials 401, clear them and continue.
6. List dashboard projects.
7. Adopt exactly one compatible `Hermes Agent` project, or create one.
8. Store fresh project credentials.
9. Validate those credentials against Spectrum immediately.
10. Create or verify the Spectrum user for `--phone`.
11. Save the phone into `PHOTON_ALLOWED_USERS` unless access is already open.
12. Install or verify sidecar dependencies.
13. Ensure a gateway service or foreground process exists for the current
    Hermes home.
14. Wait for local `/healthz`.
15. Ensure a managed tunnel exists, unless a user-owned public URL is configured.
16. Wait for public `/healthz`.
17. Register the current webhook URL if needed.
18. Save the returned signing secret.
19. Restart the current-home gateway only if runtime-loaded secrets changed.
20. Wait until gateway runtime status reports Photon connected.
21. Print the assigned Photon iMessage number and a compact health summary.

## Gateway Service Identity

The recent debugging failure was primarily a service identity mismatch:

- Photon state lived under a temp Hermes home.
- The installed launchd service was for `/Users/raysmacbookair/.hermes`.
- `hermes gateway restart` restarted the default-home service.
- The temp tunnel forwarded to `127.0.0.1:8788`, but no temp-home gateway was
  listening there.

`quick-setup` must detect this directly.

Required checks:

- Current `HERMES_HOME` as resolved by Hermes config.
- Expected launchd/systemd service name for that home.
- Installed service path, working directory, Python path, and environment.
- Running gateway pid and runtime status file for the same home.
- Local webhook port owner.

If a service for another home owns the Photon port, do not print
`hermes gateway restart`. Print the exact mismatch and either:

- offer to install/start the service for the current home, or
- ask the user to stop the other service if automatic takeover is unsafe.

For `quick-setup`, the preferred behavior is to repair only the current-home
service and never mutate another Hermes home unless an explicit `--takeover` or
`--stop-other-gateway` flag is added later.

## Cloudflared Installation And Updates

The managed tunnel code already established the right baseline:

- Prefer a user-installed `cloudflared` on `PATH`.
- Otherwise install a managed copy into the active Hermes profile under
  `<HERMES_HOME>/bin/cloudflared`.
- Resolve the latest Cloudflare release asset for the current OS/architecture.
- Require release metadata to include a SHA-256 digest.
- Verify download size when available.
- Verify SHA-256 before replacing the binary.
- Write a manifest with asset name, asset SHA-256, binary SHA-256, and version.
- Keep the previous managed binary if metadata/download/update fails.
- Never auto-update a user-owned `cloudflared` on `PATH`.
- Start quick tunnels with `--no-autoupdate` so the managed binary is the only
  update surface.

`quick-setup` should reuse this behavior through an `ensure_cloudflared()`
function.

Additional hardening to consider:

- Surface the managed binary path and version in `photon status`.
- Add `hermes photon tunnel update-cloudflared` as an advanced command only if
  manual update becomes necessary.
- Cache the last successful release metadata to avoid fragile setup during
  GitHub API outages.
- Keep release fetching out of the gateway hot path when a valid managed binary
  already exists.

## Webhook Ownership And Deletion

The current ownership model is the right safety boundary:

- Record webhooks created by this profile in `photon/tunnel.json` under
  `owned_webhooks`.
- Only auto-delete `trycloudflare.com` webhooks whose id is in that ownership
  set.
- Forget owned ids after successful deletion.
- Refuse to delete matching current URLs if the local signing secret is missing
  and the webhook is not owned by this profile.
- Show unowned stale managed webhooks in status, but treat them as manual
  cleanup unless they block the current URL.

`quick-setup` should preserve these rules.

Important failure cases:

- Local state may be lost while remote webhooks remain. Those webhooks are then
  unowned and must not be auto-deleted.
- Multiple Hermes homes may share one Photon project. One home must not clean up
  another home's current webhook.
- Photon returns the webhook signing secret only once. If the local secret is
  missing for an existing remote webhook, the safe repair is to delete/recreate
  only when ownership is known.

Recommended status language:

```text
registered webhooks : current URL registered; 1 unowned stale managed
cleanup             : optional; use `hermes photon webhook list`
```

## Active Hermes Home Ownership

The active-home guard prevents split-brain gateway startup:

- `active-home.json` records the Hermes home that owns the current Photon
  project/webhook.
- The adapter refuses to connect if the current gateway home does not match.
- Status reports whether the active owner is this home or another home.

`quick-setup` should keep this guard, but improve remediation:

- If no active owner exists, claim ownership after verified webhook registration.
- If this home already owns Photon, continue reconciliation.
- If another home owns Photon, stop with a clear message unless an explicit
  future takeover flag is provided.

## Status Redesign

`hermes photon status` should become an invariant report, not just a checklist.

It should include:

- Dashboard token validity.
- Spectrum credential validity.
- Active Hermes home ownership.
- Service identity for the current home.
- Local port owner and local `/healthz`.
- Tunnel pid, public URL, and public `/healthz`.
- Registered webhook match.
- Webhook secret presence and whether it was obtained from the current
  registration.
- Gateway runtime platform state.
- Assigned phone number if known.

For public `502`, status must check local health and service identity before
suggesting restart. A useful failure message would be:

```text
public health       : fail, Cloudflare cannot reach local gateway
local health        : fail, 127.0.0.1:8788 is not listening
gateway service     : wrong home, launchd points at /Users/.../.hermes
repair              : start/install gateway for <current HERMES_HOME>
```

## Reset Semantics

`hermes photon reset` should reset local Photon integration state without
destroying account-level resources by default.

Default reset should:

- Stop the managed tunnel for the current Hermes home.
- Delete only owned managed remote webhooks.
- Remove `PHOTON_PROJECT_ID`.
- Remove `PHOTON_PROJECT_SECRET`.
- Remove `PHOTON_WEBHOOK_SECRET`.
- Remove `PHOTON_WEBHOOK_PUBLIC_URL`.
- Remove `photon/tunnel.json`.
- Remove the active-home claim only if it is owned by this home.
- Keep `PHOTON_DASHBOARD_TOKEN`.
- Keep `PHOTON_ALLOWED_USERS`.
- Leave Photon dashboard projects and Spectrum users intact.

Optional flags:

```bash
hermes photon reset --logout
hermes photon reset --phones
hermes photon reset --remote
hermes photon reset --all
```

`--remote` should still mean owned remote managed webhooks only. Deleting
unowned webhooks should require explicit ids.

## Documentation Changes

First-run docs should lead with:

```bash
hermes photon quick-setup --phone '+15551234567'
```

The README and website page should move advanced details into later sections:

- Project selection.
- Manual webhook registration.
- Tunnel logs.
- Webhook deletion.
- Custom `HERMES_HOME`.
- Launchd/systemd service repair.

The primary docs should describe outcomes, not internals:

- "Set up Photon on this machine."
- "Text this assigned number."
- "Run status if something fails."

## Testing Plan

Add focused tests for the reconciler and keep network calls mocked.

Core unit tests:

- Missing dashboard token runs login.
- Invalid dashboard token clears token and runs login.
- Stored Spectrum credentials are validated before reuse.
- Spectrum 401 clears project credentials.
- Exactly one compatible project is adopted.
- Multiple compatible projects stop with selection guidance.
- No project creates one and validates returned credentials.
- Phone user creation saves the assigned number when returned.
- Phone allowlist is idempotent.
- Sidecar install is skipped when the installed version is valid.
- Managed `cloudflared` install verifies SHA-256 and writes manifest.
- Existing valid managed `cloudflared` avoids network update.
- Failed update keeps existing managed binary.
- Tunnel URL parsing ignores old log URLs.
- Current webhook registration saves signing secret.
- Owned stale managed webhook is deleted.
- Unowned stale managed webhook is not deleted.
- Existing current webhook without local secret is recreated only if owned.
- Active-home mismatch stops setup.
- Gateway service home mismatch is detected.
- Public `502` plus failed local health does not suggest blind restart.
- Public `502` plus healthy local health retries and then reports tunnel/origin
  status.
- `reset` preserves dashboard token by default.
- `reset --all` removes local Photon env state and owned managed remote hooks.

Integration/manual tests:

- Fresh temp `HERMES_HOME` with no installed service.
- Temp `HERMES_HOME` while default `~/.hermes` gateway service is running.
- Existing default-home Photon setup with current webhook.
- Dead quick tunnel hostname.
- Missing local webhook secret with owned current webhook.
- Missing local webhook secret with unowned current webhook.

## Rollout Plan

1. Add internal `PhotonSetupContext` and read-only inspectors.
2. Add validators for dashboard token, Spectrum credentials, service identity,
   local health, public health, and webhook registration.
3. Add repair functions behind `quick-setup`, keeping existing advanced
   subcommands intact.
4. Replace the current `quick-setup` linear flow with the reconciler.
5. Update `status` to use the same inspectors.
6. Add `reset` with conservative default behavior.
7. Simplify README and website first-run docs.
8. Keep advanced command docs but move them below the happy path.
9. Run Photon unit tests and a temp-home manual QA pass.

## Open Questions

- Does Photon expose a reliable user lookup/list endpoint for verifying that a
  phone already exists and recovering its assigned iMessage number?
- Can webhook list responses identify enough metadata to distinguish Hermes
  owned webhooks if local `owned_webhooks` state is lost? If not, unowned must
  remain manual.
- Should `quick-setup` install a background service by default, or should it ask
  before installing persistent launchd/systemd state?
- Should a foreground QA mode be supported by `quick-setup --foreground` for
  users who do not want a service?
- Should takeover of another active Hermes home exist, or should users always
  run `reset` from the owning home first?

## Success Criteria

The feature is complete when a fresh user can run:

```bash
hermes photon quick-setup --phone '+15551234567'
```

and the command either:

- exits successfully with the assigned iMessage number and verified
  `photon=connected`, or
- exits with one failed invariant, the observed evidence, and the smallest
  safe repair action.

The command should not leave users in a loop of `status`, `gateway restart`,
and tunnel restarts when the real problem is stale cached state or a gateway
service pointing at another Hermes home.
