export function uploadFile(jobId, file, { onProgress = () => {} } = {}) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    request.open('POST', `/api/jobs/${encodeURIComponent(jobId)}/files`)
    request.responseType = 'json'
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    })
    request.addEventListener('load', () => {
      if (request.status >= 200 && request.status < 300) {
        resolve(request.response)
        return
      }
      const detail = request.response?.detail ?? {}
      const error = new Error(detail.message ?? '上传失败')
      error.code = detail.code ?? 'upload_failed'
      error.fileId = detail.file_id ?? null
      reject(error)
    })
    request.addEventListener('error', () => {
      const error = new Error('网络连接失败，请重新上传')
      error.code = 'network_error'
      reject(error)
    })
    const body = new FormData()
    body.append('file', file, file.name)
    request.send(body)
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

