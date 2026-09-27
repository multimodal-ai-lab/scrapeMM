<script setup lang="ts">
/**
 * The server's log, live.
 *
 * The server keeps its latest records in memory and streams every new one (see
 * `logbuffer.py`), so this page shows what `docker compose logs` would -- as far as it
 * goes through Python's logging -- without a shell on the host. A dropped connection,
 * e.g. while the container restarts, is picked up again on its own, and the backlog it
 * reconnects to fills whatever was missed.
 */
const api = useApi()

// As many as the server keeps; the view renders the latest of them only
const MAX_RECORDS = 5000
const MAX_RENDERED = 1500

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
const levelItems = LEVELS.slice(0, 4).map((level) => ({
  label: level === 'DEBUG' ? 'All levels' : `${level[0]}${level.slice(1).toLowerCase()} and above`,
  value: level,
}))

const records = ref<any[]>([])
const connection = ref<'connecting' | 'live' | 'reconnecting'>('connecting')
const error = ref('')
const minLevel = ref('INFO')
const search = ref('')
const hideAccess = ref(true)
const paused = ref(false)
const frozen = ref<any[]>([])
const clearedUpTo = ref(0)
const follow = ref(true)
const copied = ref(false)
const scroller = ref<HTMLElement | null>(null)

let abort: AbortController | null = null
let unmounted = false

const visible = computed(() => {
  const source = paused.value ? frozen.value : records.value
  const floor = LEVELS.indexOf(minLevel.value)
  const term = search.value.trim().toLowerCase()
  const matching = source.filter((r) => r.seq > clearedUpTo.value
    && LEVELS.indexOf(r.level) >= floor
    && !(hideAccess.value && r.logger === 'uvicorn.access')
    && (!term || r.message.toLowerCase().includes(term) || r.logger.toLowerCase().includes(term)))
  return matching.slice(-MAX_RENDERED)
})

function add(record: any) {
  records.value.push(record)
  if (records.value.length > MAX_RECORDS) records.value.splice(0, records.value.length - MAX_RECORDS)
}

async function connect() {
  let pause = 1000
  while (!unmounted) {
    abort = new AbortController()
    try {
      await api.streamGet('/v1/logs/stream', (message) => {
        pause = 1000
        error.value = ''
        connection.value = 'live'
        if (message.type === 'backlog') {
          // Also after a reconnect: the server's backlog is the complete recent history,
          // and after a restart its numbering starts over anyway
          records.value = message.records
          clearedUpTo.value = 0
        } else if (message.type === 'record') {
          add(message.record)
        }
      }, abort.signal)
    } catch (e: any) {
      if (unmounted) return
      if (!api.token.value) { error.value = e.message; return }
    }
    if (unmounted) return
    connection.value = 'reconnecting'
    await new Promise((resolve) => setTimeout(resolve, pause))
    pause = Math.min(pause * 2, 15000)
  }
}

// Stay at the bottom as lines arrive, unless the reader scrolled up to look at something
watch(() => visible.value.length, async () => {
  if (!follow.value || paused.value) return
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
})

function onScroll() {
  const el = scroller.value
  if (el) follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

function togglePause() {
  paused.value = !paused.value
  if (paused.value) frozen.value = [...records.value]
}

function clearView() {
  clearedUpTo.value = records.value.at(-1)?.seq ?? 0
}

function formatTime(seconds: number) {
  const d = new Date(seconds * 1000)
  return d.toLocaleTimeString(undefined, { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0')
}

function asText(r: any) {
  return `${new Date(r.time * 1000).toISOString()} [${r.level}] ${r.logger}: ${r.message}`
}

async function copyVisible() {
  copied.value = await copyText(visible.value.map(asText).join('\n'))
  setTimeout(() => { copied.value = false }, 1500)
}

const LEVEL_CLASS: Record<string, string> = {
  DEBUG: 'text-dimmed',
  INFO: 'text-default',
  WARNING: 'text-warning',
  ERROR: 'text-error',
  CRITICAL: 'text-error font-semibold',
}

onMounted(connect)
onBeforeUnmount(() => {
  unmounted = true
  abort?.abort()
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-start justify-between gap-4 flex-wrap">
      <div>
        <h1 class="text-2xl font-semibold flex items-center gap-3">
          Logs
          <UBadge
            size="sm" variant="subtle"
            :color="paused ? 'neutral' : connection === 'live' ? 'success' : 'warning'"
            :label="paused ? 'Paused' : connection === 'live' ? 'Live' : connection === 'connecting' ? 'Connecting…' : 'Reconnecting…'"
          />
        </h1>
        <p class="text-sm text-muted max-w-2xl mt-1">
          What the server logs, as it happens. Output that bypasses the server's logging,
          such as the container's startup lines, only appears in
          <code class="font-mono text-xs">docker compose logs</code>.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <UButton
          color="neutral" variant="soft" :icon="paused ? 'i-fa7-solid-play' : 'i-fa7-solid-pause'"
          :label="paused ? 'Resume' : 'Pause'" @click="togglePause"
        />
        <UButton
          color="neutral" variant="ghost" :icon="copied ? 'i-fa7-solid-check' : 'i-fa7-solid-copy'"
          :label="copied ? 'Copied' : 'Copy'" :disabled="!visible.length" @click="copyVisible"
        />
        <UButton
          color="neutral" variant="ghost" icon="i-fa7-solid-eraser" label="Clear"
          title="Hides the lines shown so far; the server keeps them" @click="clearView"
        />
      </div>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <div class="flex items-center gap-3 flex-wrap">
      <UInput
        v-model="search" icon="i-fa7-solid-magnifying-glass" placeholder="Filter lines…"
        class="w-72"
      />
      <USelect v-model="minLevel" :items="levelItems" class="w-48" />
      <UCheckbox v-model="hideAccess" label="Hide HTTP access log" />
      <span class="text-xs text-dimmed ml-auto tabular-nums">
        {{ visible.length }} line{{ visible.length === 1 ? '' : 's' }}
        <template v-if="visible.length === MAX_RENDERED">(latest shown)</template>
      </span>
    </div>

    <div
      ref="scroller"
      class="rounded-md border border-default bg-elevated/40 font-mono text-xs leading-relaxed
             h-[calc(100vh-17rem)] min-h-80 overflow-auto p-3"
      @scroll="onScroll"
    >
      <p v-if="!visible.length" class="text-muted">
        {{ connection === 'live' ? 'No lines match.' : 'Waiting for the server…' }}
      </p>
      <div v-for="r in visible" :key="r.seq" class="flex gap-3 whitespace-pre-wrap break-words">
        <span class="text-dimmed shrink-0 tabular-nums" :title="new Date(r.time * 1000).toString()">{{ formatTime(r.time) }}</span>
        <span class="shrink-0 w-16" :class="LEVEL_CLASS[r.level] || ''">{{ r.level }}</span>
        <span class="min-w-0" :class="LEVEL_CLASS[r.level] || ''">
          <span v-if="r.logger !== 'scrapeMM'" class="text-dimmed">{{ r.logger }}: </span>{{ r.message }}
        </span>
      </div>
    </div>

    <UButton
      v-if="!follow && !paused" size="sm" color="neutral" variant="soft"
      icon="i-fa7-solid-arrow-down" label="Jump to latest"
      class="fixed bottom-6 right-6 shadow"
      @click="follow = true; scroller && (scroller.scrollTop = scroller.scrollHeight)"
    />
  </div>
</template>
