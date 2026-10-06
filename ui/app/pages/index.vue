<script setup lang="ts">
/** The dashboard: can each retrieval method be used right now, and if not, why not. */
const api = useApi()

const integrations = ref<any[]>([])
const environment = ref<any>(null)
const loading = ref(false)
const error = ref('')
const busy = ref<string | null>(null)
const copied = ref<'address' | null>(null)
// Which cards are still waiting for their probe
const pending = shallowRef<Set<string>>(new Set())

/**
 * The live view. The server pushes the environment and each method's status whenever
 * one of them changes, so the page is never stale and never polls. The card layout
 * appears at once from the stream's header, and each card resolves when its own probe
 * does. A dropped connection is re-established with growing pauses.
 */
const live = ref<'connecting' | 'live' | 'reconnecting'>('connecting')
let liveAbort: AbortController | null = null
let unmounted = false

function onLive(message: any) {
  live.value = 'live'
  if (message.type === 'header') {
    const keys = message.methods.map((m: any) => m.key).join()
    // A reconnect repeats the header; only a changed method list needs new placeholders
    if (integrations.value.map((i) => i.key).join() !== keys) {
      pending.value = new Set(message.methods.map((m: any) => m.key))
      integrations.value = message.methods.map((m: any) => ({
        ...m, state: 'checking', missing_secrets: [], missing_optional_secrets: [],
        enabled: true, retrievals: 0, configurable: false, detail: '',
      }))
    }
  } else if (message.type === 'status') {
    replace(message.payload)
    pending.value.delete(message.payload.key)
    triggerRef(pending)
  } else if (message.type === 'environment') {
    environment.value = message.payload
  }
}

async function connectLive() {
  let pause = 1000
  while (!unmounted && apiKey.value) {
    liveAbort = new AbortController()
    try {
      await api.streamGet('/v1/live', (message) => {
        pause = 1000  // Got through: the next drop starts over with a short pause
        error.value = ''
        onLive(message)
      }, liveAbort.signal)
    } catch (e: any) {
      if (unmounted) return
      if (!apiKey.value) { error.value = e.message; return }
    }
    if (unmounted) return
    live.value = 'reconnecting'
    await new Promise((resolve) => setTimeout(resolve, pause))
    pause = Math.min(pause * 2, 15000)
  }
}

/** Re-probes every method now. The results reach every open dashboard via the live view. */
async function recheckAll() {
  loading.value = true
  error.value = ''
  await loadIntegrations(true)
  loading.value = false
}

async function loadIntegrations(force = false) {
  pending.value = new Set()
  try {
    await api.streamGet(`/v1/integrations/stream${force ? '?force=true' : ''}`,
      (message) => {
        if (message.type === 'header') {
          // Placeholders in the final order, so nothing jumps around as results land.
          // They carry the real name and domains already; only the verdict is pending.
          pending.value = new Set(message.methods.map((m: any) => m.key))
          integrations.value = message.methods.map((m: any) => ({
            ...m, state: 'checking', missing_secrets: [], missing_optional_secrets: [],
            enabled: true, retrievals: 0, configurable: false, detail: '',
          }))
        } else if (message.type === 'status') {
          replace(message.payload)
          pending.value.delete(message.payload.key)
          triggerRef(pending)
        }
      })
  } catch (e: any) {
    error.value = e.message
  } finally {
    pending.value = new Set()
  }
}

function replace(updated: any) {
  const index = integrations.value.findIndex((i) => i.key === updated.key)
  if (index >= 0) integrations.value[index] = updated
  else integrations.value.push(updated)
}

