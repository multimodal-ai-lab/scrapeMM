<script setup lang="ts">
/**
 * The proxy scrapeMM falls back on when this server's address is blocked: a method that
 * fails because of the address (a refused YouTube stream, its bot check, HTTP 403/451, a
 * rate limit) is retried once through it, a host unreachable from here is fetched through
 * it, and while YouTube is paused for this server, YouTube goes through it directly.
 *
 * Edited as a draft, saved with the page's save button. The username and password are
 * write-only: the server only says whether they are set.
 */
interface Draft { url: string, enabled: boolean, username: string, password: string, clear: boolean }

const api = useApi()

const server = ref<any>(null)
const draft = ref<Draft>({ url: '', enabled: true, username: '', password: '', clear: false })
const error = ref('')
const testing = ref(false)
const testResult = ref<any>(null)

const URL_PATTERN = /^(https?|socks5h?):\/\/[A-Za-z0-9.\-[\]:]+:\d{1,5}$/

const entry = computed(() => server.value?.proxies?.[0] ?? null)
const status = computed(() => server.value?.status ?? null)

function fromServer(): Draft {
  return { url: entry.value?.url ?? '', enabled: entry.value?.enabled ?? true,
    username: '', password: '', clear: false }
}

const dirty = computed(() => !!server.value
  && JSON.stringify(draft.value) !== JSON.stringify(fromServer()))

const problem = computed(() => {
  const url = draft.value.url.trim().replace(/\/$/, '')
  if (!url) return ''
  if (/^[a-z0-9+]+:\/\/[^/]*@/i.test(url)) return 'Put the username and password in their own fields, not in the URL.'
  if (!URL_PATTERN.test(url)) return 'The URL must look like scheme://host:port, with http, https or socks5 as the scheme.'
  const port = Number(url.split(':').pop())
  if (port < 1 || port > 65535) return `${port} is not a valid port.`
  return ''
})

async function load() {
  try {
    server.value = await api.get<any>('/v1/proxy')
    draft.value = fromServer()
    testResult.value = server.value.status?.last_test ?? null
  } catch (e: any) {
    error.value = e.message
  }
}

async function save() {
  if (problem.value) throw new Error(`Proxy: ${problem.value}`)
  const d = draft.value
  const body: Record<string, any> = { url: d.url.trim(), enabled: d.enabled, clear_credentials: d.clear }
  // Empty fields keep what is stored
  if (!d.clear && d.username) body.username = d.username
  if (!d.clear && d.password) body.password = d.password
  server.value = await api.put<any>('/v1/proxy', body)
  draft.value = fromServer()
}

function discard() { draft.value = fromServer() }

useSettingsSection({ id: 'proxy', title: 'Proxy', dirty, save, discard })
onMounted(load)

async function test() {
  if (problem.value || !draft.value.url.trim()) return
  testing.value = true
  testResult.value = null
  try {
    const d = draft.value
    const body: Record<string, any> = { url: d.url.trim() }
    if (d.clear) { body.username = ''; body.password = '' } else {
      if (d.username) body.username = d.username
      if (d.password) body.password = d.password
    }
    testResult.value = await api.post<any>('/v1/proxy/test', body)
  } catch (e: any) {
    testResult.value = { ok: false, error: e.message }
  } finally {
    testing.value = false
  }
}

const credentialsStored = computed(() => !draft.value.clear
  && (entry.value?.username_set || entry.value?.password_set))

