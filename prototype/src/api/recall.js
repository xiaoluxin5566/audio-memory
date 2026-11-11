import { apiRequest } from './client.js';
import { DATABASE_PAGE_SIZE } from './databasePresentation.js';
export const recallApi = {
  answerBrief: id => apiRequest(`/recall/turns/${encodeURIComponent(id)}/brief`),
  databaseTables: () => apiRequest('/recall/database/tables'),
  completeFacts: ({offset=0,limit=DATABASE_PAGE_SIZE,q='',eventVersionId=''}={}) => apiRequest(`/recall/database/facts?${new URLSearchParams({offset,limit,q,event_version_id:eventVersionId})}`),
  databaseRows: (name, { offset = 0, limit = DATABASE_PAGE_SIZE, q = '' } = {}) => apiRequest(`/recall/database/tables/${encodeURIComponent(name)}?${new URLSearchParams({ offset, limit, q })}`),
  uploads: () => apiRequest('/recall/database/uploads'),
  uploadBrief: (id) => apiRequest(`/recall/transcript-imports/${encodeURIComponent(id)}/brief`),
  uploadChanges: (id, { table, action = 'all', offset = 0, limit = DATABASE_PAGE_SIZE }) => apiRequest(`/recall/transcript-imports/${encodeURIComponent(id)}/changes?${new URLSearchParams({ table, action, offset, limit })}`),
  status: () => apiRequest('/recall/status'),
  createSession: () => apiRequest('/recall/sessions', { method: 'POST' }),
  session: (id) => apiRequest(`/recall/sessions/${encodeURIComponent(id)}`),
  submit: (id, question, messageId) => apiRequest(`/recall/sessions/${encodeURIComponent(id)}/turns`, { method: 'POST', idempotencyKey: messageId, body: JSON.stringify({ question, client_message_id: messageId }) }),
  retryTurn: (id) => apiRequest(`/recall/turns/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  retryImport: (id) => apiRequest(`/recall/transcript-imports/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  turn: (id) => apiRequest(`/recall/turns/${encodeURIComponent(id)}`),
  importTranscript: (form, id) => apiRequest('/recall/transcript-imports', { method: 'POST', idempotencyKey: id, body: form }),
  importStatus: (id) => apiRequest(`/recall/transcript-imports/${encodeURIComponent(id)}`),
  confirmDate: (id, recording_time, timezone) => apiRequest(`/recall/sources/${encodeURIComponent(id)}/recording-time`, { method: 'PUT', body: JSON.stringify({ recording_time, timezone }) }),
};
