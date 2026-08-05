import { useCallback, useEffect, useState } from 'react'

import { api } from '../api/client.js'
import { normalizeProviders } from '../api/state.js'

let configuredValidationForPageLoad


function validateConfiguredProvidersOnce() {
  if (!configuredValidationForPageLoad) {
    configuredValidationForPageLoad = api.validateConfiguredProviders()
  }
  return configuredValidationForPageLoad
}


export function useProviders() {
  const [providerState, setProviderState] = useState(() => normalizeProviders({ providers: [] }))
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    const next = normalizeProviders(await api.providers())
    setProviderState(next)
    setLoading(false)
    return next
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer
    const poll = async () => {
      try {
        const next = await refresh()
        if (!cancelled && Object.values(next.providers).some((provider) =>
          ['initializing', 'validating'].includes(provider.state))) {
          timer = setTimeout(poll, 500)
        }
      } catch {
        if (!cancelled) setLoading(false)
      }
    }
    const loadInitialState = async () => {
      try {
        await refresh()
        await validateConfiguredProvidersOnce()
        await poll()
      } catch {
        if (!cancelled) setLoading(false)
      }
    }
    loadInitialState()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [refresh])
  return { ...providerState, loading, refresh }
}
