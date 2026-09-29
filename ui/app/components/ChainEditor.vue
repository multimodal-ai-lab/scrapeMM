<script setup lang="ts">
/**
 * The retrieval chain: which methods scrapeMM tries for a URL, and in which order.
 *
 * One list, in the order the engine follows. Each card says whether its method is a live
 * one (fetches the page as it is now) or an archive (a copy captured earlier). A card is
 * dragged as a whole -- with the mouse, or on a touch screen after a short press, so that
 * swiping still scrolls -- and moved from the keyboard on a focused card with Alt+Arrow
 * keys, or Space to pick it up, arrows to move and Space again to put it down. The switch
 * on a card works as a switch, never as a drag.
 *
 * Changes stay a draft until the page's save button saves them.
 */
interface Step { method: string, enabled: boolean }

const api = useApi()
const draftForPreview = useChainDraft()

const server = ref<any>(null)
const draft = ref<Step[]>([])
const hedging = ref<string>('')
const loading = ref(true)
const error = ref('')

const info = computed<Record<string, any>>(() =>
  Object.fromEntries((server.value?.methods || []).map((m: any) => [m.key, m])))

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

watch(draft, (value) => { draftForPreview.value = value.map((s) => ({ ...s })) }, { deep: true })

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
    throw new Error('The hedging delay must be a number of seconds, or empty to turn it off.')
  }
  adopt(await api.put<any>('/v1/chain', {
    chain: draft.value,
    hedging_delay: delay ? Number(delay) : null,
    set_hedging_delay: true,
  }))
}

function discard() {
  if (server.value) adopt(server.value)
}

function resetToDefaults() {
  draft.value = server.value.defaults.map((s: Step) => ({ ...s }))
  hedging.value = ''
}

useSettingsSection({ id: 'chain', title: 'Retrieval chain', dirty, save, discard })
onMounted(load)

// --- Reordering ---------------------------------------------------------------------

const announcement = ref('')
const name = (key: string) => info.value[key]?.name || key

function moveTo(method: string, to: number) {
  const from = draft.value.findIndex((s) => s.method === method)
  to = Math.max(0, Math.min(draft.value.length - 1, to))
  if (from < 0 || from === to) return
  const list = [...draft.value]
  const [step] = list.splice(from, 1)
  list.splice(to, 0, step!)
  draft.value = list
}

function announceMove(method: string) {
  const at = draft.value.findIndex((s) => s.method === method) + 1
  announcement.value = `${name(method)}: position ${at} of ${draft.value.length}.`
}

// Keyboard: Alt+Arrow moves at once; Space picks up, arrows move, Space/Enter puts down,
// Escape puts back where it was
const grabbed = ref<{ method: string, origin: Step[] } | null>(null)

function focusCard(method: string) {
  nextTick(() => document.getElementById(`chain-card-${method}`)?.focus())
}

function onCardKey(event: KeyboardEvent, method: string) {
  if (event.target !== event.currentTarget) return // Keys meant for the switch
  const index = draft.value.findIndex((s) => s.method === method)
  const step = event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0
  if (step && (event.altKey || grabbed.value?.method === method)) {
    event.preventDefault()
    moveTo(method, index + step)
    announceMove(method)
    focusCard(method)
  } else if (event.key === ' ' || (event.key === 'Enter' && grabbed.value)) {
    event.preventDefault()
    if (grabbed.value?.method === method) {
      grabbed.value = null
      announcement.value = `${name(method)} put down at position ${index + 1}.`
    } else {
      grabbed.value = { method, origin: draft.value.map((s) => ({ ...s })) }
      announcement.value = `${name(method)} picked up. Arrow keys move it, Space puts it down, Escape cancels.`
    }
  } else if (event.key === 'Escape' && grabbed.value) {
    draft.value = grabbed.value.origin
    announcement.value = `${name(method)} put back.`
    grabbed.value = null
    focusCard(method)
  }
}

// Pointer (mouse, pen) and touch: the list reorders under the card as it is dragged
const dragging = ref<string | null>(null)
const listEl = ref<HTMLElement | null>(null)
let pending: { method: string, y: number } | null = null
let pressTimer: ReturnType<typeof setTimeout> | undefined

