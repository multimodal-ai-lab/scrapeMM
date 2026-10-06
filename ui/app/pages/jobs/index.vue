<script setup lang="ts">
/**
 * Everything this server has ever retrieved, searchable, sortable and live.
 *
 * Kept alive while you look at a job (see app.vue), so coming back shows exactly what
 * you left: the same filters, the same sorting, as many jobs loaded and the same scroll
 * position. It stays current by asking the server every few seconds whether anything
 * changed at all, which costs next to nothing while nothing does.
 */
defineOptions({ name: 'JobsOverview' })

const api = useApi()
const route = useRoute()
const router = useRouter()

const jobs = ref<any[]>([])
const stats = ref<any>({})
const methods = ref<string[]>([])
const total = ref(0)
const pageSize = 25
const loading = ref(false)
const loadingMore = ref(false)
const error = ref('')
let version: number | null = null

// Reka UI (behind USelect) refuses an empty-string option value, because it reserves
// the empty string for "no selection". So "any" is the sentinel, translated away when
// the query is built.
const ANY = 'any'

// Filters and sorting live in the query string, so a search can be linked to and
// survives a reload -- which is the whole point of being able to find a job again.
const filters = reactive({
  url: (route.query.url as string) || '',
  method: (route.query.method as string) || ANY,
  format: (route.query.format as string) || ANY,
  // `success` is what links from before the outcome classes used ('ok' / 'failed')
  outcome: (route.query.outcome as string)
    || ({ ok: 'ok', failed: 'error' } as Record<string, string>)[route.query.success as string]
    || ANY,
  since: (route.query.since as string) || ANY,
})
const sort = ref((route.query.sort as string) || 'newest')

function isSet(value: string) {
  return !!value && value !== ANY
}

const FORMATS = [
  { label: 'Any format', value: ANY },
  { label: 'Multimodal', value: 'multimodal' },
  { label: 'Markdown', value: 'markdown' },
  { label: 'HTML', value: 'html' },
]
// Grouped: the three classes, then the kinds of "unavailable" (see useOutcome.ts)
const OUTCOMES = outcomeFilterItems(ANY)
const OUTCOME_OPTIONS = OUTCOMES.flat().filter((o: any) => o.value) as
  { label: string, value: string, icon: string, tone: string }[]
const PERIODS = [
  { label: 'Any time', value: ANY },
  { label: 'Last hour', value: '1h' },
  { label: 'Today', value: 'today' },
  { label: 'Last 24 hours', value: '24h' },
  { label: 'Last 7 days', value: '7d' },
  { label: 'Last 30 days', value: '30d' },
]
const SORTS = [
  { label: 'Newest first', value: 'newest', icon: 'i-fa7-solid-arrow-down-wide-short' },
  { label: 'Oldest first', value: 'oldest', icon: 'i-fa7-solid-arrow-up-wide-short' },
  { label: 'Longest duration', value: 'longest', icon: 'i-fa7-solid-hourglass-end' },
  { label: 'Shortest duration', value: 'shortest', icon: 'i-fa7-solid-hourglass-start' },
  { label: 'Most URLs', value: 'most_urls', icon: 'i-fa7-solid-list-ol' },
  { label: 'Fewest URLs', value: 'fewest_urls', icon: 'i-fa7-solid-list' },
  { label: 'Most failures', value: 'most_failed', icon: 'i-fa7-solid-circle-exclamation' },
]

const methodOptions = computed(() => [
  { label: 'Any method', value: ANY },
  ...methods.value.map((m) => ({ label: m, value: m })),
])

const active = computed(() => Object.values(filters).filter(isSet).length)

/**
 * The filters currently in force, as things you can take off one at a time. Reading
 * them back out of the dropdowns' own option lists means a chip always says what the
 * control says -- "Last 24 hours", not "24h".
 */
