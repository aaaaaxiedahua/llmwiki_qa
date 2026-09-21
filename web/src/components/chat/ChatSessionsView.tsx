'use client'

import * as React from 'react'
import { Loader2, MessageSquare, Pencil, Plus, Trash2, Check, X } from 'lucide-react'
import { useChatStream } from '@/components/chat/useChatStream'
import type { ChatMessage } from '@/components/chat/useChatStream'
import { ChatComposer, ChatMessages } from '@/components/chat/ChatConversation'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

interface SessionItem {
  id: string
  title: string
  preview: string
  updated_at: string
}

export function ChatSessionsView({ kbId }: { kbId: string }) {
  const [sessions, setSessions] = React.useState<SessionItem[]>([])
  const [loadingSessions, setLoadingSessions] = React.useState(true)
  const [activeId, setActiveId] = React.useState<string | null>(null)
  const [loadingMessages, setLoadingMessages] = React.useState(false)
  const [mode, setMode] = React.useState<'fast' | 'deep'>('deep')
  const [editingId, setEditingId] = React.useState<string | null>(null)
  const [editingTitle, setEditingTitle] = React.useState('')
  const { messages, setMessages, streaming, stage, send } = useChatStream(kbId)

  const refreshSessions = React.useCallback(async () => {
    try {
      const resp = await fetch(`${API_URL}/v1/chat/sessions?kb_id=${kbId}`)
      if (resp.ok) setSessions(await resp.json())
    } catch {
      // list failure leaves the current list as-is
    } finally {
      setLoadingSessions(false)
    }
  }, [kbId])

  React.useEffect(() => {
    void refreshSessions()
  }, [refreshSessions])

  async function openSession(id: string) {
    if (id === activeId || streaming) return
    setActiveId(id)
    setLoadingMessages(true)
    try {
      const resp = await fetch(`${API_URL}/v1/chat/sessions/${id}/messages`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = (await resp.json()) as Array<ChatMessage & { created_at?: string }>
      setMessages(
        data.map((m) => ({ role: m.role, content: m.content, references: m.references })),
      )
    } catch {
      setMessages([{ role: 'assistant', content: '加载历史消息失败' }])
    } finally {
      setLoadingMessages(false)
    }
  }

  function startNewSession() {
    if (streaming) return
    setActiveId(null)
    setMessages([])
  }

  async function handleSend(question: string) {
    const session = await send(question, { mode, sessionId: activeId })
    if (session?.id && !activeId) setActiveId(session.id)
    void refreshSessions()
  }

  async function commitRename(id: string) {
    const title = editingTitle.trim()
    setEditingId(null)
    if (!title) return
    const resp = await fetch(`${API_URL}/v1/chat/sessions/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    })
    if (resp.ok) {
      setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title } : s)))
    }
  }

  async function removeSession(id: string) {
    if (!window.confirm('删除这个会话及其全部消息？')) return
    const resp = await fetch(`${API_URL}/v1/chat/sessions/${id}`, { method: 'DELETE' })
    if (resp.ok || resp.status === 204) {
      setSessions((prev) => prev.filter((s) => s.id !== id))
      if (activeId === id) startNewSession()
    }
  }

  return (
    <div className="flex h-full">
      <div className="flex w-60 shrink-0 flex-col border-r border-border">
        <div className="border-b border-border p-2">
          <button
            onClick={startNewSession}
            className="flex w-full items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
          >
            <Plus className="size-3.5" />
            新会话
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-1.5 space-y-0.5">
          {loadingSessions ? (
            <div className="flex justify-center py-6">
              <Loader2 className="size-4 animate-spin text-muted-foreground" />
            </div>
          ) : sessions.length === 0 ? (
            <p className="px-2 py-6 text-center text-xs text-muted-foreground">
              还没有会话，发出第一个问题即自动创建
            </p>
          ) : (
            sessions.map((s) => (
              <div
                key={s.id}
                className={`group flex items-start gap-1 rounded-md px-2 py-1.5 text-sm cursor-pointer transition-colors ${
                  s.id === activeId
                    ? 'bg-accent text-foreground'
                    : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground'
                }`}
                onClick={() => void openSession(s.id)}
              >
                {editingId === s.id ? (
                  <div
                    className="flex flex-1 items-center gap-1"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <input
                      autoFocus
                      value={editingTitle}
                      onChange={(e) => setEditingTitle(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') void commitRename(s.id)
                        if (e.key === 'Escape') setEditingId(null)
                      }}
                      className="min-w-0 flex-1 rounded border border-input bg-background px-1.5 py-0.5 text-xs"
                    />
                    <button onClick={() => void commitRename(s.id)} aria-label="确认重命名">
                      <Check className="size-3.5" />
                    </button>
                    <button onClick={() => setEditingId(null)} aria-label="取消重命名">
                      <X className="size-3.5" />
                    </button>
                  </div>
                ) : (
                  <>
                    <div className="min-w-0 flex-1">
                      <div className="truncate">{s.title}</div>
                      {s.preview && (
                        <div className="truncate text-xs text-muted-foreground/70">
                          {s.preview}
                        </div>
                      )}
                    </div>
                    <div className="hidden shrink-0 items-center gap-0.5 group-hover:flex">
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          setEditingId(s.id)
                          setEditingTitle(s.title)
                        }}
                        aria-label="重命名会话"
                        className="rounded p-0.5 hover:bg-background"
                      >
                        <Pencil className="size-3" />
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          void removeSession(s.id)
                        }}
                        aria-label="删除会话"
                        className="rounded p-0.5 hover:bg-background hover:text-destructive"
                      >
                        <Trash2 className="size-3" />
                      </button>
                    </div>
                  </>
                )}
              </div>
            ))
          )}
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        {loadingMessages ? (
          <div className="flex flex-1 items-center justify-center">
            <Loader2 className="size-5 animate-spin text-muted-foreground" />
          </div>
        ) : messages.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 text-muted-foreground">
            <MessageSquare className="size-8 opacity-30" />
            <p className="text-sm">向知识库提问，开始一个新会话</p>
          </div>
        ) : (
          <ChatMessages messages={messages} stage={stage} />
        )}
        <ChatComposer
          mode={mode}
          onModeChange={setMode}
          onSend={(q) => void handleSend(q)}
          disabled={false}
          streaming={streaming}
          placeholder="向这个知识库提问…"
        />
      </div>
    </div>
  )
}
