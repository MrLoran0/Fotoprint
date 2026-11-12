import type { components } from './schema'

/** Типы контракта — генерируются из OpenAPI: npm run generate:api */
export type S = components['schemas']

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) { super(message) }
}

const base = import.meta.env.VITE_API_BASE || '/api'
const tokenKey = 'fokus-token'
export const demoPassword = import.meta.env.VITE_DEMO_PASSWORD || undefined

export function hasSession() {
  return sessionStorage.getItem(tokenKey) !== null
}

export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const token = path === '/health' || path.startsWith('/auth/') ? null : sessionStorage.getItem(tokenKey)
  let response: Response
  try {
    response = await fetch(`${base}${path}`, {
      method,
      headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, 'Нет связи с сервером. Повторите, когда связь появится.')
  }
  return readResponse<T>(response, token)
}

async function readResponse<T>(response: Response, token: string | null, audioUpload = false): Promise<T> {
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    if (response.status === 401 && token && token === sessionStorage.getItem(tokenKey)) {
      window.dispatchEvent(new Event('fokus-unauthorized'))
    }
    const detail = typeof error?.detail === 'string' ? error.detail
      : Array.isArray(error?.detail) ? 'Проверьте заполнение полей.'
      : audioUpload && response.status === 413 ? 'Запись превышает допустимый размер.'
      : audioUpload && response.status === 503 ? 'Распознавание сейчас недоступно. Повторите позже.'
      : `Ошибка сервера (${response.status}).`
    throw new ApiError(response.status, detail, error?.code)
  }
  const result = response.status === 204 ? undefined : await response.json()
  if (token && token !== sessionStorage.getItem(tokenKey)) {
    throw new ApiError(0, 'Сеанс изменился. Повторите запрос.')
  }
  return result as T
}

function uploadAudio(path: string, file: File, signal: AbortSignal, progress: (percent: number) => void) {
  const token = sessionStorage.getItem(tokenKey)
  return new Promise<S['AudioTranscriptOut']>((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return }
    const xhr = new XMLHttpRequest()
    const abort = () => xhr.abort()
    xhr.open('POST', `${base}${path}`)
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.upload.onprogress = e => { if (e.lengthComputable) progress(Math.floor(e.loaded * 100 / e.total)) }
    xhr.upload.onload = () => progress(100)
    xhr.onload = () => {
      const response = new Response(xhr.status === 204 ? null : xhr.responseText, { status: xhr.status })
      readResponse<S['AudioTranscriptOut']>(response, token, true)
        .then(resolve, reject)
    }
    xhr.onerror = () => reject(new ApiError(0, 'Нет связи с сервером. Повторите загрузку.'))
    xhr.onabort = () => reject(signal.reason)
    xhr.onloadend = () => signal.removeEventListener('abort', abort)
    signal.addEventListener('abort', abort)
    const form = new FormData()
    form.append('file', file)
    xhr.send(form)
  })
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) => request<T>(path, 'POST', body),
  patch: <T,>(path: string, body: unknown) => request<T>(path, 'PATCH', body),
  uploadAudio,
}

export async function login(login: string, password: string) {
  const result = await api.post<S['TokenOut']>('/auth/login', { login, password })
  sessionStorage.setItem(tokenKey, result.access_token)
  return result.user
}

/** Черновики ввода живут в браузере — при выходе и смене роли их не оставляем (общий компьютер). */
export function clearDrafts() {
  for (const key of Object.keys(sessionStorage)) if (key.startsWith('fokus-draft:')) sessionStorage.removeItem(key)
}

export function logout() {
  sessionStorage.removeItem(tokenKey)
  clearDrafts()
}

export const id = encodeURIComponent