const activeChips = computed(() => {
  const label = (options: { label: string, value: string }[], value: string) =>
    options.find((o) => o.value === value)?.label ?? value

  const chips: { key: keyof typeof filters, icon: string, text: string }[] = []
  if (isSet(filters.url)) {
    chips.push({ key: 'url', icon: 'i-fa7-solid-magnifying-glass', text: filters.url })
  }
  if (isSet(filters.method)) {
    chips.push({ key: 'method', icon: 'i-fa7-solid-wrench',
                 text: label(methodOptions.value, filters.method) })
  }
  if (isSet(filters.format)) {
    chips.push({ key: 'format', icon: 'i-fa7-solid-file-lines',
                 text: label(FORMATS, filters.format) })
  }
  if (isSet(filters.outcome)) {
    chips.push({ key: 'outcome',
                 icon: OUTCOME_OPTIONS.find((o) => o.value === filters.outcome)?.icon
                   || 'i-fa7-solid-circle-check',
                 text: label(OUTCOME_OPTIONS, filters.outcome) })
  }
  if (isSet(filters.since)) {
    chips.push({ key: 'since', icon: 'i-fa7-solid-clock',
                 text: label(PERIODS, filters.since) })
  }
  return chips
})

function clearFilter(key: keyof typeof filters) {
  filters[key] = key === 'url' ? '' : ANY
}

function reset() {
  Object.assign(filters, { url: '', method: ANY, format: ANY, outcome: ANY, since: ANY })
}

function sinceTimestamp(period: string): number | null {
  const spans: Record<string, number> = {
    '1h': 3600, '24h': 86400, '7d': 604800, '30d': 2592000,
  }
  if (period === 'today') return Math.floor(new Date().setHours(0, 0, 0, 0) / 1000)
  const span = spans[period]
  return span ? Math.floor(Date.now() / 1000 - span) : null
}

function query(offset: number, limit: number, withVersion = false) {
  const q = new URLSearchParams({ limit: String(limit), offset: String(offset), sort: sort.value })
  if (isSet(filters.url)) q.set('url', filters.url)
  if (isSet(filters.method)) q.set('method', filters.method)
  if (isSet(filters.format)) q.set('output_format', filters.format)
  if (isSet(filters.outcome)) q.set('outcome', filters.outcome)
  const since = isSet(filters.since) ? sinceTimestamp(filters.since) : null
  if (since) q.set('since', String(since))
  if (withVersion && version !== null) q.set('version', String(version))
  return `/v1/jobs?${q}`
}

function absorb(data: any) {
  version = data.version
  stats.value = data.stats
  total.value = data.total
  // Keep the known methods even when a filter narrows the result to none, or the
  // dropdown would empty itself out from under the filter that is in force
  if (data.methods?.length) methods.value = data.methods
}

/** Starts over: after a change of filters or sorting. */
async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api.get<any>(query(0, pageSize))
    absorb(data)
    jobs.value = data.jobs
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
  await nextTick()
  if (sentinelInView()) loadMore()
}

/** The next page, when the end of the list comes into view. */
async function loadMore() {
  if (loading.value || loadingMore.value || jobs.value.length >= total.value) return
  loadingMore.value = true
  try {
    const data = await api.get<any>(query(jobs.value.length, pageSize))
    absorb(data)
    // New jobs arriving at the top shift the offsets; never list one twice
    const known = new Set(jobs.value.map((j) => j.id))
    jobs.value = [...jobs.value, ...data.jobs.filter((j: any) => !known.has(j.id))]
  } catch (e: any) {
    error.value = e.message
    return
  } finally {
    loadingMore.value = false
  }
  // On a tall screen the end may still be in view; the observer only fires on change
  await nextTick()
  if (sentinelInView()) loadMore()
}

function sentinelInView() {
  const box = sentinel.value?.getBoundingClientRect()
  return !!box && box.top < window.innerHeight + 600
}

/** Keeps what is loaded current, as long as the history actually changed. */
async function refresh() {
  if (loading.value || loadingMore.value || document.hidden) return
  try {
    const limit = Math.min(Math.max(jobs.value.length, pageSize), 500)
    const data = await api.get<any>(query(0, limit, true))
    if (data.unchanged) return
    absorb(data)
    // Beyond what the refresh covers, keep what was loaded before
    const fresh = new Set(data.jobs.map((j: any) => j.id))
    jobs.value = [...data.jobs, ...jobs.value.slice(limit).filter((j) => !fresh.has(j.id))]
  } catch {
    // A missed refresh is no reason to disturb the page; the next one will try again
  }
}

function syncQuery() {
  const q: Record<string, string> = {}
  for (const [key, value] of Object.entries(filters)) if (isSet(value)) q[key] = value
  if (sort.value !== 'newest') q.sort = sort.value
  router.replace({ query: q })
}

