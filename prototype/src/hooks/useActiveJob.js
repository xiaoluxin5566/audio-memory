import { useEffect } from 'react'

import { api } from '../api/client.js'


export function useActiveJob(jobId, onUpdate, onComplete) {
  useEffect(() => {
    if (!jobId) return undefined
    let cancelled = false
    const poll = async () => {
      try {
        const job = await api.job(jobId)
        if (cancelled) return
        onUpdate(job)
        if (job.stage === 'completed') {
          onComplete(job)
          return
        }
        if (!['failed', 'interrupted', 'cancelled'].includes(job.stage)) {
          setTimeout(poll, 1200)
        }
      } catch {
        if (!cancelled) setTimeout(poll, 1800)
      }
    }
    poll()
    return () => { cancelled = true }
  }, [jobId, onUpdate, onComplete])
}

