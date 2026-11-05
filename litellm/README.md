# LiteLLM

OpenAI-compatible gateway in front of LAN models and OpenCode Go. Models and the LLM complexity router live in `config.yaml`; keys/spend/UI state live in Postgres under `data/postgres`.

## OpenCode CLI (front-end)

OpenCode is installed at `~/.opencode/bin/opencode` (on PATH via `~/.zshrc`). Global config: `~/.config/opencode/opencode.jsonc` — default model `litellm/smart-router`, plus direct `windows-strata/*`.

```bash
opencode auth login opencode --method key   # paste Console Go API key
opencode                                   # TUI; /models to switch
opencode run "Reply with exactly: ok"
```

LiteLLM stays the gateway; OpenCode is the client that talks to it (and can also use Go natively after login).

## Start

```bash
cd ~/docker/litellm
# Put your OpenCode Console API key in .env as OPENCODE_API_KEY=...
docker compose up -d
```

| Where | URL |
|-------|-----|
| Admin UI | http://\<host\>:4000/ui |
| OpenAI API | http://\<host\>:4000/v1 |
| Health | http://\<host\>:4000/health/liveliness |

UI login: user `admin`, password = `LITELLM_MASTER_KEY` from `.env`.

## OpenCode Go API key

Create a **personal** or **service** API key in [OpenCode Console](https://opencode.ai/console) (Account → Personal API keys). An OpenCode app login alone does not authenticate LiteLLM. Set it in `.env`:

```bash
OPENCODE_API_KEY=sk-...
```

Then `docker compose up -d` (or recreate the litellm service).

## Models (from `config.yaml`)

| Client model name | Backend |
|-------------------|---------|
| `swift-1.5-iq3_xxs` | Windows Strata (LAN, free, ~40 tok/s, ~5 s on a one-liner, text + image) |
| All Go roster models | OpenCode Go via `opencode_go/` (direct names) |
| `claude-sonnet-5`, `claude-opus-5-5`, `claude-haiku-5-5`, `claude-fable-5-1` | Claude Pro $20 subscription, via CLIProxyAPI (see below) |
| `smart-router` | Allowance-aware auto-router, every pooled model ≥200k context. |
| `smart-router-500k` | Same router logic, every pooled model ≥500k context (drops Claude Pro, `hy3`, `kimi-k2.7-code`, swift). |

OpenCode Go IDs currently mirrored from Console: `beam-free`, `claude-haiku-5-5`, `deepseek-v4-pro`, `deepseek-v4.1-flash`, `gemini-4-argon`, `glm-5.2`, `glm-5.3`, `glm-5.3-flash`, `gpt-5.6-luna`, `gpt-6-luna`, `grok-4.7`, `hy3`, `hy4-preview`, `iceman-contributor-free`, `kimi-k2.7-code`, `kimi-k3`, `kimi-k3.1-pro`, `longcat-2.0`, `mimo-v2.6-flash`, `mimo-v2.6-pro`, `minimax-m3`, `minimax-m3.1-flash`, `muse-spark-1.2-contributor`, `muse-spark-1.3-contributor`, `qwen3.7-plus`, `qwen3.8-flash`, `qwen3.8-max`, `step-5-preview`.

### Claude Pro via CLIProxyAPI

Anthropic 429-gates subscription OAuth for any client that isn't the real Claude Code binary, so direct `ANTHROPIC_AUTH_TOKEN` calls from LiteLLM don't work. The `cliproxyapi` compose service (`eceasy/cli-proxy-api`) reproduces Claude Code's own OAuth transport and exposes an OpenAI-compatible API LiteLLM calls as `openai/claude-*` with `api_base`/`api_key` pointed at it (`CLIPROXY_API_BASE`, `CLIPROXY_API_KEY`). The Claude Pro OAuth login lives in `cliproxy/auth/*.json` (git-ignored); CLIProxyAPI auto-refreshes it. `claude-fable-5-1` needs Pro usage credits (429 `credits_required`) and is excluded from both routers' pools.

`smart-router` tiers (spend-lean; `smart-router-500k` mirrors this minus every model under 500k context — see `config.yaml`'s header comment for the exact floors):

| Tier | Pool | Cost |
|------|------|------|
| SIMPLE | `swift-1.5-iq3_xxs`, `beam-free` | zero allowance |
| MEDIUM | `claude-haiku-5-5`, `mimo-v2.6-flash`, `longcat-2.0`, `glm-5.3-flash`, `qwen3.8-flash`, `minimax-m3.1-flash` | cheap/cheapest |
| COMPLEX | `claude-sonnet-5`, `glm-5.3`, `glm-5.2`, `qwen3.7-plus`, `kimi-k2.7-code`, `kimi-k3`, `minimax-m3`, `mimo-v2.6-pro`, `gpt-5.6-luna`, `grok-4.7`, `gemini-4-argon` | mid-strong |
| REASONING | `claude-opus-5-5`, `gpt-6-luna`, `hy4-preview`, `qwen3.8-max`, `step-5-preview`, `kimi-k3.1-pro` | top few, smallest buckets |

DeepSeek and Muse Spark stay callable by name but are commented out of the pools until Console privacy allows them (Global regions / training opt-in). `classification_mode: user_turn` re-classifies each new human ask (so allowance caps are re-checked mid-session) and replays the held pin on agent-loop continuation turns, keeping prompt caches warm and avoiding mid-loop model switches. Both routers share one classifier instance (`go_router.go_allowance_classifier`), so a cooldown/outage on one applies to the other too.

Classifier details: the classifier is `claude-haiku-5-5` (Claude Pro, via CLIProxyAPI) — a failed/timed-out call falls back to the local heuristic and records a 120 s negative cache so later turns skip straight to the heuristic while it's hot. It sees the session's opening ask plus the latest follow-up, so short follow-ups ("now add unit tests") are classified with the task they belong to. Obvious SIMPLE asks skip the classifier call entirely, and the classifier is also skipped (straight to heuristic) when Claude Pro's own usage windows are exhausted (see Provider balancing below).

Allowance: `go_router.py` polls `GET https://opencode.ai/zen/go/v1/usage` (max 60 s cache) and caps tiers per window — rolling caps at 50/70/90%, weekly at 45/65/85%, monthly at 65/80/95% (cap tier / cap at MEDIUM / zero-cost pool). Rolling binds first because its cap is 20% of the monthly bucket, so its ladder is the tightest. A limited window, disagreement between classifier and heuristic, or a failed Go call all land on the zero-cost pool.

Vision: swift declares `supports_vision: true`, so `modality_routing` lets image turns stay on the free SIMPLE pool when swift can handle them.

### Provider balancing (Go ↔ Claude Pro ↔ swift)

`go_router.py`'s `ProviderBalanceFilter` (registered as its own litellm callback alongside the session hook) is a hard backstop under the tier-capping above: it polls **both** subscriptions' real quota windows and drops a provider's deployments from the candidate list for *every* request — direct calls and auto-router picks alike — once that provider's hourly/weekly/monthly limit is actually exceeded, not just close.

- **OpenCode Go**: the same `/zen/go/v1/usage` rolling/weekly/monthly windows the classifier already polls; a window reports `status != "ok"` or ≥100% → every `opencode_go/*` deployment is excluded.
- **Claude Pro**: Anthropic's own (undocumented) `GET https://api.anthropic.com/api/oauth/usage`, called with the OAuth `access_token` read straight off the credential file CLIProxyAPI maintains (mounted read-only at `CLAUDE_OAUTH_AUTH_DIR`, default `/app/cliproxy-auth`). Its `five_hour`/`seven_day` utilization at or past a hard cap (90% / 85%, mirroring the Go ladder's margins) → every `openai/claude-*` deployment is excluded.
- Unlimited free Go models (`*-free`) are exempt — the Go account's windows don't apply to them.
- If a tier resolves to zero candidates after filtering, `router_settings.fallbacks` (below) carries the request the rest of the way — onto the other still-healthy provider, or onto local swift-1.5 / a free Go model if both subscriptions are exhausted at once.
- Both usage sources are cached for 60 s and fail open on a fetch error (stale-but-last-known value, not "treat as exhausted").

### See which model handled a turn

`smart-router` appends a one-line marker to the **final** assistant text of each human turn:

```
…the model's reply…

> ⚡ routed via longcat-2.0
```

It is added by `opencode_session_hook.py` from `response.model` (the real deployment the router picked), only when the newest message is a human ask — tool-loop continuations do not repeat it, and title/summary calls are skipped. The marker is stripped from assistant history before the next upstream call, so the model never sees it.

Disable it with `LITELLM_SHOW_ROUTE_MARKER=0` in `.env` and recreating the container. The same value is always available as the `x-litellm-model-name` response header, and per request in `LiteLLM_SpendLogs.model` / `GET /spend/logs?session_id=...`.

Restart after editing `config.yaml` (`docker compose up -d --force-recreate litellm`). A live edit changes deployment ids and the DB reconcile will drop those models until reload.

### Resilience

`router_settings.fallbacks` gives `smart-router` and `smart-router-500k` each an ordered chain of known-reachable models (`smart-router: beam-free → claude-haiku-5-5 → glm-5.3-flash → qwen3.8-flash → kimi-k2.7-code`; `smart-router-500k` drops the sub-500k entries). `num_retries: 0` means one attempt per model group — a dead/exhausted pick fails over immediately instead of retrying itself into a 500 or a retry storm. This chain is what the provider balancer (above) and a plain backend outage both rely on to still return 200.

OpenCode Go requires `x-opencode-session`. Prefer sending a stable `x-litellm-session-id` per conversation (better caching). If the client omits it, `opencode_session_hook.py` mints one so `smart-router` and direct Go models still work.

## Example

```bash
source ~/docker/litellm/.env
curl -sS http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H 'Content-Type: application/json' \
  -H 'x-litellm-session-id: test-1' \
  -d '{"model":"glm-5.3-flash","messages":[{"role":"user","content":"Reply with exactly: ok"}]}'
```

## Layout

| Path | Purpose |
|------|---------|
| `config.yaml` | Model list + `smart-router` |
| `.env` | Master/salt keys, Postgres password, Strata + OpenCode keys |
| `data/postgres/` | Postgres data (bind-mounted) |

Do not rotate `LITELLM_SALT_KEY` casually — it encrypts provider credentials stored in the DB.
