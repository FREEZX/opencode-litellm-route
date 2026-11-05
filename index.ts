// Server entrypoint for the litellm-route plugin.
//
// The server resolves <plugin-dir>/index.ts as the server part and needs it to
// exist, otherwise it raises "Plugin entrypoint not found". It is a no-op on
// purpose: the TUI (which reads <plugin-dir>/tui.ts) does all the work.
//
// setup() must not return anything: OpenCode 2.2.5 treats a setup return value as
// a dispose callback and invokes it, which is what killed the earlier attempt.
export default {
  id: "litellm.route",
  name: "litellm-route",
  setup() {},
}
