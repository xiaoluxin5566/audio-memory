import { useCallback, useEffect, useState } from 'react'

import { api } from '../api/client.js'
import { normalizeProviders } from '../api/state.js'


export function useProviders() {
  const [providerState, setProviderState] = useState(() => normalizeProviders({ providers: [] }))
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    const next = normalizeProviders(await api.providers())
    setProviderState(next)
    setLoading(false)
    return next
  }, [])

  useEffect(() => { refresh().catch(() => setLoading(false)) }, [refresh])
  return { ...providerState, loading, refresh }
}

