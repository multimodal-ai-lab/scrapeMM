<script setup lang="ts">
/**
 * The retrieval chain: which methods scrapeMM tries for a URL, and in which order.
 *
 * The chain is edited as a draft and saved as a whole, so a half-finished reordering
 * never reaches retrievals that run meanwhile. Two stages, drawn apart: the live methods,
 * tried in order until one gets the page, and the archives, which only run once every
 * live method has failed. Rows move by drag and drop within their stage, or with the
 * arrow buttons -- and Alt+Arrow keys on a focused row -- for keyboards and touch.
 *
 * The preview resolves the draft for a URL typed in: which integration or domain route
 * takes over, which methods then run, and why the others are skipped.
 */
interface Step { method: string, enabled: boolean }

const api = useApi()

const server = ref<any>(null)
const draft = ref<Step[]>([])
const hedging = ref<string>('')
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')

const info = computed<Record<string, any>>(() =>
  Object.fromEntries((server.value?.methods || []).map((m: any) => [m.key, m])))

const live = computed(() => draft.value.filter((s) => info.value[s.method]?.stage === 'live'))
const archive = computed(() => draft.value.filter((s) => info.value[s.method]?.stage === 'archive'))

function serverHedging(): string {
  const value = server.value?.hedging_delay
  return value === null || value === undefined || value === 0 ? '' : String(value)
}

const dirty = computed(() => !!server.value && (
  JSON.stringify(draft.value) !== JSON.stringify(server.value.chain)
  || hedging.value.trim() !== serverHedging()))

const isDefaultDraft = computed(() => !!server.value
  && JSON.stringify(draft.value) === JSON.stringify(server.value.defaults)
  && hedging.value.trim() === '')

