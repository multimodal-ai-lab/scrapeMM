<script setup lang="ts">
/**
 * Everything this server has ever retrieved and searched, searchable, sortable and live,
 * in three views: the jobs, every single retrieval (one URL of a job), and the searches.
 *
 * Kept alive while you look at a job (see app.vue), so coming back shows exactly what
 * you left: the same view, filters and sorting, as much loaded and the same scroll
 * position. It stays current by asking the server every few seconds whether anything
 * changed at all, which costs next to nothing while nothing does.
 */
defineOptions({ name: 'HistoryOverview' })

const api = useApi()
const route = useRoute()
const router = useRouter()

type View = 'jobs' | 'retrievals' | 'searches'
const VIEWS = [
  { label: 'Jobs', value: 'jobs', icon: 'i-fa7-solid-layer-group' },
  { label: 'Retrievals', value: 'retrievals', icon: 'i-fa7-solid-link' },
  { label: 'Searches', value: 'searches', icon: 'i-fa7-solid-magnifying-glass' },
]
const view = ref<View>(VIEWS.some((v) => v.value === route.query.view)
  ? route.query.view as View : 'jobs')

const items = ref<any[]>([])
const stats = ref<any>({})
const methods = ref<string[]>([])
const providers = ref<string[]>([])
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
const STATUS_PREFIX = 'status:'

// Filters and sorting live in the query string, so a search can be linked to and
// survives a reload -- which is the whole point of being able to find something again.
// `url` is the text box: URLs in jobs and retrievals, the query in searches.
const filters = reactive({
  url: (route.query.url as string) || '',
  method: (route.query.method as string) || ANY,
  format: (route.query.format as string) || ANY,
  // One filter for a URL's outcome and a job's status: the status as `status:<status>`.
  // `success` is what links from before the outcome classes used ('ok' / 'failed').
  outcome: (route.query.outcome as string)
    || (route.query.status && view.value === 'jobs' ? `${STATUS_PREFIX}${route.query.status}` : '')
    || ({ ok: 'ok', failed: 'error' } as Record<string, string>)[route.query.success as string]
    || ANY,
  provider: (route.query.provider as string) || ANY,
  // A search's status: answered, failed, or an HTTP status that says why
  status: (view.value === 'searches' && route.query.status as string) || ANY,
  since: (route.query.since as string) || ANY,
})
type FilterKey = keyof typeof filters

/** Which filters each view has, in the order of their controls */
const VIEW_FILTERS: Record<View, FilterKey[]> = {
  jobs: ['url', 'method', 'format', 'outcome', 'since'],
  retrievals: ['url', 'method', 'format', 'outcome', 'since'],
  searches: ['url', 'provider', 'status', 'since'],
}
const applicable = (key: FilterKey) => VIEW_FILTERS[view.value].includes(key)

// Sorting: by what, and which way. Together they name one of the server's orders.
const SORT_ORDERS: Record<View, Record<string, [string, string]>> = {  // key: [descending, ascending]
  jobs: {
    date: ['newest', 'oldest'],
    duration: ['longest', 'shortest'],
    urls: ['most_urls', 'fewest_urls'],
    failures: ['most_failed', 'fewest_failed'],
  },
  retrievals: { date: ['newest', 'oldest'], duration: ['slowest', 'fastest'] },
  searches: {
    date: ['newest', 'oldest'],
    duration: ['slowest', 'fastest'],
    results: ['most_results', 'fewest_results'],
  },
}
const orders = computed(() => SORT_ORDERS[view.value])
const initialSort = (route.query.sort as string) || 'newest'
const sortKey = ref(Object.keys(orders.value).find((k) => orders.value[k]!.includes(initialSort)) || 'date')
const sortDir = ref(Object.values(orders.value).some((o) => o[1] === initialSort) ? 'asc' : 'desc')
const sort = computed(() => (orders.value[sortKey.value] || orders.value.date!)[sortDir.value === 'asc' ? 1 : 0])

function isSet(value: string) {
  return !!value && value !== ANY
}

