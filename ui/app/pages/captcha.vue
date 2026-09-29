<script setup lang="ts">
/**
 * The CAPTCHAs standing between scrapeMM and some content, one challenge per site.
 *
 * A gated URL never blocks its batch: it is queued with its site's challenge, and so is
 * every further URL of that site until somebody decides. Per challenge, that decision is
 * either to solve the check in the server's own browser -- after which the queue is
 * retrieved and cached in the background, so asking again returns the content -- or to
 * discard it, which drops the queue and keeps the site blacklisted for a while.
 *
 * The panel with the server's browser closes the moment the check is passed or reported
 * missing: the queue's retrieval does not need anybody watching.
 */
const api = useApi()
const route = useRoute()

const state = ref<any>(null)
const error = ref('')
const notice = ref('')
const busy = ref<string | null>(null)  // The domain an action is running for
const timeout = ref(300)
const expanded = ref<Set<string>>(new Set())
const panel = ref<HTMLElement | null>(null)

let poller: ReturnType<typeof setTimeout> | null = null
let unmounted = false
// Set by "No CAPTCHA here": the panel closes at once, not when the server confirms
const dismissed = ref(false)

const session = computed(() => state.value?.session)
const solving = computed(() => session.value?.running === true && !dismissed.value)
const retrieving = computed<string[]>(() => session.value?.retrieving || [])
const challenges = computed<any[]>(() => state.value?.challenges || [])
const waiting = computed(() => challenges.value.reduce((n, c) => n + c.waiting, 0))

async function load() {
  try {
    state.value = await api.get<any>('/v1/captcha')
    if (!state.value?.session?.running) dismissed.value = false
  } catch (e: any) {
    error.value = e.message
  }
}

