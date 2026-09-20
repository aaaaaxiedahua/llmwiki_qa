'use client'

import * as React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Loader2, MessageCircle, X, SendHorizonal } from 'lucide-react'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const isLocal = process.env.NEXT_PUBLIC_MODE === 'local'

interface Reference {
  num: number
  title: string
  relative_path: string
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  references?: Reference[]
}

const STAGE_LABELS: Record<string, string> = {
  searching: '正在检索知识库…',
  retrieved: '已找到相关页面',
  generating: '正在生成回答…',
}

export function ChatPanel({ kbId }: { kbId: string }) {
  const [open, setOpen] = React.useState(false)
  const [enabled, setEnabled] = React.useState<boolean | null>(null)
  const [mode, setMode] = React.useState<'fast' | 'deep'>('deep')
  const [messages, setMessages] = React.useState<Message[]>([])
  const [input, setInput] = React.useState('')
  const [streaming, setStreaming] = React.useState(false)
  const [stage, setStage] = React.useState('')
  const bottomRef = React.useRef<HTMLDivElement>(null)

  React.useEffect(() => {
    fetch(`${API_URL}/v1/chat/status`)
      .then((r) => r.json())
      .then((d) => setEnabled(d.enabled))
      .catch(() => setEnabled(false))
  }, [])

  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, stage])

  async function send() {
    const question = input.trim()
    if (!question || streaming) return
    setInput('')
    setStreaming(true)
    setStage('searching')

    const history = messages.slice(-10).map((m) => ({ role: m.role, content: m.content }))
    setMessages((prev) => [...prev, { role: 'user', content: question }, { role: 'assistant', content: '' }])

    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      const resp = await fetch(`${API_URL}/v1/chat/stream`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ kb_id: kbId, message: question, history, mode }),
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

          if (currentEvent === 'status') {
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
    } catch (e) {
      setMessages((prev) => {
        const next = [...prev]
        next[next.length - 1] = {
          ...next[next.length - 1],
          content: `出错了：${e instanceof Error ? e.message : String(e)}`,
        }
        return next
      })
    } finally {
      setStreaming(false)
      setStage('')
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-6 right-6 z-50 flex size-12 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg hover:opacity-90"
        aria-label="打开问答"
      >
        <MessageCircle className="size-5" />
      </button>
    )
  }

  return (
    <div className="fixed bottom-6 right-6 z-50 flex h-[70vh] w-96 flex-col rounded-xl border border-border bg-background shadow-xl">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="text-sm font-medium">知识库问答</div>
        <button onClick={() => setOpen(false)} aria-label="关闭">
          <X className="size-4 text-muted-foreground" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
        {enabled === false && (
          <div className="rounded-lg border border-dashed border-border p-3 text-sm text-muted-foreground">
            问答未启用。请在 <code>.env</code> 中配置
            <code> LLM_BASE_URL / LLM_API_KEY / LLM_MODEL</code> 后重启服务。
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'text-right' : ''}>
            <div
              className={
                m.role === 'user'
                  ? 'inline-block rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground'
                  : 'prose prose-sm dark:prose-invert max-w-none text-sm'
              }
            >
              {m.role === 'user' ? (
                m.content
              ) : (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
              )}
            </div>
            {m.references && m.references.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1">
                {m.references.map((r) => (
                  <span
                    key={r.num}
                    title={r.relative_path}
                    className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                  >
                    [{r.num}] {r.title}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
        {stage && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" />
            {STAGE_LABELS[stage] ?? stage}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t border-border p-3">
        <div className="mb-2 flex">
          <div className="flex rounded-full bg-muted p-0.5 text-xs">
            <button
              onClick={() => setMode('fast')}
              className={`rounded-full px-3 py-1 transition-colors ${
                mode === 'fast'
                  ? 'bg-background font-medium text-foreground shadow-sm'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              快速
            </button>
            <button
              onClick={() => setMode('deep')}
              className={`rounded-full px-3 py-1 transition-colors ${
                mode === 'deep'
                  ? 'bg-background font-medium text-foreground shadow-sm'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              深度
            </button>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && !e.nativeEvent.isComposing && send()}
            placeholder={enabled ? '向这个知识库提问…' : '请先配置 LLM'}
            disabled={!enabled || streaming}
            className="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm disabled:opacity-50"
          />
          <button
            onClick={send}
            disabled={!enabled || streaming || !input.trim()}
            className="rounded-lg bg-primary p-2 text-primary-foreground disabled:opacity-40"
            aria-label="发送"
          >
            <SendHorizonal className="size-4" />
          </button>
        </div>
      </div>
    </div>
  )
}
