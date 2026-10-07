<script setup lang="ts">
import type RFB from '@novnc/novnc'
import type { RFBDisconnectEvent } from '@novnc/novnc'

/**
 * A live view of the server's browser, for solving the Archive.today access check.
 *
 * The browser has to stay on the server: Archive.today binds a solved check to the
 * browser that solved it and to that browser's IP address, so a check passed in your
 * own browser would buy the server nothing. noVNC gives you its screen and sends your
 * clicks and keystrokes back.
 *
 * The socket goes through the Python server (see `api/vnc.py`), so it uses the same
 * port and the same API key as everything else.
 */
const props = defineProps<{ active: boolean }>()

const container = ref<HTMLElement | null>(null)
const state = ref<'idle' | 'connecting' | 'connected' | 'failed'>('idle')
const detail = ref('')
const token = useToken()

let rfb: RFB | null = null

function socketUrl() {
  const base = apiBase()
  const origin = base || window.location.origin
  const url = new URL('/v1/captcha/vnc', origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.searchParams.set('token', token.value || '')
  return url.toString()
}

async function connect() {
  if (rfb) return
  state.value = 'connecting'
  detail.value = ''
  try {
    // Imported lazily: noVNC touches the DOM on import and is dead weight on every
    // other page. The package's single export is the RFB class itself.
    const { default: Client } = await import('@novnc/novnc')
    if (!container.value) throw new Error('The view is not on the page yet.')
    rfb = new Client(container.value, socketUrl(), {})
    rfb.scaleViewport = true
    rfb.clipViewport = true
    // A held button must reach the server's browser as a drag (slider CAPTCHAs), not
    // pan the view
    rfb.dragViewport = false
    rfb.viewOnly = false
    // Your own pointer is shown instead of a copy of the server's (see the style below
    // and x11vnc's -cursor none in docker/entrypoint.sh), so nothing extra is drawn
    rfb.showDotCursor = false
    rfb.addEventListener('connect', () => { state.value = 'connected' })
    rfb.addEventListener('disconnect', (event: Event) => {
      const clean = (event as RFBDisconnectEvent).detail?.clean
      state.value = clean ? 'idle' : 'failed'
      if (!clean) {
        detail.value = 'The connection to the server\'s screen dropped. '
          + 'Check that the container has a display and x11vnc running.'
      }
      rfb = null
    })
  } catch (e: any) {
    state.value = 'failed'
    detail.value = e?.message || 'Could not open the view.'
    rfb = null
  }
}

function disconnect() {
  if (rfb) {
    try { rfb.disconnect() } catch { /* already gone */ }
    rfb = null
  }
  state.value = 'idle'
}

watch(() => props.active, (active) => {
  if (active) connect()
  else disconnect()
}, { immediate: true })

onBeforeUnmount(disconnect)
</script>

<template>
  <div class="space-y-2">
    <div class="flex items-center gap-2 text-sm">
      <UBadge
        :color="state === 'connected' ? 'success' : state === 'failed' ? 'error' : 'neutral'"
        variant="subtle"
        :label="state === 'connected' ? 'Live' : state === 'connecting' ? 'Connecting…' : state === 'failed' ? 'Disconnected' : 'Idle'"
      />
      <span class="text-muted">Click and type in the frame as if it were your own browser.</span>
    </div>

    <UAlert v-if="detail" color="error" variant="subtle" :description="detail" />

    <div
      ref="container"
      class="vnc w-full h-[600px] bg-black rounded border border-default overflow-hidden"
    />
  </div>
</template>

<style scoped>
/* noVNC paints the server's cursor shape as an inline `cursor: url(...)` bitmap, or as
   `cursor: none` while it has none. The native pointer is crisper and never lags. */
.vnc :deep(canvas) {
  cursor: default !important;
}
</style>
