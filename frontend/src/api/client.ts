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
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    if (response.status === 401 && token && token === sessionStorage.getItem(tokenKey)) {
      window.dispatchEvent(new Event('fokus-unauthorized'))
    }
    const detail = typeof error?.detail === 'string' ? error.detail
      : Array.isArray(error?.detail) ? 'Проверьте заполнение полей.' : `Ошибка сервера (${response.status}).`
    throw new ApiError(response.status, detail, error?.code)
  }
  const result = response.status === 204 ? undefined : await response.json()
  if (token && token !== sessionStorage.getItem(tokenKey)) {
    throw new ApiError(0, 'Сеанс изменился. Повторите запрос.')
  }
  return result as T
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) => request<T>(path, 'POST', body),
  patch: <T,>(path: string, body: unknown) => request<T>(path, 'PATCH', body),
}

export async function login(login: string, password: string) {
  const result = await api.post<S['TokenOut']>('/auth/login', { login, password })
  sessionStorage.setItem(tokenKey, result.access_token)
  return result.user
}

export function logout() {
  sessionStorage.removeItem(tokenKey)
}

export const id = encodeURIComponent
