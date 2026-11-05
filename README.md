# litellm-route

An [OpenCode](https://opencode.ai) TUI plugin that shows **which backend model
LiteLLM's `smart-router` picked** for the turn you are running — as a toast in the
terminal UI, not as text inside the model's answer.

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

The answer text itself stays clean — the route is never injected into the model's
output.

## Requirements

1. **OpenCode 2.2.5+** with plugin support.
2. **A LiteLLM proxy that writes route files.** The plugin only reads a JSON file
   per session; it never talks to LiteLLM and never sees the master key.

The route files come from the LiteLLM side of this setup:

```
/home/freezx/docker/litellm/opencode_session_hook.py   writes the files
/home/freezx/docker/litellm/routes/                     bind-mounted to /app/routes
```

The session id comes from the `x-opencode-session-id` request header, so the file
name matches the OpenCode session:

```
/home/freezx/docker/litellm/routes/<session_id>.json
```

Route file format:

```json
{"model": "beam-free", "raw_model": "beam-free", "request_id": null, "updated_at": "2026-11-05T08:45:44+00:00"}
```

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
   ls -l /home/freezx/docker/litellm/routes/<session_id>.json
   ```
3. If the turn failed upstream (e.g. a blocked model), no route file is written and
   no toast is expected.

**Toast shows the previous model.** The route file is rewritten per turn; if the
LiteLLM side is not writing files, the plugin falls back to the last file it saw.

## Development

```bash
cd ~/.config/opencode/plugins/litellm-route
git log --oneline
git status
```

- `index.ts` — server entrypoint, intentionally a no-op.
- `tui.ts` — TUI entrypoint: polling, toasts, palette command.
