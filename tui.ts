// TUI entrypoint for the litellm-route plugin.
//
// The server scans plugin directories and reports features.tui when this file
// exists; the TUI then loads it. Both loaders validate the default export against
// the plugin schema, so it must be `{ id, setup }` (what Plugin.define produces).
//
// Verified against the running 2.2.5 runtime (this plugin's own probe log and the
// compiled TUI source):
//   data.on("session.execution.started" | "session.execution.succeeded", cb)
//                                               cb gets the event; the session id
//                                               is event.data.sessionID
//   ui.toast.show({ title, message, variant, duration })
//   keymap.layer(cb)  cb returns { commands: [{ id, title, group, bind, run }] }
//
// LiteLLM writes one JSON file per OpenCode session into the bind-mounted routes
// directory; this plugin only reads it. No master key, no HTTP round trip.

const ROUTE_DIR = process.env.LITELLM_ROUTE_DIR || "/home/freezx/docker/litellm/routes"
const LOG_FILE = process.env.LITELLM_ROUTE_LOG || "/tmp/opencode/litellm-route.log"

const COMMAND_ID = "litellm.route.show"
const POLL_MS = 200
const POLL_TRIES = 150 // ~30s; LiteLLM writes the file on the first stream chunk

function log(message: string) {
  try {
    require("node:fs").appendFileSync(LOG_FILE, `${new Date().toISOString()} pid=${process.pid} ${message}\n`)
  } catch {}
}

function readRoute(sessionID: string) {
  if (!sessionID || typeof sessionID !== "string") return null
  try {
    const data = JSON.parse(require("node:fs").readFileSync(`${ROUTE_DIR}/${sessionID}.json`, "utf8"))
    return data && typeof data === "object" ? data : null
  } catch {
    return null
  }
}

function modelOf(route: any) {
  if (!route) return null
  return route.model || route.raw_model || null
}

function stampOf(route: any) {
  return String(route?.updated_at ?? "")
}

// The event callback receives an event object; the session id lives in data.sessionID.
function sessionIdFrom(arg: any): string {
  if (typeof arg === "string") return arg
  if (!arg || typeof arg !== "object") return ""
  return arg.data?.sessionID || arg.sessionID || arg.properties?.sessionID || ""
}

export default {
  id: "litellm.route",
  name: "litellm-route",

  // Return value is the cleanup callback, so it must be a function (or nothing).
  setup(context: any) {
    log("setup")

    const cleanups: Array<() => void> = []
    const endSeen = new Set<string>() // event ids already toasted at the end
    const timers = new Map<string, any>() // sessionID -> poll interval
    const shown = new Map<string, string>() // sessionID -> route stamp already announced
    let lastSession = ""

    const toast = (message: string) => {
      try {
        context?.ui?.toast?.show({
          title: "smart-router",
          message,
          variant: "info",
          duration: 8000,
        })
        log(`toast: ${message}`)
      } catch (error) {
        log(`toast failed: ${error}`)
      }
    }

    // Announce once per session+turn. The route file is rewritten every turn, so
    // a changed updated_at means "this turn's model is known".
    const announce = (sessionID: string, where: string) => {
      const route = readRoute(sessionID)
      const model = modelOf(route)
      if (!model) return false
      const stamp = stampOf(route)
      if (shown.get(sessionID) === stamp) return false // same turn, already announced
      shown.set(sessionID, stamp)
      lastSession = sessionID
      toast(`routed via ${model}`)
      log(`${where} session=${sessionID} stamp=${stamp}`)
      return true
    }

    const stopPoll = (sessionID: string) => {
      const timer = timers.get(sessionID)
      if (timer) {
        clearInterval(timer)
        timers.delete(sessionID)
      }
    }

    // Always toast, even when the start already showed the same route.
    const forceAnnounce = (sessionID: string, where: string) => {
      const route = readRoute(sessionID)
      const model = modelOf(route)
      if (!model) return false
      const stamp = stampOf(route)
      shown.set(sessionID, stamp)
      lastSession = sessionID
      toast(`routed via ${model}`)
      log(`${where} session=${sessionID} stamp=${stamp}`)
      return true
    }

    // The response starts when LiteLLM records the model, which can be before or
    // after any TUI event; poll from turn start so the toast lands as early as
    // possible, then stop as soon as it fired.
    const startPoll = (sessionID: string) => {
      stopPoll(sessionID)
      let tries = 0
      const timer = setInterval(() => {
        tries += 1
        if (tries > POLL_TRIES) {
          stopPoll(sessionID)
          return
        }
        if (announce(sessionID, "start")) stopPoll(sessionID)
      }, POLL_MS)
      timers.set(sessionID, timer)
    }

    // Turn boundaries drive the start/end toasts.
    if (context?.data && typeof context.data.on === "function") {
      const on = context.data.on

      const offStarted = on("session.execution.started", (event: any) => {
        const sessionID = sessionIdFrom(event)
        if (!sessionID) return
        log(`turn start session=${sessionID}`)
        startPoll(sessionID)
      })
      if (typeof offStarted === "function") cleanups.push(offStarted)

      const offEnded = on("session.execution.succeeded", (event: any) => {
        const sessionID = sessionIdFrom(event)
        const eventId = event && typeof event === "object" ? String(event.id ?? "") : ""
        if (!sessionID) return
        if (eventId) {
          if (endSeen.has(eventId)) return // this event was already toasted
          if (endSeen.size > 512) endSeen.clear()
          endSeen.add(eventId)
        }
        stopPoll(sessionID)
        lastSession = sessionID
        // Always toast at the end (the start toast already fired earlier).
        if (!forceAnnounce(sessionID, "end")) log(`end session=${sessionID} no route file`)
      })
      if (typeof offEnded === "function") cleanups.push(offEnded)
    } else {
      log("no data.on on the TUI context")
    }

    // /route - re-show the current session's route on demand.
    const command = () => {
      if (lastSession && readRoute(lastSession)?.model) {
        shown.delete(lastSession) // manual command always answers
        announce(lastSession, "command")
        return
      }
      toast(lastSession ? `no route file for ${lastSession}` : "no session seen yet")
    }

    if (context?.keymap && typeof context.keymap.layer === "function") {
      try {
        // Commands are { id, title, group, bind, run }; the palette dispatches by id.
        const off = (context.keymap as any).layer(() => ({
          commands: [
            {
              id: COMMAND_ID,
              title: "Show litellm route",
              group: "smart-router",
              bind: "ctrl+g",
              run: command,
            },
          ],
        }))
        if (typeof off === "function") cleanups.push(off)
        log("command registered")
      } catch (error) {
        log(`keymap.layer failed: ${error}`)
      }
    } else {
      log("no keymap.layer on the TUI context")
    }

    return () => {
      for (const timer of timers.values()) {
        try {
          clearInterval(timer)
        } catch {}
      }
      timers.clear()
      for (const fn of cleanups) {
        try {
          fn()
        } catch {}
      }
    }
  },
}
