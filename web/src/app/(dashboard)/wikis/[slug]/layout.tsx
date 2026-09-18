import { ChatMount } from '@/components/chat/ChatMount'

export default function KBLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      {children}
      <ChatMount />
    </>
  )
}
