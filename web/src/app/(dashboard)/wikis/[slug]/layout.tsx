import { ChatMount } from '@/components/chat/ChatMount'
import { IngestionIndicator } from '@/components/ingestion/IngestionIndicator'

export default function KBLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      {children}
      <ChatMount />
      <IngestionIndicator />
    </>
  )
}
