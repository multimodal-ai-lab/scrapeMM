/**
 * The single place that talks to the scrapeMM API.
 *
 * Every call carries the bearer token, and a 401 puts the UI back on the token screen
 * rather than leaving a page half-populated with errors.
 */

const TOKEN_KEY = 'scrapemm.token'

export const useToken = () => useState<string | null>('token', () => {
  if (import.meta.client) return localStorage.getItem(TOKEN_KEY)
  return null
})

export type Role = 'root' | 'admin' | 'client'

/** The key this browser signed in with: its name and role. Null until known. */
export interface Me { id: string, name: string, role: Role }

export const useMe = () => useState<Me | null>('me', () => null)

/** Root and Admin keys see what configures the server: API keys, Settings, Logs. */
export const useIsAdmin = () => {
  const me = useMe()
  return computed(() => me.value?.role === 'root' || me.value?.role === 'admin')
}

export const ROLE_LABELS: Record<Role, string> = { root: 'Root', admin: 'Admin', client: 'Client' }

export function setToken(value: string | null) {
  const token = useToken()
  token.value = value
  if (!value) useMe().value = null
  if (import.meta.client) {
    if (value) localStorage.setItem(TOKEN_KEY, value)
    else localStorage.removeItem(TOKEN_KEY)
  }
}

export function apiBase(): string {
  return useRuntimeConfig().public.apiBase || ''
}

export function useApi() {
  const token = useToken()

  async function call<T>(path: string, options: any = {}): Promise<T> {
    try {
      return await $fetch<T>(`${apiBase()}${path}`, {
        ...options,
        headers: {
          ...(options.headers || {}),
          ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}),
        },
      })
    } catch (error: any) {
      if (error?.status === 401 || error?.statusCode === 401) {
        setToken(null)
        throw new Error('The API key was rejected. Enter it again.')
      }
      throw new Error(error?.data?.detail || error?.message || 'The request failed.')
    }
  }

  /**
   * Reads an NDJSON response, handing each message to `onMessage` as it lands. This is
   * what lets the playground and the dashboard fill in progressively instead of sitting
   * blank for as long as the slowest part takes.
   */
  async function readNdjson(path: string, init: RequestInit,
                            onMessage: (message: any) => void) {
    const response = await fetch(`${apiBase()}${path}`, init)

    if (response.status === 401) {
      setToken(null)
      throw new Error('The API key was rejected. Enter it again.')
    }
    if (!response.ok) {
      throw new Error(`The server responded ${response.status}: ${await response.text()}`)
    }
    if (!response.body) throw new Error('The server sent no body.')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // A chunk can split a line, so only whole lines are parsed and the remainder is
      // carried over to the next read.
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        if (line.trim()) onMessage(JSON.parse(line))
      }
    }
    if (buffer.trim()) onMessage(JSON.parse(buffer))
  }

  function authHeaders(extra: Record<string, string> = {}) {
    return {
      ...extra,
      ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}),
    }
  }

  return {
    token,
    get: <T>(path: string) => call<T>(path),
    post: <T>(path: string, body?: any) => call<T>(path, { method: 'POST', body: body ?? {} }),
    put: <T>(path: string, body?: any) => call<T>(path, { method: 'PUT', body }),
    patch: <T>(path: string, body?: any) => call<T>(path, { method: 'PATCH', body }),
    del: <T>(path: string) => call<T>(path, { method: 'DELETE' }),

    /** Streams an NDJSON response to a POST (retrieval). */
    stream: (path: string, body: any, onMessage: (message: any) => void) =>
      readNdjson(path, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      }, onMessage),

    /** Streams an NDJSON response to a GET (integration statuses, the live view). */
    streamGet: (path: string, onMessage: (message: any) => void, signal?: AbortSignal) =>
      readNdjson(path, { headers: authHeaders(), signal }, onMessage),
  }
}
