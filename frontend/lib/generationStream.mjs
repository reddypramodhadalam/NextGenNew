/** Wait for the existing generation SSE contract to finish, not just its headers. */
export async function readGenerationResult(response) {
  if (!response.ok) {
    let message = `Generation request failed (${response.status})`
    try {
      const body = await response.json()
      message = body?.detail?.message || body?.error?.message || body?.detail || message
    } catch { /* preserve the HTTP error */ }
    throw new Error(String(message))
  }
  if (!response.body) throw new Error('Generation response has no event stream')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let failure = null
  let completed = null
  function consume(block) {
    const lines = block.split('\n')
    const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim()
    const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
    if (!data) return
    const parsed = JSON.parse(data)
    // Kavach api.sse.event_stream uses data: {type, data}; also accept
    // standard named SSE events for backwards-compatible stream consumers.
    const type = event || parsed.type
    const value = event ? parsed : parsed.data
    if (type === 'error') failure = value?.message || 'Generation failed'
    if (type === 'complete') {
      if (!value || typeof value !== 'object') throw new Error('Invalid generation completion event')
      completed = value
    }
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      // Normalize only complete CRLF pairs; a chunk may end at the CR.
      buffer = buffer.replace(/\r\n/g, '\n')
      let boundary
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        consume(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary + 2)
      }
      if (done) {
        if (buffer.trim()) consume(buffer)
        break
      }
    }
  } finally {
    reader.releaseLock()
  }
  if (!completed) throw new Error(failure || 'Generation stream ended before completion')
  const rejected = (completed.results || []).filter(result => result.result !== 'generated')
  if (failure || completed.incomplete || completed.blocked || rejected.length) {
    const message = failure || rejected[0]?.notes || rejected[0]?.message || 'Generation is incomplete; review the test case details.'
    const error = new Error(message)
    error.results = completed.results || []
    throw error
  }
  return completed
}