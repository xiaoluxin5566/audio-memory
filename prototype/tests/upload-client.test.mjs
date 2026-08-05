import assert from 'node:assert/strict'
import test from 'node:test'

import { uploadFilesSequentially } from '../src/api/upload.js'


test('batch uploads files sequentially in selection order', async () => {
  const calls = []
  let concurrent = 0
  let maxConcurrent = 0
  const uploadOne = async (_jobId, file) => {
    concurrent += 1
    maxConcurrent = Math.max(maxConcurrent, concurrent)
    calls.push(file.name)
    await new Promise((resolve) => setTimeout(resolve, 5))
    concurrent -= 1
    return { id: file.name }
  }

  const result = await uploadFilesSequentially(
    'job-1',
    [{ name: 'a.mp3' }, { name: 'b.aac' }],
    { uploadOne },
  )

  assert.deepEqual(calls, ['a.mp3', 'b.aac'])
  assert.equal(maxConcurrent, 1)
  assert.equal(result.completed.length, 2)
})


test('unsupported file pauses later submissions', async () => {
  const calls = []
  const uploadOne = async (_jobId, file) => {
    calls.push(file.name)
    if (file.name === 'bad.wav') {
      const error = new Error('unsupported')
      error.code = 'unsupported_format'
      throw error
    }
    return { id: file.name }
  }

  const result = await uploadFilesSequentially(
    'job-1',
    [{ name: 'a.mp3' }, { name: 'bad.wav' }, { name: 'never.mp3' }],
    { uploadOne },
  )

  assert.deepEqual(calls, ['a.mp3', 'bad.wav'])
  assert.equal(result.pausedAt, 1)
  assert.equal(result.pending.length, 1)
})