async function recheck(key: string) {
  busy.value = key
  try {
    replace(await api.post<any>(`/v1/integrations/${encodeURIComponent(key)}/check`))
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

async function toggle(key: string, enabled: boolean) {
  busy.value = key
  try {
    replace(await api.put<any>(`/v1/integrations/${encodeURIComponent(key)}/enabled`,
                               { enabled }))
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

/**
 * The tally beside a group's heading. Limited, gated and unreachable are all "works,
 * but not fully", so they are counted together rather than spelled out -- the card
 * itself says which kind it is.
 */
function tally(items: any[]) {
  const counts = { ready: 0, warnings: 0, unconfigured: 0, disabled: 0 }
  for (const item of items) {
    if (item.state === 'checking') continue
    if (item.state === 'ready') counts.ready++
    else if (item.state === 'unconfigured') counts.unconfigured++
    else if (item.state === 'disabled') counts.disabled++
    else if (isWarning(item.state)) counts.warnings++
  }
  return counts
}

/** Retrieval integrations and methods, and apart from them the search providers,
 *  which retrieve nothing (see the server's `status.py`) */
const groups = computed(() => {
  const retrieval = integrations.value.filter((i) => i.kind !== 'search')
  const search = integrations.value.filter((i) => i.kind === 'search')
  return [
    { key: 'retrieval', title: 'Retrieval integrations', items: retrieval,
      summary: tally(retrieval), placeholders: 12 },
    { key: 'search', title: 'Search integrations', items: search,
      summary: tally(search), placeholders: 1 },
  ].filter((g) => g.items.length || !integrations.value.length)
})

/**
 * "Today" is the viewer's calendar day. The server counts per 15-minute slot over the
 * last day (every time zone's offset is a multiple of 15 minutes, so the slots line up
 * with any local midnight), and the page adds up the slots since its own midnight. The
 * clock ticks so that the figure starts over at midnight without a reload.
 */
const now = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | undefined
const midnight = computed(() => new Date(now.value).setHours(0, 0, 0, 0) / 1000)

function sinceMidnight(rows: any[] | undefined): any[] {
  return (rows || []).filter((row) => row[0] >= midnight.value)
}

const retrievedToday = computed(() => sinceMidnight(environment.value?.throughput?.recent?.counts)
  .reduce((sum: number, row: any) => sum + row[1], 0))

/** Today's search requests, in total and per provider (by its dashboard label) */
const searchesToday = computed(() => {
  const providers = new Map<string, { ok: number, failed: number }>()
  for (const [, provider, ok, failed] of sinceMidnight(environment.value?.searches?.recent)) {
    const entry = providers.get(provider) || { ok: 0, failed: 0 }
    entry.ok += ok
    entry.failed += failed
    providers.set(provider, entry)
  }
  const label = (key: string) => integrations.value.find((i) => i.key === key)?.name || key
  const list = [...providers].map(([key, n]) => ({ name: label(key), ...n }))
  return {
    total: list.reduce((sum, p) => sum + p.ok + p.failed, 0),
    failed: list.reduce((sum, p) => sum + p.failed, 0),
    providers: list,
  }
})

/**
 * Where this server can be reached. The server knows its port and any declared public
 * URL; inside a container its own interface addresses are the bridge network's and mean
 * nothing outside it, so the origin this browser is already using is the honest answer.
 */
const address = computed(() => {
  const env = environment.value
  if (!env) return ''
  if (env.address?.public_url) return env.address.public_url
  if (import.meta.client) return window.location.origin
  return `:${env.address?.port}`
})

const lanAddresses = computed(() => {
  const env = environment.value
  if (!env?.address || env.address.containerised) return []
  return env.address.addresses.map((a: string) => `http://${a}:${env.address.port}`)
})

// Signed in or not: the live view stops when the key goes. The key itself is shown
// nowhere -- only once, when it is created (see the API Keys page).
const apiKey = useToken()

async function copy(what: 'address') {
  const value = address.value
  if (!value) return
  const ok = await copyText(value)
  // Only claim success when the clipboard really has it
  if (ok) copied.value = what
  else error.value = 'Copying to the clipboard failed. This browser blocks it here.'
  setTimeout(() => { if (copied.value === what) copied.value = null }, 1500)
}

function duration(seconds: number) {
  if (!seconds || seconds <= 0) return 'caching off'
  if (seconds >= 86400) return `kept ${Math.round(seconds / 86400)} d`
  if (seconds >= 3600) return `kept ${Math.round(seconds / 3600)} h`
  return `kept ${Math.round(seconds / 60)} min`
}

/** Media as a share of the room available to it: what it uses, over that plus free. */
function mediaShare(media: any): number | null {
  if (media.disk_free == null || media.bytes == null) return null
  const available = media.bytes + media.disk_free
  return available > 0 ? media.bytes / available : null
}

/** Media crowding out its own headroom is worth saying before it stops the server. */
function diskTone(media: any) {
  const share = mediaShare(media)
  if (share == null) return 'neutral' as const
  if (share >= 0.95) return 'error' as const
  if (share >= 0.85) return 'warning' as const
  return 'neutral' as const
}

/** >95% green, 80–95% amber, below that red. */
function rateTone(rate: number | null) {
  if (rate == null) return { tone: 'neutral' as const, value: '—' }
  const percent = rate * 100
  const tone = percent > 95 ? 'success' as const
    : percent >= 80 ? 'warning' as const
      : 'error' as const
  return { tone, value: `${percent.toFixed(1)}%` }
}

const tiles = computed(() => {
  const env = environment.value
  if (!env) return []
  const rate = rateTone(env.throughput.success_rate.rate)
  return [
    { label: 'Success rate', icon: 'i-fa7-solid-check',
      value: rate.value, tone: rate.tone,
      ring: env.throughput.success_rate.rate,
      // scrapeMM's success: a target that was unavailable counts, since scrapeMM did its
      // part (see useOutcome.ts); the breakdown says how many that were
      detail: env.throughput.success_rate.total
        ? (env.throughput.success_rate.outcomes
          ? `${env.throughput.success_rate.outcomes.error} failed · `
            + `${env.throughput.success_rate.outcomes.unavailable} unavailable · `
            + `${env.throughput.success_rate.outcomes.ok} retrieved, `
            + `of the last ${env.throughput.success_rate.total}`
          : `of the last ${env.throughput.success_rate.total} retrievals`)
        : 'nothing retrieved yet',
      to: env.throughput.success_rate.outcomes?.error ? '/jobs?outcome=error' : '/jobs' },
    { label: 'Retrieved today', icon: 'i-fa7-solid-gauge-high',
      value: `${retrievedToday.value}`, tone: 'neutral' as const,
      detail: 'URLs since midnight', to: '/jobs?since=today' },
    { label: 'Search requests today', icon: 'i-fa7-solid-magnifying-glass',
      value: `${searchesToday.value.total}`,
      tone: searchesToday.value.failed ? 'warning' as const : 'neutral' as const,
      detail: searchesToday.value.total
        ? searchesToday.value.providers
          .map((p) => `${p.name} ${p.ok + p.failed}${p.failed ? ` (${p.failed} failed)` : ''}`)
          .join(' · ')
        : `none since midnight · ${env.searches?.total ?? 0} all time`,
      to: '/playground/search' },
    { label: 'Awaiting CAPTCHA', icon: 'i-fa7-solid-lock',
      value: `${env.captcha.waiting}`,
      tone: env.captcha.waiting ? 'warning' as const : 'neutral' as const,
      detail: env.captcha.waiting
        ? `URLs on ${env.captcha.challenges} site${env.captcha.challenges === 1 ? '' : 's'} · solve or discard`
        : 'nothing waiting on a human',
      // Only a link when there is something to decide; which challenge to solve, and
      // whether at all, is decided on that page
      to: env.captcha.waiting ? '/captcha' : undefined },
    { label: 'Blacklisted', icon: 'i-fa7-solid-ban',
      value: `${env.blacklist.domains}`,
      tone: env.blacklist.domains ? 'warning' as const : 'neutral' as const,
      detail: env.blacklist.domains ? 'domains excluded from retrieval'
        : 'no domains excluded',
      to: '/blacklist' },
    { label: 'Jobs run', icon: 'i-fa7-solid-clock-rotate-left',
      value: `${env.jobs.jobs}`, tone: 'neutral' as const,
      detail: `${env.jobs.urls} URLs all time`, to: '/jobs' },
    { label: 'Media stored', icon: 'i-fa7-solid-photo-film',
      // Null until the server's first background measurement is done
      value: env.media.bytes == null ? '…' : bytes(env.media.bytes),
      tone: diskTone(env.media),
      // The ring measures media against the space media could actually use -- what it
      // already occupies plus what is still free. Whatever else is on the volume is
      // somebody else's business and only made the figure look alarming for no reason.
      ring: mediaShare(env.media),
      detail: env.media.files == null
        ? 'measuring…'
        : env.media.disk_free
          ? `${env.media.files} files · ${bytes(env.media.disk_free)} free`
          : `${env.media.files} files downloaded`,
      to: '/settings' },
    // How often a retrieval was answered from the cache instead of being scraped again:
    // relative as the value, absolute below it, alongside what the cache holds now
    { label: 'Cache hits', icon: 'i-fa7-solid-bolt',
      value: env.jobs.cache_hit_rate == null ? '—'
        : `${(env.jobs.cache_hit_rate * 100).toFixed(1)}%`,
      tone: 'neutral' as const, ring: env.jobs.cache_hit_rate,
      detail: env.jobs.urls
        ? `${env.jobs.from_cache} of ${env.jobs.urls} URLs · ${env.cache.entries} cached, ${duration(env.cache.ttl)}`
        : `${env.cache.entries} cached · ${duration(env.cache.ttl)}`,
      to: '/settings' },
    // URLs accepted by a job but not started yet, because every slot is taken
    { label: 'Waiting in line', icon: 'i-fa7-solid-hourglass-half',
      value: `${env.queue.waiting}`, tone: 'neutral' as const,
      detail: env.queue.waiting || env.queue.active || env.jobs.running
        ? `URLs queued · ${env.queue.active}/${env.queue.limit} scraping · `
          + `${env.jobs.running} job${env.jobs.running === 1 ? '' : 's'} running`
        : 'no URLs queued',
      to: '/jobs' },
  ]
})

onMounted(() => {
  connectLive()
  clockTimer = setInterval(() => { now.value = Date.now() }, 30_000)
})
onUnmounted(() => {
  unmounted = true
  liveAbort?.abort()
  clearInterval(clockTimer)
})
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-start justify-between gap-4">
      <div class="flex items-baseline gap-3 flex-wrap min-w-0">
        <h1 class="text-2xl font-semibold">Dashboard</h1>

        <!-- Where to reach this server, ready to hand to somebody -->
        <button
          v-if="address" type="button"
          class="inline-flex items-center gap-2 font-mono text-base text-muted
                 hover:text-primary transition-colors"
          :title="lanAddresses.length
            ? `Also reachable at ${lanAddresses.join(', ')} — click to copy`
            : 'Click to copy'"
          @click="copy('address')"
        >
          <UIcon
            :name="copied === 'address' ? 'i-fa7-solid-check' : 'i-fa7-regular-copy'"
            class="size-4" :class="copied === 'address' ? 'text-success' : ''"
          />
          {{ address }}
        </button>
        <USkeleton v-else-if="!error" class="h-5 w-48 self-center" />

      </div>

      <div class="flex items-center gap-3 shrink-0">
        <!-- Whether what is on screen is current: the page updates itself while live -->
        <span
          class="inline-flex items-center gap-1.5 text-xs text-muted"
          :title="live === 'live' ? 'Updates as soon as something changes'
            : 'The connection to the server dropped; trying again'"
        >
          <span
            class="size-2 rounded-full"
            :class="live === 'live' ? 'bg-success animate-pulse'
              : live === 'reconnecting' ? 'bg-warning' : 'bg-neutral-400'"
          />
          {{ live === 'live' ? 'Live' : live === 'reconnecting' ? 'Reconnecting…' : 'Connecting…' }}
        </span>
        <UButton
          icon="i-fa7-solid-rotate" color="neutral" variant="subtle"
          :loading="loading" label="Re-check all" @click="recheckAll"
        />
      </div>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <!-- Status -->
    <section class="space-y-2.5">
      <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">
        Status
      </h2>
      <div class="grid gap-2.5 grid-cols-2 lg:grid-cols-3">
        <template v-if="!environment">
          <div
            v-for="n in 9" :key="n"
            class="surface-card rounded-xl p-3.5 flex items-center gap-3.5"
          >
            <USkeleton class="size-11 rounded-lg shrink-0" />
            <div class="space-y-2 min-w-0 flex-1">
              <USkeleton class="h-6 w-16" />
              <USkeleton class="h-3 w-24" />
            </div>
          </div>
        </template>
        <StatCard
          v-for="tile in tiles" :key="tile.label" v-bind="tile"
        />
      </div>
    </section>

    <!-- Retrieval integrations, then search integrations -->
    <section v-for="group in groups" :key="group.key" class="space-y-2.5">
      <div class="flex items-baseline justify-between gap-4 flex-wrap">
        <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">
          {{ group.title }}
        </h2>
        <p v-if="group.items.length" class="text-sm flex flex-wrap items-center gap-x-2 text-dimmed">
          <span class="text-success">{{ group.summary.ready }} ready</span>
          <span v-if="group.summary.warnings" class="text-warning">
            · {{ group.summary.warnings }} warning{{ group.summary.warnings === 1 ? '' : 's' }}
          </span>
          <span v-if="group.summary.unconfigured" class="text-error">
            · {{ group.summary.unconfigured }} to configure
          </span>
          <span v-if="group.summary.disabled">· {{ group.summary.disabled }} disabled</span>
        </p>
      </div>

      <div class="grid gap-2.5 grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <template v-if="!group.items.length">
          <div v-for="n in group.placeholders" :key="n" class="surface-card rounded-xl p-3.5">
            <div class="flex items-start gap-3.5">
              <USkeleton class="size-10 rounded-lg shrink-0" />
              <div class="flex-1 space-y-2">
                <USkeleton class="h-5 w-32" />
                <USkeleton class="h-5 w-24 rounded-full" />
                <USkeleton class="h-3 w-3/4" />
              </div>
            </div>
          </div>
        </template>
        <IntegrationCard
          v-for="item in group.items" :key="item.key" :item="item"
          :busy="busy === item.key" :checking="pending.has(item.key)"
          @recheck="recheck" @toggle="toggle"
        />
      </div>
    </section>
  </div>
</template>