async function act(domain: string, run: () => Promise<void>) {
  busy.value = domain
  error.value = ''
  notice.value = ''
  try {
    await run()
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

function solve(domain: string) {
  return act(domain, async () => {
    await api.post(`/v1/captcha/${encodeURIComponent(domain)}/solve`,
      { timeout: Number(timeout.value) })
    await nextTick()
    // Cosmetic: never let it turn a started session into a reported failure
    try {
      panel.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    } catch { /* the panel is on screen already */ }
  })
}

function retry(domain: string) {
  return act(domain, async () => {
    const result = await api.post<any>(`/v1/captcha/${encodeURIComponent(domain)}/retry`)
    notice.value = result.remaining
      ? `Retrieved ${result.retrieved}; ${result.remaining} still gated — solve the check to continue.`
      : `Retrieved and cached all ${result.retrieved} queued URL(s).`
  })
}

function discard(domain: string, waitingCount: number) {
  if (!confirm(`Discard the ${domain} challenge? Its ${waitingCount} queued URL(s) are `
    + 'dropped, and the site is blacklisted for a while.')) return
  return act(domain, async () => {
    const result = await api.del<any>(`/v1/captcha/${encodeURIComponent(domain)}`)
    notice.value = `Discarded ${domain} and its ${result.dropped} queued URL(s).`
  })
}

function cancel() {
  return act(session.value?.domain || '', async () => { await api.del('/v1/captcha/session') })
}

/** The panel shows the normal page, no check: logged as a false detection, then the
 *  queue is retrieved anyway. */
function reportNoCaptcha() {
  dismissed.value = true
  return act(session.value?.domain || '', async () => {
    try {
      await api.post('/v1/captcha/session/no-captcha')
    } catch (e) {
      dismissed.value = false  // Still running: the panel is needed after all
      throw e
    }
  })
}

function toggle(domain: string) {
  const next = new Set(expanded.value)
  if (next.has(domain)) next.delete(domain)
  else next.add(domain)
  expanded.value = next
}

onMounted(async () => {
  await load()
  // Arriving from the dashboard's "Awaiting CAPTCHA" card means the intent was to solve
  // one, so it starts without a second click: the named one, or else the oldest
  const wanted = route.query.solve
  if (wanted !== undefined && !solving.value && state.value?.solvable) {
    const domain = (typeof wanted === 'string' && wanted) || challenges.value[0]?.domain
    if (domain) await solve(domain)
  }
  // While a session runs, the countdown and the outcome only exist server-side. Polled
  // faster then, so the panel closes right after the check is passed.
  const tick = async () => {
    await load()
    if (!unmounted) poller = setTimeout(tick, solving.value ? 1000 : 3000)
  }
  poller = setTimeout(tick, 1000)
})
onBeforeUnmount(() => {
  unmounted = true
  if (poller) clearTimeout(poller)
})
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-start justify-between gap-4 flex-wrap">
      <div>
        <h1 class="text-2xl font-semibold">CAPTCHA</h1>
        <p class="text-sm text-muted max-w-2xl mt-1">
          When a site puts a human check in the way, its URLs are queued instead of
          failing over and over. Solve the check in the server's browser to retrieve the
          queue, or discard it.
        </p>
      </div>
      <UFormField label="Time to solve" class="shrink-0">
        <UInput
          v-model="timeout" type="number" min="60" step="60" class="w-28"
          :disabled="solving" :ui="{ trailing: 'pe-2' }"
        >
          <template #trailing><span class="text-xs text-muted">s</span></template>
        </UInput>
      </UFormField>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UAlert v-if="notice" color="success" variant="subtle" :description="notice" />
    <UAlert
      v-if="state && !state.solvable && challenges.length" color="warning" variant="subtle"
      description="This server has no display, so there is no browser to solve a check in. Discard challenges, or run the server with its virtual display."
    />

    <!-- The live browser, while somebody is solving -->
    <div v-if="solving || session?.message" ref="panel">
      <UCard>
        <template #header>
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <h2 class="font-medium flex items-center gap-2">
              <UIcon name="i-fa7-solid-shield-halved" class="size-4 text-dimmed" />
              {{ solving ? `Solving ${session.domain}` : `Last session · ${session.domain}` }}
            </h2>
            <div class="flex items-center gap-3">
              <span
                v-if="solving && session.seconds_remaining != null"
                class="text-sm text-muted tabular-nums"
              >{{ Math.ceil(session.seconds_remaining) }}s left</span>
              <UButton
                v-if="session.can_report_no_captcha" color="neutral" variant="soft"
                icon="i-fa7-solid-eye-slash" label="No CAPTCHA here"
                title="The page shows its normal content and no check. Logs this as a possible false detection and retrieves the queue."
                :disabled="busy !== null" @click="reportNoCaptcha"
              />
              <UButton
                v-if="solving" color="error" variant="soft" icon="i-fa7-solid-xmark"
                :loading="busy === session.domain" label="Cancel" @click="cancel"
              />
            </div>
          </div>
        </template>
        <UAlert
          v-if="session.message" :class="solving ? 'mb-4' : ''"
          :color="session.state === 'passed' ? 'success'
            : session.state === 'failed' ? 'error' : 'info'"
          variant="subtle" :description="session.message"
        />
        <VncPanel v-if="solving" :active="solving" />
      </UCard>
    </div>

    <!-- Challenges -->
    <section class="space-y-2.5">
      <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">
        Waiting for a decision
        <span v-if="challenges.length" class="normal-case font-normal tracking-normal">
          · {{ challenges.length }} site{{ challenges.length === 1 ? '' : 's' }},
          {{ waiting }} URL{{ waiting === 1 ? '' : 's' }}
        </span>
      </h2>

      <div v-if="!state" class="space-y-2">
        <USkeleton v-for="n in 2" :key="n" class="h-24 w-full" />
      </div>

      <UCard v-else-if="!challenges.length">
        <p class="text-sm text-muted flex items-center gap-2">
          <UIcon name="i-fa7-solid-circle-check" class="size-4 text-success" />
          No CAPTCHA is in the way right now.
        </p>
      </UCard>

      <UCard v-for="c in challenges" v-else :key="c.domain">
        <div class="flex items-start justify-between gap-4 flex-wrap">
          <div class="min-w-0 space-y-1">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-medium">{{ c.domain }}</span>
              <UBadge color="warning" variant="subtle" size="sm" :label="c.captcha" />
              <UBadge
                v-if="retrieving.includes(c.domain)" color="info" variant="subtle" size="sm"
                icon="i-fa7-solid-spinner" label="retrieving the queue"
                :ui="{ leadingIcon: 'animate-spin' }"
                title="The check was passed; the queued URLs are being retrieved in the background"
              />
              <UBadge
                v-if="c.held" color="neutral" variant="outline" size="sm"
                label="new URLs are queued"
                title="Further URLs of this site wait here instead of being scraped into the same check"
              />
            </div>
            <p class="text-xs text-dimmed flex flex-wrap gap-x-3">
              <span>{{ c.waiting }} URL{{ c.waiting === 1 ? '' : 's' }} waiting</span>
              <span v-if="c.first_seen" :title="absoluteTime(c.first_seen)">
                first hit {{ timeAgo(c.first_seen) }}
              </span>
              <span v-if="c.last_seen && c.last_seen !== c.first_seen" :title="absoluteTime(c.last_seen)">
                last {{ timeAgo(c.last_seen) }}
              </span>
              <span v-if="c.kind === 'archive_today' && state.archive_today_cached_pages">
                {{ state.archive_today_cached_pages }} pages cached for good
              </span>
            </p>
          </div>

          <div class="flex items-center gap-2 shrink-0">
            <UButton
              icon="i-fa7-solid-shield-halved" label="Solve"
              :loading="busy === c.domain && !solving"
              :disabled="solving || !state.solvable || retrieving.includes(c.domain)"
              @click="solve(c.domain)"
            />
            <UButton
              color="neutral" variant="ghost" icon="i-fa7-solid-rotate" label="Retry"
              title="Retrieve the queue without solving: works while an earlier clearance is still valid"
              :disabled="solving || busy !== null || retrieving.includes(c.domain)"
              @click="retry(c.domain)"
            />
            <UButton
              color="error" variant="ghost" icon="i-fa7-solid-trash" label="Discard"
              :disabled="busy !== null" @click="discard(c.domain, c.waiting)"
            />
          </div>
        </div>

        <button
          type="button" class="mt-3 text-xs text-muted hover:text-default inline-flex items-center gap-1"
          @click="toggle(c.domain)"
        >
          <UIcon
            name="i-fa7-solid-chevron-right" class="size-2.5 transition-transform"
            :class="expanded.has(c.domain) ? 'rotate-90' : ''"
          />
          {{ expanded.has(c.domain) ? 'Hide' : 'Show' }} queued URLs
        </button>
        <ul
          v-if="expanded.has(c.domain)"
          class="mt-2 text-sm font-mono space-y-1 max-h-72 overflow-y-auto"
        >
          <li v-for="url in c.urls" :key="url" class="truncate" :title="url">{{ url }}</li>
        </ul>
      </UCard>
    </section>
  </div>
</template>
