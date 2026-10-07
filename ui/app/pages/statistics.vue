<script setup lang="ts">
/**
 * Past retrievals over time: how many, by what method, with what outcome, how fast.
 *
 * The main chart stacks the retrievals of each hour, day or week by outcome or by
 * method. The figures and the smaller charts below cover the same range, so everything
 * on the page answers the same question. It refreshes itself every minute while open.
 */
const api = useApi()
const isAdmin = useIsAdmin()
const route = useRoute()
const router = useRouter()

type Bucket = 'hour' | 'day' | 'week'
type Group = 'outcome' | 'kind' | 'method' | 'key'

const BUCKETS = [
  { label: 'Hourly', value: 'hour' },
  { label: 'Daily', value: 'day' },
  { label: 'Weekly', value: 'week' },
]
const GROUPS = [
  { label: 'Outcome', value: 'outcome' },
  { label: 'Outcome, detailed', value: 'kind' },
  { label: 'Method', value: 'method' },
  { label: 'API key', value: 'key', admin: true }, // Names the keys: for those who manage them
]
const groups = computed(() => GROUPS.filter((g) => !g.admin || isAdmin.value))
// Each bucket's ranges; the first is the default
const RANGES: Record<Bucket, { label: string, value: number }[]> = {
  hour: [{ label: 'Last 48 hours', value: 48 }, { label: 'Last 24 hours', value: 24 },
         { label: 'Last 7 days', value: 168 }],
  day: [{ label: 'Last 30 days', value: 30 }, { label: 'Last 7 days', value: 7 },
        { label: 'Last 90 days', value: 90 }],
  week: [{ label: 'Last 26 weeks', value: 26 }, { label: 'Last 12 weeks', value: 12 },
         { label: 'Last 52 weeks', value: 52 }],
}

// In the query string, so a view can be linked to and survives a reload
const bucket = ref<Bucket>((['hour', 'day', 'week'].includes(route.query.bucket as string)
  ? route.query.bucket : 'hour') as Bucket)
const group = ref<Group>((['outcome', 'kind', 'method', 'key'].includes(route.query.group as string)
  ? route.query.group : 'outcome') as Group)
const periods = ref<number>(Number(route.query.periods) || RANGES[bucket.value][0]!.value)

// A link to the by-key view, opened with a Client key: the outcome view instead
const me = useMe()
watch(me, (value) => {
  if (value && !isAdmin.value && group.value === 'key') group.value = 'outcome'
}, { immediate: true })

const data = ref<any>(null)
const loading = ref(false)
const error = ref('')
const updated = ref<number | null>(null)
const showTable = ref(false)

async function load() {
  loading.value = true
  try {
    // Days and weeks start at local midnight: the offset from UTC, east positive
    const tz = -new Date().getTimezoneOffset()
    const q = new URLSearchParams({ bucket: bucket.value, group: group.value,
                                    periods: String(periods.value), tz_offset: String(tz) })
    data.value = await api.get<any>(`/v1/stats/retrievals?${q}`)
    updated.value = Date.now() / 1000
    error.value = ''
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

watch(bucket, (b) => {
  if (!RANGES[b].some((r) => r.value === periods.value)) periods.value = RANGES[b][0]!.value
})
watch([bucket, group, periods], () => {
  router.replace({ query: { bucket: bucket.value, group: group.value, periods: String(periods.value) } })
  load()
})

let poller: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  load()
  poller = setInterval(() => { if (!document.hidden) load() }, 60_000)
})
onBeforeUnmount(() => { if (poller) clearInterval(poller) })

// --- Labels ---------------------------------------------------------------------

