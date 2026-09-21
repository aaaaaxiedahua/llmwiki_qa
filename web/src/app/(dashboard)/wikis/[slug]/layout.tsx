import { IngestionIndicator } from '@/components/ingestion/IngestionIndicator'

export default function KBLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      {children}
      <IngestionIndicator />
    </>
  )
}
