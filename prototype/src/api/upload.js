import { getLocalSessionHeaders, refreshLocalSessionHeaders, requireWritableRuntime } from './client.js'


export async function uploadFile(
  jobId,
  file,
  { onProgress = () => {}, idempotencyKey = crypto.randomUUID() } = {},
) {
  let localHeaders = await getLocalSessionHeaders(idempotencyKey)
  const body = new FormData()
  body.append('file', file, file.name)
  return new Promise((resolve, reject) => {
    let refreshed = false
    let transportRetries = 0

    function retryTransport(errorCode, message) {
      if (transportRetries < 1) {
        transportRetries += 1
        send()
        return
      }
      const error = new Error(message)
      error.code = errorCode
      reject(error)
    }

    async function send() {
      try {
        await requireWritableRuntime()
      } catch (error) {
        reject(error)
        return
      }
      const request = new XMLHttpRequest()
      request.open('POST', `/api/jobs/${encodeURIComponent(jobId)}/files`)
      request.timeout = 120_000
      for (const [name, value] of Object.entries(localHeaders)) {
        request.setRequestHeader(name, value)
      }
      request.responseType = 'json'
      request.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable) {
          onProgress(Math.round((event.loaded / event.total) * 100))
        }
      })
      request.addEventListener('load', async () => {
        const detail = request.response?.detail ?? {}
        if (request.status === 401 && detail.code === 'invalid_session' && !refreshed) {
          refreshed = true
          try {
            localHeaders = await refreshLocalSessionHeaders(
              idempotencyKey,
              localHeaders['X-Audio-Memory-Session'],
            )
            send()
          } catch (error) {
            reject(error)
          }
          return
        }
        if (request.status >= 200 && request.status < 300) {
          resolve(request.response)
          return
        }
        const error = new Error(detail.message ?? '上传失败')
        error.code = detail.code ?? 'upload_failed'
        error.fileId = detail.file_id ?? null
        reject(error)
      })
      request.addEventListener('error', () => {
        retryTransport('network_error', '网络连接失败，请重新上传')
      })
      request.addEventListener('timeout', () => {
        retryTransport('network_timeout', '上传超时，请重试')
      })
      request.send(body)
    }

    send()
  })
}


export async function uploadFilesSequentially(
  jobId,
  files,
  { uploadOne = uploadFile, onProgress = () => {} } = {},
) {
  const completed = []
  for (let index = 0; index < files.length; index += 1) {
    try {
      const uploaded = await uploadOne(jobId, files[index], {
        onProgress: (progress) => onProgress(index, progress),
      })
      completed.push(uploaded)
    } catch (error) {
      if (error.code === 'unsupported_format') {
        return {
          completed,
          pausedAt: index,
          error,
          pending: files.slice(index + 1),
        }
      }
      throw error
    }
  }
  return { completed, pausedAt: null, error: null, pending: [] }
}
