'use client'

import * as React from 'react'
import { useRouter, usePathname } from 'next/navigation'
import { AlertCircle, CheckCircle2, ChevronDown, FileText, Loader2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useUploadStore, type UploadItem } from '@/stores'
import { cn } from '@/lib/utils'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const INGESTION_POLL_MS = 5000
const TICK_MS = 1000
const SLOW_PROCESSING_MS = 60_000

interface IngestionEntry {
  document_id: string
  title: string | null
  error: string | null
}

interface IngestionStatus {
  enabled: boolean
  active: IngestionEntry[]
  failed_items?: IngestionEntry[]
}

type IngestionMap = Map<string, { state: 'active' | 'failed'; error: string | null }>

function useIngestionStatus(enabled: boolean): IngestionMap {
  const [map, setMap] = React.useState<IngestionMap>(new Map())

  React.useEffect(() => {
    if (!enabled) {
      setMap(new Map())
      return
    }
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch(`${API_URL}/v1/ingestion/status`)
        if (!res.ok) return
        const data = (await res.json()) as IngestionStatus
        if (cancelled || !data.enabled) return
        const next: IngestionMap = new Map()
        for (const entry of data.active) {
          next.set(entry.document_id, { state: 'active', error: entry.error })
        }
        for (const entry of data.failed_items ?? []) {
          if (!next.has(entry.document_id)) {
            next.set(entry.document_id, { state: 'failed', error: entry.error })
          }
        }
        setMap(next)
      } catch {
        // status endpoint unreachable — keep last known state
      }
    }
    poll()
    const interval = setInterval(poll, INGESTION_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [enabled])

  return map
}

// Ticking clock so elapsed-time labels refresh while anything is in flight.
function useNow(active: boolean): number {
  const [now, setNow] = React.useState(() => Date.now())
  React.useEffect(() => {
    if (!active) return
    const interval = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(interval)
  }, [active])
  return now
}

