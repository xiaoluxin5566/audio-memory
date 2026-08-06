import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client.js'
import { isActiveReanalysis, normalizeReanalysisPreview } from '../api/state.js'

function actionKey() {
  return crypto.randomUUID()
}

export function useReanalysis() {
  const [current, setCurrent] = useState(null)
  const [preview, setPreview] = useState(null)
  const [loadingPreview, setLoadingPreview] = useState(false)
  const [error, setError] = useState('')
  const currentRef = useRef(null)

  const refreshCurrent = useCallback(async () => {
    const batch = await api.currentReanalysis()
    currentRef.current = batch
    setCurrent(batch)
    return batch
  }, [])

  useEffect(() => {
    refreshCurrent().catch(() => {})
  }, [refreshCurrent])

  useEffect(() => {
    if (!isActiveReanalysis(current)) return undefined
    const timer = setInterval(() => refreshCurrent().catch(() => {}), 1200)
    return () => clearInterval(timer)
  }, [current?.status, refreshCurrent])

  const loadPreview = useCallback(async () => {
    setLoadingPreview(true)
    setError('')
    try {
      const next = normalizeReanalysisPreview(await api.reanalysisPreview())
      setPreview(next)
      return next
    } catch (nextError) {
      setError(nextError.message)
      throw nextError
    } finally {
      setLoadingPreview(false)
    }
  }, [])

  const start = useCallback(async (previewToken) => {
    const batch = await api.createReanalysis(previewToken, actionKey())
    currentRef.current = batch
    setCurrent(batch)
    return batch
  }, [])
  const stop = useCallback(async () => {
    if (!currentRef.current?.id) return null
    const batch = await api.stopReanalysis(currentRef.current.id, actionKey())
    currentRef.current = batch
    setCurrent(batch)
    return batch
  }, [])
  const resume = useCallback(async () => {
    if (!currentRef.current?.id) return null
    const batch = await api.resumeReanalysis(currentRef.current.id, actionKey())
    currentRef.current = batch
    setCurrent(batch)
    return batch
  }, [])
  const retryProfile = useCallback(async () => {
    if (!currentRef.current?.id) return null
    const batch = await api.retryReanalysisProfile(currentRef.current.id, actionKey())
    currentRef.current = batch
    setCurrent(batch)
    return batch
  }, [])

  return { current, preview, loadingPreview, error, refreshCurrent, loadPreview, start, stop, resume, retryProfile }
}