const ago = (at?: number | null) => {
  if (!at) return ''
  const s = Math.max(0, Date.now() / 1000 - at)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return new Date(at * 1000).toLocaleDateString()
}
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex items-center justify-between gap-2">
        <h2 class="font-medium flex items-center gap-2">
          <UIcon name="i-fa7-solid-shuffle" class="size-4 text-primary" />
          Proxy
          <UBadge v-if="dirty" color="warning" variant="subtle" size="sm" label="Unsaved" />
          <UBadge v-else-if="entry" :color="entry.enabled ? 'success' : 'neutral'" variant="subtle" size="sm"
            :label="entry.enabled ? 'In use' : 'Off'" />
        </h2>
        <span v-if="status?.configured" class="text-xs text-muted tabular-nums">
          {{ status.requests_today }} {{ status.requests_today === 1 ? 'retrieval' : 'retrievals' }} through it today
        </span>
      </div>
    </template>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" class="mb-4" />

    <div class="space-y-4">
      <p class="text-xs text-muted">
        When a method fails because a site refuses <em>this server's address</em> — YouTube refusing a
        stream or demanding its bot check, HTTP 403 or 451, a rate limit — it is retried once through the
        proxy. Hosts that refuse connections from here are fetched through it right away, and while YouTube
        is paused for this server, YouTube videos go through it directly. Results say which attempts used it
        ("browser (via proxy)"). Decodo, Firecrawl and the archives fetch from elsewhere and never use it.
      </p>

      <USwitch v-model="draft.enabled" label="Use the proxy" :disabled="!draft.url.trim()" />

      <UFormField label="Proxy URL" description="http://, https:// or socks5:// with host and port."
        :error="problem || undefined">
        <UInput v-model="draft.url" class="w-full font-mono" placeholder="http://proxy.example.com:8080"
          autocomplete="off" spellcheck="false" />
      </UFormField>

      <div class="grid sm:grid-cols-2 gap-4" :class="draft.clear ? 'opacity-60' : ''">
        <UFormField label="Username" hint="Optional">
          <UInput v-model="draft.username" class="w-full" autocomplete="off" :disabled="draft.clear"
            :placeholder="entry?.username_set && !draft.clear ? 'stored — type to replace' : ''" />
        </UFormField>
        <UFormField label="Password" hint="Optional">
          <UInput v-model="draft.password" type="password" class="w-full" autocomplete="new-password"
            :disabled="draft.clear"
            :placeholder="entry?.password_set && !draft.clear ? 'stored — type to replace' : ''" />
        </UFormField>
      </div>
      <div v-if="entry?.username_set || entry?.password_set || draft.clear" class="flex items-center gap-2 text-xs">
        <UCheckbox v-model="draft.clear" label="Remove the stored username and password" />
      </div>
      <p v-if="credentialsStored" class="text-xs text-muted -mt-2">
        The login is stored as a secret and never shown again. Leave the fields empty to keep it.
      </p>

      <div class="flex flex-wrap items-center gap-3 border-t border-default pt-4">
        <UButton size="sm" variant="soft" icon="i-fa7-solid-satellite-dish" label="Test proxy"
          :loading="testing" :disabled="!draft.url.trim() || !!problem" @click="test" />
        <template v-if="testResult">
          <span v-if="testResult.ok" class="text-sm flex flex-wrap items-center gap-x-3 gap-y-1">
            <UBadge color="success" variant="subtle" size="sm" label="Works" />
            <span>Exit address <code class="font-mono">{{ testResult.ip }}</code></span>
            <span v-if="testResult.country">{{ testResult.country }}<template v-if="testResult.city">, {{ testResult.city }}</template></span>
            <span class="text-muted tabular-nums">{{ testResult.latency_ms }} ms</span>
            <span v-if="testResult.at" class="text-muted text-xs">{{ ago(testResult.at) }}</span>
          </span>
          <span v-else class="text-sm text-error flex items-center gap-2">
            <UBadge color="error" variant="subtle" size="sm" label="Failed" />
            {{ testResult.error }}
          </span>
        </template>
        <span v-else-if="!draft.url.trim()" class="text-xs text-muted">Enter a URL to test it.</span>
      </div>
      <p v-if="status?.youtube_paused_until" class="text-xs text-warning">
        YouTube is paused for this server's address until
        {{ new Date(status.youtube_paused_until * 1000).toLocaleTimeString() }}<template v-if="status.enabled">;
          YouTube retrievals go through the proxy meanwhile</template>.
      </p>
    </div>
  </UCard>
</template>
