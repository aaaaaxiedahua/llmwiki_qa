import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useChatStream } from './useChatStream'

function sseResponse(frames: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(frames))
      controller.close()
    },
  })
  return new Response(stream, { status: 200 })
}

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useChatStream', () => {
  it('streams deltas into the last assistant message and applies references on done', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      sseResponse(
        sse('status', { stage: 'searching' }) +
        sse('delta', { content: '你好' }) +
        sse('delta', { content: '世界' }) +
        sse('done', { references: [{ num: 1, title: 'P', relative_path: 'wiki/p.md' }] }),
      ),
    ))

    const { result } = renderHook(() => useChatStream('kb1'))
    await act(async () => {
      await result.current.send('问题', { mode: 'fast', history: [] })
    })

    expect(result.current.messages).toEqual([
      { role: 'user', content: '问题' },
      {
        role: 'assistant',
        content: '你好世界',
        references: [{ num: 1, title: 'P', relative_path: 'wiki/p.md' }],
      },
    ])
    expect(result.current.streaming).toBe(false)
  })

  it('returns the session event so a first message can establish a session', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse(
        sse('session', { id: 's1', title: '第一个问题' }) +
        sse('delta', { content: '答' }) +
        sse('done', { session_id: 's1', references: [] }),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useChatStream('kb1'))
    let session: unknown
    await act(async () => {
      session = await result.current.send('第一个问题', { mode: 'deep' })
    })

    expect(session).toEqual({ id: 's1', title: '第一个问题' })
    // no sessionId/history → body omits both so the server auto-creates
    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect(body).toMatchObject({ kb_id: 'kb1', message: '第一个问题', mode: 'deep' })
    expect(body).not.toHaveProperty('session_id')
    expect(body).not.toHaveProperty('history')
  })

  it('passes session_id when continuing a persisted session', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse(sse('delta', { content: 'ok' }) + sse('done', { session_id: 's9', references: [] })),
    )
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useChatStream('kb1'))
    await act(async () => {
      await result.current.send('追问', { mode: 'deep', sessionId: 's9' })
    })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect(body.session_id).toBe('s9')
  })

  it('surfaces server error events inside the assistant message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      sseResponse(sse('error', { detail: 'boom' })),
    ))

    const { result } = renderHook(() => useChatStream('kb1'))
    await act(async () => {
      await result.current.send('问题', { mode: 'fast', history: [] })
    })

    await waitFor(() => {
      expect(result.current.messages[1].content).toContain('出错了')
      expect(result.current.messages[1].content).toContain('boom')
    })
  })
})
