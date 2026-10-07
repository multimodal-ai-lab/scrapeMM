/**
 * How a retrieval came out, in three classes: green (retrieved), yellow (scrapeMM did its
 * part, but the target is unavailable, behind a paywall or a CAPTCHA, ...) and red
 * (something went wrong on scrapeMM's end). The server decides (see
 * `scrapemm/common/outcome.py`) and sends the class along with every stored result; the
 * tables here mirror it for results that arrive without one (the playground's live
 * results). Change both together.
 */
export type Outcome = 'ok' | 'unavailable' | 'error'

// Error type -> kind of unavailability. Anything not listed is an error.
const UNAVAILABLE_KINDS: Record<string, string> = {
  TargetUnavailableError: 'missing',
  PaywallError: 'paywall',
  CaptchaEncounteredError: 'captcha',
  AccessBlockedError: 'blocked',
  RegionBlockedError: 'blocked',
  RateLimitError: 'rate_limit',
  DomainBlacklistedError: 'unsupported',
  UnsupportedDomainError: 'unsupported',
}

// Most telling first: decides between several methods' findings
const PRIORITY = ['missing', 'paywall', 'captcha', 'blocked', 'rate_limit', 'unsupported']

export function classifyOutcome(
  success: boolean, errors?: Record<string, { type?: string } | null> | null,
): { outcome: Outcome, kind: string | null } {
  if (success) return { outcome: 'ok', kind: null }
  const kinds = new Set(Object.values(errors || {}).map((e) => UNAVAILABLE_KINDS[e?.type || '']))
  const kind = PRIORITY.find((k) => kinds.has(k))
  return kind ? { outcome: 'unavailable', kind } : { outcome: 'error', kind: 'error' }
}

/** Whether an error, on its own, is of the yellow kind (for colouring single errors). */
export function isUnavailableError(type?: string | null) {
  return !!type && type in UNAVAILABLE_KINDS
}

export interface OutcomeLook {
  label: string
  icon: string
  color: 'success' | 'warning' | 'error'
  text: string
}

const CLASS_LOOKS: Record<Outcome, OutcomeLook> = {
  ok: { label: 'Retrieved', icon: 'i-fa7-solid-circle-check', color: 'success', text: 'text-success' },
  unavailable: { label: 'Unavailable', icon: 'i-fa7-solid-circle-minus', color: 'warning', text: 'text-warning' },
  error: { label: 'Failed', icon: 'i-fa7-solid-circle-exclamation', color: 'error', text: 'text-error' },
}

// The kinds of yellow, each with its own label and icon
const KIND_LOOKS: Record<string, { label: string, icon: string }> = {
  missing: { label: 'Target unavailable', icon: 'i-fa7-solid-link-slash' },
  paywall: { label: 'Paywall', icon: 'i-fa7-solid-sack-dollar' },
  captcha: { label: 'CAPTCHA', icon: 'i-fa7-solid-robot' },
  blocked: { label: 'Access blocked', icon: 'i-fa7-solid-ban' },
  rate_limit: { label: 'Rate limited', icon: 'i-fa7-solid-hourglass-half' },
  unsupported: { label: 'Not supported', icon: 'i-fa7-solid-circle-minus' },
}

export function outcomeLook(outcome?: string | null, kind?: string | null): OutcomeLook {
  const base = CLASS_LOOKS[(outcome as Outcome) || 'error'] || CLASS_LOOKS.error
  if (outcome === 'unavailable' && kind && KIND_LOOKS[kind]) return { ...base, ...KIND_LOOKS[kind] }
  return base
}

/** The Jobs view's outcome filter, grouped for USelect: the three classes, with the
 *  kinds of yellow in a group of their own. Values are what the API's `outcome` filter
 *  takes; `any` is the caller's "no filter" sentinel. */
export function outcomeFilterItems(any: string) {
  // `tone` colours the icon, in the select's #item-leading slot
  const item = (label: string, value: string, icon: string, tone: string) =>
    ({ label, value, icon, tone })
  return [
    [item('Any outcome', any, 'i-fa7-solid-circle-half-stroke', 'text-dimmed')],
    [
      item('Retrieved', 'ok', CLASS_LOOKS.ok.icon, 'text-success'),
      item('Unavailable', 'unavailable', CLASS_LOOKS.unavailable.icon, 'text-warning'),
      item('Failed', 'error', CLASS_LOOKS.error.icon, 'text-error'),
    ],
    [
      { type: 'label' as const, label: 'Unavailable because of' },
      ...PRIORITY.map((kind) => item(KIND_LOOKS[kind]!.label, kind, KIND_LOOKS[kind]!.icon, 'text-warning')),
    ],
  ]
}
