/** How each retrieval method and each status is drawn. */

/**
 * Status appearance. `surface` tints the whole card, so the state of the deployment
 * reads at a glance rather than one badge at a time.
 *
 * Amber covers everything that works but not fully -- limited, gated, unreachable -- so
 * the section header can count them together as "warnings" instead of spelling out
 * every variety.
 */
export const TONES: Record<string, {
  icon: string
  color: string
  label: string
  surface: string
  text: string
  warning?: boolean
}> = {
  ready: {
    icon: 'i-fa7-solid-circle-check',
    color: 'success',
    label: 'Ready',
    surface: 'tone-success',
    text: 'text-success',
  },
  limited: {
    icon: 'i-fa7-solid-circle-half-stroke',
    color: 'warning',
    label: 'Limited',
    surface: 'tone-warning',
    text: 'text-warning',
    warning: true,
  },
  gated: {
    icon: 'i-fa7-solid-lock',
    color: 'warning',
    label: 'CAPTCHA gated',
    surface: 'tone-warning',
    text: 'text-warning',
    warning: true,
  },
  error: {
    icon: 'i-fa7-solid-triangle-exclamation',
    color: 'warning',
    label: 'Unreachable',
    surface: 'tone-warning',
    text: 'text-warning',
    warning: true,
  },
  unconfigured: {
    icon: 'i-fa7-solid-key',
    color: 'error',
    label: 'Not configured',
    surface: 'tone-error',
    text: 'text-error',
  },
  disabled: {
    icon: 'i-fa7-solid-ban',
    color: 'neutral',
    label: 'Disabled',
    surface: 'tone-muted',
    text: 'text-dimmed',
  },
  checking: {
    icon: 'i-fa7-solid-spinner',
    color: 'neutral',
    label: 'Checking…',
    surface: '',
    text: 'text-dimmed',
  },
}

/**
 * The icon for each method, by its lower-case key. Font Awesome carries brand marks for
 * the platforms, which read faster than any generic glyph; the archives and the general
 * scrapers fall back to solid icons that say what they do.
 */
const ICONS: Record<string, string> = {
  'x (twitter)': 'i-fa7-brands-x-twitter',
  'telegram': 'i-fa7-brands-telegram',
  'bluesky': 'i-fa7-brands-bluesky',
  'tiktok': 'i-fa7-brands-tiktok',
  'instagram': 'i-fa7-brands-instagram',
  'facebook': 'i-fa7-brands-facebook',
  'threads': 'i-fa7-brands-threads',
  'reddit': 'i-fa7-brands-reddit',
  'youtube': 'i-fa7-brands-youtube',
  'perma.cc': 'i-fa7-solid-link',
  'archive.today': 'i-fa7-solid-box-archive',
  'internet archive': 'i-fa7-solid-building-columns',
  'headed browser': 'i-fa7-solid-window-maximize',
  'ghostarchive': 'i-fa7-solid-ghost',
  'awesomescreenshot': 'i-fa7-solid-camera',
  'firecrawl': 'i-fa7-solid-fire',
  'decodo': 'i-fa7-solid-tower-broadcast',
}

export function integrationIcon(key: string): string {
  return ICONS[key?.toLowerCase()] || 'i-fa7-solid-globe'
}

export function toneFor(state: string) {
  return TONES[state] || TONES.error
}

/** Whether a state counts towards the "warnings" tally in the section header. */
export function isWarning(state: string): boolean {
  return TONES[state]?.warning === true
}
