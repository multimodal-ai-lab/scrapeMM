<script setup lang="ts">
/** Everything this server has ever retrieved, newest first, and searchable. */
const api = useApi()
const route = useRoute()
const router = useRouter()

const jobs = ref<any[]>([])
const stats = ref<any>({})
const methods = ref<string[]>([])
const total = ref(0)
const page = ref(1)
const perPage = 25
const loading = ref(false)
const error = ref('')

// Reka UI (behind USelect) refuses an empty-string option value, because it reserves
// the empty string for "no selection". So "any" is the sentinel, translated away when
// the query is built.
const ANY = 'any'

// Filters live in the query string, so a search can be linked to and survives a reload
// -- which is the whole point of being able to find a job again.
const filters = reactive({
  url: (route.query.url as string) || '',
  method: (route.query.method as string) || ANY,
  format: (route.query.format as string) || ANY,
  success: (route.query.success as string) || ANY,
  since: (route.query.since as string) || ANY,
})

/** A filter counts as set when it is neither empty nor the "any" sentinel. */
function isSet(value: string) {
  return !!value && value !== ANY
}

const FORMATS = [
  { label: 'Any format', value: ANY },
  { label: 'Multimodal', value: 'multimodal' },
  { label: 'Markdown', value: 'markdown' },
  { label: 'HTML', value: 'html' },
]
const OUTCOMES = [
  { label: 'Any outcome', value: ANY },
  { label: 'Has successes', value: 'ok' },
  { label: 'Has failures', value: 'failed' },
]
const PERIODS = [
  { label: 'Any time', value: ANY },
  { label: 'Last hour', value: '1h' },
  { label: 'Last 24 hours', value: '24h' },
  { label: 'Last 7 days', value: '7d' },
  { label: 'Last 30 days', value: '30d' },
]

const methodOptions = computed(() => [
  { label: 'Any method', value: ANY },
  ...methods.value.map((m) => ({ label: m, value: m })),
])

const active = computed(() => Object.values(filters).filter(isSet).length)

/**
 * The filters currently in force, as things you can take off one at a time.
 *
 * Reading them back out of the dropdowns' own option lists means a chip always says
 * what the control says -- "Last 24 hours", not "24h".
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
  if (isSet(filters.success)) {
    chips.push({ key: 'success', icon: 'i-fa7-solid-circle-check',
                 text: label(OUTCOMES, filters.success) })
  }
  if (isSet(filters.since)) {
    chips.push({ key: 'since', icon: 'i-fa7-solid-clock',
                 text: label(PERIODS, filters.since) })
  }
  return chips
})

/** Takes one filter off. The URL box empties; the dropdowns go back to "any". */
function clearFilter(key: keyof typeof filters) {
  filters[key] = key === 'url' ? '' : ANY
}

