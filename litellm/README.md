# LiteLLM gateway — OpenCode Go + LAN models, with the smart-router

This directory is the **LiteLLM half** of the project: the proxy that OpenCode
talks to, the tiered `smart-router`, and the callback hook that hands the picked
model to the OpenCode TUI plugin in the parent directory.

```
OpenCode TUI  ──►  LiteLLM :4000/v1  ──►  opencode_go/<model>   (subscription)
                       │                └►  Windows Strata LAN   (free, swift-1.5)
                       │
                       └─ writes routes/<session_id>.json  ──►  TUI plugin reads it
```

## Files

| File | Purpose |
|------|---------|
| `config.yaml` | Model list, `smart-router` tiers, fallbacks, router settings. Mounted at `/app/config.yaml`. |
| `opencode_session_hook.py` | LiteLLM `CustomLogger` callback: pins the Go session header, writes the per-session route file, and (optionally) appends the `> ⚡ routed via …` marker to the answer. |
| `go_router.py` | Custom complexity classifier: heuristic tier + local swift classifier + allowance caps from the OpenCode usage API. |
| `docker-compose.yml` | The proxy (`litellm`), Postgres (`db`), and the Claude bridge (`cliproxyapi`). |
| `.env.example` | Template for the environment. Copy to `.env` and fill in. |
| `cliproxy/` | CLIProxyAPI config template + the Claude Code OAuth login (git-ignored). |

`routes/` (host) is bind-mounted to `/app/routes` in the container — that is the
handoff directory the TUI plugin reads.

## Requirements

