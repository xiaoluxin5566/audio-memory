const API_BASE = '/api'


export async function apiRequest(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      Accept: 'application/json',
      ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...options.headers,
    },
  })
  const payload = response.status === 204 ? null : await response.json()
  if (!response.ok) {
    const detail = payload?.detail ?? {}
    const error = new Error(detail.message ?? detail ?? '请求失败')
    error.code = detail.code ?? 'request_failed'
    error.fileId = detail.file_id ?? null
    error.status = response.status
    throw error
  }
  return payload
}

