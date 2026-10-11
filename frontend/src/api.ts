let csrfToken = ''
export class ApiError extends Error { constructor(message: string, public status = 0) { super(message); this.name = 'ApiError' } }
export function setCsrfToken(token: string): void { csrfToken = token }
function errorText(payload: unknown, fallback: string): string {
  if (typeof payload === 'string' && payload.trim()) return payload
  if (payload && typeof payload === 'object') {
    const object = payload as Record<string, unknown>
    for (const key of ['detail', 'message', 'error']) {
      const value = object[key]
      if (typeof value === 'string' && value.trim()) return value
      if (Array.isArray(value)) return value.map(item => typeof item === 'object' && item && 'msg' in item ? String(item.msg) : String(item)).join('；')
    }
  }
  return fallback
}
export async function request<T>(path: string, method = 'GET', body?: unknown, asText = false): Promise<T> {
  const headers: Record<string, string> = { Accept: asText ? 'text/plain' : 'application/json' }
  if (method !== 'GET') headers['X-CSRF-Token'] = csrfToken
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let response: Response
  try {
    response = await fetch(path, { method, credentials: 'same-origin', headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
  } catch { throw new ApiError('暂时无法连接本机服务。请确认服务已启动，然后重试。') }
  if (!response.ok) {
    const text = await response.text()
    let payload: unknown = text
    try { payload = JSON.parse(text) } catch { /* A plain-text API error is also useful. */ }
    throw new ApiError(errorText(payload, `请求没有完成（${response.status}）。请稍后重试。`), response.status)
  }
  if (response.status === 204) return undefined as T
  if (asText) return await response.text() as T
  try { return await response.json() as T }
  catch { throw new ApiError('服务返回的内容无法读取，请稍后重试。', response.status) }
}
