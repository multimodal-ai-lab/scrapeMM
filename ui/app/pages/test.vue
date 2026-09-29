<script setup lang="ts">
/**
 * Measuring scrapeMM: one button runs the whole suite of known URLs as a job, the page
 * follows it live, and the run ends in a report of coverage and speed -- by category,
 * by method and over past runs.
 *
 * Coverage counts a URL only if it yielded everything its test expects (at least so many
 * images and videos); "retrieved" also counts the ones that came back with media
 * missing. The cache is bypassed: a cached answer would measure the cache.
 */
const api = useApi()

const suite = ref<any[]>([])
const run = ref<any>({ state: 'idle' })
const history = ref<any[]>([])
const viewing = ref<any>(null)  // A past report chosen from the history, if any
const error = ref('')
const starting = ref(false)
const outcomeFilter = ref<'all' | 'running' | 'failed' | 'partial' | 'passed'>('all')

// `unavailable`: the entry expects the target to be unavailable instead of content
const newUrl = reactive({ url: '', category: '', image: 0, video: 0, unavailable: false })
const adding = ref(false)
const showSuite = ref(false)

let poller: ReturnType<typeof setInterval> | null = null

const running = computed(() => run.value?.running === true)
/** What the page shows: a past report if one is picked, else the current or last run */
const report = computed(() => viewing.value || (run.value?.state !== 'idle' ? run.value : null))
const summary = computed(() => report.value?.summary)
const categories = computed(() => [...new Set(suite.value.map((e) => e.category))].sort())

async function load() {
  try {
    const data = await api.get<any>('/v1/test')
    suite.value = data.suite
    run.value = data.run
    history.value = data.history
    // Nothing run since the server started: the latest saved report is the last run
    if (data.run.state === 'idle' && data.history.length && !viewing.value) {
      await openReport(data.history[0].id)
    }
  } catch (e: any) {
    error.value = e.message
  }
}

async function poll() {
  try {
    const status = await api.get<any>('/v1/test/run')
    const wasRunning = running.value
    run.value = status
    if (wasRunning && !status.running) await load()  // The finished run joins the history
  } catch { /* the next poll tries again */ }
  if (!running.value) stopPolling()
}

function startPolling() {
  if (!poller) poller = setInterval(poll, 1000)
}
function stopPolling() {
  if (poller) clearInterval(poller)
  poller = null
}

async function start() {
  starting.value = true
  error.value = ''
  viewing.value = null
  try {
    run.value = await api.post<any>('/v1/test/run')
    startPolling()
  } catch (e: any) {
    error.value = e.message
  } finally {
    starting.value = false
  }
}

/** URLs of the report shown that failed on a CAPTCHA: solvable on the CAPTCHA page */
const captchaResults = computed<any[]>(() => (report.value?.results || [])
  .filter((r: any) => r.outcome === 'failed' && r.error?.type === 'CaptchaEncounteredError'))
/** Reruns apply to the latest run only; a past report is a record */
const isLatest = computed(() => !viewing.value || viewing.value.id === history.value[0]?.id)
const rerunning = ref(false)

async function rerunCaptchas() {
  rerunning.value = true
  error.value = ''
  try {
    run.value = await api.post<any>('/v1/test/run/rerun-captchas')
    viewing.value = null
    startPolling()
  } catch (e: any) {
    error.value = e.message
  } finally {
    rerunning.value = false
  }
}

async function cancel() {
  try {
    run.value = await api.del<any>('/v1/test/run')
    await load()
  } catch (e: any) {
    error.value = e.message
  }
}

async function openReport(id: string) {
  if (id === run.value?.id) {
    viewing.value = null
    return
  }
  try {
    viewing.value = await api.get<any>(`/v1/test/runs/${id}`)
  } catch (e: any) {
    error.value = e.message
  }
}

async function addUrl() {
  adding.value = true
  error.value = ''
  try {
    const expected: Record<string, number> = {}
    if (!newUrl.unavailable && newUrl.image > 0) expected.image = Number(newUrl.image)
    if (!newUrl.unavailable && newUrl.video > 0) expected.video = Number(newUrl.video)
    await api.post('/v1/test/suite', {
      url: newUrl.url, category: newUrl.category || 'Added', expected,
      expect: newUrl.unavailable ? 'unavailable' : null,
    })
    Object.assign(newUrl, { url: '', image: 0, video: 0, unavailable: false })
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    adding.value = false
  }
}