function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m${s % 60}s`
}

export function UploadProgressPanel({ raisedForConnectionDock = false }: { raisedForConnectionDock?: boolean }) {
  const router = useRouter()
  const pathname = usePathname()
  const items = useUploadStore((s) => s.items)
  const dismiss = useUploadStore((s) => s.dismiss)
  const clearFinished = useUploadStore((s) => s.clearFinished)
  const requestOpenDocument = useUploadStore((s) => s.requestOpenDocument)
  const [collapsed, setCollapsed] = React.useState(false)

  const inFlight = items.filter((item) => item.phase === 'uploading' || item.phase === 'processing').length
  const now = useNow(inFlight > 0)
  const needsIngestion = items.some((item) => item.documentId != null && item.phase !== 'failed')
  const ingestion = useIngestionStatus(needsIngestion)

  const handleView = React.useCallback((item: UploadItem) => {
    if (item.documentNumber == null) return
    requestOpenDocument(item.kbId, item.documentNumber)
    const base = `/wikis/${item.kbSlug}`
    const onTargetWiki = pathname === base || pathname.startsWith(`${base}/`)
    if (!onTargetWiki) router.push(`${base}/files`)
  }, [pathname, requestOpenDocument, router])

  if (items.length === 0) return null

  return (
    <div
      style={{
        bottom: raisedForConnectionDock
          ? 'calc(max(1rem, env(safe-area-inset-bottom)) + 3rem)'
          : 'max(1rem, env(safe-area-inset-bottom))',
        right: 'max(1rem, env(safe-area-inset-right))',
      }}
      className={cn(
        'fixed z-50 w-[min(20rem,calc(100vw-2rem))] overflow-hidden rounded-lg border bg-background shadow-lg transition-[bottom] duration-200',
      )}
    >
      <div className="flex items-center justify-between border-b px-3 py-2">
        <span className="text-sm font-medium">{headerLabel(inFlight, items.length)}</span>
        <div className="flex items-center gap-0.5">
          <Button
            variant="ghost"
            size="icon"
            className="size-6 text-muted-foreground"
            onClick={() => setCollapsed((v) => !v)}
            aria-label={collapsed ? 'Expand' : 'Collapse'}
          >
            <ChevronDown className={`size-4 transition-transform ${collapsed ? '-rotate-180' : ''}`} />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-6 shrink-0 text-muted-foreground"
            onClick={clearFinished}
            disabled={inFlight === items.length}
            aria-label="Clear completed"
          >
            <X className="size-4" />
          </Button>
        </div>
      </div>

      {!collapsed && (
        <ul className="max-h-72 divide-y overflow-y-auto">
          {items.map((item) => (
            <UploadRow
              key={item.id}
              item={item}
              now={now}
              ingestion={item.documentId ? ingestion.get(item.documentId) : undefined}
              onView={handleView}
              onDismiss={dismiss}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

function headerLabel(inFlight: number, total: number): string {
  if (inFlight > 0) return `正在上传 ${inFlight} 个文件`
  return `${total} 个文件已处理完`
}

function UploadRow({
  item,
  now,
  ingestion,
  onView,
  onDismiss,
}: {
  item: UploadItem
  now: number
  ingestion?: { state: 'active' | 'failed'; error: string | null }
  onView: (item: UploadItem) => void
  onDismiss: (id: string) => void
}) {
  const ingesting = item.phase === 'ready' && ingestion?.state === 'active'
  return (
    <li className="flex items-center gap-3 px-3 py-2.5">
      <StatusIcon phase={item.phase} ingesting={ingesting} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm" title={item.filename}>
          {item.filename}
        </p>
        <RowStatus item={item} now={now} ingestion={ingestion} />
      </div>
      <RowAction item={item} onView={onView} onDismiss={onDismiss} />
    </li>
  )
}

function StatusIcon({ phase, ingesting }: { phase: UploadItem['phase']; ingesting: boolean }) {
  if (ingesting) return <Loader2 className="size-4 shrink-0 animate-spin text-muted-foreground" />
  if (phase === 'ready') return <CheckCircle2 className="size-4 shrink-0 text-emerald-600" />
  if (phase === 'failed') return <AlertCircle className="size-4 shrink-0 text-destructive" />
  if (phase === 'processing') return <Loader2 className="size-4 shrink-0 animate-spin text-muted-foreground" />
  return <FileText className="size-4 shrink-0 text-muted-foreground" />
}

function RowStatus({
  item,
  now,
  ingestion,
}: {
  item: UploadItem
  now: number
  ingestion?: { state: 'active' | 'failed'; error: string | null }
}) {
  const elapsed = now - item.phaseStartedAt

  if (item.phase === 'uploading') {
    const pct = Math.round(item.progress * 100)
    return (
      <div className="mt-1.5">
        <div className="flex items-center gap-2">
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-foreground transition-[width] duration-150" style={{ width: `${pct}%` }} />
          </div>
          <span className="text-xs tabular-nums text-muted-foreground">{pct}%</span>
        </div>
        {pct >= 100 && (
          <p className="mt-0.5 text-xs text-muted-foreground">等待服务器响应…</p>
        )}
      </div>
    )
  }

  if (item.phase === 'processing') {
    if (elapsed > SLOW_PROCESSING_MS) {
      return (
        <p className="mt-0.5 text-xs text-amber-600">
          解析耗时较长（{formatElapsed(elapsed)}），可能解析异常
        </p>
      )
    }
    return <p className="mt-0.5 text-xs text-muted-foreground">解析中 · {formatElapsed(elapsed)}</p>
  }

  if (item.phase === 'ready') {
    if (ingestion?.state === 'active') {
      return <p className="mt-0.5 text-xs text-muted-foreground">已解析，生成 wiki 中…</p>
    }
    if (ingestion?.state === 'failed') {
      return (
        <p className="mt-0.5 truncate text-xs text-destructive" title={ingestion.error ?? undefined}>
          Wiki 生成失败{ingestion.error ? `：${ingestion.error}` : ''}
        </p>
      )
    }
    return <p className="mt-0.5 text-xs text-muted-foreground">完成</p>
  }

  return (
    <p className="mt-0.5 truncate text-xs text-destructive" title={item.error ?? undefined}>
      {item.error || '上传失败'}
    </p>
  )
}

function RowAction({
  item,
  onView,
  onDismiss,
}: {
  item: UploadItem
  onView: (item: UploadItem) => void
  onDismiss: (id: string) => void
}) {
  if (item.phase === 'ready') {
    return (
      <Button variant="ghost" size="sm" className="h-7 shrink-0 cursor-pointer px-2 text-xs" onClick={() => onView(item)}>
        查看
      </Button>
    )
  }
  if (item.phase === 'failed') {
    return (
      <Button
        variant="ghost"
        size="icon"
        className="size-6 shrink-0 text-muted-foreground"
        onClick={() => onDismiss(item.id)}
        aria-label="Dismiss"
      >
        <X className="size-4" />
      </Button>
    )
  }
  return null
}
