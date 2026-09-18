'use client'

import { useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

interface IngestionStatus {
  enabled: boolean
  pending: number
  processing: number
  failed: number
  active: { document_id: string; title: string }[]
}

const POLL_MS = 5000

/** Polls the ingestion queue; shows a badge while docs are being distilled
 *  into wiki pages, and refreshes the wiki tree when the queue drains. */
export function IngestionIndicator() {
  const router = useRouter()
  const [status, setStatus] = useState<IngestionStatus | null>(null)
  const wasActive = useRef(false)

  useEffect(() => {
    let timer: ReturnType<typeof setInterval>
    let cancelled = false

    async function poll() {
      try {
        const resp = await fetch(`${API_URL}/v1/ingestion/status`)
        if (!resp.ok) return
        const s: IngestionStatus = await resp.json()
        if (cancelled || !s.enabled) return
        const active = s.pending + s.processing > 0
        setStatus(s)
        if (wasActive.current && !active) {
          router.refresh() // queue drained — new wiki pages landed
        }
        wasActive.current = active
      } catch {
        // route absent (older server) or offline — stay silent
      }
    }

    poll()
    timer = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [router])

  if (!status || status.pending + status.processing === 0) return null

  const current = status.active[0]?.title
  return (
    <div className="fixed bottom-6 left-6 z-50 flex items-center gap-2 rounded-lg border bg-background px-3 py-2 text-sm shadow-lg">
      <span className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
      <span>
        正在摄入 {status.pending + status.processing} 个文档
        {current ? `：${current}` : '…'}
      </span>
    </div>
  )
}
