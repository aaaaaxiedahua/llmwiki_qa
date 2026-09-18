'use client'

import { useParams } from 'next/navigation'
import { useKBStore } from '@/stores'
import { ChatPanel } from '@/components/chat/ChatPanel'

export function ChatMount() {
  const params = useParams<{ slug: string }>()
  const kb = useKBStore((s) => s.knowledgeBases.find((k) => k.slug === params.slug))
  if (!kb) return null
  return <ChatPanel kbId={kb.id} />
}