const INTERACTIVE = 'button, input, textarea, select, a, [role="switch"], [data-no-drag]'

function reorderAt(y: number) {
  if (!dragging.value || !listEl.value) return
  const cards = [...listEl.value.querySelectorAll<HTMLElement>('[data-method]')]
  let target = cards.length - 1
  for (let i = 0; i < cards.length; i++) {
    const rect = cards[i]!.getBoundingClientRect()
    if (y < rect.top + rect.height / 2) { target = i; break }
  }
  moveTo(dragging.value, target)
}

function startDrag(method: string) {
  dragging.value = method
  grabbed.value = null
  window.getSelection()?.removeAllRanges() // The press may have begun selecting text
}

function endDrag() {
  if (dragging.value) announceMove(dragging.value)
  dragging.value = null
  pending = null
  clearTimeout(pressTimer)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', endDrag)
  window.removeEventListener('pointercancel', endDrag)
  window.removeEventListener('touchmove', onTouchMove)
  window.removeEventListener('touchend', endDrag)
  window.removeEventListener('touchcancel', endDrag)
}

function onPointerDown(event: PointerEvent, method: string) {
  if (event.pointerType === 'touch' || event.button !== 0) return
  if ((event.target as HTMLElement).closest(INTERACTIVE)) return
  pending = { method, y: event.clientY }
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', endDrag)
  window.addEventListener('pointercancel', endDrag)
}

function onPointerMove(event: PointerEvent) {
  if (!dragging.value && pending && Math.abs(event.clientY - pending.y) > 4) startDrag(pending.method)
  if (dragging.value) {
    event.preventDefault()
    reorderAt(event.clientY)
  }
}

// Touch: a short press picks the card up; moving before that scrolls the page as usual
function onTouchStart(event: TouchEvent, method: string) {
  if ((event.target as HTMLElement).closest(INTERACTIVE) || event.touches.length !== 1) return
  pending = { method, y: event.touches[0]!.clientY }
  window.addEventListener('touchmove', onTouchMove, { passive: false })
  window.addEventListener('touchend', endDrag)
  window.addEventListener('touchcancel', endDrag)
  pressTimer = setTimeout(() => {
    if (!pending) return
    startDrag(pending.method)
    navigator.vibrate?.(15)
  }, 280)
}

function onTouchMove(event: TouchEvent) {
  const y = event.touches[0]!.clientY
  if (dragging.value) {
    event.preventDefault() // No scrolling while a card is held
    reorderAt(y)
  } else if (pending && Math.abs(y - pending.y) > 8) {
    endDrag() // A swipe: the page scrolls
  }
}

onBeforeUnmount(endDrag)

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

/** The status badge: the draft's switch wins over what the server last reported. */
function badge(step: Step) {
  const m = info.value[step.method]
  if (!m?.available) return toneFor('unavailable')
  if (!step.enabled) return toneFor('disabled')
  if (m.state === 'disabled') return { ...toneFor('ready'), label: 'On after saving' }
  return toneFor(m.state)
}

