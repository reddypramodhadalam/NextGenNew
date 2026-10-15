import { useEffect, useState, useSyncExternalStore } from 'react'

export class WorkflowCancelledError extends Error {
  constructor() {
    super('This workflow is stopping; no further work can start.')
    this.name = 'WorkflowCancelledError'
  }
}

/** Flow-scoped capability, held only in memory. This is not user authentication. */
export function createWorkflowCancellationController({
  apiBase,
  fetchImpl = (...args) => fetch(...args),
  schedule = (fn, ms) => setTimeout(fn, ms),
  unschedule = id => clearTimeout(id),
  requestTimeoutMs = 15000,
  pollIntervalMs = 1000,
}) {
  let snapshot = { state: 'active', error: null }
  let flow = null
  let initialization = null // Keep even a rejected promise: never silently create another flow.
  let requested = false
  let live = true
  let pollTimer = null
  let attempt = null
  let epoch = 0
  const listeners = new Set()
  const controls = new Set()
  const transports = new Set()
  const readers = new Set()
  const responses = new WeakMap()

  const publish = patch => {
    snapshot = { ...snapshot, ...patch }
    if (live) listeners.forEach(listener => listener())
  }
  const isCancelled = () => requested || !live
  const assertActive = () => {
    if (isCancelled()) throw new WorkflowCancelledError()
  }
  const guard = callback => (...args) => {
    if (!isCancelled()) return callback(...args)
  }
  const clearPoll = () => {
    if (pollTimer !== null) unschedule(pollTimer)
    pollTimer = null
  }

  // Bound control requests, including reading their bodies. A timeout is never an acknowledgement.
  const controlJson = async (url, options = {}) => {
    const abort = new AbortController()
    let timer
    let stop
    const interrupted = new Promise((_, reject) => {
      stop = () => {
        abort.abort()
        reject(new Error('Cancellation confirmation unavailable.'))
      }
      timer = schedule(stop, requestTimeoutMs)
    })
    controls.add(stop)
    try {
      return await Promise.race([
        (async () => {
          const response = await fetchImpl(url, { ...options, signal: abort.signal, cache: 'no-store' })
          if (!response.ok) throw new Error('Workflow control request failed.')
          return response.json()
        })(),
        interrupted,
      ])
    } finally {
      unschedule(timer)
      controls.delete(stop)
    }
  }

  const initialize = () => {
    if (!initialization) {
      initialization = controlJson(`${apiBase}/v1/test-suites/flows`, { method: 'POST' })
        .then(data => {
          if (!data?.flow_id || !data?.control_key || data.state !== 'active' || data.active_operations !== 0) {
            throw new Error('Could not establish workflow tracking. No action was started.')
          }
          flow = { id: data.flow_id, key: data.control_key }
          return flow
        })
    }
    return initialization
  }

  const releaseResponse = response => {
    const transport = responses.get(response)
    if (transport) transports.delete(transport)
    responses.delete(response)
  }
  const trackReader = reader => {
    if (snapshot.state === 'cancelled') {
      Promise.resolve().then(() => reader.cancel()).catch(() => {})
      return () => {}
    }
    readers.add(reader)
    return () => readers.delete(reader)
  }

  const request = async (url, options = {}) => {
    assertActive()
    const current = await initialize()
    assertActive() // Cancellation during initialization must not launch the action.
    const headers = new Headers(options.headers)
    headers.set('X-Workflow-Id', current.id)
    headers.set('X-Workflow-Key', current.key)
    const transport = new AbortController()
    transports.add(transport)
    try {
      // Do not forward caller abort signals: stopping a connection is not stopping server work.
      const response = await fetchImpl(url, { ...options, headers, signal: transport.signal })
      assertActive()
      const wrapped = new Proxy(response, {
        get(target, property) {
          if (['json', 'text', 'blob', 'arrayBuffer', 'formData'].includes(property)) {
            return async (...args) => {
              try {
                const result = await target[property](...args)
                assertActive() // Suppress late JSON results as well as late SSE callbacks.
                return result
              } finally {
                releaseResponse(wrapped)
              }
            }
          }
          const value = Reflect.get(target, property, target)
          return typeof value === 'function' ? value.bind(target) : value
        },
      })
      responses.set(wrapped, transport)
      return wrapped
    } catch (error) {
      // Keep late response transports until backend acknowledgement, not merely until fetch resolves.
      if (!requested) transports.delete(transport)
      throw error
    }
  }

  const confirmed = () => {
    clearPoll()
    // Only this backend-confirmed path may close active work transports/readers.
    transports.forEach(transport => transport.abort())
    transports.clear()
    readers.forEach(reader => { Promise.resolve().then(() => reader.cancel()).catch(() => {}) })
    readers.clear()
    publish({ state: 'cancelled', error: null })
  }

  const runAttempt = (sendCancel) => {
    if (attempt) return attempt
    const currentEpoch = epoch
    const work = async () => {
      try {
        if (!initialization) {
          // Only a never-started, local configuration can exit without a server acknowledgement.
          confirmed()
          return
        }
        await initialization
        if (!live || currentEpoch !== epoch) return
        const data = await controlJson(
          `${apiBase}/v1/test-suites/flows/${encodeURIComponent(flow.id)}${sendCancel ? '/cancel' : ''}`,
          { method: sendCancel ? 'POST' : 'GET', headers: { 'X-Workflow-Key': flow.key } },
        )
        if (!live || currentEpoch !== epoch) return
        if (data?.state === 'cancelled' && data.active_operations === 0) {
          confirmed()
        } else if (data?.state === 'cancelling' && Number.isInteger(data.active_operations) && data.active_operations >= 0) {
          publish({ state: 'cancelling', error: null })
          pollTimer = schedule(() => {
            pollTimer = null
            if (live && snapshot.state === 'cancelling') void runAttempt(false)
          }, pollIntervalMs)
        } else {
          throw new Error('Cleanup is not confirmed.')
        }
      } catch {
        if (live && currentEpoch === epoch) {
          publish({
            state: 'cancelling',
            error: 'Stopping and cleanup are not confirmed. Retry confirmation. If the server restarted or the flow expired, confirmation may be unavailable; do not assume the work stopped.',
          })
        }
      }
    }
    // Defer work so repeated calls always share the same attempt, including the local exit path.
    attempt = Promise.resolve().then(work).finally(() => {
      if (currentEpoch === epoch) attempt = null
    })
    return attempt
  }

  const cancel = () => {
    if (requested) return attempt || Promise.resolve()
    requested = true // Synchronous fence, before React renders or any await.
    publish({ state: 'cancelling', error: null })
    return runAttempt(true)
  }
  const retry = () => {
    if (!requested || snapshot.state === 'cancelled' || !live) return Promise.resolve()
    clearPoll()
    publish({ state: 'cancelling', error: null })
    return runAttempt(true)
  }
  const deactivate = () => {
    live = false
    epoch += 1
    clearPoll()
    controls.forEach(stop => stop())
    controls.clear()
    attempt = null
    // Never auto-cancel server work or abort its streams on navigation/unmount.
  }
  const activate = () => {
    live = true
    if (requested && snapshot.state === 'cancelling' && !snapshot.error) void runAttempt(true)
  }

  return {
    request, cancel, retry, isCancelled, guard, releaseResponse, trackReader, activate, deactivate,
    getSnapshot: () => snapshot,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener) },
  }
}

export default function useWorkflowCancellation(apiBase) {
  // API base is fixed for this mounted wizard; controller methods retain stable identities.
  const [controller] = useState(() => createWorkflowCancellationController({ apiBase }))
  const status = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.activate()
    return controller.deactivate
  }, [controller])
  return { ...controller, ...status }
}