- Docker + Docker Compose v2 (`docker compose`).
- An **OpenCode Go API key** (personal or service key from
  [OpenCode Console](https://opencode.ai/console) → Account → Personal API keys).
  An app login alone does not authenticate LiteLLM.
- Optional: a reachable Windows/LAN box that serves an OpenAI-compatible
  `swift-1.5-iq3_xxs` (the free local model used as classifier and SIMPLE tier).
  Without it the router still works — the classifier falls back to its heuristic
  and `smart-router` falls over to the next model.

## Setup from scratch

### 1. Environment

```bash
cd litellm
cp .env.example .env
chmod 600 .env
```

Fill in `.env`:

| Variable | Notes |
|----------|-------|
| `LITELLM_MASTER_KEY` | Admin password for `/ui` and the bearer token clients use. Must start with `sk-`. Generate with `printf 'sk-%s\n' "$(openssl rand -hex 32)"`. |
| `LITELLM_SALT_KEY` | Encrypts provider credentials stored in Postgres. **Do not rotate casually.** Generate with `openssl rand -hex 32`. |
| `POSTGRES_PASSWORD` | Password for the `db` service. |
| `POSTGRES_USER` / `POSTGRES_DB` | Defaults `litellm` / `litellm`. |
| `OPENCODE_API_KEY` | The OpenCode Go key from the Console. |
| `STRATA_API_BASE` / `STRATA_API_KEY` | LAN box base URL (e.g. `http://192.168.1.134:8080/v1`) and its key. Leave empty to run without it. |
| `CLIPROXY_API_KEY` / `CLIPROXY_API_BASE` | Local client key for the `cliproxyapi` service and its base URL (`http://cliproxyapi:8317/v1`). Required for the Claude models. |
| `LITELLM_SHOW_ROUTE_MARKER` | `0` keeps the answer text clean (the TUI toast replaces the marker). Default `1`. |
| `TZ` | Container timezone, e.g. `Europe/Skopje`. |

### 2. Key files for OpenCode

OpenCode can read a key from a file (`{file:/path}`), so the master key does not
have to sit in `opencode.jsonc`:

```bash
mkdir -p secrets && chmod 700 secrets
printf '%s' "$LITELLM_MASTER_KEY" > secrets/LITELLM_MASTER_KEY   # no trailing newline
printf '%s' "$OPENCODE_API_KEY"    > secrets/OPENCODE_API_KEY
printf '%s' "$STRATA_API_KEY"      > secrets/STRATA_API_KEY      # only if you use Strata
```

### 3. Start the stack

```bash
mkdir -p routes data/postgres
docker compose up -d
docker compose ps          # wait for "healthy"
```

| Where | URL |
|-------|-----|
| Admin UI | http://127.0.0.1:4000/ui (user `admin`, password = `LITELLM_MASTER_KEY`) |
| OpenAI API | http://127.0.0.1:4000/v1 |
| Health | http://127.0.0.1:4000/health/liveliness |

Smoke test:

```bash
source .env
curl -sS http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H 'Content-Type: application/json' \
  -H 'x-opencode-session-id: test-1' \
  -d '{"model":"smart-router","messages":[{"role":"user","content":"Reply with exactly: ok"}]}'
```

### 4. Point OpenCode at the gateway

`~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "model": "litellm/smart-router",
  "providers": {
    "litellm": {
      "name": "LiteLLM",
      "package": "@ai-sdk/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:4000/v1",
        "apiKey": "{file:/path/to/litellm/secrets/LITELLM_MASTER_KEY}"
      },
      "models": {
        "smart-router": { "name": "Smart Router (LiteLLM)" },
        "beam-free": { "name": "beam-free" },
        "glm-5.3-flash": { "name": "glm-5.3-flash" }
      }
    }
  }
}
```

List every model you want to be able to pick directly — the names must match
`model_name` entries in `config.yaml`.

### 5. Wire the route handoff to the TUI plugin

The plugin reads `<routes dir>/<session_id>.json`; the hook writes the same file
inside the container.

- Container side: `ROUTE_DIR` defaults to `/app/routes` (`LITELLM_ROUTE_DIR`
  inside the container), bind-mounted from `./routes` by `docker-compose.yml`.
- Host side: the plugin's `LITELLM_ROUTE_DIR` must point at that **same host
  directory** (default `/home/freezx/docker/litellm/routes`).

The session id comes from the `x-opencode-session-id` request header, so the file
name is the OpenCode session id. If the plugin and the hook disagree on the
directory, no toast appears — check `ls routes/` while a turn is running.

### 6. Install the TUI plugin

See the [parent README](../README.md#install) — the plugin lives in
`~/.config/opencode/plugins/litellm-route/`.

## `smart-router` behaviour

- **Classifier** (`go_router.py`): a free heuristic tier; anything that is not
  obviously SIMPLE is classified by the local swift model. If swift is down, a
  120 s negative cache kicks in and the heuristic is used for the next turns
  instead of paying the timeout every time.
- **Allowance caps**: the tier is capped from the live OpenCode usage API
  (rolling / weekly / monthly), so a tight window forces the zero-cost pool.
- **Fallbacks**: when the picked model fails, the router retries an ordered chain
  (`beam-free` → `claude-haiku-5-5` → `glm-5.3-flash` → `qwen3.8-flash` →
  `kimi-k2.7-code`) with `num_retries: 0` — one attempt per model, then the
  next fallback.
- **Blocked models**: models this workspace rejects (Global regions / training
  opt-in) are commented out of the tier pools in `config.yaml`.

The router logs one line per request:

```
smart-router classify heuristic=SIMPLE swift=- tier=SIMPLE capped=SIMPLE windows={'rolling': 0, 'weekly': 37, 'monthly': 18}
```

## Claude Pro subscription ($20)

Anthropic **blocks subscription OAuth from third-party clients**: the same
`sk-ant-oat…` token that answers in Claude Code returns `429 rate_limit_error`
when LiteLLM (or `curl`) calls `api.anthropic.com` directly, even with the OAuth
beta header and Claude Code user-agent. So Claude goes through **CLIProxyAPI**, a
small proxy that speaks the Claude Code transport and exposes an OpenAI-compatible
API. LiteLLM treats it as just another upstream.

```
OpenCode -> LiteLLM -> cliproxyapi:8317 -> Claude Code transport -> Anthropic
```

| Client name | Upstream | Tier |
|-------------|----------|------|
| `claude-haiku-5-5` | `openai/claude-haiku-5-5` | MEDIUM + first fallback |
| `claude-sonnet-5` | `openai/claude-sonnet-5` | COMPLEX |
| `claude-opus-5-5` | `openai/claude-opus-5-5` | REASONING |
| `claude-fable-5-1` | `openai/claude-fable-5-1` | not in tiers: needs Pro usage credits (`429 credits_required`) |

### 1. Configure

```bash
mkdir -p cliproxy/auth
cp cliproxy/config.example.yaml cliproxy/config.yaml
# set access.api-keys[0] in cliproxy/config.yaml, then put the same value in .env:
#   CLIPROXY_API_KEY=<that key>
#   CLIPROXY_API_BASE=http://cliproxyapi:8317/v1
```

`cliproxy/config.yaml` and `cliproxy/auth/` are git-ignored (they hold the client
key and the OAuth login).

### 2. Log in once

The login writes one auth file per Claude account into `cliproxy/auth/`:

```bash
# from litellm/
docker run --rm -it -p 54545:54545 \
  -v "$PWD/cliproxy/auth:/root/.cli-proxy-api" \
  eceasy/cli-proxy-api:latest -claude-login
```

Open the URL it prints, sign in with the Pro account, and approve. The callback
lands on `localhost:54545`; if the browser cannot reach it, paste the final
`localhost:54545/callback?...` URL back into the terminal. You should see
`Claude authentication successful` and a file in `cliproxy/auth/`.

### 3. Start

```bash
docker compose up -d          # creates cliproxyapi + recreates litellm
```

### Verify

```bash
curl -sS http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"Reply with exactly: hello"}]}'
```

If this returns a `429`, check `docker compose logs cliproxyapi` — the auth file
may be missing or its refresh token may have expired (re-run step 2).

### Maintenance

- CLIProxyAPI refreshes the access token itself using the stored refresh token.
  If it starts returning auth errors, re-run the login.
- The container does not publish port 8317; only LiteLLM reaches it over the
  compose network. Publish it temporarily if you want to call it directly.
- `latest` is pinned by digest only through the local image; re-pull deliberately
  after checking the release notes.

### Caveats

- **Terms**: Anthropic's usage terms tie subscription OAuth to Claude Code. This
  setup routes through a proxy that reproduces the Claude Code transport; check
  the terms before relying on it for anything beyond personal use.
- **Quota**: the subscription has 5-hour and weekly limits shared with Claude
  Code. LiteLLM reports list-price cost in its logs even though Anthropic bills
  the plan, so those numbers are not real spend.
- **Model access**: Pro includes Sonnet 5, Opus 5.5 and Haiku; Fable 5.1 needs
  usage credits and is therefore kept out of the tiers.

## Route marker

`opencode_session_hook.py` also writes the route into the answer:

```
> ⚡ routed via claude-haiku-5-5

...the model's reply...

> ⚡ routed via claude-haiku-5-5
```

It is prepended to the first assistant text of each human turn and appended at
the end, so it is visible immediately and confirmable after the answer. It is
stripped from assistant history before the next upstream call (the model never
sees it, token counts stay clean), tool-loop continuations do not add a second
copy, and title/summary calls are skipped.

Set `LITELLM_SHOW_ROUTE_MARKER=0` to turn it off; the TUI toasts keep working
either way.

## Restarting after a change

`config.yaml` is read at start-up only:

```bash
docker compose up -d --force-recreate litellm
```

With `STORE_MODEL_IN_DB=True` the proxy reconciles deployment ids from the file,
so a live edit can leave a model dropped until the process reloads. Python files
(`go_router.py`, `opencode_session_hook.py`) are bind-mounted read-only, so they
only need a container restart.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `/ui` unreachable | `docker compose ps` — the proxy waits for Postgres to be healthy. |
| `no fallback model group was found` | That model group has no fallback entry; only `smart-router` has one. |
| Go requests rejected with a privacy error | The model needs Global regions / training opt-in in the Console — remove it from the tier pools. |
| `Connection error` to the LAN box | `STRATA_API_BASE` unreachable; the router should fall over to the Go models. |
| No route file, no toast | The turn failed upstream, or the hook is not mounted. Check `docker compose logs litellm` and `ls routes/`. |
| Plugin sees nothing | `LITELLM_ROUTE_DIR` on the TUI side points somewhere else than the compose bind mount. |

## Security notes

- `.env` and `secrets/` hold the master key, the salt key and provider keys —
  never commit them (they are git-ignored).
- The TUI plugin never receives the master key: it only reads route files.
- The OpenCode Go key is only used server-side by the proxy.
