<script setup lang="ts">
/** One job: its parameters and every URL it touched, rendered as the playground would. */
const api = useApi()
const route = useRoute()
const router = useRouter()
const nuxtApp = useNuxtApp()

const job = ref<any>(null)
const error = ref('')
const loading = ref(true)
const copied = ref(false)

// The URL a card in the overview led here for: shown open, and scrolled to
const focus = computed(() => (route.query.result as string) || null)

async function load() {
  loading.value = true
  try {
    job.value = await api.get<any>(`/v1/jobs/${route.params.id}`)
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
  follow()
  if (focus.value) {
    await nextTick()
    const index = entries.value.findIndex((e) => e.url === focus.value)
    const scroll = () => document.getElementById(`result-${index}`)
      ?.scrollIntoView({ block: 'start' })
    scroll()
    // The router scrolls a newly opened page to the top once it has finished; when the
    // job loads faster than that, it would undo this. So scroll again after it.
    nuxtApp.hooks.hookOnce('page:finish', () => setTimeout(scroll, 0))
  }
}

/** Back to the overview as it was left -- the browser's back, when that is where we
 *  came from; a fresh navigation would start the list over at the top. */
function backToJobs() {
  const previous = (window.history.state as any)?.back as string | undefined
  if (previous?.startsWith('/jobs') && !previous.startsWith('/jobs/')) router.back()
  else router.push('/jobs')
}

async function remove() {
  try {
    await api.del(`/v1/jobs/${route.params.id}`)
    router.push('/jobs')
  } catch (e: any) {
    error.value = e.message
  }
}

async function copyId() {
  if (!(await copyText(String(route.params.id)))) return
  copied.value = true
  setTimeout(() => { copied.value = false }, 1500)
}

const requested = computed<string[]>(() => {
  const urls: string[] = [...new Set<string>(job.value?.params?.urls || [])]
  return urls.length ? urls : (job.value?.results || []).map((r: any) => r.url)
})

const running = computed(() => job.value?.status === 'running')

/** Every requested URL in the order it was asked for, with its result once it has one.
 *  URLs still being retrieved keep their place instead of appearing at the end. */
const entries = computed(() => {
  const byUrl = new Map((job.value?.results || []).map((r: any) => [r.url, r]))
  const listed = requested.value.map((url) => ({ url, result: byUrl.get(url) as any }))
  // A result for a URL the request did not list (should not happen) is still shown
  for (const [url, result] of byUrl) {
    if (!requested.value.includes(url as string)) listed.push({ url: url as string, result })
  }
  return listed
})

const pendingCount = computed(() => entries.value.filter((e) => !e.result).length)

/** Start to finish, as the overview shows it. Not the sum of the URLs' retrieval times:
 *  they run concurrently, so that sum was a multiple of how long the job really took. */
const duration = computed<number | null>(() => job.value?.duration ?? null)

// While the job runs, follow it: results appear as they land. Cards that were opened
// stay open, since each keeps its identity (its URL) across updates.
let poller: ReturnType<typeof setInterval> | null = null

function follow() {
  if (running.value && !poller) {
    poller = setInterval(async () => {
      try {
        job.value = await api.get<any>(`/v1/jobs/${route.params.id}`)
      } catch { /* the next refresh tries again */ }
      if (!running.value) stopFollowing()
    }, 3000)
  }
}

function stopFollowing() {
  if (poller) clearInterval(poller)
  poller = null
}

onMounted(load)
onBeforeUnmount(stopFollowing)
</script>

<template>
  <div class="space-y-5">
    <div class="flex items-start justify-between gap-4">
      <div class="min-w-0">
        <UButton
          class="-ml-3 mb-1 transition-transform duration-150 hover:-translate-x-0.5"
          variant="link" icon="i-fa7-solid-arrow-left" label="All jobs" @click="backToJobs"
        />
        <!-- From the requested URLs, which exist before any result does -->
        <h1 class="text-2xl font-semibold flex items-baseline gap-2 min-w-0">
          <UrlLabel v-if="requested[0]" :url="requested[0]" />
          <template v-else>Job</template>
          <span v-if="requested.length > 1" class="text-muted font-normal shrink-0">
            and {{ requested.length - 1 }} more
          </span>
        </h1>
        <div
          v-if="job"
          class="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5 text-sm text-muted"
        >
          <span class="inline-flex items-center gap-1.5" :title="absoluteTime(job.created_at)">
            <UIcon name="i-fa7-solid-clock" class="size-3" />
            {{ timeAgo(job.created_at) }}
          </span>
          <span
            v-if="duration != null" class="inline-flex items-center gap-1.5"
            :title="job.status === 'running' ? 'Running so far' : 'How long the job took'"
          >
            <UIcon name="i-fa7-solid-stopwatch" class="size-3" />
            {{ seconds(duration) }}
          </span>
          <span class="inline-flex items-center gap-1.5 text-success">
            <UIcon name="i-fa7-solid-circle-check" class="size-3" />
            {{ job.outcomes?.ok ?? job.succeeded }} retrieved
          </span>
          <span
            v-if="job.outcomes?.unavailable" class="inline-flex items-center gap-1.5 text-warning"
            title="scrapeMM did its part, but the target was unavailable, behind a paywall or a CAPTCHA, or rate-limited"
          >
            <UIcon name="i-fa7-solid-circle-minus" class="size-3" />
            {{ job.outcomes.unavailable }} unavailable
          </span>
          <span
            class="inline-flex items-center gap-1.5"
            :class="(job.outcomes?.error ?? job.failed) ? 'text-error' : ''"
          >
            <UIcon name="i-fa7-solid-circle-exclamation" class="size-3" />
            {{ job.outcomes?.error ?? job.failed }} failed
          </span>
          <span v-if="pendingCount && running" class="inline-flex items-center gap-1.5 text-info">
            <UIcon name="i-fa7-solid-circle-notch" class="size-3 animate-spin" />
            {{ pendingCount }} running
          </span>
          <span v-else-if="pendingCount" class="inline-flex items-center gap-1.5 text-dimmed">
            <UIcon name="i-fa7-solid-circle-stop" class="size-3" />
            {{ pendingCount }} not retrieved
          </span>
        </div>
      </div>
      <div class="flex items-center gap-2 shrink-0">
        <UButton
          v-if="job" color="neutral" variant="ghost" icon="i-fa7-solid-trash"
          label="Delete" class="transition-transform duration-150 hover:scale-105"
          @click="remove"
        />
      </div>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <div v-if="loading && !job" class="space-y-4">
      <USkeleton class="h-24 w-full" />
      <USkeleton class="h-48 w-full" />
    </div>

    <template v-else-if="job">
      <!-- An interrupted job is not a failed one: it just never got to some URLs -->
      <UAlert
        v-if="job.status === 'interrupted'" color="neutral" variant="subtle"
        icon="i-fa7-solid-circle-stop" title="Interrupted"
        :description="`The server restarted or the client disconnected before this job finished. ${job.results?.length || 0} of ${requested.length} URL(s) were done${pendingCount ? '; the ones marked below were never retrieved, so submit them again to get them' : ''}.`"
      />

      <div class="space-y-3">
        <template v-for="(entry, index) in entries" :key="entry.url">
          <!-- Collapsed when there are several, so the page is an overview first -->
          <ResultView
            v-if="entry.result" :id="`result-${index}`"
            :url="entry.url" :content="entry.result.content" :method="entry.result.method"
            :errors="entry.result.errors" :retrieval-time="entry.result.retrieval_time"
            :from-cache="entry.result.from_cache" :success="entry.result.success"
            :outcome="entry.result.outcome" :outcome-kind="entry.result.outcome_kind"
            collapsible :collapsed="entries.length > 1 && entry.url !== focus"
            class="scroll-mt-6"
          />
          <!-- Not done: still being retrieved, or never will be -->
          <div
            v-else :id="`result-${index}`"
            class="scroll-mt-6 rounded-lg bg-elevated/50 px-4 py-3 sm:px-6 flex items-center gap-3
                   transition-[background-color,transform] duration-200
                   hover:bg-elevated hover:-translate-y-px"
          >
            <UIcon
              :name="running ? 'i-fa7-solid-circle-notch' : 'i-fa7-solid-circle-stop'"
              class="size-4 shrink-0" :class="running ? 'text-info animate-spin' : 'text-dimmed'"
            />
            <a
              :href="entry.url" target="_blank" rel="noopener noreferrer"
              class="min-w-0 flex hover:underline"
            ><UrlLabel :url="entry.url" /></a>
            <span class="ml-auto shrink-0 text-xs" :class="running ? 'text-info' : 'text-dimmed'">
              {{ running ? 'Retrieving…' : 'Not retrieved' }}
            </span>
          </div>
        </template>
      </div>

      <!-- Parameters and the id live at the bottom: useful when debugging, noise when
           you are just looking at what came back. -->
      <UCard :ui="{ root: 'divide-y-0', header: 'pb-0 sm:pb-0' }">
        <template #header>
          <div class="flex items-center justify-between gap-2">
            <h2 class="font-medium text-sm flex items-center gap-2">
              <UIcon name="i-fa7-solid-sliders" class="size-3.5 text-dimmed" />
              Request
            </h2>
            <button
              type="button"
              class="font-mono text-[10px] text-dimmed/70 hover:text-dimmed transition-colors"
              :title="`${route.params.id} — click to copy`" @click="copyId"
            >
              {{ copied ? 'copied' : route.params.id }}
            </button>
          </div>
        </template>
        <pre class="text-xs overflow-x-auto">{{ JSON.stringify(job.params, null, 2) }}</pre>
      </UCard>
    </template>
  </div>
</template>
