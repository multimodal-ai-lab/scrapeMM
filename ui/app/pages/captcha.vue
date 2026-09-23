<script setup lang="ts">
/**
 * Solving the CAPTCHAs that stand between scrapeMM and some content.
 *
 * Only Archive.today gates scrapeMM today, so it is the only section here -- but the
 * page is named and laid out for the general case, because it will not be the last
 * service to put a human check in the way.
 *
 * The rhythm it supports: a gated request never blocks. The URL goes into a buffer and
 * the whole buffer is retrieved the moment a session exists, so one solved check clears
 * a batch rather than a single page.
 */
const api = useApi()
const route = useRoute()

const state = ref<any>(null)
const error = ref('')
const notice = ref('')
const busy = ref(false)
const timeout = ref(300)
const panel = ref<HTMLElement | null>(null)

let poller: ReturnType<typeof setInterval> | null = null

const solving = computed(() => state.value?.captcha?.running === true)
const waiting = computed(() => state.value?.buffer?.length ?? 0)

async function load() {
  try {
    state.value = await api.get<any>('/v1/archive-today')
  } catch (e: any) {
    error.value = e.message
  }
}

async function startSession() {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await api.post('/v1/archive-today/session', { timeout: Number(timeout.value) })
    await load()
    // Cosmetic: never let it turn a started session into a reported failure.
    try {
      panel.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    } catch { /* the panel is on screen already */ }
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

async function cancelSession() {
  busy.value = true
  try {
    await api.del('/v1/archive-today/session')
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

async function drain() {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await api.post<any>('/v1/archive-today/drain')
    notice.value = `Retrieved and cached ${result.cached} snapshot page(s).`
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

async function clearBuffer() {
  busy.value = true
  try {
    const result = await api.del<any>('/v1/archive-today/buffer')
    notice.value = `Forgot ${result.dropped} buffered URL(s).`
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await load()
  // Arriving from the dashboard's "Awaiting CAPTCHA" card means the intent was to
  // solve one, so the session starts without a second click.
  if (route.query.solve !== undefined && !solving.value) await startSession()
  // While a session runs, the countdown and the outcome only exist server-side
  poller = setInterval(load, 3000)
})
onBeforeUnmount(() => { if (poller) clearInterval(poller) })
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">CAPTCHA</h1>
      <p class="text-sm text-muted max-w-2xl mt-1">
        Some services put a human check between scrapeMM and their content. Gated
        requests never block: they are buffered, and everything buffered is retrieved
        the moment you pass a check here.
      </p>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UAlert v-if="notice" color="success" variant="subtle" :description="notice" />

    <section class="space-y-2.5">
      <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">
        Archive.today
      </h2>
      <p class="text-sm text-muted">
        Snapshot pages are gated by a reCAPTCHA, and a solved one lasts about five
        minutes.
      </p>

      <div v-if="state" class="grid gap-2.5 sm:grid-cols-3">
        <StatCard
          label="Awaiting a solve" icon="i-fa7-solid-lock"
          :value="`${waiting}`"
          :tone="waiting ? 'warning' : 'neutral'"
          :detail="waiting ? 'URLs buffered' : 'nothing waiting'"
        />
        <StatCard
          label="Pages cached" icon="i-fa7-solid-box-archive"
          :value="`${state.cached_pages}`" tone="neutral"
          detail="kept forever — a capture never changes"
        />
        <StatCard
          label="Stored session" icon="i-fa7-solid-key"
          :value="state.has_stored_session ? 'Yes' : 'No'" tone="neutral"
          detail="expires ~5 min after it was established"
        />
      </div>
    </section>

    <div ref="panel">
      <UCard>
      <template #header>
        <div class="flex items-center justify-between gap-3 flex-wrap">
          <h2 class="font-medium">Solver</h2>
          <div class="flex items-center gap-2">
            <UInput
              v-if="!solving" v-model="timeout" type="number" min="60" step="60"
              class="w-24" :ui="{ trailing: 'pe-1' }"
            >
              <template #trailing><span class="text-xs text-muted">s</span></template>
            </UInput>
            <UButton
              v-if="!solving" icon="i-fa7-solid-shield-halved" :loading="busy"
              label="Start session" @click="startSession"
            />
            <UButton
              v-else color="error" variant="soft" icon="i-fa7-solid-xmark" :loading="busy"
              label="Cancel" @click="cancelSession"
            />
          </div>
        </div>
      </template>

      <UAlert
        v-if="state?.captcha?.message" class="mb-4"
        :color="state.captcha.state === 'passed' ? 'success'
          : state.captcha.state === 'failed' ? 'error' : 'info'"
        variant="subtle" :description="state.captcha.message"
      />

      <p v-if="solving && state.captcha.seconds_remaining != null" class="text-sm text-muted mb-3">
        {{ Math.ceil(state.captcha.seconds_remaining) }}s left to pass the check.
      </p>

        <VncPanel :active="solving" />
      </UCard>
    </div>

    <UCard>
      <template #header>
        <div class="flex items-center justify-between gap-2 flex-wrap">
          <h2 class="font-medium">Buffer</h2>
          <div class="flex gap-2">
            <UButton
              size="xs" variant="ghost" icon="i-fa7-solid-download" :loading="busy"
              label="Retrieve with stored session" @click="drain"
            />
            <UButton
              size="xs" color="error" variant="ghost" icon="i-fa7-solid-trash"
              :loading="busy" label="Clear" @click="clearBuffer"
            />
          </div>
        </div>
      </template>
      <p v-if="!waiting" class="text-sm text-muted">Nothing waiting.</p>
      <ul v-else class="text-sm font-mono space-y-1 max-h-72 overflow-y-auto">
        <li v-for="url in state.buffer" :key="url" class="truncate" :title="url">
          {{ url }}
        </li>
      </ul>
    </UCard>
  </div>
</template>
