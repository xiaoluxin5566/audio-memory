import { apiRequest } from './client.js'


export function watchJob(jobId, { onEvent, onState, onError }) {
  let source
  const connect = () => {
    source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`)
    source.onmessage = async (event) => {
      onEvent?.(JSON.parse(event.data), event.lastEventId)
    }
    source.onerror = async (error) => {
      try {
        onState?.(await apiRequest(`/jobs/${encodeURIComponent(jobId)}`))
      } catch (refreshError) {
        onError?.(refreshError)
      }
      onError?.(error)
      // Native EventSource reconnects automatically and sends Last-Event-ID.
    }
  }
  connect()
  return () => source?.close()
}
