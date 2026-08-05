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
