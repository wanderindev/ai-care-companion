/**
 * Minimal Server-Sent Events parser for a `fetch` body (EventSource cannot
 * POST). Follows the WHATWG event-stream rules that matter here: lines end in
 * LF, CRLF or CR (also split across chunks); `:` lines are comments (the
 * server's `: ping` heartbeats); several `data:` lines join with "\n"; one
 * space after the colon is dropped; a blank line dispatches the event.
 */
export function createSseParser(onEvent) {
  let buffer = ''
  let pendingCr = false // a chunk ended in "\r": a following "\n" belongs to it
  let eventName = ''
  let data = []

  function dispatch() {
    if (data.length) onEvent({ event: eventName || 'message', data: data.join('\n') })
    eventName = ''
    data = []
  }

  function line(text) {
    if (text === '') return dispatch()
    if (text.startsWith(':')) return
    const colon = text.indexOf(':')
    const field = colon === -1 ? text : text.slice(0, colon)
    let value = colon === -1 ? '' : text.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') eventName = value
    else if (field === 'data') data.push(value)
  }

  return {
    /** Feeds decoded text; may hold an incomplete last line until the next call. */
    push(text) {
      if (pendingCr && text.startsWith('\n')) text = text.slice(1)
      pendingCr = false
      buffer += text
      let start = 0
      for (let i = 0; i < buffer.length; i++) {
        const ch = buffer[i]
        if (ch !== '\n' && ch !== '\r') continue
        line(buffer.slice(start, i))
        if (ch === '\r') {
          if (i + 1 < buffer.length) {
            if (buffer[i + 1] === '\n') i++
          } else {
            pendingCr = true
          }
        }
        start = i + 1
      }
      buffer = buffer.slice(start)
    },
    /** End of stream: an unterminated last event is still delivered. */
    end() {
      if (buffer) line(buffer)
      buffer = ''
      dispatch()
    },
  }
}
