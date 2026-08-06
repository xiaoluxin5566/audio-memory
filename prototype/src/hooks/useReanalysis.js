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
  const requestGeneration = useRef({ epoch: 0, current: 0, preview: 0 })

  function beginRequest(kind) {
    const generation = requestGeneration.current
    generation[kind] += 1
    return { epoch: generation.epoch, request: generation[kind] }
  }

  function isLatestRequest(kind, token) {
    const generation = requestGeneration.current
    return generation.epoch === token.epoch && generation[kind] === token.request
  }

  function invalidateRequests() {
    const generation = requestGeneration.current
    generation.epoch += 1
    generation.current += 1
    generation.preview += 1
  }

  const refreshCurrent = useCallback(async () => {
    const token = beginRequest('current')
    const batch = await api.currentReanalysis()
    if (!isLatestRequest('current', token)) return null
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
    const token = beginRequest('preview')
    setLoadingPreview(true)
    setError('')
    setPreview(null)
    try {
      const next = normalizeReanalysisPreview(await api.reanalysisPreview())
      if (!isLatestRequest('preview', token)) return null
      setPreview(next)
      return next
    } catch (nextError) {
      if (!isLatestRequest('preview', token)) return null
      setError(nextError.message)
      throw nextError
    } finally {
      if (isLatestRequest('preview', token)) setLoadingPreview(false)
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

  const clearState = useCallback(() => {
    invalidateRequests()
    currentRef.current = null
    setCurrent(null)
    setPreview(null)
    setError('')
  }, [])

  const dismissPreview = useCallback(() => {
    invalidateRequests()
    setPreview(null)
    setError('')
    setLoadingPreview(false)
  }, [])

  return { current, preview, loadingPreview, error, refreshCurrent, loadPreview, start, stop, resume, retryProfile, clearState, dismissPreview }
}