// Every option carries an icon (and `tone`, its colour), shown in the list and, for the
// chosen one, in the select itself (see `optionLook()`)
const FORMATS = [
  { label: 'Any format', value: ANY, icon: 'i-fa7-solid-file', tone: 'text-dimmed' },
  { label: 'Multimodal', value: 'multimodal', icon: 'i-fa7-solid-photo-film', tone: 'text-muted' },
  { label: 'Markdown', value: 'markdown', icon: 'i-fa7-brands-markdown', tone: 'text-muted' },
  { label: 'HTML', value: 'html', icon: 'i-fa7-solid-code', tone: 'text-muted' },
]
// Grouped: the three classes, the kinds of "unavailable" (see useOutcome.ts), and, for
// jobs, the job's own status
const JOB_STATUSES = [
  { type: 'label' as const, label: 'Job status' },
  { label: 'Running', value: `${STATUS_PREFIX}running`, icon: 'i-fa7-solid-circle-notch', tone: 'text-info' },
  { label: 'Completed', value: `${STATUS_PREFIX}completed`, icon: 'i-fa7-solid-flag-checkered', tone: 'text-success' },
  { label: 'Failed', value: `${STATUS_PREFIX}failed`, icon: 'i-fa7-solid-bug', tone: 'text-error' },
  { label: 'Interrupted', value: `${STATUS_PREFIX}interrupted`, icon: 'i-fa7-solid-circle-stop', tone: 'text-dimmed' },
]
const outcomeOptions = computed(() => view.value === 'jobs'
  ? [...outcomeFilterItems(ANY), JOB_STATUSES] : outcomeFilterItems(ANY))
const SEARCH_STATUSES = [
  { label: 'Any status', value: ANY, icon: 'i-fa7-solid-circle-half-stroke', tone: 'text-dimmed' },
  { label: 'Answered', value: 'ok', icon: 'i-fa7-solid-circle-check', tone: 'text-success' },
  { label: 'Failed', value: 'failed', icon: 'i-fa7-solid-circle-exclamation', tone: 'text-error' },
  { label: 'Rate-limited', value: '429', icon: 'i-fa7-solid-hourglass-half', tone: 'text-warning' },
  { label: 'Out of quota', value: '402', icon: 'i-fa7-solid-coins', tone: 'text-warning' },
  { label: 'Not available', value: '503', icon: 'i-fa7-solid-plug-circle-xmark', tone: 'text-dimmed' },
]
const PERIODS = [
  { label: 'Any time', value: ANY, icon: 'i-fa7-solid-infinity', tone: 'text-dimmed' },
  { label: 'Last hour', value: '1h', icon: 'i-fa7-solid-clock', tone: 'text-muted' },
  { label: 'Today', value: 'today', icon: 'i-fa7-solid-sun', tone: 'text-muted' },
  { label: 'Last 24 hours', value: '24h', icon: 'i-fa7-solid-clock-rotate-left', tone: 'text-muted' },
  { label: 'Last 7 days', value: '7d', icon: 'i-fa7-solid-calendar-week', tone: 'text-muted' },
  { label: 'Last 30 days', value: '30d', icon: 'i-fa7-solid-calendar-days', tone: 'text-muted' },
]
const SORT_LOOKS: Record<string, { label: string, icon: string, words: [string, string] }> = {
  date: { label: 'Date', icon: 'i-fa7-solid-calendar', words: ['Newest first', 'Oldest first'] },
  duration: { label: 'Duration', icon: 'i-fa7-solid-hourglass-half', words: ['Longest first', 'Shortest first'] },
  urls: { label: 'URLs', icon: 'i-fa7-solid-list-ol', words: ['Most first', 'Fewest first'] },
  failures: { label: 'Failures', icon: 'i-fa7-solid-circle-exclamation', words: ['Most first', 'Fewest first'] },
  results: { label: 'Results', icon: 'i-fa7-solid-list-ol', words: ['Most first', 'Fewest first'] },
}
const sortKeys = computed(() => Object.keys(orders.value).map((key) => ({
  label: SORT_LOOKS[key]!.label, value: key, icon: SORT_LOOKS[key]!.icon, tone: 'text-muted',
})))
// The direction in the words of the property: "Newest first" reads better than "Descending"
const SORT_DIRECTIONS = computed(() => {
  const [down, up] = SORT_LOOKS[sortKey.value]?.words || ['Descending', 'Ascending']
  return [
    { label: down, value: 'desc', icon: 'i-fa7-solid-arrow-down-wide-short', tone: 'text-muted' },
    { label: up, value: 'asc', icon: 'i-fa7-solid-arrow-up-wide-short', tone: 'text-muted' },
  ]
})

