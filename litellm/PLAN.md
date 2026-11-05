# Plan: show the routed model in the OpenCode TUI

Goal: when `smart-router` picks a model, the OpenCode terminal UI shows which one
it was — as a toast at the end of the turn, not as text inside the model's answer.

| # | Mechanism | Where | Status |
|---|-----------|-------|--------|
| 1 | Response marker `> ⚡ routed via <model>` | LiteLLM hook (`opencode_session_hook.py`) | **ON** — top and bottom of the answer (`LITELLM_SHOW_ROUTE_MARKER=1`) |
| 2 | TUI toast + palette command | OpenCode plugin (`~/.config/opencode/plugins/litellm-route/`) | **working** |

---

## How it works

LiteLLM writes one small JSON file per OpenCode session:

```
litellm/routes/<session_id>.json   (container: /app/routes, bind mount)
{"model": "swift-1.5-iq3_xxs", "raw_model": "...", "request_id": null, "updated_at": "..."}
```

The session id comes from the `x-opencode-session-id` request header (the hook's
`_existing_session` prefers it over the affinity key `x-opencode-session`).

The TUI plugin reads that file and never talks to LiteLLM, so the master key stays
out of the TUI.

## Plugin contract (verified against OpenCode 2.2.5)

- A plugin directory needs `index.ts` (server part) and `tui.ts` (TUI part).
- Both parts export `{ id, setup }`. `setup`'s return value is treated as the
  dispose callback, so it must be a function or nothing — returning a hooks object
  is what crashed the earlier attempt (`TypeError: r is not a function`).
- The TUI part gets a plain-JS api:
  - `data.on("session.execution.succeeded", cb)` — cb receives the event object;
    the session id is `event.data.sessionID` (not `event.id`, not the raw string).
  - `ui.toast.show({ title, message, variant, duration })` — the toast.
  - `keymap.layer(() => ({ commands: [{ id, title, group, bind, run }] }))` —
    commands land in `keymapState.commands`, which is what the palette lists.

## Verified behaviour

- `opencode plugin list` discovers `/home/freezx/.config/opencode/plugins/litellm-route`.
- TUI boots with no plugin error; log shows `setup`, `command registered`.
- **Start of response**: the plugin polls the route file from `session.execution.started`;
  as soon as LiteLLM writes it (first stream chunk) the toast appears — measured
  202 ms after the planted file, ~1 s into a real turn.
- **End of turn**: `session.execution.succeeded` toasts again with the same route
  (both were requested), deduped by event id so duplicate event delivery does not
  fire it twice.
