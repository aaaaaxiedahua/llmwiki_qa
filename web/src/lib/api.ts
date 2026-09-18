import { refreshAccessToken } from '@/lib/auth-token'
import { useUserStore } from '@/stores/useUserStore'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const WS_URL = API_URL.replace(/^http/, 'ws')
const isLocal = process.env.NEXT_PUBLIC_MODE === 'local'

/** Thrown by apiFetch on non-2xx responses. Callers can branch on `.status`
 *  for clean retry logic (e.g. 409 conflict reconciliation). */
export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, message: string, detail: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError
}

function withRequestTimeout(signal: AbortSignal | null | undefined, timeoutMs: number) {
  const controller = new AbortController()
  let timedOut = false
  let timeoutId: ReturnType<typeof setTimeout> | undefined

  const abortFromCaller = () => controller.abort(signal?.reason)

  if (signal?.aborted) {
    abortFromCaller()
  } else {
    signal?.addEventListener('abort', abortFromCaller, { once: true })
    timeoutId = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, timeoutMs)
  }

  return {
    signal: controller.signal,
    didTimeout: () => timedOut,
    cleanup: () => {
      if (timeoutId) clearTimeout(timeoutId)
      signal?.removeEventListener('abort', abortFromCaller)
    },
  }
}

export async function apiFetch<T>(
  path: string,
  token: string,
  options?: RequestInit,
): Promise<T> {
  const { signal: callerSignal, ...fetchOptions } = options ?? {}
  const timeout = withRequestTimeout(callerSignal, 15000)
  const request = (requestToken: string) => {
    const headers = new Headers(fetchOptions.headers)
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')

    // In local mode, skip Authorization header (API doesn't check it)
    if (!isLocal && requestToken) {
      headers.set('Authorization', `Bearer ${requestToken}`)
    }

    return fetch(`${API_URL}${path}`, {
      ...fetchOptions,
      headers,
      signal: timeout.signal,
    })
  }

  try {
    // A component may have captured an older token just before Supabase's
    // TOKEN_REFRESHED event updated the store.
    let requestToken = token
    const storedToken = useUserStore.getState().accessToken
    if (!isLocal && storedToken && storedToken !== requestToken) {
      requestToken = storedToken
    }

    let res = await request(requestToken)

    // Tabs can resume before Supabase's background auto-refresh runs. Refresh
    // once on an actual authentication failure, then replay the request with
    // the new access token. Authorization failures (403) are not retried.
    if (!isLocal && res.status === 401) {
      const refreshedToken = await refreshAccessToken(requestToken).catch(() => null)
      if (refreshedToken && refreshedToken !== requestToken) {
        requestToken = refreshedToken
        res = await request(requestToken)
      }
    }

    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      const message =
        typeof body?.detail === 'string'
          ? body.detail
          : `API error: ${res.status}`
      throw new ApiError(res.status, message, body)
    }
    if (res.status === 204) return undefined as T
    return res.json()
  } catch (err) {
    if (timeout.didTimeout()) throw new Error('API request timeout')
    throw err
  } finally {
    timeout.cleanup()
  }
}

export function getDocumentsWsUrl(kbId: string): string {
  return `${WS_URL}/v1/ws/documents/${kbId}`
}
