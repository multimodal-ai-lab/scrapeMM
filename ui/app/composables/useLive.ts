/**
 * The server's environment, live, on every page: the sidebar's logo and indicators
 * follow it. The stream carries the environment only (`methods=false`), so it does not
 * keep the server probing the retrieval methods; the dashboard opens its own full
 * stream for those and writes the environment it receives here too.
 */
export const useEnvironment = () => useState<any>('environment', () => null)

let running = false

/** Follows the environment for as long as a key is set. Started once, by the app shell. */
export async function followEnvironment() {
  if (running || !import.meta.client) return
  running = true
  const api = useApi()
  const token = useToken()
  const environment = useEnvironment()
  let pause = 1000
  try {
    while (token.value) {
      try {
        await api.streamGet('/v1/live?methods=false', (message) => {
          pause = 1000
          if (message.type === 'environment') environment.value = message.payload
        })
      } catch { /* Dropped or refused: tried again below, unless signed out */ }
      if (!token.value) break
      await new Promise((resolve) => setTimeout(resolve, pause))
      pause = Math.min(pause * 2, 15000)
    }
  } finally {
    running = false
  }
}

// --- What the viewer has seen -------------------------------------------------------

/**
 * What this browser last saw on a page whose news the sidebar points out: the newest
 * CAPTCHA URL and blacklist entry (by when they came), and the latest test run (by id).
 * Kept per browser, which is who "seen" is about.
 */
type SeenKey = 'captcha' | 'blacklist' | 'test'
const SEEN_KEY = 'scrapemm.seen'

export const useSeen = () => useState<Partial<Record<SeenKey, number | string>>>('seen', () => {
  if (!import.meta.client) return {}
  try {
    return JSON.parse(localStorage.getItem(SEEN_KEY) || '{}')
  } catch {
    return {}
  }
})

export function markSeen(key: SeenKey, value: number | string | null | undefined) {
  const seen = useSeen()
  if (value == null || seen.value[key] === value) return
  seen.value = { ...seen.value, [key]: value }
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify(seen.value))
  } catch { /* private window, or storage blocked: seen for this session only */ }
}
