'use client'

import { createClient } from '@/lib/supabase/client'
import { useUserStore } from '@/stores/useUserStore'

const isLocal = process.env.NEXT_PUBLIC_MODE === 'local'

let refreshInFlight: Promise<string | null> | null = null

async function performRefresh(staleToken?: string): Promise<string | null> {
  const userStore = useUserStore.getState()
  const currentToken = userStore.accessToken

  // Supabase may have refreshed the session while the failed request was in
  // flight. Prefer that newer token instead of rotating the refresh token
  // again.
  if (staleToken && currentToken && currentToken !== staleToken) {
    return currentToken
  }

  const supabase = createClient()
  const { data, error } = await supabase.auth.refreshSession()
  if (!error && data.session?.access_token) {
    const token = data.session.access_token
    useUserStore.getState().setAccessToken(token)
    return token
  }

  // A browser-client auto refresh can win the race with refreshSession(). In
  // that case the stored Supabase session is usable, but never hand the caller
  // the same token that the API just rejected.
  const { data: current } = await supabase.auth.getSession()
  const token = current.session?.access_token ?? null
  if (token && (!staleToken || token !== staleToken)) {
    useUserStore.getState().setAccessToken(token)
    return token
  }

  return null
}

export function refreshAccessToken(staleToken?: string): Promise<string | null> {
  if (isLocal) {
    useUserStore.getState().setAccessToken('local')
    return Promise.resolve('local')
  }

  const currentToken = useUserStore.getState().accessToken
  if (staleToken && currentToken && currentToken !== staleToken) {
    return Promise.resolve(currentToken)
  }

  if (!refreshInFlight) {
    refreshInFlight = performRefresh(staleToken).finally(() => {
      refreshInFlight = null
    })
  }

  return refreshInFlight
}
