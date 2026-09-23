<script setup lang="ts">
/** The dashboard: can each retrieval method be used right now, and if not, why not. */
const api = useApi()

const integrations = ref<any[]>([])
const environment = ref<any>(null)
const loading = ref(true)
const error = ref('')
const busy = ref<string | null>(null)
const copied = ref<'address' | 'key' | null>(null)
// Which cards are still waiting for their probe
const pending = shallowRef<Set<string>>(new Set())

/**
 * Loads the dashboard progressively.
 *
 * Probing seventeen methods takes as long as the slowest of them, so waiting for all of
 * them before drawing anything left the page blank for seconds. Instead the card layout
 * appears at once from the stream's header, each card resolves when its own probe does,
 * and the environment tiles arrive on their own timeline beside them.
 */
async function load(force = false) {
  loading.value = true
  error.value = ''
  // The two halves are independent; neither should wait on the other.
  await Promise.all([loadIntegrations(force), loadEnvironment()])
  loading.value = false
}

async function loadEnvironment() {
  try {
    environment.value = await api.get<any>('/v1/environment')
  } catch (e: any) {
    error.value = e.message
  }
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
 * The tally beside the Integrations heading. Limited, gated and unreachable are all
 * "works, but not fully", so they are counted together rather than spelled out -- the
 * card itself says which kind it is.
 */
const summary = computed(() => {
  const tally = { ready: 0, warnings: 0, unconfigured: 0, disabled: 0 }
  for (const item of integrations.value) {
    if (item.state === 'checking') continue
    if (item.state === 'ready') tally.ready++
    else if (item.state === 'unconfigured') tally.unconfigured++
    else if (item.state === 'disabled') tally.disabled++
    else if (isWarning(item.state)) tally.warnings++
  }
  return tally
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

/**
 * The API key comes from this browser's own session rather than from the server: the UI
 * already holds it to authenticate with, and an endpoint that hands the key back out
 * would be a way to read it that did not exist before.
 */
const apiKey = useToken()

// Fixed-width mask, so the display does not even give away how long the key is.
const maskedKey = '•'.repeat(16)

function copy(what: 'address' | 'key') {
  const value = what === 'address' ? address.value : apiKey.value
  if (!value) return
  navigator.clipboard?.writeText(value)
  copied.value = what
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
  if (media.disk_free == null) return null
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
      detail: env.throughput.success_rate.total
        ? `of the last ${env.throughput.success_rate.total} retrievals`
        : 'nothing retrieved yet',
      to: '/jobs' },
    { label: 'Retrieved today', icon: 'i-fa7-solid-gauge-high',
      value: `${env.throughput.last_24h}`, tone: 'neutral' as const,
      detail: 'URLs in the past 24 hours', to: '/jobs?since=24h' },
    { label: 'Awaiting CAPTCHA', icon: 'i-fa7-solid-lock',
      value: `${env.captcha.waiting}`,
      tone: env.captcha.waiting ? 'warning' as const : 'neutral' as const,
      detail: env.captcha.waiting
        ? (env.captcha.solvable ? 'click to solve and clear them'
          : 'no display to solve on')
        : 'nothing waiting on a human',
      // Only a link when there is something to do, and then straight into the solver
      // rather than the page around it.
      to: env.captcha.waiting && env.captcha.solvable ? '/captcha?solve' : undefined },
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
      value: bytes(env.media.bytes), tone: diskTone(env.media),
      // The ring measures media against the space media could actually use -- what it
      // already occupies plus what is still free. Whatever else is on the volume is
      // somebody else's business and only made the figure look alarming for no reason.
      ring: mediaShare(env.media),
      detail: env.media.disk_free
        ? `${env.media.files} files · ${bytes(env.media.disk_free)} free`
        : `${env.media.files} files downloaded`,
      to: '/settings' },
    { label: 'Cached responses', icon: 'i-fa7-solid-bolt',
      value: `${env.cache.entries}`, tone: 'neutral' as const,
      detail: duration(env.cache.ttl), to: '/settings' },
    { label: 'FFmpeg', icon: 'i-fa7-solid-film',
      value: env.ffmpeg.available ? 'Ready' : 'Missing',
      tone: env.ffmpeg.available ? 'neutral' as const : 'warning' as const,
      detail: env.ffmpeg.available ? 'video merged and normalized'
        : 'videos download without sound' },
  ]
})

onMounted(() => load())
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

        <!-- The key is copyable but never legible: what somebody needs from it here is
             to paste it into a client, not to read it off a screen others can see. -->
        <button
          v-if="apiKey" type="button"
          class="inline-flex items-center gap-2 font-mono text-base text-muted
                 hover:text-primary transition-colors"
          title="API key — click to copy"
          @click="copy('key')"
        >
          <UIcon
            :name="copied === 'key' ? 'i-fa7-solid-check' : 'i-fa7-regular-copy'"
            class="size-4" :class="copied === 'key' ? 'text-success' : ''"
          />
          <span class="tracking-tight select-none" aria-label="API key, hidden">
            {{ copied === 'key' ? 'copied' : maskedKey }}
          </span>
        </button>
      </div>

      <UButton
        class="shrink-0" icon="i-fa7-solid-rotate" color="neutral" variant="subtle"
        :loading="loading" label="Re-check all" @click="load(true)"
      />
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <!-- Status -->
    <section class="space-y-2.5">
      <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">
        Status
      </h2>
      <div class="grid gap-2.5 grid-cols-2 lg:grid-cols-4">
        <template v-if="!environment">
          <div
            v-for="n in 8" :key="n"
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

    <!-- Integrations -->
    <section class="space-y-2.5">
      <div class="flex items-baseline justify-between gap-4 flex-wrap">
        <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">
          Integrations
        </h2>
        <p class="text-sm flex flex-wrap items-center gap-x-2 text-dimmed">
          <span class="text-success">{{ summary.ready }} ready</span>
          <span v-if="summary.warnings" class="text-warning">
            · {{ summary.warnings }} warnings
          </span>
          <span v-if="summary.unconfigured" class="text-error">
            · {{ summary.unconfigured }} to configure
          </span>
          <span v-if="summary.disabled">· {{ summary.disabled }} disabled</span>
        </p>
      </div>

      <div class="grid gap-2.5 grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <template v-if="!integrations.length">
          <div v-for="n in 12" :key="n" class="surface-card rounded-xl p-3.5">
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
          v-for="item in integrations" :key="item.key" :item="item"
          :busy="busy === item.key" :checking="pending.has(item.key)"
          @recheck="recheck" @toggle="toggle"
        />
      </div>
    </section>
  </div>
</template>
