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
  let payload = null
  let responseText = ''
  if (response.status !== 204) {
    responseText = await response.text()
    if (responseText) {
      try {
        payload = JSON.parse(responseText)
      } catch {
        if (response.ok) {
          const error = new Error('本地服务返回了无法识别的内容')
          error.code = 'invalid_response'
          error.status = response.status
          throw error
        }
      }
    }
  }
  if (!response.ok) {
    const detail = payload?.detail ?? {}
    const fallback = response.status >= 500
      ? '本地服务内部错误，请重试或运行诊断'
      : responseText || '请求失败'
    const error = new Error(detail.message ?? (typeof detail === 'string' ? detail : fallback))
    error.code = detail.code ?? 'request_failed'
    error.fileId = detail.file_id ?? null
    error.status = response.status
    throw error
  }
  return payload
}

export const api = {
  providers: () => apiRequest('/providers'),
  validateProvider: (id) => apiRequest(`/providers/${id}/validate`, { method: 'POST' }),
  saveProviderKey: (id, apiKey, sessionId) => apiRequest(`/providers/${id}/key`, {
    method: 'PUT',
    headers: { 'X-Configuration-Session': sessionId },
    body: JSON.stringify({ api_key: apiKey }),
  }),
  cancelCandidate: (id, sessionId) => apiRequest(`/providers/${id}/candidate/${sessionId}`, { method: 'DELETE' }),
  activateProvider: (id) => apiRequest(`/providers/${id}/activate`, { method: 'POST' }),
  createJob: () => apiRequest('/jobs', { method: 'POST' }),
  activeJob: () => apiRequest('/jobs/active'),
  job: (id) => apiRequest(`/jobs/${id}`),
  startJob: (id) => apiRequest(`/jobs/${id}/start`, { method: 'POST' }),
  resumeJob: (id) => apiRequest(`/jobs/${id}/resume`, { method: 'POST' }),
  retryAnalysis: (id) => apiRequest(`/jobs/${id}/retry-analysis`, { method: 'POST' }),
  cancelJob: (id) => apiRequest(`/jobs/${id}`, { method: 'DELETE' }),
  removeFile: (jobId, fileId) => apiRequest(`/jobs/${jobId}/files/${fileId}`, { method: 'DELETE' }),
  feed: () => apiRequest('/feed'),
  history: () => apiRequest('/history'),
  prompts: () => apiRequest('/prompts'),
  savePrompt: (sceneId, version, content) => apiRequest(`/prompts/${sceneId}`, {
    method: 'PUT', body: JSON.stringify({ expected_version: version, content }),
  }),
  updateTodo: (id, patch) => apiRequest(`/todos/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  deleteTodo: (id) => apiRequest(`/todos/${id}`, { method: 'DELETE' }),
  askCard: (id, question) => apiRequest(`/cards/${id}/questions`, { method: 'POST', body: JSON.stringify({ question }) }),
  feedback: (id, rating, explanation) => apiRequest(`/cards/${id}/feedback`, { method: 'POST', body: JSON.stringify({ rating, explanation }) }),
  clearHistory: () => apiRequest('/history', { method: 'DELETE', body: JSON.stringify({ confirm: true }) }),
}