function adopt(data: any) {
  server.value = data
  draft.value = data.chain.map((s: Step) => ({ ...s }))
  hedging.value = serverHedging()
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    adopt(await api.get<any>('/v1/chain'))
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function save() {
  const delay = hedging.value.trim()
  if (delay && (Number.isNaN(Number(delay)) || Number(delay) < 0)) {
    error.value = 'The hedging delay must be a number of seconds, or empty to turn hedging off.'
    return
  }
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    adopt(await api.put<any>('/v1/chain', {
      chain: draft.value,
      hedging_delay: delay ? Number(delay) : null,
      set_hedging_delay: true,
    }))
    notice.value = 'Retrieval chain saved. It applies to every retrieval from now on.'
  } catch (e: any) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

function discard() {
  if (server.value) adopt(server.value)
  notice.value = ''
}

function resetToDefaults() {
  draft.value = server.value.defaults.map((s: Step) => ({ ...s }))
  hedging.value = ''
  notice.value = 'Defaults loaded. Save to apply them.'
}

// --- Reordering ---------------------------------------------------------------------

/** Moves a step within its stage; the stages themselves never mix. */
function move(method: string, delta: number) {
  const stage = info.value[method].stage
  const group = draft.value.filter((s) => info.value[s.method].stage === stage)
  const from = group.findIndex((s) => s.method === method)
  const to = from + delta
  if (to < 0 || to >= group.length) return
  const reordered = [...group]
  const [step] = reordered.splice(from, 1)
  reordered.splice(to, 0, step!)
  placeGroup(stage, reordered)
  announce(`${info.value[method].name} moved to position ${to + 1} of ${group.length}.`)
  nextTick(() => document.getElementById(`chain-row-${method}`)?.focus())
}

function placeGroup(stage: string, group: Step[]) {
  const others = draft.value.filter((s) => info.value[s.method].stage !== stage)
  draft.value = stage === 'live' ? [...group, ...others] : [...others, ...group]
}

function onRowKey(event: KeyboardEvent, method: string) {
  if (!event.altKey) return
  if (event.key === 'ArrowUp') { event.preventDefault(); move(method, -1) }
  if (event.key === 'ArrowDown') { event.preventDefault(); move(method, 1) }
}

// Drag and drop, within a stage. The row under the pointer shows where the step lands.
const dragging = ref<string | null>(null)
const dropTarget = ref<{ method: string, after: boolean } | null>(null)

function onDragStart(event: DragEvent, method: string) {
  dragging.value = method
  event.dataTransfer?.setData('text/plain', method)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}

function onDragOver(event: DragEvent, method: string) {
  if (!dragging.value || dragging.value === method) return
  if (info.value[dragging.value].stage !== info.value[method].stage) return
  event.preventDefault()
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  dropTarget.value = { method, after: event.clientY > rect.top + rect.height / 2 }
}

function onDrop(event: DragEvent) {
  event.preventDefault()
  const source = dragging.value
  const target = dropTarget.value
  endDrag()
  if (!source || !target || source === target.method) return
  const stage = info.value[source].stage
  const group = draft.value.filter((s) => info.value[s.method].stage === stage)
  const step = group.find((s) => s.method === source)!
  const rest = group.filter((s) => s.method !== source)
  let index = rest.findIndex((s) => s.method === target.method)
  if (target.after) index += 1
  rest.splice(index, 0, step)
  placeGroup(stage, rest)
}

function endDrag() {
  dragging.value = null
  dropTarget.value = null
}

// Screen readers hear what a move did
const announcement = ref('')
function announce(text: string) { announcement.value = text }

// --- Unsaved changes ----------------------------------------------------------------

function beforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => {
  load()
  window.addEventListener('beforeunload', beforeUnload)
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
onBeforeRouteLeave(() => {
  if (dirty.value && !confirm('The retrieval chain has unsaved changes. Leave without saving?')) {
    return false
  }
})

// --- Preview ------------------------------------------------------------------------

const previewUrl = ref('')
const preview = ref<any>(null)
const previewError = ref('')
let previewTimer: ReturnType<typeof setTimeout> | undefined

async function runPreview() {
  const url = previewUrl.value.trim()
  if (!url) { preview.value = null; previewError.value = ''; return }
  try {
    preview.value = await api.post<any>('/v1/chain/preview', { url, chain: draft.value })
    previewError.value = ''
  } catch (e: any) {
    previewError.value = e.message
  }
}

watch([previewUrl, draft], () => {
  clearTimeout(previewTimer)
  previewTimer = setTimeout(runPreview, 350)
}, { deep: true })

const EXAMPLES = [
  'https://www.politifact.com/factchecks/2016/apr/19/doug-ducey/are-90-percent-fires-arizona-caused-humans/',
  'https://x.com/PopBase/status/1938496291908030484',
  'https://www.washingtonpost.com/politics/2018/09/12/anatomy/',
  'https://web.archive.org/web/2023/https://example.com/',
]

// --- Presentation -------------------------------------------------------------------

const ICONS: Record<string, string> = {
  integrations: 'i-fa7-solid-puzzle-piece',
  browser: 'i-fa7-solid-window-maximize',
  firecrawl: 'i-fa7-solid-fire',
  decodo: 'i-fa7-solid-tower-broadcast',
  plain_http: 'i-fa7-solid-code',
  wayback: 'i-fa7-solid-building-columns',
  perma_cc: 'i-fa7-solid-link',
}
const stepIcon = (key: string) => ICONS[key] || integrationIcon(key)

/** The badge for a step: its switch wins over what the server last reported. */
function badge(step: Step) {
  const m = info.value[step.method]
  if (!m?.available) return toneFor('unavailable')
  if (!step.enabled) return toneFor('disabled')
  // Switched on in the draft but off on the server: the probe result is not known yet
  if (m.state === 'disabled') return { ...toneFor('ready'), label: 'On after saving' }
  return toneFor(m.state)
}

function detail(step: Step) {
  const m = info.value[step.method]
  if (!m?.available || m.state !== 'disabled' || !step.enabled) return m?.detail
  return 'Switched on in this draft.'
}

function position(step: Step) {
  const group = info.value[step.method].stage === 'live' ? live.value : archive.value
  return group.findIndex((s) => s.method === step.method) + 1
}

/** Whether a step in the preview is switched on but cannot work as things stand */
const unready = (key: string) => ['unconfigured', 'error'].includes(info.value[key]?.state)

const routeEntries = computed(() => Object.entries(server.value?.routes || {}) as [string, string[]][])
const methodName = (key: string) => info.value[key]?.name || key
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex flex-col sm:flex-row sm:items-start gap-x-3 gap-y-2">
        <div class="min-w-0 flex-1">
          <h2 class="font-medium flex items-center gap-2">
            <UIcon name="i-fa7-solid-list-ol" class="size-4 text-primary" />
            Retrieval chain
            <UBadge v-if="dirty" color="warning" variant="subtle" size="sm" label="Unsaved changes" />
          </h2>
          <p class="text-sm text-muted mt-0.5">
            The methods scrapeMM tries for every URL, top to bottom, until one gets the page.
            Clients that name their own methods bypass it.
          </p>
        </div>
        <UButton
          size="xs" variant="ghost" color="neutral" icon="i-fa7-solid-arrow-rotate-left" class="self-start"
          label="Reset to defaults" :disabled="loading || isDefaultDraft" @click="resetToDefaults"
        />
      </div>
    </template>

    <div v-if="loading && !server" class="py-6 text-center text-sm text-muted">
      <UIcon name="i-fa7-solid-spinner" class="size-4 animate-spin" /> Loading the chain…
    </div>

    <div v-else-if="server" class="space-y-5">
      <UAlert v-if="error" color="error" variant="subtle" :description="error" />
      <UAlert v-if="notice && !error" color="success" variant="subtle" :description="notice" />

      <p class="sr-only" aria-live="polite">{{ announcement }}</p>

      <!-- Both stages share one row template; the loop keeps them identical. -->
      <section
        v-for="group in [
          { stage: 'live', steps: live, title: 'Live methods',
            hint: 'Fetch the page as it is now. Tried in this order until one succeeds.' },
          { stage: 'archive', steps: archive, title: 'Archives',
            hint: 'Only when every live method failed: the page as an archive captured it.' },
        ]"
        :key="group.stage"
        :aria-label="group.title"
      >
        <div v-if="group.stage === 'archive'" class="flex items-center gap-3 mb-3 text-xs text-muted">
          <span class="h-px flex-1 bg-(--ui-border)" />
          <UIcon name="i-fa7-solid-arrow-down" class="size-3" />
          <span>If every live method fails</span>
          <span class="h-px flex-1 bg-(--ui-border)" />
        </div>
        <div class="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-x-2 gap-y-0.5 mb-2">
          <h3 class="text-sm font-semibold flex items-center gap-2">
            <UIcon
              :name="group.stage === 'live' ? 'i-fa7-solid-bolt' : 'i-fa7-solid-box-archive'"
              class="size-3.5" :class="group.stage === 'live' ? 'text-primary' : 'text-info'"
            />
            {{ group.title }}
          </h3>
          <p class="text-xs text-muted sm:text-right">{{ group.hint }}</p>
        </div>

        <ol class="space-y-2" @dragend="endDrag">
          <li
            v-for="step in group.steps" :id="`chain-row-${step.method}`" :key="step.method"
            tabindex="0"
            class="chain-row relative surface-card rounded-xl p-3 outline-none
                   focus-visible:ring-2 focus-visible:ring-primary"
            :class="[
              !step.enabled || !info[step.method].available ? 'opacity-60' : '',
              dragging === step.method ? 'opacity-40' : '',
            ]"
            :aria-label="`${position(step)}. ${info[step.method].name}, ${step.enabled ? 'on' : 'off'}. Alt+Arrow keys move it.`"
            draggable="true"
            @dragstart="onDragStart($event, step.method)"
            @dragover="onDragOver($event, step.method)"
            @drop="onDrop"
            @keydown="onRowKey($event, step.method)"
          >
            <span
              v-if="dropTarget?.method === step.method"
              class="absolute inset-x-3 h-0.5 rounded bg-primary"
              :class="dropTarget.after ? '-bottom-1.5' : '-top-1.5'"
            />
            <!-- Top line: position, identity, controls. The explanation spans the full
                 width below it, so a narrow screen does not squeeze it into a column. -->
            <div class="flex items-center gap-3">
              <div class="flex flex-col items-center text-dimmed cursor-grab select-none w-4" aria-hidden="true">
                <UIcon name="i-fa7-solid-grip-vertical" class="size-3.5" />
                <span class="text-xs tabular-nums">{{ position(step) }}</span>
              </div>
              <div class="icon-plate shrink-0 size-9 rounded-lg hidden sm:grid place-items-center">
                <UIcon :name="stepIcon(step.method)" class="size-4.5" :class="badge(step).text" />
              </div>
              <div class="min-w-0 flex-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                <span class="font-semibold">{{ info[step.method].name }}</span>
                <UTooltip :text="detail(step) || badge(step).label">
                  <UBadge
                    :color="badge(step).color as any" variant="subtle" size="sm"
                    :icon="badge(step).icon" :label="badge(step).label"
                  />
                </UTooltip>
              </div>
              <div class="flex items-center gap-0.5 shrink-0">
                <UButton
                  size="xs" variant="ghost" color="neutral" square icon="i-fa7-solid-chevron-up"
                  :disabled="position(step) === 1" :aria-label="`Move ${info[step.method].name} up`"
                  @click="move(step.method, -1)"
                />
                <UButton
                  size="xs" variant="ghost" color="neutral" square icon="i-fa7-solid-chevron-down"
                  :disabled="position(step) === group.steps.length"
                  :aria-label="`Move ${info[step.method].name} down`"
                  @click="move(step.method, 1)"
                />
                <USwitch
                  v-model="step.enabled" :disabled="!info[step.method].available" class="ml-2"
                  :aria-label="`Use ${info[step.method].name}`"
                />
              </div>
            </div>
            <div class="mt-1.5 pl-7 sm:pl-[4.75rem]">
              <p class="text-sm text-muted">{{ info[step.method].description }}</p>
              <div class="flex flex-wrap gap-1.5 mt-2">
                <span class="chip"><UIcon name="i-fa7-regular-clock" class="size-3" />{{ info[step.method].speed }}</span>
                <span class="chip"><UIcon name="i-fa7-solid-coins" class="size-3" />{{ info[step.method].cost }}</span>
                <span v-if="info[step.method].local" class="chip">
                  <UIcon name="i-fa7-solid-server" class="size-3" />Fetches from this server
                </span>
              </div>
              <p v-if="detail(step) && step.enabled && info[step.method].available" class="text-xs text-dimmed mt-1.5">
                {{ detail(step) }}
              </p>
            </div>
          </li>
        </ol>

        <div v-if="group.stage === 'live'" class="mt-3 grid sm:grid-cols-[1fr_auto] gap-3 items-start">
          <p class="text-xs text-muted">
            <UIcon name="i-fa7-solid-circle-info" class="size-3" />
            A site's own integration always comes first for its domains, and some domains are
            routed to fixed methods (see below). A missing page (404) or a dead host skips
            the remaining live methods and goes straight to the archives.
          </p>
          <UFormField
            label="Hedging (s)"
            description="Start the next live method after this head start, alongside the running one. Faster, but duplicates work."
            class="sm:w-64"
          >
            <UInput v-model="hedging" type="number" min="0" step="0.5" placeholder="Off" class="w-full" />
          </UFormField>
        </div>
      </section>

      <div class="flex flex-wrap items-center gap-2 pt-1">
        <UButton :loading="saving" :disabled="!dirty" icon="i-fa7-solid-floppy-disk" label="Save chain" @click="save" />
        <UButton v-if="dirty" variant="ghost" color="neutral" label="Discard changes" @click="discard" />
        <span v-if="!dirty" class="text-xs text-muted">
          {{ server.is_default ? 'Using the defaults.' : 'Customised.' }}
        </span>
      </div>

      <USeparator />

      <!-- Preview -->
      <section aria-label="Preview">
        <h3 class="text-sm font-semibold flex items-center gap-2 mb-2">
          <UIcon name="i-fa7-solid-eye" class="size-3.5 text-primary" />
          Preview for a URL
        </h3>
        <div class="flex gap-2">
          <UInput
            v-model="previewUrl" icon="i-fa7-solid-link" placeholder="Paste a URL to see what the chain does with it"
            class="flex-1 min-w-0" aria-label="URL to preview"
          />
          <UButton
            v-if="previewUrl" variant="ghost" color="neutral" icon="i-fa7-solid-xmark"
            aria-label="Clear the URL" @click="previewUrl = ''"
          />
        </div>
        <div v-if="!previewUrl" class="flex flex-wrap gap-1.5 mt-2">
          <button
            v-for="example in EXAMPLES" :key="example" type="button"
            class="chip hover:text-default" @click="previewUrl = example"
          >
            {{ example.replace(/^https?:\/\/(www\.)?/, '').slice(0, 38) }}…
          </button>
        </div>
        <p v-if="previewError" class="text-sm text-error mt-2">{{ previewError }}</p>

        <div v-if="preview && previewUrl" class="mt-3 space-y-3">
          <p class="text-sm">
            <span class="text-muted">Domain</span> <code class="font-mono">{{ preview.domain }}</code>
            <template v-if="preview.integrations.length">
              · handled by <strong>{{ preview.integrations.join(', ') }}</strong>
            </template>
            <template v-if="preview.route">
              · routed to <strong>{{ preview.route.map(methodName).join(' → ') }}</strong>
            </template>
            <span v-if="dirty" class="text-xs text-warning"> (with your unsaved changes)</span>
          </p>
          <ol v-if="preview.live.length || preview.archive.length" class="flex flex-wrap items-center gap-1.5">
            <template v-for="(step, i) in [...preview.live, ...preview.archive]" :key="step.method">
              <li
                v-if="i === preview.live.length && preview.live.length"
                class="text-xs text-muted flex items-center gap-1 px-1"
              >
                <UIcon name="i-fa7-solid-box-archive" class="size-3" /> then
              </li>
              <li
                class="flex items-center gap-1.5 rounded-lg px-2 py-1 text-sm"
                :class="step.stage === 'archive' ? 'bg-info/10 text-info' : 'bg-primary/10 text-primary'"
                :title="unready(step.method) ? `Likely to fail: ${info[step.method].detail}` : undefined"
              >
                <span class="text-xs opacity-70 tabular-nums">{{ i + 1 }}</span>
                <UIcon :name="stepIcon(step.method)" class="size-3.5" />
                {{ step.name }}
                <UIcon v-if="unready(step.method)" name="i-fa7-solid-triangle-exclamation" class="size-3 text-warning" />
              </li>
              <UIcon
                v-if="i < preview.live.length + preview.archive.length - 1 && i !== preview.live.length - 1"
                name="i-fa7-solid-chevron-right" class="size-3 text-dimmed"
              />
            </template>
          </ol>
          <UAlert
            v-else color="warning" variant="subtle" icon="i-fa7-solid-triangle-exclamation"
            description="No method would run for this URL: every applicable one is switched off."
          />
          <details v-if="preview.skipped.length" class="text-xs text-muted">
            <summary>Skipped ({{ preview.skipped.length }})</summary>
            <ul class="mt-1.5 space-y-0.5 pl-4 list-disc">
              <li v-for="s in preview.skipped" :key="s.method"><strong>{{ s.name }}</strong>: {{ s.reason }}</li>
            </ul>
          </details>
        </div>
      </section>

      <details class="text-sm">
        <summary class="cursor-pointer text-muted">Domain routes ({{ routeEntries.length }})</summary>
        <p class="text-xs text-muted mt-2">
          These domains use fixed live methods instead of the chain above. Switching a method
          off still switches it off for them; the archives apply as configured.
        </p>
        <div class="mt-2 grid sm:grid-cols-2 gap-x-6 gap-y-1 text-xs">
          <div v-for="[domain, methods] in routeEntries" :key="domain" class="flex justify-between gap-3 border-b border-default py-1">
            <code class="font-mono truncate">{{ domain }}</code>
            <span class="text-muted text-right">{{ methods.map(methodName).join(' → ') }}</span>
          </div>
        </div>
      </details>
    </div>
  </UCard>
</template>

<style scoped>
.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.75rem;
  line-height: 1rem;
  padding: 0.15rem 0.5rem;
  border-radius: 9999px;
  background-color: var(--surface-sunken, rgb(127 127 127 / 0.12));
  color: var(--ui-text-muted);
}
.chain-row[draggable="true"]:active { cursor: grabbing; }
</style>