async function removeUrl(url: string) {
  try {
    await api.del(`/v1/test/suite?url=${encodeURIComponent(url)}`)
    await load()
  } catch (e: any) {
    error.value = e.message
  }
}

async function restoreDefaults() {
  await api.post('/v1/test/suite/restore')
  await load()
}

onMounted(async () => {
  await load()
  if (running.value) startPolling()
})
onBeforeUnmount(stopPolling)

// --- Figures ----------------------------------------------------------------------

function percent(value: number | null | undefined) {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

function secs(value: number | null | undefined) {
  return value == null ? '—' : seconds(value)
}

// Thresholds, the same wherever the measure appears: in a tile, a bar or a column
type Tone = 'success' | 'warning' | 'error' | 'neutral'
const TONE_COLOR: Record<Tone, string> = {
  success: 'var(--viz-good)', warning: 'var(--viz-warning)',
  error: 'var(--viz-critical)', neutral: 'var(--viz-seq)',
}

/** Up to 5 s is fast, up to 20 s acceptable, beyond that slow */
function timeTone(value: number | null | undefined): Tone {
  if (value == null) return 'neutral'
  return value <= 5 ? 'success' : value <= 20 ? 'warning' : 'error'
}

/** Coverage: at least 90% is good, at least 70% acceptable, below that poor */
function coverageTone(value: number | null | undefined): Tone {
  if (value == null) return 'neutral'
  return value >= 0.9 ? 'success' : value >= 0.7 ? 'warning' : 'error'
}

/** At least 95% is good, at least 80% acceptable, below that poor */
function shareTone(value: number | null | undefined): Tone {
  if (value == null) return 'neutral'
  return value >= 0.95 ? 'success' : value >= 0.8 ? 'warning' : 'error'
}

/** The share of all expected images and videos that were found */
const mediaShare = computed(() => {
  const m = summary.value?.media
  if (!m) return null
  const expected = m.image.expected + m.video.expected
  return expected ? (m.image.found + m.video.found) / expected : null
})

const tiles = computed(() => {
  const s = summary.value
  if (!s) return []
  return [
    { label: 'Coverage', icon: 'i-fa7-solid-bullseye', value: percent(s.coverage), ring: s.coverage,
      detail: `${s.passed} of ${s.done} URLs with all expected media`,
      tone: coverageTone(s.coverage) },
    { label: 'Median time', icon: 'i-fa7-solid-stopwatch', value: secs(s.time.median),
      detail: `90% within ${secs(s.time.p90)} · slowest ${secs(s.time.max)}`,
      tone: timeTone(s.time.median) },
    { label: 'Media found', icon: 'i-fa7-solid-photo-film', value: percent(mediaShare.value),
      ring: mediaShare.value, tone: shareTone(mediaShare.value),
      detail: `${s.media.image.found + s.media.video.found} of ${s.media.image.expected + s.media.video.expected} expected images and videos` },
    { label: 'Throughput', icon: 'i-fa7-solid-gauge-high',
      value: s.throughput == null ? '—' : `${s.throughput.toFixed(1)}/min`,
      detail: `${s.done} URLs in ${secs(s.elapsed)}`, tone: 'neutral' },
  ] as const
})

/** How long retrievals took, in bins that stay readable from 1 s to a minute */
const BINS = [[0, 2], [2, 5], [5, 10], [10, 20], [20, 30], [30, 60], [60, Infinity]] as const
const timeHistogram = computed(() => {
  const times: number[] = summary.value?.time?.all || []
  return BINS.map(([lo, hi]) => {
    const n = times.filter((t) => t >= lo && t < hi).length
    const label = hi === Infinity ? `${lo}s+` : `${lo}–${hi}s`
    // The bins break at 5 s and 20 s, so each one is wholly fast, acceptable or slow
    return { label, value: n, tooltip: `${label}: ${n} URL${n === 1 ? '' : 's'}`,
             color: TONE_COLOR[timeTone(hi === Infinity ? lo + 1 : hi)] }
  })
})

const categoryTimes = computed(() => (summary.value?.categories || [])
  .filter((c: any) => c.median_time != null)
  .map((c: any) => ({ label: c.category, value: Math.round(c.median_time * 10) / 10,
                      hint: `${c.category}: median ${secs(c.median_time)}`,
                      color: TONE_COLOR[timeTone(c.median_time)] })))

const methodBars = computed(() => Object.entries(summary.value?.methods || {})
  .map(([label, value]) => ({ label, value: value as number })))

/** Failures by what they mean. Several error types share one description (everything
 *  unrecognised is "unexpected"), so they are summed under it; the tooltip keeps the
 *  breakdown by type. */
const errorBars = computed(() => {
  const byLabel = new Map<string, { value: number, types: string[] }>()
  for (const [type, count] of Object.entries(summary.value?.errors || {})) {
    const label = describeError(type)
    const entry = byLabel.get(label) || { value: 0, types: [] }
    entry.value += count as number
    entry.types.push(`${type} ${count}`)
    byLabel.set(label, entry)
  }
  return [...byLabel.entries()]
    .map(([label, e]) => ({ label, value: e.value, hint: `${label} ${e.types.join(' · ')}` }))
    .sort((a, b) => b.value - a.value)
})

/** The kept runs, oldest first, for the trend charts */
const trend = computed(() => [...history.value].reverse().filter((h) => h.summary?.done))
// Coloured by the same thresholds as the Coverage and Median time tiles
const trendCoverage = computed(() => trend.value.map((h, i) => ({
  label: `#${i + 1}`, value: h.summary.coverage ?? 0,
  tooltip: `${absoluteTime(h.started)}: ${percent(h.summary.coverage)} coverage (${h.summary.passed}/${h.summary.done})`,
  color: TONE_COLOR[coverageTone(h.summary.coverage)],
})))
const trendMedian = computed(() => trend.value.map((h, i) => ({
  label: `#${i + 1}`, value: h.summary.time?.median ?? 0,
  tooltip: `${absoluteTime(h.started)}: median ${secs(h.summary.time?.median)}`,
  color: TONE_COLOR[timeTone(h.summary.time?.median)],
})))

const OUTCOME = {
  passed: { icon: 'i-fa7-solid-circle-check', color: 'var(--viz-good)', label: 'Passed' },
  partial: { icon: 'i-fa7-solid-circle-half-stroke', color: 'var(--viz-warning)', label: 'Partial' },
  failed: { icon: 'i-fa7-solid-circle-exclamation', color: 'var(--viz-critical)', label: 'Failed' },
} as const

/** The URLs of the live run that have no result yet. They were all started together,
 *  so some may still wait for a slot rather than load: "in progress" covers both. */
const inProgress = computed<any[]>(() => (running.value && !viewing.value ? run.value.pending || [] : [])
  .map((p: any) => ({ ...p, outcome: 'running' })))

const rows = computed(() => {
  const results: any[] = report.value?.results || []
  if (outcomeFilter.value === 'running') return inProgress.value
  const filtered = outcomeFilter.value === 'all' ? results
    : results.filter((r) => r.outcome === outcomeFilter.value)
  // Worst first: what needs looking at leads
  const rank = { failed: 0, partial: 1, passed: 2 } as Record<string, number>
  const order = (r: any) => rank[r.outcome] ?? Object.keys(rank).length  // Unknown ones last
  const sorted = [...filtered].sort((a, b) => order(a) - order(b)
    || a.category.localeCompare(b.category))
  // What is still changing comes first
  return outcomeFilter.value === 'all' ? [...inProgress.value, ...sorted] : sorted
})

// Leave the Running tab once there is nothing left running
watch(inProgress, (now) => {
  if (!now.length && outcomeFilter.value === 'running') outcomeFilter.value = 'all'
})

function media(record: Record<string, number> | undefined) {
  const parts = Object.entries(record || {}).filter(([, n]) => n)
    .map(([kind, n]) => `${n} ${kind}${n === 1 ? '' : 's'}`)
  return parts.join(', ') || 'none'
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-start justify-between gap-6 flex-wrap">
      <div class="max-w-2xl">
        <h1 class="text-2xl font-semibold">Test</h1>
        <p class="text-sm text-muted mt-1">
          Measures coverage and speed on a suite of {{ suite.length }} known URLs, each
          with the media it must yield. The cache is bypassed, and the run is a job like
          any other.
        </p>
      </div>

      <!-- The one thing this page is for -->
      <div class="flex items-center gap-3">
        <UButton
          v-if="running" size="xl" color="error" variant="soft"
          icon="i-fa7-solid-stop" label="Stop the run" @click="cancel"
        />
        <!-- Neutral at rest, the accent on hover: it invites without shouting -->
        <UButton
          v-else size="xl" color="neutral" variant="subtle" icon="i-fa7-solid-flask"
          :loading="starting" :disabled="!suite.length"
          class="px-8 py-4 text-lg transition-colors duration-200
                 hover:bg-primary! hover:text-inverted! hover:ring-primary!"
          :label="report ? 'Run the test again' : 'Run the test'" @click="start"
        />
      </div>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <!-- Nothing run yet -->
    <UCard v-if="!report">
      <p class="text-sm text-muted">
        No test has run yet. It retrieves every URL of the suite below, which takes a few
        minutes; the page follows it live and ends with a report.
      </p>
    </UCard>

    <template v-else>
      <!-- Which report this is -->
      <div class="flex items-center gap-3 text-sm text-muted flex-wrap">
        <span v-if="running" class="inline-flex items-center gap-2 text-info">
          <UIcon name="i-fa7-solid-circle-notch" class="size-4 animate-spin" />
          Running · {{ summary.done }} of {{ summary.total }} URLs done
        </span>
        <span v-else>
          {{ viewing && viewing.id !== history[0]?.id ? 'Past run' : 'Last run' }} ·
          <span :title="absoluteTime(report.started)">{{ timeAgo(report.started) }}</span>
          <template v-if="report.state !== 'completed'"> · {{ report.state }}</template>
        </span>
        <NuxtLink
          v-if="report.job_id" :to="`/jobs/${report.job_id}`"
          class="inline-flex items-center gap-1 hover:text-default"
        >
          <UIcon name="i-fa7-solid-clock-rotate-left" class="size-3" /> the job
        </NuxtLink>
        <UButton
          v-if="viewing && viewing.id !== history[0]?.id" size="xs" variant="ghost"
          color="neutral" label="Back to the latest"
          @click="run.state === 'idle' ? openReport(history[0].id) : (viewing = null)"
        />
      </div>

      <!-- CAPTCHAs in the way: solve them, then rerun just those URLs -->
      <UAlert
        v-if="captchaResults.length && isLatest && !running" color="warning" variant="subtle"
        icon="i-fa7-solid-shield-halved"
        :title="`${captchaResults.length} URL${captchaResults.length === 1 ? '' : 's'} ran into a CAPTCHA`"
      >
        <template #description>
          <div class="flex items-center justify-between gap-4 flex-wrap">
            <span>
              Solve the checks on the
              <NuxtLink to="/captcha" class="underline font-medium">CAPTCHA page</NuxtLink>,
              then rerun them. The rerun uses what solving retrieved and cached.
            </span>
            <UButton
              size="sm" color="warning" variant="soft" icon="i-fa7-solid-rotate"
              :loading="rerunning" label="Rerun the CAPTCHA URLs" @click="rerunCaptchas"
            />
          </div>
        </template>
      </UAlert>

      <!-- Headline figures -->
      <!-- Four tiles in a row only where they have room for their figures -->
      <div class="grid gap-2.5 grid-cols-2 lg:grid-cols-4">
        <StatCard
          v-for="t in tiles" :key="t.label" :label="t.label" :icon="t.icon"
          :value="t.value" :detail="t.detail" :tone="t.tone" :ring="(t as any).ring"
        />
      </div>

      <!-- Progress and outcome split -->
      <UCard>
        <div class="space-y-3">
          <VizOutcomeBar
            :passed="summary.passed" :partial="summary.partial" :failed="summary.failed"
            :pending="summary.total - summary.done" :height="18"
          />
          <!-- The legend, with the numbers: identity never rests on colour alone -->
          <div class="flex flex-wrap gap-x-5 gap-y-1 text-sm">
            <span v-for="(o, key) in OUTCOME" :key="key" class="inline-flex items-center gap-1.5">
              <UIcon :name="o.icon" class="size-3.5" :style="{ color: o.color }" />
              <span class="text-muted">{{ o.label }}</span>
              <span class="text-highlighted tabular-nums">{{ summary[key] }}</span>
            </span>
            <span v-if="summary.total > summary.done" class="inline-flex items-center gap-1.5">
              <span class="size-3 rounded-sm" style="background: var(--viz-track)" />
              <span class="text-muted">Pending</span>
              <span class="text-highlighted tabular-nums">{{ summary.total - summary.done }}</span>
            </span>
          </div>
        </div>
      </UCard>

      <div class="grid gap-4 lg:grid-cols-2">
        <!-- Coverage by category -->
        <UCard>
          <template #header><h2 class="font-medium text-sm">Coverage by category</h2></template>
          <div class="space-y-2.5">
            <div
              v-for="c in summary.categories" :key="c.category"
              class="grid grid-cols-[minmax(0,9rem)_1fr_auto] items-center gap-3 text-sm"
            >
              <span class="truncate text-muted" :title="c.category">{{ c.category }}</span>
              <VizOutcomeBar :passed="c.passed" :partial="c.partial" :failed="c.failed" />
              <span class="tabular-nums text-xs text-highlighted w-10 text-right">
                {{ c.passed }}/{{ c.done }}
              </span>
            </div>
          </div>
        </UCard>

        <!-- Speed -->
        <UCard>
          <template #header>
            <div class="flex items-baseline justify-between gap-2">
              <h2 class="font-medium text-sm">How long retrievals took</h2>
              <span class="text-xs text-dimmed">URLs per time range</span>
            </div>
          </template>
          <VizColumnChart :items="timeHistogram" />
          <div class="mt-4 space-y-2">
            <h3 class="text-xs font-semibold uppercase tracking-wider text-dimmed">Median time by category</h3>
            <VizBarList :items="categoryTimes" />
          </div>
        </UCard>

        <!-- Methods -->
        <UCard>
          <template #header><h2 class="font-medium text-sm">Which methods delivered</h2></template>
          <VizBarList :items="methodBars" :total="summary.passed + summary.partial" />
        </UCard>

        <!-- Failures and media -->
        <UCard>
          <template #header><h2 class="font-medium text-sm">Why URLs failed</h2></template>
          <VizBarList :items="errorBars" :total="summary.failed" color="var(--viz-critical)" />
          <div class="mt-5 space-y-2">
            <h3 class="text-xs font-semibold uppercase tracking-wider text-dimmed">
              Expected media found
            </h3>
            <div
              v-for="(m, kind) in summary.media" :key="kind"
              class="grid grid-cols-[minmax(0,9rem)_1fr_auto] items-center gap-3 text-sm"
            >
              <span class="text-muted capitalize">{{ kind }}s</span>
              <div class="h-3 rounded-r-[4px] overflow-hidden" style="background: var(--viz-track)">
                <div
                  class="h-full rounded-r-[4px] transition-[width] duration-500"
                  :style="{ width: m.expected ? `${(m.found / m.expected) * 100}%` : '0%',
                            background: TONE_COLOR[shareTone(m.expected ? m.found / m.expected : null)] }"
                />
              </div>
              <span class="tabular-nums text-xs text-highlighted">{{ m.found }} of {{ m.expected }}</span>
            </div>
          </div>
        </UCard>
      </div>

      <!-- Over time -->
      <UCard v-if="trend.length > 1">
        <template #header>
          <div class="flex items-baseline justify-between gap-2">
            <h2 class="font-medium text-sm">Over the last {{ trend.length }} runs</h2>
            <span class="text-xs text-dimmed">oldest to newest</span>
          </div>
        </template>
        <!-- Two measures, two charts: never one chart with two scales -->
        <div class="grid gap-6 md:grid-cols-2">
          <div>
            <h3 class="text-xs text-muted mb-2">Coverage</h3>
            <VizColumnChart
              :items="trendCoverage" :max="1" :format="(v: number) => `${Math.round(v * 100)}%`"
              labels="all" :height="120"
            />
          </div>
          <div>
            <h3 class="text-xs text-muted mb-2">Median retrieval time</h3>
            <VizColumnChart
              :items="trendMedian" :format="(v: number) => `${v.toFixed(1)}s`"
              labels="all" :height="120"
            />
          </div>
        </div>
        <div class="mt-4 flex flex-wrap gap-2">
          <UButton
            v-for="(h, i) in trend" :key="h.id" size="xs" color="neutral"
            :variant="(viewing?.id || run.id) === h.id ? 'soft' : 'ghost'"
            :label="`#${i + 1} · ${percent(h.summary.coverage)}`" :title="absoluteTime(h.started)"
            @click="openReport(h.id)"
          />
        </div>
      </UCard>

      <!-- Every URL -->
      <UCard>
        <template #header>
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <h2 class="font-medium text-sm">Every URL</h2>
            <UTabs
              v-model="outcomeFilter" size="xs" :content="false"
              :items="[
                { label: `All ${report.results.length + inProgress.length}`, value: 'all' },
                ...(inProgress.length ? [{ label: `Running ${inProgress.length}`, value: 'running' }] : []),
                { label: `Failed ${summary.failed}`, value: 'failed' },
                { label: `Partial ${summary.partial}`, value: 'partial' },
                { label: `Passed ${summary.passed}`, value: 'passed' },
              ]"
            />
          </div>
        </template>
        <div class="divide-y divide-default">
          <div
            v-for="r in rows" :key="r.url"
            class="py-2 grid grid-cols-[auto_minmax(0,1fr)_auto] gap-x-3 items-start"
          >
            <UIcon
              v-if="r.outcome === 'running'" name="i-fa7-solid-circle-notch"
              class="size-4 mt-0.5 animate-spin text-info" title="In progress"
            />
            <UIcon
              v-else
              :name="OUTCOME[r.outcome as keyof typeof OUTCOME].icon" class="size-4 mt-0.5"
              :style="{ color: OUTCOME[r.outcome as keyof typeof OUTCOME].color }"
              :title="OUTCOME[r.outcome as keyof typeof OUTCOME].label"
            />
            <div class="min-w-0">
              <!-- To this URL's entry in the run's job, where the full result is -->
              <div class="flex items-center gap-1.5 min-w-0">
                <!-- A rerun result lives in the rerun's own job -->
                <NuxtLink
                  v-if="r.job_id || report.job_id"
                  :to="{ path: `/jobs/${r.job_id || report.job_id}`, query: { result: r.url } }"
                  class="flex min-w-0 hover:underline" title="Show the result in the job"
                >
                  <UrlLabel :url="r.url" class="text-sm" />
                </NuxtLink>
                <UrlLabel v-else :url="r.url" class="text-sm" />
                <a
                  :href="r.url" target="_blank" rel="noopener noreferrer"
                  class="shrink-0 text-dimmed hover:text-default" title="Open the original page"
                  aria-label="Open the original page"
                >
                  <UIcon name="i-fa7-solid-arrow-up-right-from-square" class="size-3" />
                </a>
              </div>
              <p class="text-xs text-dimmed mt-0.5 flex flex-wrap gap-x-3">
                <span>{{ r.category }}</span>
                <span v-if="r.method">{{ r.method }}</span>
                <span v-if="r.outcome === 'running'">
                  in progress<template v-if="r.expect"> · expects: {{ r.expect }}</template>
                  <template v-else-if="Object.keys(r.expected).length"> · expects {{ media(r.expected) }}</template>
                </span>
                <span v-else-if="r.expect" class="inline-flex items-center gap-1.5">
                  expected: {{ r.expect }} · got:
                  <span
                    class="inline-flex items-center gap-1"
                    :class="outcomeLook(r.result_class, r.result_kind).text"
                  >
                    <UIcon :name="outcomeLook(r.result_class, r.result_kind).icon" class="size-3" />
                    {{ outcomeLook(r.result_class, r.result_kind).label }}
                  </span>
                </span>
                <span v-else-if="Object.keys(r.expected).length">
                  expected {{ media(r.expected) }} · found {{ media(r.found) }}
                </span>
              </p>
              <p
                v-if="r.outcome === 'failed' && r.expect && r.result_class === 'ok'"
                class="text-xs text-error/90 mt-0.5"
              >
                Content came back, but the target was expected to be unavailable.
              </p>
              <p v-else-if="r.outcome === 'failed'" class="text-xs text-error/90 mt-0.5" :title="r.error?.message">
                {{ describeError(r.error?.type) }}
              </p>
              <p v-else-if="r.outcome === 'partial'" class="text-xs text-warning/90 mt-0.5">
                Missing {{ media(r.missing) }}.
              </p>
            </div>
            <span class="text-xs text-dimmed tabular-nums">{{ r.outcome === 'running' ? '' : secs(r.retrieval_time) }}</span>
          </div>
        </div>
      </UCard>
    </template>

    <!-- The suite -->
    <UCard>
      <template #header>
        <div class="flex items-center justify-between gap-3">
          <button type="button" class="font-medium text-sm inline-flex items-center gap-2" @click="showSuite = !showSuite">
            <UIcon name="i-fa7-solid-chevron-right" class="size-3 transition-transform" :class="showSuite ? 'rotate-90' : ''" />
            The suite · {{ suite.length }} URLs in {{ categories.length }} categories
          </button>
          <UButton size="xs" variant="ghost" color="neutral" label="Restore removed defaults" @click="restoreDefaults" />
        </div>
      </template>

      <form class="grid gap-2 sm:grid-cols-[1fr_12rem_auto_auto_auto_auto] items-end" @submit.prevent="addUrl">
        <UFormField label="Add a URL">
          <UInput v-model="newUrl.url" placeholder="https://…" class="w-full" />
        </UFormField>
        <UFormField label="Category">
          <UInput v-model="newUrl.category" placeholder="Added" list="test-categories" class="w-full" />
          <datalist id="test-categories">
            <option v-for="c in categories" :key="c" :value="c" />
          </datalist>
        </UFormField>
        <UFormField label="Images" hint="at least">
          <UInput
            v-model.number="newUrl.image" type="number" min="0" class="w-20"
            :disabled="newUrl.unavailable"
          />
        </UFormField>
        <UFormField label="Videos" hint="at least">
          <UInput
            v-model.number="newUrl.video" type="number" min="0" class="w-20"
            :disabled="newUrl.unavailable"
          />
        </UFormField>
        <!-- For targets that are not there to be had: a private post, a removed page.
             Such an entry passes when scrapeMM recognises that. -->
        <UFormField label="Expects" hint="instead of media">
          <UCheckbox
            v-model="newUrl.unavailable" label="Unavailable" class="h-8 items-center"
            title="Passes when the target is recognised as unavailable (yellow), fails when content comes back or scrapeMM errs"
          />
        </UFormField>
        <UButton type="submit" icon="i-fa7-solid-plus" label="Add" :loading="adding" :disabled="!newUrl.url.trim()" />
      </form>

      <div v-if="showSuite" class="mt-4 divide-y divide-default max-h-[32rem] overflow-y-auto">
        <div v-for="e in suite" :key="e.url" class="py-1.5 flex items-center gap-3 group">
          <UrlLabel :url="e.url" class="text-sm flex-1" />
          <span class="text-xs text-dimmed shrink-0">{{ e.category }}</span>
          <span class="shrink-0 w-28 text-right text-xs text-dimmed">
            <UBadge
              v-if="e.expect" size="sm" variant="subtle" color="warning"
              :icon="outcomeLook(e.expect).icon" :label="`expects: ${e.expect}`"
            />
            <template v-else>{{ media(e.expected) }}</template>
          </span>
          <UBadge v-if="e.source === 'user'" size="sm" variant="subtle" color="neutral" label="added" />
          <UButton
            size="xs" variant="ghost" color="error" icon="i-fa7-solid-xmark"
            class="opacity-0 group-hover:opacity-100 reveal-on-hover" aria-label="Remove from the suite"
            @click="removeUrl(e.url)"
          />
        </div>
      </div>
    </UCard>
  </div>
</template>
