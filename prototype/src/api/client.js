const API_BASE = '/api'
const MUTATION_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])
let localSessionPromise = null
let runtimeEnvironmentPromise = null
const expectedRuntimeProfile = import.meta.env?.VITE_AUDIO_MEMORY_EXPECTED_PROFILE
  ?? (typeof process === 'undefined' ? '' : process.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE ?? '')


export function runtimeEnvironment(expectedProfile, healthPayload) {
  const profile = ['production', 'development'].includes(healthPayload?.profile)
    ? healthPayload.profile
    : 'unknown'
  const blocked = Boolean(expectedProfile) && profile !== expectedProfile
  const label = expectedProfile === 'development' ? '开发环境' : ''
  const message = !blocked
    ? ''
    : profile === 'production'
      ? '当前开发界面连接到了正式环境，已阻止所有写入操作。'
      : '无法确认本地服务环境，已阻止所有写入操作。'
  return { profile, blocked, label, message }
}


async function fetchHealth() {
  const response = await fetch(`${API_BASE}/health`, {
    cache: 'no-store',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw new Error('无法确认本地服务环境，已阻止所有写入操作。')
  return response.json()
}


export async function loadRuntimeEnvironment() {
  if (runtimeEnvironmentPromise === null) {
    runtimeEnvironmentPromise = fetchHealth()
      .then((payload) => runtimeEnvironment(
        expectedRuntimeProfile,
        payload?.status === 'ok' ? payload : null,
      ))
      .catch(() => runtimeEnvironment(expectedRuntimeProfile, null))
  }
  return runtimeEnvironmentPromise
}


async function requireWritableRuntime() {
  if (!expectedRuntimeProfile) return
  const environment = await loadRuntimeEnvironment()
  if (!environment.blocked) return
  const error = new Error(environment.message)
  error.code = 'runtime_environment_blocked'
  throw error
}


async function fetchLocalSession() {
  const response = await fetch(`${API_BASE}/session`, {
    cache: 'no-store',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    throw new Error('无法建立本地安全会话')
  }
  const payload = await response.json()
  if (typeof payload?.token !== 'string' || !payload.token) {
    throw new Error('本地服务返回了无效会话')
  }
  return payload.token
}


export async function getLocalSessionHeaders(idempotencyKey = crypto.randomUUID()) {
  await requireWritableRuntime()
  if (localSessionPromise === null) {
    localSessionPromise = fetchLocalSession().catch((error) => {
      localSessionPromise = null
      throw error
    })
  }
  const token = await localSessionPromise
  return {
    'X-Audio-Memory-Session': token,
    'Idempotency-Key': idempotencyKey,
  }
}


export async function refreshLocalSessionHeaders(idempotencyKey, rejectedToken) {
  const observedPromise = localSessionPromise
  if (observedPromise !== null) {
    const observedToken = await observedPromise.catch(() => null)
    if (observedToken === rejectedToken && localSessionPromise === observedPromise) {
      localSessionPromise = null
    }
  }
  return getLocalSessionHeaders(idempotencyKey)
}


export async function apiRequest(path, options = {}) {
  const { idempotencyKey, ...fetchOptions } = options
  const method = (fetchOptions.method ?? 'GET').toUpperCase()
  const isMutation = MUTATION_METHODS.has(method)
  const actionKey = isMutation ? (idempotencyKey ?? crypto.randomUUID()) : null
  let localHeaders = isMutation
    ? await getLocalSessionHeaders(actionKey)
    : {}
  const send = () => fetch(`${API_BASE}${path}`, {
    ...fetchOptions,
    headers: {
      Accept: 'application/json',
      ...(fetchOptions.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...fetchOptions.headers,
      ...localHeaders,
    },
  })
  let response
  let payload = null
  let responseText = ''
  let transportRetries = 0
  let sessionRefreshes = 0
  while (true) {
    try {
      response = await send()
      responseText = response.status === 204 ? '' : await response.text()
    } catch (error) {
      if (!isMutation || transportRetries >= 1) throw error
      transportRetries += 1
      continue
    }
    payload = null
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
    if (isMutation && sessionRefreshes < 1 && isInvalidSession(response, payload)) {
      sessionRefreshes += 1
      localHeaders = await refreshLocalSessionHeaders(
        actionKey,
        localHeaders['X-Audio-Memory-Session'],
      )
      continue
    }
    break
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

function isReportPreview() {
  return new URLSearchParams(window.location.search).get('reportPreview') === 'deepseek'
}


function isInvalidSession(response, payload) {
  return response.status === 401 && payload?.detail?.code === 'invalid_session'
}

export const api = {
  health: () => fetchHealth(),
  runtimeEnvironment: () => loadRuntimeEnvironment(),
  providers: () => apiRequest('/providers'),
  validateConfiguredProviders: () => apiRequest('/providers/validate-configured', { method: 'POST' }),
  validateProvider: (id) => apiRequest(`/providers/${id}/validate`, { method: 'POST' }),
  saveProviderKey: (id, apiKey, sessionId, modelId) => apiRequest(`/providers/${id}/key`, {
    method: 'PUT',
    headers: { 'X-Configuration-Session': sessionId },
    body: JSON.stringify({ api_key: apiKey, model_id: modelId }),
  }),
  cancelCandidate: (id, sessionId) => apiRequest(`/providers/${id}/candidate/${sessionId}`, { method: 'DELETE' }),
  activateProvider: (id) => apiRequest(`/providers/${id}/activate`, { method: 'POST' }),
  selectProviderModel: (id, modelId) => apiRequest(`/providers/${id}/model`, {
    method: 'PUT',
    body: JSON.stringify({ model_id: modelId }),
  }),
  analysisSettings: () => apiRequest('/settings/analysis'),
  updateAnalysisSettings: (preventSleep) => apiRequest('/settings/analysis', {
    method: 'PUT', body: JSON.stringify({ prevent_sleep: preventSleep }),
  }),
  createJob: () => apiRequest('/jobs', { method: 'POST' }),
  activeJob: () => apiRequest('/jobs/active'),
  job: (id) => apiRequest(`/jobs/${id}`),
  startJob: (id) => apiRequest(`/jobs/${id}/start`, { method: 'POST' }),
  resumeJob: (id) => apiRequest(`/jobs/${id}/resume`, { method: 'POST' }),
  retryAnalysis: (id) => apiRequest(`/jobs/${id}/retry-analysis`, { method: 'POST' }),
  cancelJob: (id) => apiRequest(`/jobs/${id}`, { method: 'DELETE' }),
  removeFile: (jobId, fileId) => apiRequest(`/jobs/${jobId}/files/${fileId}`, { method: 'DELETE' }),
  feed: () => isReportPreview()
    ? fetch('/output/deepseek-historical-report-preview.json').then((response) => response.json())
    : apiRequest('/feed'),
  history: () => isReportPreview() ? Promise.resolve({ days: [] }) : apiRequest('/history'),
  reanalysisPreview: () => apiRequest('/history/reanalysis-batches/preview'),
  currentReanalysis: () => apiRequest('/history/reanalysis-batches/current'),
  createReanalysis: (previewToken, idempotencyKey) => apiRequest('/history/reanalysis-batches', {
    method: 'POST', idempotencyKey, body: JSON.stringify({ preview_token: previewToken }),
  }),
  stopReanalysis: (id, idempotencyKey) => apiRequest(`/history/reanalysis-batches/${id}/stop`, {
    method: 'POST', idempotencyKey,
  }),
  resumeReanalysis: (id, idempotencyKey) => apiRequest(`/history/reanalysis-batches/${id}/resume`, {
    method: 'POST', idempotencyKey,
  }),
  retryReanalysisProfile: (id, idempotencyKey) => apiRequest(`/history/reanalysis-batches/${id}/retry-profile`, {
    method: 'POST', idempotencyKey,
  }),
  prompts: () => apiRequest('/prompts'),
  savePrompt: (sceneId, version, content) => apiRequest(`/prompts/${sceneId}`, {
    method: 'PUT', body: JSON.stringify({ expected_version: version, content }),
  }),
  updateTodo: (id, patch) => apiRequest(`/todos/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  deleteTodo: (id) => apiRequest(`/todos/${id}`, { method: 'DELETE' }),
  feedback: (id, rating, explanation) => apiRequest(`/cards/${id}/feedback`, { method: 'POST', body: JSON.stringify({ rating, explanation }) }),
  clearHistory: () => apiRequest('/history', { method: 'DELETE', body: JSON.stringify({ confirm: true }) }),
}