const methodOptions = computed(() => [
  { label: 'Any method', value: ANY, icon: 'i-fa7-solid-screwdriver-wrench', tone: 'text-dimmed' },
  ...methods.value.map((m) => ({ label: m, value: m, icon: 'i-fa7-solid-wrench', tone: 'text-muted' })),
])
const providerOptions = computed(() => [
  { label: 'Any provider', value: ANY, icon: 'i-fa7-solid-server', tone: 'text-dimmed' },
  ...providers.value.map((p) => ({ label: providerLabel(p), value: p, icon: 'i-fa7-solid-magnifying-glass', tone: 'text-muted' })),
])

function providerLabel(name: string) {
  return { serper: 'Serper' }[name] || name
}

/** The dropdowns of the view in force, in order (the text box comes first, apart) */
const selects = computed(() => {
  const all: Record<string, { items: any[], label: string, width: string }> = {
    method: { items: methodOptions.value, label: 'Filter by method', width: 'w-40' },
    format: { items: FORMATS, label: 'Filter by output format', width: 'w-40' },
    outcome: { items: outcomeOptions.value, label: view.value === 'jobs' ? 'Filter by outcome or job status' : 'Filter by outcome', width: 'w-40' },
    provider: { items: providerOptions.value, label: 'Filter by provider', width: 'w-40' },
    status: { items: SEARCH_STATUSES, label: 'Filter by status', width: 'w-40' },
    since: { items: PERIODS, label: 'Filter by time', width: 'w-40' },
  }
  return VIEW_FILTERS[view.value].filter((key) => key !== 'url')
    .map((key) => ({ model: key, ...all[key]! }))
})

/** The icon and its colour of the option `value` among `items`, for the select's own face */
function optionLook(items: any[], value: string): { icon?: string, tone?: string } {
  const option = items.flat().find((o: any) => o.value === value)
  return { icon: option?.icon, tone: option?.tone }
}

const active = computed(() => VIEW_FILTERS[view.value].filter((k) => isSet(filters[k])).length)

/**
 * The filters currently in force, as things you can take off one at a time. Reading
 * them back out of the dropdowns' own option lists means a chip always says what the
 * control says -- "Last 24 hours", not "24h".
 */
const activeChips = computed(() => {
  const chips: { key: FilterKey, icon: string, text: string }[] = []
  if (isSet(filters.url)) {
    chips.push({ key: 'url', icon: 'i-fa7-solid-magnifying-glass', text: filters.url })
  }
  for (const select of selects.value) {
    const value = filters[select.model]
    if (!isSet(value)) continue
    const option = select.items.flat().find((o: any) => o.value === value)
    chips.push({ key: select.model, icon: option?.icon || 'i-fa7-solid-filter',
                 text: option?.label ?? value })
  }
  return chips
})

function clearFilter(key: FilterKey) {
  filters[key] = key === 'url' ? '' : ANY
}

function reset() {
  for (const key of VIEW_FILTERS[view.value]) clearFilter(key)
}

function sinceTimestamp(period: string): number | null {
  const spans: Record<string, number> = {
    '1h': 3600, '24h': 86400, '7d': 604800, '30d': 2592000,
  }
  if (period === 'today') return Math.floor(new Date().setHours(0, 0, 0, 0) / 1000)
  const span = spans[period]
  return span ? Math.floor(Date.now() / 1000 - span) : null
}

const ENDPOINTS: Record<View, string> = {
  jobs: '/v1/jobs', retrievals: '/v1/retrievals', searches: '/v1/searches',
}

function query(offset: number, limit: number, withVersion = false) {
  const q = new URLSearchParams({ limit: String(limit), offset: String(offset), sort: sort.value })
  if (isSet(filters.url)) q.set(view.value === 'searches' ? 'query' : 'url', filters.url)
  if (applicable('method') && isSet(filters.method)) q.set('method', filters.method)
  if (applicable('format') && isSet(filters.format)) q.set('output_format', filters.format)
  if (applicable('outcome') && isSet(filters.outcome)) {
    if (filters.outcome.startsWith(STATUS_PREFIX)) q.set('status', filters.outcome.slice(STATUS_PREFIX.length))
    else q.set('outcome', filters.outcome)
  }
  if (applicable('provider') && isSet(filters.provider)) q.set('provider', filters.provider)
  if (applicable('status') && isSet(filters.status)) q.set('status', filters.status)
  const since = isSet(filters.since) ? sinceTimestamp(filters.since) : null
  if (since) q.set('since', String(since))
  if (withVersion && version !== null) q.set('version', String(version))
  return `${ENDPOINTS[view.value]}?${q}`
}

