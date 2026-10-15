// hooks/useSSE.js
// Shared hook for consuming Server-Sent Events from the backend.
// Used by the new-test-suite flow and any other streaming endpoint.

import { useCallback, useRef } from 'react'
import { API_BASE, currentUserEmail } from '../lib/api'

/**
 * Returns a startStream function that opens an SSE connection to the
 * given path, calls onEvent for each event, and calls onDone when
 * the stream ends (on a 'complete' or 'error' event).
 *
 * Usage:
 *   const { startStream } = useSSE()
 *   await startStream('/v1/test-suites/survey', formData, onEvent, onDone)
 */
export function useSSE() {
  const abortRef = useRef(null)

  const startStream = useCallback(async (path, formData, onEvent, onDone) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    try {
      const response = await fetch(`${API_BASE}${path}`, {
        method: 'POST',
        headers: { 'X-Triggered-By': currentUserEmail() },
        body: formData,
        signal: controller.signal,
      })

      if (!response.ok) {
        onDone?.({ error: `Request failed: ${response.status}` })
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const chunks = buffer.split('\n\n')
        buffer = chunks.pop()

        for (const chunk of chunks) {
          if (!chunk.startsWith('data: ')) continue
          try {
            const payload = JSON.parse(chunk.slice(6))
            onEvent?.(payload)
            if (payload.type === 'complete' || payload.type === 'error') {
              onDone?.(payload.data)
              return
            }
          } catch {
            // Malformed SSE chunk — skip it
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        onDone?.({ error: err.message })
      }
    }
  }, [])

  const cancel = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return { startStream, cancel }
}
