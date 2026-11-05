# litellm-route

An [OpenCode](https://opencode.ai) TUI plugin that shows **which backend model
LiteLLM's `smart-router` picked** for the turn you are running — as a toast in the
terminal UI at the start and end of the response. The gateway also marks the
answer itself with `> ⚡ routed via <model>` at the top and bottom.

```
┌─────────────────────────────────────────────┐
│ smart-router                                 │
│ routed via beam-free                        │
└─────────────────────────────────────────────┘
```

## What you get

| When | What happens |
|------|--------------|
| Start of the response | A toast fires as soon as LiteLLM records the model (usually ~1 s into the turn). |
| End of the turn | The same toast fires again, so a long turn shows the route at both ends. |
| On demand | Palette command **"Show litellm route"** (group `smart-router`) re-shows the current session's route. |

The route is shown twice: as a `> ⚡ routed via <model>` marker at the **top and
bottom** of the answer (both are kept), and as a toast at the start and end of the
turn. A stale file from the previous turn is never announced at the start (dedupe
by the route file's `updated_at`).

## Requirements

1. **OpenCode 2.2.5+** with plugin support.
2. **A LiteLLM proxy that writes route files.** The plugin only reads a JSON file
   per session; it never talks to LiteLLM and never sees the master key.

The route files come from the LiteLLM side of this setup, which lives in the
`litellm/` folder of this repo:

```
litellm/opencode_session_hook.py   writes the files (container path /app/routes)
litellm/routes/                     bind-mounted from the host
```

The session id comes from the `x-opencode-session-id` request header, so the file
name matches the OpenCode session:

```
<routes dir>/<session_id>.json
```

Route file format:

```json
{"model": "beam-free", "raw_model": "beam-free", "request_id": null, "updated_at": "2026-11-05T08:45:44+00:00"}
```

## Repository layout

```
litellm-route/
├── index.ts       # OpenCode plugin: server part (no-op, required for discovery)
├── tui.ts         # OpenCode plugin: TUI part (polling, toasts, palette command)
├── README.md      # this file
└── litellm/       # the LiteLLM half of the setup
    ├── config.yaml              # models, smart-router tiers, fallbacks
    ├── opencode_session_hook.py # callback: session pinning + route files
    ├── go_router.py             # custom classifier (heuristic + swift + allowance caps)
    ├── docker-compose.yml       # proxy + postgres
    ├── .env.example             # template for .env
    └── README.md                # LiteLLM setup guide (from scratch)
```

Both halves are needed: the plugin is useless without a LiteLLM proxy that writes
route files, and the route files only exist because of the hook.

## Install

The plugin lives in the OpenCode plugin directory:

```
~/.config/opencode/plugins/litellm-route/
├── index.ts     # server part (no-op; required for discovery)
├── tui.ts       # TUI part (all the work)
└── README.md
```

Verify that OpenCode sees it:

```bash
opencode plugin list
# ID             VERSION  SOURCE
# litellm.route  local    /home/freezx/.config/opencode/plugins/litellm-route
```

Restart the TUI after any change to the plugin files.

## Install from scratch

Two halves, in this order:

**1. LiteLLM gateway** (see [`litellm/README.md`](litellm/README.md) for the full guide):

```bash
cd litellm
cp .env.example .env && chmod 600 .env      # fill in keys
mkdir -p secrets routes data/postgres
printf '%s' "$LITELLM_MASTER_KEY" > secrets/LITELLM_MASTER_KEY
docker compose up -d
curl -sS http://127.0.0.1:4000/health/liveliness
```

**2. OpenCode plugin** (this directory):

```bash
cp -r <this repo> ~/.config/opencode/plugins/litellm-route
opencode plugin list     # must show litellm.route
```

**3. Point OpenCode at the gateway** — add the `litellm` provider to
`~/.config/opencode/opencode.jsonc` (`baseURL: http://127.0.0.1:4000/v1`,
`apiKey: {file:.../secrets/LITELLM_MASTER_KEY}`) and set
`"model": "litellm/smart-router"`. A ready-to-copy snippet is in the
[LiteLLM guide](litellm/README.md#4-point-opencode-at-the-gateway).

**4. Check the handoff** — the plugin and the proxy must agree on the routes
directory:

| Side | Setting |
|------|---------|
| Proxy (container) | `/app/routes` (default) ← bind-mounted from `<repo>/litellm/routes` |
| Plugin (host) | `LITELLM_ROUTE_DIR=<repo>/litellm/routes` (default is `/home/freezx/docker/litellm/routes`) |

Run a prompt and check that a file appears in the routes directory with the
current session id; if it does, the toast will appear.

> The live deployment of this stack runs from `/home/freezx/docker/litellm`
> (that is where the running container mounts its files from). The `litellm/`
> folder in this repo is the source of truth for those files — copy or symlink
> them when you change them here.

### Claude Pro models

Anthropic blocks the Claude Pro subscription's OAuth token for non-Claude-Code
clients, so the gateway reaches Claude through **CLIProxyAPI** (a `cliproxyapi`
service in the compose stack) instead of calling Anthropic directly:

```
OpenCode -> LiteLLM -> cliproxyapi:8317 -> Claude Code transport -> Anthropic
```

| Model | Tier |
|-------|------|
| `claude-haiku-5-5` | MEDIUM + fallback |
| `claude-sonnet-5` | COMPLEX |
| `claude-opus-5-5` | REASONING |
| `claude-fable-5-1` | not in tiers (needs usage credits) |

See [`litellm/README.md`](litellm/README.md#claude-pro-subscription-20) for the
one-time Claude login, the `cliproxy/` config, and the quota/terms caveats.

## Usage

- **Palette**: `ctrl+p` → type `litellm` → Enter.
- **Direct bind**: `ctrl+g` (registered by the plugin).
- The toast title is `smart-router`, the message is `routed via <model>`.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `LITELLM_ROUTE_DIR` | `/home/freezx/docker/litellm/routes` | Directory holding `<session_id>.json` route files. |
| `LITELLM_ROUTE_LOG` | `/tmp/opencode/litellm-route.log` | Plugin debug log. |

Both must be exported in the environment that starts the OpenCode TUI.

## How it works

1. `session.execution.started` fires → the plugin starts a 200 ms poll of the
   session's route file (up to ~30 s).
2. LiteLLM writes the route file on the first stream chunk → the poll sees it and
   toasts the model. The file's `updated_at` is the dedupe key, so a stale file
   from the previous turn is never announced.
3. `session.execution.succeeded` fires → the plugin toasts the route again
   (deduped by event id, so a duplicated event cannot double-fire).
4. The palette command re-reads the file on demand and always answers.

### Plugin API used (verified against OpenCode 2.2.5)

```ts
data.on("session.execution.started",   cb)  // cb gets the event object
data.on("session.execution.succeeded", cb)  // session id = event.data.sessionID
ui.toast.show({ title, message, variant, duration })
keymap.layer(() => ({ commands: [{ id, title, group, bind, run }] }))
```

Two contract details that cost time to find:

- The session id is `event.data.sessionID` — not `event.id` and not the raw string.
- `setup()` must **not** return an object. OpenCode treats the return value as a
  dispose callback and calls it, which throws `TypeError: r is not a function`.

## Troubleshooting

**No toast appears.**

1. Check the plugin log:
   ```bash
   tail -f /tmp/opencode/litellm-route.log
   ```
   A healthy turn looks like:
   ```
   turn start session=ses_...
   start session=ses_... stamp=2026-11-05T08:45:44+00:00
   end   session=ses_... stamp=2026-11-05T08:45:44+00:00
   ```
2. Check that LiteLLM is writing the file for *this* session:
   ```bash
   ls -l "${LITELLM_ROUTE_DIR:-/home/freezx/docker/litellm/routes}"/<session_id>.json
   ```
3. If the turn failed upstream (e.g. a blocked model), no route file is written and
   no toast is expected.

**Toast shows the previous model.** The route file is rewritten per turn; if the
LiteLLM side is not writing files, the plugin falls back to the last file it saw.

**LiteLLM side errors.** See [`litellm/README.md`](litellm/README.md#troubleshooting)
for gateway problems (upstream privacy errors, missing fallbacks, unreachable LAN
box).

## Development

```bash
cd ~/.config/opencode/plugins/litellm-route
git log --oneline
git status
```

- `index.ts` — server entrypoint, intentionally a no-op.
- `tui.ts` — TUI entrypoint: polling, toasts, palette command.
- `litellm/` — the gateway side: `config.yaml` (models, tiers, fallbacks),
  `opencode_session_hook.py` (route files), `go_router.py` (classifier).

After changing a `litellm/` file, restart the container that runs it:

```bash
cd /home/freezx/docker/litellm
docker compose up -d --force-recreate litellm   # config.yaml
docker compose restart litellm                  # python files
```
