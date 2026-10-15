/**
 * How healthy scrapeMM is, by the same thresholds wherever a measure appears: on the
 * dashboard's status cards, in the Test view, and in the sidebar, whose logo takes the
 * colour of the worst status card and whose entries point out what needs a look.
 *
 * The rule for the sidebar's indicators: yellow means something wants attention at some
 * point (something new turned up), red means something went wrong and wants it now.
 */
export type HealthTone = 'success' | 'warning' | 'error' | 'neutral'

/** Up to 10 s is fast, up to 20 s acceptable, beyond that slow */
export function timeTone(value: number | null | undefined): HealthTone {
  if (value == null) return 'neutral'
  return value <= 10 ? 'success' : value <= 20 ? 'warning' : 'error'
}

/** Test coverage: at least 95% is good, at least 70% acceptable, below that poor */
export function coverageTone(value: number | null | undefined): HealthTone {
  if (value == null) return 'neutral'
  return value >= 0.95 ? 'success' : value >= 0.7 ? 'warning' : 'error'
}

/** At least 95% is good, at least 80% acceptable, below that poor */
export function shareTone(value: number | null | undefined): HealthTone {
  if (value == null) return 'neutral'
  return value >= 0.95 ? 'success' : value >= 0.8 ? 'warning' : 'error'
}

/** The success rate: above 95% good, from 80% acceptable, below that poor */
export function rateTone(rate: number | null | undefined): HealthTone {
  if (rate == null) return 'neutral'
  return rate > 0.95 ? 'success' : rate >= 0.8 ? 'warning' : 'error'
}

/** Media as a share of the room available to it: what it uses, over that plus free */
export function mediaShare(media: any): number | null {
  if (media?.disk_free == null || media?.bytes == null) return null
  const available = media.bytes + media.disk_free
  return available > 0 ? media.bytes / available : null
}

/** Media crowding out its own headroom is worth saying before it stops the server */
export function diskTone(media: any): HealthTone {
  const share = mediaShare(media)
  if (share == null) return 'neutral'
  return share >= 0.95 ? 'error' : share >= 0.85 ? 'warning' : 'neutral'
}

/** URLs waiting for a slot: more than 100 want a look, more than 1000 are a backlog */
export function queueTone(waiting: number | null | undefined): HealthTone {
  if (!waiting) return 'neutral'
  return waiting > 1000 ? 'error' : waiting > 100 ? 'warning' : 'neutral'
}

/** The worse of some tones: error over warning over success over neutral */
export function worst(tones: HealthTone[]): HealthTone {
  for (const tone of ['error', 'warning', 'success'] as const) {
    if (tones.includes(tone)) return tone
  }
  return 'neutral'
}

/** What the sidebar knows of what is new: everything here is per browser. */
export function useAttention() {
  const environment = useEnvironment()
  const seen = useSeen()

  /** URLs that started waiting for a CAPTCHA since the CAPTCHA page was last open */
  const captchaNew = computed(() => {
    const c = environment.value?.captcha
    return !!c?.waiting && c.latest != null && c.latest > Number(seen.value.captcha ?? 0)
  })

  /** Domains blacklisted since the Blacklist page was last open */
  const blacklistNew = computed(() => {
    const b = environment.value?.blacklist
    return !!b?.domains && b.latest != null && b.latest > Number(seen.value.blacklist ?? 0)
  })

  /** A run under way, or how the latest one went until the Test page was opened */
  const test = computed<'running' | HealthTone | null>(() => {
    const t = environment.value?.test
    if (!t) return null
    if (t.running) return 'running'
    if (t.key === seen.value.test) return null
    return worst([coverageTone(t.coverage), timeTone(t.median), shareTone(t.media)])
  })

  /**
   * The dashboard's status cards that carry a verdict, each with its tone. Search is
   * left out: like the integrations, it has its own section of the dashboard.
   */
  const statuses = computed(() => {
    const env = environment.value
    if (!env) return []
    const rows: { label: string, tone: HealthTone }[] = [
      { label: 'Success rate', tone: rateTone(env.throughput?.success_rate?.rate) },
      { label: 'Scraping time', tone: timeTone(env.throughput?.scrape_times?.median) },
      { label: 'Awaiting CAPTCHA', tone: env.captcha?.waiting ? 'warning' : 'neutral' },
      { label: 'Blacklisted', tone: blacklistNew.value ? 'warning' : 'neutral' },
      { label: 'Media stored', tone: diskTone(env.media) },
      { label: 'Waiting in line', tone: queueTone(env.queue?.waiting) },
    ]
    if (env.proxy?.configured) rows.push({ label: 'Proxy', tone: proxyTone(env.proxy) })
    return rows
  })

  /** The logo's colour: green unless a status card is yellow or red. Neutral until known. */
  const health = computed<HealthTone>(() => {
    if (!statuses.value.length) return 'neutral'
    const tone = worst(statuses.value.map((s) => s.tone))
    return tone === 'neutral' ? 'success' : tone
  })

  return { captchaNew, blacklistNew, test, statuses, health }
}

export function proxyTone(proxy: any): HealthTone {
  if (!proxy.enabled) return 'neutral'
  // None usable is as good as none; some down or failing deserve a look
  if (!proxy.usable_count) return 'error'
  return proxy.usable_count < proxy.enabled_count ? 'warning' : 'success'
}