function bucketLabel(start: number, long = false) {
  const d = new Date(start * 1000)
  if (bucket.value === 'hour') {
    const day = d.toLocaleDateString([], { month: 'short', day: 'numeric' })
    if (long) return `${day}, ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
    return d.getHours() === 0 ? day : d.toLocaleTimeString([], { hour: 'numeric' })
  }
  const day = d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  if (bucket.value === 'week') return long ? `Week of ${day}` : day
  return long ? d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' }) : day
}

const labels = computed(() => (data.value?.buckets || []).map((b: number) => bucketLabel(b)))
// For the small charts, which label every column: only every k-th, like the main chart
const sparseLabels = computed(() => {
  const step = Math.max(1, Math.ceil(labels.value.length / 4))
  return labels.value.map((l: string, i: number) => (i % step === 0 ? l : ''))
})
const titles = computed(() => (data.value?.buckets || []).map((b: number) => bucketLabel(b, true)))

// --- Series colours -------------------------------------------------------------

// Methods keep their colour whatever else is in view: the common ones have a slot of
// their own, the others take the free slots in name order. Slot 8 (red) is left out,
// since red means failure on this page.
const METHOD_SLOTS: Record<string, number> = {
  'browser': 1, 'Perma.cc': 2, 'Facebook': 3, 'Archive.today': 4,
  'X (Twitter)': 5, 'Instagram': 6, 'Telegram': 7,
}
const KIND_SHADES: Record<string, string> = {
  missing: '100%', captcha: '82%', paywall: '70%', blocked: '58%',
  rate_limit: '46%', unsupported: '34%',
}

const series = computed(() => {
  const raw: any[] = data.value?.series || []
  if (group.value === 'key') {
    // Most retrievals first, as the server sorts them; colours in that order
    let slot = 0
    return raw.map((s) => ({
      ...s,
      label: s.key === '(none)' ? 'No key' : s.key === 'Other'
        ? `Other (${s.members?.length || 0})` : s.name,
      color: s.key === '(none)' || s.key === 'Other' ? 'var(--viz-other)'
        : `var(--viz-cat-${Math.min(++slot, 7)})`,
      hint: s.key === '(none)' ? 'Retrievals from before keys were recorded'
        : s.key === 'Other' ? s.members?.join(', ') : undefined,
    }))
  }
  if (group.value === 'method') {
    const taken = new Set<number>()
    const colours: Record<string, string> = {}
    for (const s of raw) {
      const slot = METHOD_SLOTS[s.key]
      if (slot) { colours[s.key] = `var(--viz-cat-${slot})`; taken.add(slot) }
    }
    const free = [1, 2, 3, 4, 5, 6, 7].filter((n) => !taken.has(n))
    for (const s of [...raw].sort((a, b) => a.key.localeCompare(b.key))) {
      if (colours[s.key] || s.key === 'Other' || s.key === '(none)') continue
      const slot = free.shift()
      colours[s.key] = slot ? `var(--viz-cat-${slot})` : 'var(--viz-other)'
    }
    return raw.map((s) => ({
      ...s,
      label: s.key === '(none)' ? 'None (failed)' : s.key === 'Other'
        ? `Other (${s.members?.length || 0})` : s.key,
      color: s.key === '(none)' ? 'var(--viz-critical)'
        : s.key === 'Other' ? 'var(--viz-other)' : colours[s.key],
      hint: s.key === 'Other' ? s.members?.join(', ') : undefined,
    }))
  }
  return raw.map((s) => {
    if (s.key.startsWith('unavailable:')) {
      const kind = s.key.split(':')[1]
      // The kinds of yellow as steps of the one warning hue, so the stack still reads
      // as green / yellow / red at a glance
      return { ...s, label: outcomeLook('unavailable', kind).label,
               color: `color-mix(in oklab, var(--viz-warning) ${KIND_SHADES[kind] || '60%'}, var(--ui-bg))` }
    }
    const look = outcomeLook(s.key)
    return { ...s, label: look.label,
             color: { ok: 'var(--viz-good)', unavailable: 'var(--viz-warning)',
                      error: 'var(--viz-critical)' }[s.key as string] || 'var(--viz-other)' }
  })
})

// --- Figures --------------------------------------------------------------------

const summary = computed(() => data.value?.summary || null)
const empty = computed(() => !loading.value && data.value && !summary.value?.total)

function share(part: number, whole: number) {
  return whole ? part / whole : null
}
function percent(v: number | null) {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`
}

const rangeLabel = computed(() =>
  RANGES[bucket.value].find((r) => r.value === periods.value)?.label.toLowerCase() || '')

const tiles = computed(() => {
  const s = summary.value
  if (!s) return []
  const success = share(s.ok + s.unavailable, s.total)
  const tone = success == null ? 'neutral' as const : success > 0.95 ? 'success' as const
    : success >= 0.8 ? 'warning' as const : 'error' as const
  return [
    { label: 'Retrievals', icon: 'i-fa7-solid-list-check', value: s.total.toLocaleString(),
      detail: rangeLabel.value, tone: 'neutral' as const },
    { label: 'Success rate', icon: 'i-fa7-solid-check', value: percent(success), tone,
      ring: success,
      detail: `${s.error} failed · ${s.unavailable} unavailable · ${s.ok} retrieved` },
    { label: 'Cache hits', icon: 'i-fa7-solid-bolt', value: percent(share(s.cached, s.total)),
      ring: share(s.cached, s.total), tone: 'neutral' as const,
      detail: `${s.cached} of ${s.total} from the cache` },
    { label: 'Median time', icon: 'i-fa7-solid-stopwatch', value: seconds(s.median_time),
      tone: 'neutral' as const, title: RETRIEVAL_TIME_MEANING,
      detail: s.median_queue_time == null ? 'per retrieved URL, from its first request'
        : `per retrieved URL, from its first request · waited ${seconds(s.median_queue_time)} before` },
  ]
})

// Success rate per bucket: scrapeMM's successes (retrieved or unavailable) of all
const successItems = computed(() => {
  const p = data.value?.per_bucket
  if (!p) return []
  return p.total.map((t: number, i: number) => {
    const rate = share(p.ok[i] + p.unavailable[i], t)
    return {
      label: sparseLabels.value[i], value: rate ?? 0,
      color: rate == null ? 'var(--viz-track)' : rate > 0.95 ? 'var(--viz-good)'
        : rate >= 0.8 ? 'var(--viz-warning)' : 'var(--viz-critical)',
      tooltip: t ? `${titles.value[i]}: ${percent(rate)} (${p.error[i]} failed of ${t})`
        : `${titles.value[i]}: no retrievals`,
    }
  })
})

const timeItems = computed(() => {
  const p = data.value?.per_bucket
  if (!p) return []
  return p.median_time.map((m: number | null, i: number) => ({
    label: sparseLabels.value[i], value: m ?? 0,
    tooltip: m == null ? `${titles.value[i]}: no retrievals`
      : `${titles.value[i]}: median ${seconds(m)}, p90 ${seconds(p.p90_time[i])}`
        + (p.median_queue_time?.[i] != null ? `, after waiting ${seconds(p.median_queue_time[i])} for a slot` : ''),
  }))
})

const methodItems = computed(() => (data.value?.methods || []).slice(0, 10).map((m: any) => ({
  label: m.method, value: m.count,
  hint: `${m.method}: ${m.count} retrieved · median ${seconds(m.median_time)} · p90 ${seconds(m.p90_time)}`,
})))

const domainItems = computed(() => (data.value?.failing_domains || []).map((d: any) => ({
  label: d.domain, value: d.count, hint: `${d.domain}: ${d.count} failed (red)`,
})))

function jobsLinkFor(domain: string) {
  return { path: '/jobs', query: { url: domain, outcome: 'error' } }
}
</script>

<template>
  <div class="space-y-5">
    <div class="flex items-start justify-between gap-4 flex-wrap">
      <div>
        <h1 class="text-2xl font-semibold">Statistics</h1>
        <p class="text-sm text-muted mt-1">
          Past retrievals, {{ rangeLabel }}.
          <span v-if="updated" class="text-dimmed">Updated {{ timeAgo(updated) }}.</span>
        </p>
      </div>
      <UButton
        color="neutral" variant="ghost" icon="i-fa7-solid-rotate" label="Refresh"
        :loading="loading" @click="load"
      />
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <!-- Every control in one row, above everything it controls -->
    <div class="flex flex-wrap items-center gap-2">
      <UTabs v-model="bucket" :items="BUCKETS" :content="false" size="sm" />
      <USelect v-model="periods" :items="RANGES[bucket]" class="w-40" aria-label="Time range" />
      <span class="text-xs text-dimmed ml-1">grouped by</span>
      <UTabs v-model="group" :items="groups" :content="false" size="sm" />
    </div>

    <div v-if="loading && !data" class="space-y-3">
      <div class="grid gap-2.5 grid-cols-2 lg:grid-cols-4">
        <USkeleton v-for="n in 4" :key="n" class="h-20" />
      </div>
      <USkeleton class="h-72 w-full" />
    </div>

    <UCard v-else-if="empty">
      <p class="text-sm text-muted">
        No retrievals {{ rangeLabel }}. Try a longer range, or retrieve a URL in the
        <ULink to="/playground">playground</ULink>.
      </p>
    </UCard>

    <template v-else-if="data">
      <div class="grid gap-2.5 grid-cols-2 lg:grid-cols-4">
        <StatCard
          v-for="t in tiles" :key="t.label" :label="t.label" :icon="t.icon"
          :value="t.value" :detail="t.detail" :tone="t.tone" :ring="(t as any).ring"
        />
      </div>

      <!-- The main chart -->
      <UCard>
        <template #header>
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <h2 class="font-medium text-sm">
              Retrievals per {{ bucket }}, by {{ { method: 'method', key: 'API key' }[group as string] || 'outcome' }}
            </h2>
            <UButton
              size="xs" color="neutral" variant="ghost"
              :icon="showTable ? 'i-fa7-solid-chart-column' : 'i-fa7-solid-table'"
              :label="showTable ? 'Chart' : 'Table'" @click="showTable = !showTable"
            />
          </div>
        </template>

        <div v-if="!showTable" class="space-y-4">
          <VizStackedColumns :labels="labels" :titles="titles" :series="series" :height="240" />
          <!-- The legend, with each series' total: identity never rests on colour alone -->
          <div class="flex flex-wrap gap-x-5 gap-y-1.5 text-sm">
            <span
              v-for="s in [...series].reverse()" :key="s.key"
              class="inline-flex items-center gap-1.5" :title="s.hint"
            >
              <span class="size-2.5 rounded-sm shrink-0" :style="{ background: s.color }" />
              <span class="text-muted">{{ s.label }}</span>
              <span class="text-highlighted tabular-nums">{{ s.total.toLocaleString() }}</span>
            </span>
          </div>
        </div>

        <!-- The same numbers as a table: for exact values, and for reading without colour -->
        <div v-else class="overflow-x-auto max-h-96">
          <table class="w-full text-xs tabular-nums">
            <thead class="text-dimmed text-left sticky top-0 bg-default">
              <tr>
                <th class="py-1.5 pr-3 font-medium">{{ bucket === 'week' ? 'Week' : bucket === 'day' ? 'Day' : 'Hour' }}</th>
                <th v-for="s in [...series].reverse()" :key="s.key" class="py-1.5 px-2 font-medium text-right whitespace-nowrap">
                  {{ s.label }}
                </th>
                <th class="py-1.5 pl-2 font-medium text-right">Total</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-default">
              <tr v-for="(t, i) in [...titles].reverse()" :key="t">
                <td class="py-1 pr-3 text-muted whitespace-nowrap">{{ t }}</td>
                <td v-for="s in [...series].reverse()" :key="s.key" class="py-1 px-2 text-right">
                  {{ s.counts[titles.length - 1 - i] || '' }}
                </td>
                <td class="py-1 pl-2 text-right text-highlighted">
                  {{ data.per_bucket.total[titles.length - 1 - i] || '' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </UCard>

      <!-- Secondary charts, two per row where there is room -->
      <div class="grid gap-4 md:grid-cols-2">
        <UCard>
          <template #header>
            <h2 class="font-medium text-sm">Success rate per {{ bucket }}</h2>
            <p class="text-xs text-dimmed mt-0.5">Retrieved or unavailable, of all retrievals</p>
          </template>
          <VizColumnChart
            :items="successItems" :max="1" :height="120" wide-labels
            :format="(v: number) => `${Math.round(v * 100)}%`"
          />
        </UCard>

        <UCard>
          <template #header>
            <h2 class="font-medium text-sm">Median retrieval time per {{ bucket }}</h2>
            <p class="text-xs text-dimmed mt-0.5">Retrieved URLs, cache hits excluded; the p90 on hover</p>
          </template>
          <VizColumnChart :items="timeItems" :height="120" wide-labels :format="(v: number) => seconds(v)" />
        </UCard>

        <UCard>
          <template #header>
            <h2 class="font-medium text-sm">Retrieved by method</h2>
            <p class="text-xs text-dimmed mt-0.5">Median and p90 time on hover</p>
          </template>
          <VizBarList :items="methodItems" :total="summary?.ok" />
        </UCard>

        <UCard>
          <template #header>
            <h2 class="font-medium text-sm">Most failing domains</h2>
            <p class="text-xs text-dimmed mt-0.5">Failures on scrapeMM's end (red), {{ rangeLabel }}</p>
          </template>
          <VizBarList :items="domainItems" color="var(--viz-critical)" />
          <div v-if="domainItems.length" class="flex flex-wrap gap-x-3 gap-y-1 mt-3 text-xs">
            <NuxtLink
              v-for="d in domainItems.slice(0, 4)" :key="d.label" :to="jobsLinkFor(d.label)"
              class="text-muted hover:text-highlighted inline-flex items-center gap-1"
            >
              <UIcon name="i-fa7-solid-arrow-right" class="size-2.5" /> {{ d.label }} in Jobs
            </NuxtLink>
          </div>
        </UCard>
      </div>
    </template>
  </div>
</template>