// Typing in the URL box should not fire a request per keystroke
let debounce: ReturnType<typeof setTimeout> | null = null
watch([filters, sort], () => {
  syncQuery()
  version = null
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(() => load(), 250)
})

// Endless scrolling: the next page loads when the sentinel below the list comes near.
// A scroll listener (one check per frame) rather than an IntersectionObserver: the
// sentinel only exists once there are jobs, and an observer set up before that watches
// nothing.
const sentinel = ref<HTMLElement | null>(null)
let poller: ReturnType<typeof setInterval> | null = null
let frame = 0

function onScroll() {
  if (frame) return
  frame = requestAnimationFrame(() => {
    frame = 0
    if (sentinelInView()) loadMore()
  })
}

function startWatching() {
  if (!poller) poller = setInterval(refresh, 4000)
  window.addEventListener('scroll', onScroll, { passive: true })
  window.addEventListener('resize', onScroll, { passive: true })
}

function stopWatching() {
  if (poller) clearInterval(poller)
  poller = null
  window.removeEventListener('scroll', onScroll)
  window.removeEventListener('resize', onScroll)
}

onMounted(async () => {
  startWatching()
  await load()
  await nextTick()
  onScroll()  // A short first page may not fill the screen
})
// Kept alive: while a job is open, this page neither polls nor listens
onActivated(() => {
  startWatching()
  refresh()
})
onDeactivated(stopWatching)
onBeforeUnmount(stopWatching)

// --- Presentation ---------------------------------------------------------------

function urlLook(entry: any, job: any) {
  if (entry.state === 'ok' || entry.state === 'failed') {
    // Results stored before the outcome classes existed are classified on the server
    // too; the fallback only covers an entry without any
    const outcome = entry.outcome || (entry.state === 'ok' ? 'ok' : 'error')
    const look = outcomeLook(outcome, entry.outcome_kind)
    return { icon: look.icon, color: look.text,
             title: outcome === 'ok' && entry.from_cache ? 'Retrieved (from cache)' : look.label }
  }
  if (job.status === 'running') {
    return { icon: 'i-fa7-solid-circle-notch', color: 'text-info animate-spin', title: 'In progress' }
  }
  // Pending in a job that has ended: it was never retrieved
  return { icon: 'i-fa7-solid-circle-stop', color: 'text-dimmed', title: 'Not retrieved: the job was interrupted' }
}

function resultLink(job: any, url: string) {
  return { path: `/jobs/${job.id}`, query: { result: url } }
}

const copied = ref('')
async function copy(id: string) {
  if (!(await copyText(id))) return
  copied.value = id
  setTimeout(() => { if (copied.value === id) copied.value = '' }, 1500)
}
</script>

