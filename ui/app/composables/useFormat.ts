/** Formatting shared across the pages. */

const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })

const UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['year', 365 * 24 * 60 * 60],
  ['month', 30 * 24 * 60 * 60],
  ['week', 7 * 24 * 60 * 60],
  ['day', 24 * 60 * 60],
  ['hour', 60 * 60],
  ['minute', 60],
  ['second', 1],
]

/**
 * "4 minutes ago", "6 hours ago", "8 months ago".
 *
 * Takes a UNIX timestamp in seconds, which is what the API serves.
 */
export function timeAgo(timestamp: number): string {
  if (!timestamp) return '—'
  const elapsed = Date.now() / 1000 - timestamp
  for (const [unit, seconds] of UNITS) {
    if (Math.abs(elapsed) >= seconds || unit === 'second') {
      return RELATIVE.format(-Math.round(elapsed / seconds), unit)
    }
  }
  return 'just now'
}

/** The absolute time, for the tooltip behind a relative one. */
export function absoluteTime(timestamp: number): string {
  if (!timestamp) return ''
  return new Date(timestamp * 1000).toLocaleString()
}

/** Seconds as a short, readable duration: "0.4s", "12s", "3m 20s". */
export function seconds(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value < 10) return `${value.toFixed(1)}s`
  if (value < 60) return `${Math.round(value)}s`
  const minutes = Math.floor(value / 60)
  return `${minutes}m ${Math.round(value % 60)}s`
}

/** What a retrieval time measures (see the server's `timing.py`). Results recorded
 *  before October 2026 may include a wait for a browser slot. */
export const RETRIEVAL_TIME_MEANING
  = 'Retrieval time: from the URL’s first outgoing request to its result. '
  + 'Time spent waiting for a free slot beforehand is not included.'

/** The tooltip of one result's retrieval time, with its wait when known. */
export function retrievalTimeTitle(retrieval: number | null | undefined,
                                   queue?: number | null): string {
  if (retrieval == null) return RETRIEVAL_TIME_MEANING
  const waited = queue != null && queue >= 0.05
    ? `\nWaited ${seconds(queue)} for a slot before that.` : ''
  return `${RETRIEVAL_TIME_MEANING}${waited}`
}

export function bytes(value: number): string {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const power = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  return `${(value / 1024 ** power).toFixed(power ? 1 : 0)} ${units[power]}`
}
