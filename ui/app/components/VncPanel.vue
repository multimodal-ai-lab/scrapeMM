<script setup lang="ts">
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

let rfb: any = null

function socketUrl() {
  const base = apiBase()
  const origin = base || window.location.origin
  const url = new URL('/v1/archive-today/vnc', origin)
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
    const { default: RFB } = await import('@novnc/novnc')
    rfb = new RFB(container.value, socketUrl(), {})
    rfb.scaleViewport = true
    rfb.clipViewport = true
    rfb.addEventListener('connect', () => { state.value = 'connected' })
    rfb.addEventListener('disconnect', (event: any) => {
      state.value = event?.detail?.clean ? 'idle' : 'failed'
      if (!event?.detail?.clean) {
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
      class="w-full h-[600px] bg-black rounded border border-default overflow-hidden"
    />
  </div>
</template>