/** What tells two entries apart, to never list one twice */
function keyOf(item: any): string {
  return view.value === 'retrievals' ? `${item.job_id}|${item.url}` : String(item.id)
}

function absorb(data: any) {
  version = data.version
  stats.value = data.stats
  total.value = data.total
  // Keep the known methods even when a filter narrows the result to none, or the
  // dropdown would empty itself out from under the filter that is in force
  if (data.methods?.length) methods.value = data.methods
  if (data.providers?.length) providers.value = data.providers
}

/** The entries of an answer, whichever view it is for */
function entries(data: any): any[] {
  return data[view.value] || []
}

/** Starts over: after a change of view, filters or sorting. */
async function load() {
  loading.value = true
  error.value = ''
  const asked = view.value
  try {
    const data = await api.get<any>(query(0, pageSize))
    if (asked !== view.value) return  // Switched meanwhile: the next load has it
    absorb(data)
    items.value = entries(data)
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
  if (loading.value || loadingMore.value || items.value.length >= total.value) return
  loadingMore.value = true
  const asked = view.value
  try {
    const data = await api.get<any>(query(items.value.length, pageSize))
    if (asked !== view.value) return
    absorb(data)
    // New entries arriving at the top shift the offsets; never list one twice
    const known = new Set(items.value.map(keyOf))
    items.value = [...items.value, ...entries(data).filter((i) => !known.has(keyOf(i)))]
  } catch (e: any) {
    error.value = e.message
    return
  } finally {
    loadingMore.value = false
  }
  // On a tall screen the end may still be in view; the scroll check only runs on scroll
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
  const asked = view.value
  try {
    const limit = Math.min(Math.max(items.value.length, pageSize), 500)
    const data = await api.get<any>(query(0, limit, true))
    if (data.unchanged || asked !== view.value) return
    absorb(data)
    // Beyond what the refresh covers, keep what was loaded before
    const loaded = entries(data)
    const fresh = new Set(loaded.map(keyOf))
    items.value = [...loaded, ...items.value.slice(limit).filter((i) => !fresh.has(keyOf(i)))]
  } catch {
    // A missed refresh is no reason to disturb the page; the next one will try again
  }
}

function syncQuery() {
  const q: Record<string, string> = {}
  if (view.value !== 'jobs') q.view = view.value
  for (const key of VIEW_FILTERS[view.value]) {
    const value = filters[key]
    if (!isSet(value)) continue
    if (key === 'outcome' && value.startsWith(STATUS_PREFIX)) q.status = value.slice(STATUS_PREFIX.length)
    else q[key] = value
  }
  if (sort.value !== 'newest') q.sort = sort.value
  router.replace({ query: q })
}

// Another view: what it has no use for is dropped, a job status among them (only jobs
// have one), and so is a sorting it does not offer
watch(view, () => {
  if (view.value !== 'jobs' && filters.outcome.startsWith(STATUS_PREFIX)) filters.outcome = ANY
  if (!orders.value[sortKey.value]) sortKey.value = 'date'
  items.value = []
  total.value = 0
  stats.value = {}
})

// Typing in the text box should not fire a request per keystroke
let debounce: ReturnType<typeof setTimeout> | null = null
watch([view, filters, sort], () => {
  syncQuery()
  version = null
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(() => load(), 250)
})

// Endless scrolling: the next page loads when the sentinel below the list comes near.
// A scroll listener (one check per frame) rather than an IntersectionObserver: the
// sentinel only exists once there are entries, and an observer set up before that
// watches nothing.
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

/** What the view's entries are called, for counts */
const NOUNS: Record<View, [string, string]> = {
  jobs: ['job', 'jobs'], retrievals: ['retrieval', 'retrievals'], searches: ['search', 'searches'],
}
function noun(n: number) {
  return NOUNS[view.value][n === 1 ? 0 : 1]
}

const searchPlaceholder = computed(() =>
  view.value === 'searches' ? 'Search queries…' : 'Search URLs…')

/** How a URL is drawn: a job's entry (`state`) or a single retrieval (`success`) */
function urlLook(entry: any, jobStatus: string, interruptedBy?: string | null) {
  const done = entry.state ? entry.state !== 'pending' : true
  if (done && entry.error?.type === 'RetrievalInterrupted') {
    return { icon: 'i-fa7-solid-circle-stop', color: 'text-dimmed', title: 'Stopped by a user' }
  }
  if (done) {
    // Results stored before the outcome classes existed are classified on the server
    // too; the fallback only covers an entry without any
    const succeeded = entry.state ? entry.state === 'ok' : entry.success
    const outcome = entry.outcome || (succeeded ? 'ok' : 'error')
    const look = outcomeLook(outcome, entry.outcome_kind)
    return { icon: look.icon, color: look.text,
             title: outcome === 'ok' && entry.from_cache ? 'Retrieved (from cache)' : look.label }
  }
  if (jobStatus === 'running') {
    return { icon: 'i-fa7-solid-circle-notch', color: 'text-info animate-spin', title: 'In progress' }
  }
  // Pending in a job that has ended: it was never retrieved
  return { icon: 'i-fa7-solid-circle-stop', color: 'text-dimmed',
           title: `Not retrieved: ${interruption(interruptedBy).short}` }
}

function resultLink(jobId: string, url: string) {
  return { path: `/jobs/${jobId}`, query: { result: url } }
}

/** Whether a URL came back with no content (and so has an error line) */
function failed(entry: any) {
  return entry.state ? entry.state === 'failed' : !entry.success
}

/** What can be done to a URL: stop one that is being retrieved (the job goes on with
 *  its others), or retrieve one that was again, without the cache, as a new job (see
 *  useUrlActions.ts). */
const { acting, urlAction: actionFor, runUrlAction } = useUrlActions()

function urlAction(entry: any, jobStatus: string) {
  const done = entry.state ? entry.state !== 'pending' : true
  return actionFor(done, jobStatus === 'running')
}

async function runAction(jobId: string, entry: any, jobStatus: string) {
  const action = urlAction(entry, jobStatus)
  if (!action) return
  error.value = ''
  try {
    await runUrlAction(jobId, entry.url, action.kind)
    setTimeout(refresh, 600)  // The change is moments away; the next poll would see it too
  } catch (e: any) {
    error.value = e.message
  }
}

const copied = ref('')
async function copy(id: string) {
  if (!(await copyText(id))) return
  copied.value = id
  setTimeout(() => { if (copied.value === id) copied.value = '' }, 1500)
}

// --- Searches -----------------------------------------------------------------------

/** How a search's HTTP status reads */
function searchLook(status: number) {
  if (status === 200) return { icon: 'i-fa7-solid-circle-check', color: 'text-success', label: 'Answered' }
  const known = SEARCH_STATUSES.find((s) => s.value === String(status))
  if (known) return { icon: known.icon, color: known.tone, label: known.label }
  return { icon: 'i-fa7-solid-circle-exclamation', color: 'text-error', label: `Failed (${status})` }
}

/** The query's parameters besides its terms and type, as `name value` */
function queryParams(query: Record<string, any> | null): string[] {
  return Object.entries(query || {})
    .filter(([name, value]) => !['q', 'type'].includes(name) && value != null && value !== '')
    .map(([name, value]) => `${name} ${Array.isArray(value) ? value.join(', ') : value}`)
}

/** Which searches have their result URLs unfolded */
const unfolded = ref(new Set<number>())
function toggle(id: number) {
  const next = new Set(unfolded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  unfolded.value = next
}
</script>

<template>
  <div class="space-y-5">
    <div class="flex items-start justify-between gap-4 flex-wrap">
      <div>
        <h1 class="text-2xl font-semibold">History</h1>
        <p v-if="view === 'searches'" class="text-sm text-muted mt-1 min-h-5">
          <template v-if="stats.searches != null">
            {{ compact(stats.searches) }} searches ·
            <span class="text-success">{{ compact(stats.searches - stats.failed) }} answered</span> ·
            <span :class="stats.failed ? 'text-error' : ''">{{ compact(stats.failed) }} failed</span>
          </template>
        </p>
        <p v-else class="text-sm text-muted mt-1 min-h-5">
          <template v-if="stats.jobs != null">
            {{ compact(stats.jobs) }} jobs · {{ compact(stats.urls) }} URLs ·
            <span class="text-success">{{ compact(stats.outcomes?.ok ?? stats.succeeded) }} retrieved</span> ·
            <span
              :class="stats.outcomes?.unavailable ? 'text-warning' : ''"
              title="scrapeMM did its part, but the target was unavailable, behind a paywall or a CAPTCHA, or rate-limited"
            >{{ compact(stats.outcomes?.unavailable ?? 0) }} unavailable</span> ·
            <span :class="stats.failed ? 'text-error' : ''">{{ compact(stats.failed) }} failed</span>
          </template>
        </p>
      </div>
      <!-- What is listed: jobs, the single URLs of all jobs, or searches -->
      <UTabs v-model="view" :items="VIEWS" :content="false" />
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <!-- Filters and sorting -->
    <div class="flex flex-wrap items-center gap-2">
      <UInput
        v-model="filters.url" :placeholder="searchPlaceholder"
        icon="i-fa7-solid-magnifying-glass" class="flex-1 min-w-56"
        :ui="{ trailing: 'pe-1' }"
      >
        <template v-if="filters.url" #trailing>
          <UButton
            color="neutral" variant="link" size="xs" icon="i-fa7-solid-xmark"
            aria-label="Clear the text filter" @click="filters.url = ''"
          />
        </template>
      </UInput>
      <USelect
        v-for="select in selects" :key="`${view}-${select.model}`"
        v-model="filters[select.model]" :items="select.items as any" :class="select.width"
        :aria-label="select.label"
        :icon="optionLook(select.items, filters[select.model]).icon"
        :ui="{ leadingIcon: optionLook(select.items, filters[select.model]).tone }"
      >
        <template #item-leading="{ item }">
          <UIcon
            v-if="(item as any).icon" :name="(item as any).icon"
            class="size-4 shrink-0" :class="(item as any).tone"
          />
        </template>
      </USelect>
      <!-- Sorting: what by, and which way -->
      <div class="flex">
        <USelect
          v-model="sortKey" :items="sortKeys" class="w-36 rounded-r-none" :aria-label="`Sort the ${view} by`"
          :icon="optionLook(sortKeys, sortKey).icon"
        >
          <template #item-leading="{ item }">
            <UIcon :name="(item as any).icon" class="size-4 shrink-0 text-muted" />
          </template>
        </USelect>
        <USelect
          v-model="sortDir" :items="SORT_DIRECTIONS" class="w-40 -ml-px rounded-l-none"
          aria-label="Sort order" :icon="optionLook(SORT_DIRECTIONS, sortDir).icon"
        >
          <template #item-leading="{ item }">
            <UIcon :name="(item as any).icon" class="size-4 shrink-0 text-muted" />
          </template>
        </USelect>
      </div>
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
        {{ compact(total) }} matching {{ noun(total) }}
      </p>
    </div>

    <!-- Results -->
    <div v-if="loading && !items.length" class="space-y-2">
      <div v-for="n in 5" :key="n" class="surface-card rounded-xl p-4 space-y-2">
        <USkeleton class="h-3 w-1/3" />
        <USkeleton class="h-9 w-full" />
      </div>
    </div>

    <UCard v-else-if="!items.length">
      <p class="text-sm text-muted">
        <template v-if="active">No {{ noun(1) }} matches these filters.</template>
        <template v-else-if="view === 'searches'">
          Nothing searched yet. Try a query in the
          <ULink to="/playground/search">playground</ULink>.
        </template>
        <template v-else>
          Nothing retrieved yet. Try a URL in the
          <ULink to="/playground">playground</ULink>.
        </template>
      </p>
    </UCard>

    <!-- Jobs -->
    <div v-else-if="view === 'jobs'" class="space-y-3">
      <div v-for="job in items" :key="job.id" class="surface-card rounded-xl p-3 space-y-2.5 group">
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
            <span v-if="job.params.screenshot" class="inline-flex items-center gap-1" title="With screenshots">
              <UIcon name="i-fa7-solid-camera" class="size-3" />
              screenshot
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
            <span v-else-if="job.status === 'interrupted'">{{ interruption(job.interrupted_by).short }}</span>
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
          <div
            v-for="entry in job.urls" :key="entry.url"
            class="group/url flex items-center gap-2.5 rounded-lg px-3 py-2 min-w-0
                   bg-elevated/50 hover:bg-elevated transition-colors"
          >
            <NuxtLink :to="resultLink(job.id, entry.url)" class="flex items-center gap-2.5 min-w-0 flex-1">
              <UIcon
                :name="urlLook(entry, job.status, job.interrupted_by).icon" class="size-3.5 shrink-0"
                :class="urlLook(entry, job.status, job.interrupted_by).color"
                :title="urlLook(entry, job.status, job.interrupted_by).title"
              />
              <div class="min-w-0 flex-1 flex flex-col">
                <UrlLabel :url="entry.url" class="text-sm" />
                <span
                  v-if="entry.state === 'failed'" class="text-xs truncate"
                  :class="entry.error?.type === 'RetrievalInterrupted' ? 'text-dimmed'
                    : entry.outcome === 'unavailable' ? 'text-warning' : 'text-error/90'"
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
            <!-- Stop while it is being retrieved, retry once it was; only on hover -->
            <UButton
              v-if="urlAction(entry, job.status)" size="xs" color="neutral" variant="ghost"
              :icon="urlAction(entry, job.status)!.icon" :aria-label="urlAction(entry, job.status)!.title"
              :title="urlAction(entry, job.status)!.title" :loading="acting === `${job.id}|${entry.url}`"
              class="shrink-0 -my-1 -mr-1.5 opacity-0 group-hover/url:opacity-100
                     focus-visible:opacity-100 transition-opacity"
              :class="urlAction(entry, job.status)!.kind === 'stop' ? 'hover:text-error' : ''"
              @click="runAction(job.id, entry, job.status)"
            />
          </div>
          <NuxtLink
            v-if="job.url_count > job.urls.length" :to="`/jobs/${job.id}`"
            class="block px-3 text-xs text-muted hover:text-default transition-colors"
          >
            + {{ job.url_count - job.urls.length }} more URL{{ job.url_count - job.urls.length === 1 ? '' : 's' }}
          </NuxtLink>
        </div>
      </div>
    </div>

    <!-- Retrievals: one URL of one job each -->
    <div v-else-if="view === 'retrievals'" class="space-y-1.5">
      <div
        v-for="entry in items" :key="`${entry.job_id}|${entry.url}`"
        class="group/url surface-card rounded-xl flex items-center gap-3 px-3 py-2.5 min-w-0"
      >
        <NuxtLink :to="resultLink(entry.job_id, entry.url)" class="flex items-center gap-3 min-w-0 flex-1">
          <UIcon
            :name="urlLook(entry, entry.job_status).icon" class="size-4 shrink-0"
            :class="urlLook(entry, entry.job_status).color" :title="urlLook(entry, entry.job_status).title"
          />
          <div class="min-w-0 flex-1 flex flex-col gap-0.5">
            <UrlLabel :url="entry.url" class="text-sm" />
            <span
              v-if="failed(entry)" class="text-xs truncate"
              :class="entry.error?.type === 'RetrievalInterrupted' ? 'text-dimmed'
                : entry.outcome === 'unavailable' ? 'text-warning' : 'text-error/90'"
              :title="entry.error?.message"
            >{{ describeError(entry.error?.type) }}</span>
          </div>
          <span class="shrink-0 text-xs text-dimmed flex items-center gap-3">
            <span v-if="entry.from_cache" class="inline-flex items-center gap-1" title="From cache">
              <UIcon name="i-fa7-solid-bolt" class="size-3" />
            </span>
            <span v-if="entry.method" class="inline-flex items-center gap-1" title="Method">
              <UIcon name="i-fa7-solid-wrench" class="size-3" />{{ entry.method }}
            </span>
            <span v-if="entry.output_format" class="hidden md:inline-flex items-center gap-1" title="Output format">
              <UIcon name="i-fa7-solid-file-lines" class="size-3" />{{ entry.output_format }}
            </span>
            <span
              v-if="entry.retrieval_time != null" class="inline-flex items-center gap-1 tabular-nums"
              :title="retrievalTimeTitle(entry.retrieval_time, entry.queue_time)"
            >
              <UIcon name="i-fa7-solid-stopwatch" class="size-3" />{{ seconds(entry.retrieval_time) }}
            </span>
            <span class="inline-flex items-center gap-1 w-32 whitespace-nowrap" :title="absoluteTime(entry.created_at)">
              <UIcon name="i-fa7-solid-clock" class="size-3" />{{ timeAgo(entry.created_at) }}
            </span>
          </span>
        </NuxtLink>
        <UButton
          v-if="urlAction(entry, entry.job_status)" size="xs" color="neutral" variant="ghost"
          :icon="urlAction(entry, entry.job_status)!.icon"
          :aria-label="urlAction(entry, entry.job_status)!.title"
          :title="urlAction(entry, entry.job_status)!.title"
          :loading="acting === `${entry.job_id}|${entry.url}`"
          class="shrink-0 -my-1 -mr-1.5 opacity-0 group-hover/url:opacity-100
                 focus-visible:opacity-100 transition-opacity"
          @click="runAction(entry.job_id, entry, entry.job_status)"
        />
      </div>
    </div>

    <!-- Searches -->
    <div v-else class="space-y-1.5">
      <div v-for="search in items" :key="search.id" class="surface-card rounded-xl px-3 py-2.5">
        <button
          type="button" class="w-full flex items-center gap-3 min-w-0 text-left"
          :disabled="!search.urls.length"
          :title="search.urls.length ? (unfolded.has(search.id) ? 'Hide the results' : 'Show the results') : undefined"
          @click="toggle(search.id)"
        >
          <UIcon
            :name="searchLook(search.status).icon" class="size-4 shrink-0"
            :class="searchLook(search.status).color" :title="searchLook(search.status).label"
          />
          <div class="min-w-0 flex-1 flex flex-col gap-0.5">
            <span class="text-sm truncate" :class="search.summary ? 'text-highlighted' : 'text-dimmed italic'">
              {{ search.summary || 'Query not recorded (searched before version 1.8.28)' }}
            </span>
            <span
              v-if="search.error || queryParams(search.query).length"
              class="text-xs truncate flex gap-x-3"
            >
              <span v-if="search.error" class="text-error/90 truncate" :title="search.error">{{ search.error }}</span>
              <span v-for="p in queryParams(search.query)" :key="p" class="text-dimmed shrink-0">{{ p }}</span>
            </span>
          </div>
          <span class="shrink-0 text-xs text-dimmed flex items-center gap-3">
            <span class="inline-flex items-center gap-1" title="Provider">
              <UIcon name="i-fa7-solid-server" class="size-3" />{{ providerLabel(search.provider) }}
            </span>
            <span v-if="search.results != null" class="inline-flex items-center gap-1 tabular-nums" title="Results">
              <UIcon name="i-fa7-solid-list-ol" class="size-3" />{{ search.results }}
            </span>
            <span v-if="search.duration != null" class="inline-flex items-center gap-1 tabular-nums" title="How long it took">
              <UIcon name="i-fa7-solid-stopwatch" class="size-3" />{{ seconds(search.duration) }}
            </span>
            <span class="inline-flex items-center gap-1 w-32 whitespace-nowrap" :title="absoluteTime(search.created_at)">
              <UIcon name="i-fa7-solid-clock" class="size-3" />{{ timeAgo(search.created_at) }}
            </span>
            <UIcon
              name="i-fa7-solid-chevron-right" class="size-3 transition-transform"
              :class="[unfolded.has(search.id) ? 'rotate-90' : '', search.urls.length ? '' : 'invisible']"
            />
          </span>
        </button>
        <!-- The URLs it found, to open -->
        <div v-if="unfolded.has(search.id)" class="mt-2 ml-7 space-y-1 border-l border-default pl-3">
          <a
            v-for="url in search.urls" :key="url" :href="url" target="_blank" rel="noopener noreferrer"
            class="flex items-center gap-2 min-w-0 text-sm hover:underline"
          >
            <UrlLabel :url="url" class="min-w-0" />
            <UIcon name="i-fa7-solid-arrow-up-right-from-square" class="size-3 shrink-0 text-dimmed" />
          </a>
        </div>
      </div>
    </div>

    <!-- Endless scrolling -->
    <div v-if="items.length" ref="sentinel" class="h-8 flex items-center justify-center">
      <UIcon
        v-if="loadingMore" name="i-fa7-solid-circle-notch"
        class="size-4 text-dimmed animate-spin"
      />
      <span v-else-if="items.length >= total && total > pageSize" class="text-xs text-dimmed">
        All {{ compact(total) }} {{ noun(total) }} are shown.
      </span>
    </div>
  </div>
</template>