- A stale file from a previous turn is never announced at the start (dedupe by the
  route file's `updated_at`).
- `keymap.commands()` reports 106 commands including `litellm.route.show`
  ("Show litellm route" under group `smart-router`).
- With the marker off: answer text is clean (`marker-off-check`), route files are
  still written (verified: `ses_e56be6828ffe9vlTcwhvImEr3c` → `beam-free`).

Observed log of a working turn:

```
turn start session=ses_...          # poll starts
start session=ses_... stamp=...     # ~1 s later, toast "routed via beam-free"
end   session=ses_... stamp=...     # second toast when the turn finishes
```

## Backend resilience (swift-1.5 unavailable)

`swift-1.5-iq3_xxs` is both the classifier and a member of the SIMPLE tier, so an
outage used to end in a 500 once the only configured fallback (swift itself) also
failed. Two independent guards now cover it:

1. **Classifier** (`go_router.py`): a failed swift classification records a
   120 s negative cache (`SWIFT_COOLDOWN_SECONDS`) and the free heuristic tier is
   used; later turns skip the 8 s classifier wait while the cache is hot.
2. **Model fallback** (`router_settings.fallbacks`): `smart-router` retries an
   ordered chain — `beam-free` → `glm-5.3-flash` → `qwen3.8-flash` →
   `kimi-k2.7-code` — and stops at the first success. `num_retries: 0` means one
   attempt per model group, so the first swift failure goes straight to the
   fallback (no retry storm); `allowed_fails: 1`, `cooldown_time: 60` cool the
   dead deployment down. LiteLLM resolves this chain for the tier model the
   router picked (`fallback_lookup_groups`), not just for the `smart-router` name.

Verified while swift was down:

- `x-litellm-attempted-fallbacks: 1` with `x-litellm-model-group: beam-free` on
  SIMPLE turns that picked swift; HTTP 200, route file written as `beam-free`.
- Logs: `swift-1.5 classifier failed ... using heuristic and skipping swift for 120s`,
  then `swift-1.5 classifier skipped (down, ...s left); using heuristic`.
- Zero `model group 'smart-router' failed` entries during the test runs.
- `iceman-contributor-free` was removed from the SIMPLE tier: this workspace
  rejects it with the training-on-data error.


## Notes

- `/route`-style palette entry: `ctrl+p`, type `litellm`, Enter. A direct bind of
  `ctrl+g` is registered too.
- Turns that fail upstream (workspace privacy / connection errors) produce no
  response, so no route file and no toast — expected.
- Log: `/tmp/opencode/litellm-route.log`.

## Router split + Claude Pro classifier (superseded the single-router/swift-classifier setup above)

Two routers now exist instead of one, split by context floor (see `config.yaml`'s
header comment for the exact per-model numbers):

- `smart-router` — every pooled model ≥200k context (includes Claude Pro).
- `smart-router-500k` — every pooled model ≥500k context (drops Claude Pro, `hy3`,
  `kimi-k2.7-code`, swift). Added because the single router previously mixed
  192k–1M+ context models, so OpenCode showed the lowest common context (~100k)
  regardless of which model actually answered.

Both share one `GoAllowanceClassifier` instance, so a classifier cooldown/outage
on one applies to the other. `opencode.jsonc` has an explicit `limit.context`/
`limit.output` on every litellm model (not just the two routers) so the TUI
always shows the real backend limit.

The classifier itself moved from local swift-1.5 to `claude-haiku-5-5` (Claude
Pro, via CLIProxyAPI) — swift stayed in the SIMPLE pool, but a model that's
also a pool candidate is a worse classifier than one that only classifies: it
was competing for the same LAN box's token budget with ordinary SIMPLE
traffic. Behavior is otherwise identical: 8 s timeout, 120 s negative cache on
failure (`CLASSIFIER_COOLDOWN_SECONDS`, renamed from `SWIFT_COOLDOWN_SECONDS`),
heuristic fallback, lower tier wins on disagreement. This trades a swift-free
classifier for spending Claude Pro subscription quota on every non-trivial
classify call — accepted as worth it for classification quality.

## Provider balancing (Go ↔ Claude Pro ↔ swift)

New requirement: both subscriptions (OpenCode Go, Claude Pro) have real
hourly/weekly/monthly limits; once either is exhausted, traffic must fail over
to the other, and to local swift-1.5 if both are exhausted at once.

Implemented as a second, independent LiteLLM callback,
`go_router.ProviderBalanceFilter` (`async_filter_deployments` — the same hook
LiteLLM's own budget-limiter and RPM-limiter callbacks use), registered
alongside `opencode_session_hook.proxy_handler_instance` in
`litellm_settings.callbacks`. It runs on every request (direct model calls and
router picks alike), not just inside the classifier/router pipeline, so it is
a hard backstop independent of `GoAllowanceClassifier._cap_tier`'s tier-steering:
that only prevents a NEW paid pick as Go usage climbs; it doesn't stop a tier
built entirely out of one already-exhausted provider's deployments.

Usage sources (both cached 60 s, shared with the classifier's own Go usage read
via one `_UsageCache` per source, fail-open — a fetch error keeps the last
known value instead of acting as if usage were unknown or zero):

- **OpenCode Go**: `GET https://opencode.ai/zen/go/v1/usage` (same endpoint
  `_cap_tier` already used). A window (`rolling`/`weekly`/`monthly`) with
  `status != "ok"` or `percent >= 100` excludes every `opencode_go/*`
  deployment.
- **Claude Pro**: `GET https://api.anthropic.com/api/oauth/usage` — Anthropic's
  real (undocumented) quota endpoint, called with `Authorization: Bearer
  <oauth access_token>` + `anthropic-beta: oauth-2025-04-20`. The token is read
  directly off the credential file CLIProxyAPI already maintains/refreshes
  (`cliproxy/auth/claude-*.json`), mounted read-only into the litellm container
  at `CLAUDE_OAUTH_AUTH_DIR` (`/app/cliproxy-auth`) — no new credential to
  manage. `five_hour`/`seven_day` utilization at or past `CLAUDE_HARD_CAPS`
  (90%/85%, the same margin the Go ladder uses) excludes every
  `openai/claude-*` deployment.

Deployments are matched to a provider by their `litellm_params.model` prefix
(`opencode_go/` or `openai/claude-`); anything else (swift, and any `*-free`
model, exempted outright since the Go account's windows don't apply to free
models) passes through untouched. When filtering empties a tier down to zero
candidates, the existing `router_settings.fallbacks` chain (see "Backend
resilience" above) carries the request onto the other provider or onto
swift/a free Go model.

Verified against real, live usage data (2026-11-05): OpenCode Go's `weekly`
window was genuinely at 100%/`rate-limited`, and `smart-router` correctly
forced every tier to SIMPLE/swift end-to-end (confirmed via the classify log
line and a live `smart-router` call that returned 200, routed via
`swift-1.5-iq3_xxs`). Claude Pro's `five_hour`/`seven_day` windows were healthy
(32%/5%) in the same window, confirmed via a direct `_fetch_claude_usage()`
call inside the container. The filter's exclude/keep logic (single-provider
exhausted, both exhausted, `-free` bypass) was also unit-tested directly
against synthetic usage data inside the running container.
