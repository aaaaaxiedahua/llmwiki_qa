'use client'

import * as React from 'react'
import { Database, Loader2, CheckCircle2 } from 'lucide-react'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

interface EmbeddingStatus {
  enabled: boolean
  model: string
  total_chunks: number
  embedded_chunks: number
  backfill_running: boolean
}

export function VectorIndexButton() {
  const [status, setStatus] = React.useState<EmbeddingStatus | null>(null)
  const [error, setError] = React.useState('')

  const poll = React.useCallback(async () => {
    try {
      const resp = await fetch(`${API_URL}/v1/embeddings/status`)
      if (resp.ok) setStatus(await resp.json())
    } catch {
      // API down — keep last state
    }
  }, [])

  React.useEffect(() => {
    poll()
    const interval = setInterval(poll, status?.backfill_running ? 2000 : 10000)
    return () => clearInterval(interval)
  }, [poll, status?.backfill_running])

  if (!status) return null

  const { enabled, total_chunks, embedded_chunks, backfill_running } = status
  const complete = enabled && embedded_chunks >= total_chunks

  async function startBackfill() {
    setError('')
    try {
      const resp = await fetch(`${API_URL}/v1/embeddings/backfill`, { method: 'POST' })
      if (!resp.ok) {
        const body = await resp.json().catch(() => null)
        setError(body?.detail || `构建失败（HTTP ${resp.status}）`)
        return
      }
      setStatus((s) => (s ? { ...s, backfill_running: true } : s))
    } catch {
      setError('无法连接 API 服务')
    }
  }

  return (
    <div className="shrink-0 px-2 pb-1">
      <div className="flex items-center gap-2 px-2.5 py-1.5 text-xs text-muted-foreground">
        <Database className="size-3.5 shrink-0" />
        <span className="flex-1 min-w-0">
          语义索引{enabled ? ` ${embedded_chunks}/${total_chunks}` : ''}
        </span>
        {backfill_running ? (
          <Loader2 className="size-3 animate-spin text-primary" />
        ) : complete ? (
          <CheckCircle2 className="size-3.5 text-green-600" />
        ) : (
          <button
            onClick={startBackfill}
            className="shrink-0 rounded-full border border-border px-2 py-0.5 hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
          >
            构建检索
          </button>
        )}
      </div>
      {error && (
        <div className="truncate px-2.5 pb-1 text-[10px] text-red-500" title={error}>
          {error}
        </div>
      )}
    </div>
  )
}
