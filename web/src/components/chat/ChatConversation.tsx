'use client'

import * as React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Loader2, SendHorizonal } from 'lucide-react'
import type { ChatMessage } from '@/components/chat/useChatStream'

const STAGE_LABELS: Record<string, string> = {
  searching: '正在检索知识库…',
  retrieved: '已找到相关页面',
  generating: '正在生成回答…',
}

export function ChatMessages({
  messages,
  stage,
}: {
  messages: ChatMessage[]
  stage: string
}) {
  const bottomRef = React.useRef<HTMLDivElement>(null)

  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, stage])

  return (
    <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
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
  )
}

export function ChatComposer({
  mode,
  onModeChange,
  onSend,
  disabled,
  streaming,
  placeholder,
}: {
  mode: 'fast' | 'deep'
  onModeChange: (mode: 'fast' | 'deep') => void
  onSend: (question: string) => void
  disabled: boolean
  streaming: boolean
  placeholder: string
}) {
  const [input, setInput] = React.useState('')

  const submit = () => {
    const q = input.trim()
    if (!q || streaming || disabled) return
    setInput('')
    onSend(q)
  }

  return (
    <div className="border-t border-border p-3">
      <div className="mb-2 flex">
        <div className="flex rounded-full bg-muted p-0.5 text-xs">
          <button
            onClick={() => onModeChange('fast')}
            className={`rounded-full px-3 py-1 transition-colors ${
              mode === 'fast'
                ? 'bg-background font-medium text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            快速
          </button>
          <button
            onClick={() => onModeChange('deep')}
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
          onKeyDown={(e) => e.key === 'Enter' && !e.nativeEvent.isComposing && submit()}
          placeholder={placeholder}
          disabled={disabled || streaming}
          className="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm disabled:opacity-50"
        />
        <button
          onClick={submit}
          disabled={disabled || streaming || !input.trim()}
          className="rounded-lg bg-primary p-2 text-primary-foreground disabled:opacity-40"
          aria-label="发送"
        >
          <SendHorizonal className="size-4" />
        </button>
      </div>
    </div>
  )
}