function sinceTimestamp(period: string): number | null {
  const spans: Record<string, number> = {
    '1h': 3600, '24h': 86400, '7d': 604800, '30d': 2592000,
  }
  const span = spans[period]
  return span ? Math.floor(Date.now() / 1000 - span) : null
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const query = new URLSearchParams({
      limit: String(perPage),
      offset: String((page.value - 1) * perPage),
    })
    if (isSet(filters.url)) query.set('url', filters.url)
    if (isSet(filters.method)) query.set('method', filters.method)
    if (isSet(filters.format)) query.set('output_format', filters.format)
    if (isSet(filters.success)) {
      query.set('success', filters.success === 'ok' ? 'true' : 'false')
    }
    const since = isSet(filters.since) ? sinceTimestamp(filters.since) : null
    if (since) query.set('since', String(since))

    const data = await api.get<any>(`/v1/jobs?${query}`)
    jobs.value = data.jobs
    stats.value = data.stats
    total.value = data.total
    // Keep the known methods even when a filter narrows the result to none, or the
    // dropdown would empty itself out from under the filter that is in force.
    if (data.methods?.length) methods.value = data.methods
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function syncQuery() {
  const query: Record<string, string> = {}
  for (const [key, value] of Object.entries(filters)) if (isSet(value)) query[key] = value
  router.replace({ query })
}

function reset() {
  Object.assign(filters, { url: '', method: ANY, format: ANY, success: ANY, since: ANY })
}

// Typing in the URL box should not fire a request per keystroke
let debounce: ReturnType<typeof setTimeout> | null = null
watch(filters, () => {
  page.value = 1
  syncQuery()
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(load, 250)
})
watch(page, load)
onMounted(load)

const copied = ref('')
function copy(id: string) {
  navigator.clipboard?.writeText(id)
  copied.value = id
  setTimeout(() => { if (copied.value === id) copied.value = '' }, 1500)
}
</script>

<template>
  <div class="space-y-5">
    <div class="flex items-center justify-between gap-4">
      <div>
        <h1 class="text-2xl font-semibold">Jobs</h1>
        <p class="text-sm text-muted mt-1">
          {{ stats.jobs }} jobs · {{ stats.urls }} URLs ·
          <span class="text-success">{{ stats.succeeded }} succeeded</span> ·
          <span :class="stats.failed ? 'text-error' : ''">{{ stats.failed }} failed</span>
        </p>
      </div>
      <UButton
        class="transition-transform duration-150 hover:scale-105"
        icon="i-fa7-solid-rotate" color="neutral" variant="subtle" :loading="loading"
        label="Refresh" @click="load"
      />
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <!-- Filters -->
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
      <USelect v-model="filters.success" :items="OUTCOMES" class="w-40" />
      <USelect v-model="filters.since" :items="PERIODS" class="w-40" />
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
          :title="`Remove this filter`"
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
      <div v-for="n in 6" :key="n" class="surface-card rounded-xl p-4 space-y-2">
        <USkeleton class="h-4 w-2/3" />
        <USkeleton class="h-3 w-1/3" />
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

    <TransitionGroup v-else tag="div" name="list" class="space-y-2 relative">
      <NuxtLink
        v-for="job in jobs" :key="job.id" :to="`/jobs/${job.id}`" class="block group"
      >
        <div
          class="surface-card rounded-xl p-3.5"
        >
          <!-- The URLs are what somebody is actually looking for, so they lead. -->
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-1.5 min-w-0">
                <UIcon
                  :name="job.failed ? 'i-fa7-solid-circle-exclamation' : 'i-fa7-solid-circle-check'"
                  class="size-3.5 shrink-0"
                  :class="job.failed ? 'text-error' : 'text-success'"
                />
                <span class="font-medium truncate" :title="job.urls?.[0]?.url">
                  {{ job.urls?.[0]?.url || '(no URL)' }}
                </span>
                <UBadge
                  v-if="job.url_count > 1" color="neutral" variant="subtle" size="sm"
                  :label="`+${job.url_count - 1}`"
                  :title="job.urls?.slice(1).map((u: any) => u.url).join('\n')"
                />
              </div>

              <div
                class="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5 text-xs text-dimmed"
              >
                <span
                  class="inline-flex items-center gap-1"
                  :title="absoluteTime(job.created_at)"
                >
                  <UIcon name="i-fa7-solid-clock" class="size-3" />
                  {{ timeAgo(job.created_at) }}
                </span>
                <span
                  v-if="job.total_retrieval_time" class="inline-flex items-center gap-1"
                  title="Total time spent retrieving"
                >
                  <UIcon name="i-fa7-solid-stopwatch" class="size-3" />
                  {{ seconds(job.total_retrieval_time) }}
                </span>
                <span class="inline-flex items-center gap-1">
                  <UIcon name="i-fa7-solid-file-lines" class="size-3" />
                  {{ job.params.output_format }}
                </span>
                <span v-if="job.methods?.length" class="inline-flex items-center gap-1">
                  <UIcon name="i-fa7-solid-wrench" class="size-3" />
                  {{ job.methods.join(', ') }}
                </span>

                <!-- The id belongs with the other metadata, not on a line of its own:
                     it is for support and debugging, and now short enough to show in
                     full. Still quiet until the row is hovered. -->
                <button
                  type="button"
                  class="reveal-on-hover inline-flex items-center gap-1 font-mono
                         opacity-0 transition-[opacity,color] duration-150
                         group-hover:opacity-100 focus-visible:opacity-100
                         hover:text-default"
                  :title="`${job.id} — click to copy`"
                  @click.prevent.stop="copy(job.id)"
                >
                  <UIcon name="i-fa7-solid-hashtag" class="size-3" />
                  {{ copied === job.id ? 'copied' : job.id }}
                </button>
              </div>
            </div>

            <div class="flex items-center gap-2 shrink-0">
              <UBadge
                color="success" variant="subtle" size="sm" :label="`${job.succeeded} ok`"
              />
              <UBadge
                v-if="job.failed" color="error" variant="subtle" size="sm"
                :label="`${job.failed} failed`"
              />
              <UBadge
                v-if="job.status !== 'completed'" color="warning" variant="subtle"
                size="sm" :label="job.status"
              />
            </div>
          </div>

        </div>
      </NuxtLink>

      <UPagination
        v-if="total > perPage" key="pagination" v-model:page="page" :total="total"
        :items-per-page="perPage" class="justify-center pt-2"
      />
    </TransitionGroup>
  </div>
</template>
