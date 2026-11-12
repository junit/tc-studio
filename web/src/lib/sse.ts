export interface SSEEvent {
  event: string
  data: string
}

// Event fields survive network reads. A blank line commits the entire event;
// a partial event at EOF is discarded, so it cannot masquerade as completion.
class SSEDecoder {
  private line = ''
  private eventName = ''
  private data: string[] = []
  private skipLineFeed = false

  push(text: string): SSEEvent[] {
    const events: SSEEvent[] = []
    for (const character of text) {
      if (this.skipLineFeed) {
        this.skipLineFeed = false
        if (character === '\n') continue
      }
      if (character !== '\r' && character !== '\n') {
        this.line += character
        continue
      }

      const line = this.line
      this.line = ''
      this.skipLineFeed = character === '\r'
      if (!line) {
        if (this.data.length) {
          events.push({ event: this.eventName || 'message', data: this.data.join('\n') })
        }
        this.eventName = ''
        this.data = []
      } else if (!line.startsWith(':')) {
        const colon = line.indexOf(':')
        const field = colon < 0 ? line : line.slice(0, colon)
        let value = colon < 0 ? '' : line.slice(colon + 1)
        if (value.startsWith(' ')) value = value.slice(1)
        if (field === 'event') this.eventName = value
        if (field === 'data') this.data.push(value)
      }
    }
    return events
  }
}

export async function* readSSEEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<SSEEvent> {
  const reader = body.getReader()
  const textDecoder = new TextDecoder('utf-8')
  const decoder = new SSEDecoder()
  let finished = false
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) {
        finished = true
        yield* decoder.push(textDecoder.decode())
        return
      }
      yield* decoder.push(textDecoder.decode(value, { stream: true }))
    }
  } finally {
    try {
      if (!finished) await reader.cancel().catch(() => undefined)
    } finally {
      reader.releaseLock()
    }
  }
}
