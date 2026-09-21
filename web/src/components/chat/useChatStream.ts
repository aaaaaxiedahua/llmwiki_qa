'use client'

import * as React from 'react'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export interface Reference {
  num: number
  title: string
  relative_path: string
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  references?: Reference[]
}

export interface ChatSessionEvent {
  id: string
  title: string
}

interface SendOptions {
  mode: 'fast' | 'deep'
  sessionId?: string | null
  history?: Array<{ role: string; content: string }>
}

export function useChatStream(kbId: string) {
  const [messages, setMessages] = React.useState<ChatMessage[]>([])
  const [streaming, setStreaming] = React.useState(false)
  const [stage, setStage] = React.useState('')

  const send = React.useCallback(
    async (question: string, opts: SendOptions): Promise<ChatSessionEvent | null> => {
      const q = question.trim()
      if (!q || streaming) return null
      setStreaming(true)
      setStage('searching')

      setMessages((prev) => [
        ...prev,
        { role: 'user', content: q },
        { role: 'assistant', content: '' },
      ])

      let session: ChatSessionEvent | null = null
      try {
        const body: Record<string, unknown> = { kb_id: kbId, message: q, mode: opts.mode }
        if (opts.sessionId) body.session_id = opts.sessionId
        else if (opts.history) body.history = opts.history

        const resp = await fetch(`${API_URL}/v1/chat/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        if (!resp.ok || !resp.body) {
          const err = await resp.json().catch(() => null)
          throw new Error(err?.detail || `HTTP ${resp.status}`)
        }

        const reader = resp.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        let currentEvent = ''

        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })

          let sep
          while ((sep = buffer.indexOf('\n\n')) >= 0) {
            const raw = buffer.slice(0, sep)
            buffer = buffer.slice(sep + 2)
            let data = ''
            for (const line of raw.split('\n')) {
              if (line.startsWith('event:')) currentEvent = line.slice(6).trim()
              else if (line.startsWith('data:')) data += line.slice(5).trim()
            }
            if (!data) continue
            const parsed = JSON.parse(data)

            if (currentEvent === 'session') {
              session = { id: parsed.id, title: parsed.title }
            } else if (currentEvent === 'status') {
              setStage(parsed.stage)
            } else if (currentEvent === 'delta') {
              setStage('')
              setMessages((prev) => {
                const next = [...prev]
                const last = next[next.length - 1]
                next[next.length - 1] = { ...last, content: last.content + parsed.content }
                return next
              })
            } else if (currentEvent === 'done') {
              if (parsed.session_id && !session) {
                session = { id: parsed.session_id, title: '' }
              }
              setMessages((prev) => {
                const next = [...prev]
                next[next.length - 1] = { ...next[next.length - 1], references: parsed.references }
                return next
              })
            } else if (currentEvent === 'error') {
              throw new Error(parsed.detail)
            }
          }
        }
        return session
      } catch (e) {
        setMessages((prev) => {
          const next = [...prev]
          next[next.length - 1] = {
            ...next[next.length - 1],
            content: `出错了：${e instanceof Error ? e.message : String(e)}`,
          }
          return next
        })
        return null
      } finally {
        setStreaming(false)
        setStage('')
      }
    },
    [kbId, streaming],
  )

  return { messages, setMessages, streaming, stage, send }
}