function detail(step: Step) {
  const m = info.value[step.method]
  if (!m?.available || m.state !== 'disabled' || !step.enabled) return m?.detail
  return 'Switched on in this draft.'
}
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex flex-col sm:flex-row sm:items-start gap-x-3 gap-y-2">
        <div class="min-w-0 flex-1">
          <h2 class="font-medium flex items-center gap-2">
            <UIcon name="i-fa7-solid-list-ol" class="size-4 text-primary" />
            Retrieval chain
            <UBadge v-if="dirty" color="warning" variant="subtle" size="sm" label="Unsaved" />
          </h2>
          <p class="text-sm text-muted mt-0.5">
            The methods scrapeMM tries for every URL, top to bottom, until one gets the page.
            Drag a card to reorder it.
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

    <div v-else-if="server" class="space-y-4">
      <UAlert v-if="error" color="error" variant="subtle" :description="error" />
      <p class="sr-only" aria-live="assertive">{{ announcement }}</p>

      <ol
        ref="listEl" class="space-y-2" aria-label="Retrieval chain, in order"
        :class="dragging ? 'select-none' : ''"
      >
        <li
          v-for="(step, index) in draft" :id="`chain-card-${step.method}`" :key="step.method"
          :data-method="step.method" tabindex="0"
          class="chain-card relative surface-card rounded-xl p-3 outline-none transition-[box-shadow,opacity,transform] duration-150
                 focus-visible:ring-2 focus-visible:ring-primary"
          :class="[
            !step.enabled || !info[step.method]?.available ? 'opacity-60' : '',
            dragging === step.method ? 'ring-2 ring-primary shadow-lg scale-[1.01] z-10' : '',
            grabbed?.method === step.method ? 'outline-2 outline-dashed outline-primary outline-offset-2' : '',
            dragging ? 'cursor-grabbing' : 'cursor-grab',
          ]"
          :aria-label="`${index + 1}. ${name(step.method)}, ${info[step.method]?.stage} method, ${step.enabled ? 'on' : 'off'}. Space picks it up, Alt+Arrow keys move it.`"
          @pointerdown="onPointerDown($event, step.method)"
          @touchstart.passive="onTouchStart($event, step.method)"
          @keydown="onCardKey($event, step.method)"
        >
          <div class="flex items-center gap-3">
            <span class="text-xs tabular-nums text-dimmed w-4 text-center shrink-0" aria-hidden="true">{{ index + 1 }}</span>
            <div class="icon-plate shrink-0 size-9 rounded-lg hidden sm:grid place-items-center">
              <UIcon :name="ICONS[step.method] || 'i-fa7-solid-globe'" class="size-4.5" :class="badge(step).text" />
            </div>
            <div class="min-w-0 flex-1 flex flex-wrap items-center gap-x-2 gap-y-1">
              <span class="font-semibold">{{ name(step.method) }}</span>
              <UBadge
                variant="outline" size="sm"
                :color="info[step.method]?.stage === 'archive' ? 'info' : 'primary'"
                :icon="info[step.method]?.stage === 'archive' ? 'i-fa7-solid-box-archive' : 'i-fa7-solid-bolt'"
                :label="info[step.method]?.stage === 'archive' ? 'Archive' : 'Live'"
              />
              <UTooltip :text="detail(step) || badge(step).label">
                <UBadge
                  :color="badge(step).color as any" variant="subtle" size="sm"
                  :icon="badge(step).icon" :label="badge(step).label"
                />
              </UTooltip>
            </div>
            <USwitch
              v-model="step.enabled" :disabled="!info[step.method]?.available" class="shrink-0"
              :aria-label="`Use ${name(step.method)}`"
            />
          </div>
          <div class="mt-1.5 pl-7 sm:pl-[4.75rem]">
            <p class="text-sm text-muted">{{ info[step.method]?.description }}</p>
            <div class="flex flex-wrap gap-1.5 mt-2">
              <span class="chip"><UIcon name="i-fa7-regular-clock" class="size-3" />{{ info[step.method]?.speed }}</span>
              <span class="chip"><UIcon name="i-fa7-solid-coins" class="size-3" />{{ info[step.method]?.cost }}</span>
              <span v-if="info[step.method]?.local" class="chip">
                <UIcon name="i-fa7-solid-server" class="size-3" />Fetches from this server
              </span>
            </div>
            <p v-if="detail(step) && step.enabled && info[step.method]?.available" class="text-xs text-dimmed mt-1.5">
              {{ detail(step) }}
            </p>
          </div>
        </li>
      </ol>

      <div class="grid sm:grid-cols-[1fr_auto] gap-3 items-start">
        <p class="text-xs text-muted">
          <UIcon name="i-fa7-solid-circle-info" class="size-3" />
          A site's own integration always comes first for its domains, and the exceptions
          below fix the live methods of some domains. A missing page (404) or a dead host
          skips the remaining live methods; archive methods still run.
        </p>
        <UFormField
          label="Hedging (s)" class="sm:w-64"
          description="Start the next live method after this head start, alongside the running one. Faster, but duplicates work."
        >
          <UInput v-model="hedging" type="number" min="0" step="0.5" placeholder="Off" class="w-full" />
        </UFormField>
      </div>
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
.chain-card { touch-action: pan-y; }
</style>