<template>
  <div class="space-y-5">
    <div>
      <h1 class="text-2xl font-semibold">Jobs</h1>
      <p class="text-sm text-muted mt-1">
        {{ stats.jobs }} jobs · {{ stats.urls }} URLs ·
        <span class="text-success">{{ stats.outcomes?.ok ?? stats.succeeded }} retrieved</span> ·
        <span
          :class="stats.outcomes?.unavailable ? 'text-warning' : ''"
          title="scrapeMM did its part, but the target was unavailable, behind a paywall or a CAPTCHA, or rate-limited"
        >{{ stats.outcomes?.unavailable ?? 0 }} unavailable</span> ·
        <span :class="stats.failed ? 'text-error' : ''">{{ stats.failed }} failed</span>
      </p>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <!-- Filters and sorting -->
    <div class="flex flex-wrap items-center gap-2">
      <UInput
        v-model="filters.url" placeholder="Search URLs…"
        icon="i-fa7-solid-magnifying-glass" class="flex-1 min-w-56"
        :ui="{ trailing: 'pe-1' }"
      >
        <template v-if="filters.url" #trailing>
          <UButton
            color="neutral" variant="link" size="xs" icon="i-fa7-solid-xmark"
            aria-label="Clear the URL filter" @click="filters.url = ''"
          />
        </template>
      </UInput>
      <USelect v-model="filters.method" :items="methodOptions" class="w-40" />
      <USelect v-model="filters.format" :items="FORMATS" class="w-40" />
      <USelect
        v-model="filters.outcome" :items="OUTCOMES" class="w-40" aria-label="Filter by outcome"
        :icon="OUTCOME_OPTIONS.find((o) => o.value === filters.outcome)?.icon"
        :ui="{ leadingIcon: OUTCOME_OPTIONS.find((o) => o.value === filters.outcome)?.tone }"
      >
        <template #item-leading="{ item }">
          <UIcon
            v-if="(item as any).icon" :name="(item as any).icon"
            class="size-4 shrink-0" :class="(item as any).tone"
          />
        </template>
      </USelect>
      <USelect v-model="filters.since" :items="PERIODS" class="w-40" />
      <USelect
        v-model="sort" :items="SORTS" class="w-48" aria-label="Sort the jobs"
        :icon="SORTS.find((s) => s.value === sort)?.icon"
      />
    </div>

    <!-- Chips and count share one row that is always present and always the same
         height, so applying or dropping a filter never moves the list underneath. -->
    <div class="-mt-2 min-h-7 flex items-center justify-between gap-3">
      <TransitionGroup tag="div" name="chips" class="flex flex-wrap items-center gap-1.5">
        <button
          v-for="chip in activeChips" :key="chip.key" type="button"
          class="surface-card inline-flex items-center gap-1.5 rounded-full
                 pl-2.5 pr-1.5 py-1 text-xs text-muted
                 hover:text-highlighted transition-colors"
          title="Remove this filter"
          @click="clearFilter(chip.key)"
        >
          <UIcon :name="chip.icon" class="size-2.5 text-dimmed" />
          <span class="max-w-40 truncate">{{ chip.text }}</span>
          <UIcon name="i-fa7-solid-xmark" class="size-2.5" />
        </button>
        <button
          v-if="activeChips.length > 1" key="clear-all" type="button"
          class="text-xs text-dimmed hover:text-highlighted transition-colors px-1"
          @click="reset"
        >
          Clear all
        </button>
      </TransitionGroup>
      <p
        class="text-xs text-dimmed shrink-0 transition-opacity duration-200"
        :class="active ? 'opacity-100' : 'opacity-0'"
      >
        {{ total }} matching job{{ total === 1 ? '' : 's' }}
      </p>
    </div>

    <!-- Results -->
    <div v-if="loading && !jobs.length" class="space-y-2">
      <div v-for="n in 5" :key="n" class="surface-card rounded-xl p-4 space-y-2">
        <USkeleton class="h-3 w-1/3" />
        <USkeleton class="h-9 w-full" />
      </div>
    </div>

    <UCard v-else-if="!jobs.length">
      <p class="text-sm text-muted">
        <template v-if="active">No job matches these filters.</template>
        <template v-else>
          Nothing retrieved yet. Try a URL in the
          <ULink to="/playground">playground</ULink>.
        </template>
      </p>
    </UCard>

    <div v-else class="space-y-3">
      <div v-for="job in jobs" :key="job.id" class="surface-card rounded-xl p-3 space-y-2.5 group">
        <!-- The job: when, how long, how, and its id -->
        <div class="flex items-center justify-between gap-3">
          <NuxtLink
            :to="`/jobs/${job.id}`"
            class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-dimmed min-w-0
                   hover:text-default transition-colors"
          >
            <span class="inline-flex items-center gap-1" :title="absoluteTime(job.created_at)">
              <UIcon name="i-fa7-solid-clock" class="size-3" />
              {{ timeAgo(job.created_at) }}
            </span>
            <span class="inline-flex items-center gap-1" title="How long the job took">
              <UIcon name="i-fa7-solid-stopwatch" class="size-3" />
              {{ seconds(job.duration) }}
            </span>
            <span class="inline-flex items-center gap-1">
              <UIcon name="i-fa7-solid-list" class="size-3" />
              {{ job.url_count }} URL{{ job.url_count === 1 ? '' : 's' }}
            </span>
            <span class="inline-flex items-center gap-1">
              <UIcon name="i-fa7-solid-file-lines" class="size-3" />
              {{ job.params.output_format }}
            </span>
            <span v-if="job.params.strip" class="inline-flex items-center gap-1" title="UI elements stripped">
              <UIcon name="i-fa7-solid-scissors" class="size-3" />
              stripped
            </span>
            <span v-if="job.methods?.length" class="inline-flex items-center gap-1">
              <UIcon name="i-fa7-solid-wrench" class="size-3" />
              {{ job.methods.join(', ') }}
            </span>
            <span
              v-if="job.url_count > 1 && job.outcomes" class="inline-flex items-center gap-2 tabular-nums"
              title="Retrieved · unavailable · failed"
            >
              <span class="inline-flex items-center gap-0.5 text-success">
                <UIcon name="i-fa7-solid-circle-check" class="size-3" />{{ job.outcomes.ok }}
              </span>
              <span v-if="job.outcomes.unavailable" class="inline-flex items-center gap-0.5 text-warning">
                <UIcon name="i-fa7-solid-circle-minus" class="size-3" />{{ job.outcomes.unavailable }}
              </span>
              <span v-if="job.outcomes.error" class="inline-flex items-center gap-0.5 text-error">
                <UIcon name="i-fa7-solid-circle-exclamation" class="size-3" />{{ job.outcomes.error }}
              </span>
            </span>
            <span v-if="job.status === 'running'" class="text-info">
              running · {{ job.done }} of {{ job.url_count }} done
            </span>
            <span v-else-if="job.status === 'interrupted'">interrupted</span>
          </NuxtLink>

          <!-- For support and debugging: quiet until the job is hovered -->
          <button
            type="button"
            class="shrink-0 inline-flex items-center gap-1 font-mono text-xs leading-none
                   text-dimmed opacity-0 transition-[opacity,color] duration-150
                   group-hover:opacity-100 focus-visible:opacity-100 hover:text-default"
            :title="`${job.id} — click to copy`"
            @click="copy(job.id)"
          >
            <UIcon name="i-fa7-solid-hashtag" class="size-3" />
            {{ copied === job.id ? 'copied' : job.id }}
          </button>
        </div>

        <!-- Its URLs, one per row, each leading to its own result -->
        <div class="space-y-1.5">
          <NuxtLink
            v-for="entry in job.urls" :key="entry.url" :to="resultLink(job, entry.url)"
            class="flex items-center gap-2.5 rounded-lg px-3 py-2 min-w-0
                   bg-elevated/50 hover:bg-elevated transition-colors"
          >
            <UIcon
              :name="urlLook(entry, job).icon" class="size-3.5 shrink-0"
              :class="urlLook(entry, job).color" :title="urlLook(entry, job).title"
            />
            <div class="min-w-0 flex-1 flex flex-col">
              <UrlLabel :url="entry.url" class="text-sm" />
              <span
                v-if="entry.state === 'failed'" class="text-xs truncate"
                :class="entry.outcome === 'unavailable' ? 'text-warning' : 'text-error/90'"
                :title="entry.error?.message"
              >{{ describeError(entry.error?.type) }}</span>
            </div>
            <span v-if="entry.state === 'ok'" class="shrink-0 text-xs text-dimmed flex items-center gap-2">
              <span v-if="entry.from_cache" class="inline-flex items-center gap-1" title="From cache">
                <UIcon name="i-fa7-solid-bolt" class="size-3" />
              </span>
              <span>{{ entry.method }}</span>
              <span v-if="entry.retrieval_time" :title="retrievalTimeTitle(entry.retrieval_time, entry.queue_time)">{{ seconds(entry.retrieval_time) }}</span>
            </span>
          </NuxtLink>
          <NuxtLink
            v-if="job.url_count > job.urls.length" :to="`/jobs/${job.id}`"
            class="block px-3 text-xs text-muted hover:text-default transition-colors"
          >
            + {{ job.url_count - job.urls.length }} more URL{{ job.url_count - job.urls.length === 1 ? '' : 's' }}
          </NuxtLink>
        </div>
      </div>

      <!-- Endless scrolling -->
      <div ref="sentinel" class="h-8 flex items-center justify-center">
        <UIcon
          v-if="loadingMore" name="i-fa7-solid-circle-notch"
          class="size-4 text-dimmed animate-spin"
        />
        <span v-else-if="jobs.length >= total && total > pageSize" class="text-xs text-dimmed">
          All {{ total }} jobs are shown.
        </span>
      </div>
    </div>
  </div>
</template>
