<script setup lang="ts">
/**
 * The proxies scrapeMM falls back on when this server's address is blocked: a method that
 * fails because of the address (a refused YouTube stream, its bot check, HTTP 403/451, a
 * rate limit) is retried through the best proxy for it, a host unreachable from here is
 * fetched through one, and while YouTube is paused for this server, YouTube goes through
 * one directly. Each endpoint shows its last test (exit address, location, latency) and
 * its record: how often each method got through it and how often it was refused.
 *
 * Edited as a draft, saved with the page's save button. The username and password are
 * shared by all endpoints and write-only: the server only says whether they are set.
 */
interface Endpoint { key: number, url: string, enabled: boolean }
interface Draft { proxies: Endpoint[], username: string, password: string, clear: boolean }

const api = useApi()

const server = ref<any>(null)
const draft = ref<Draft>({ proxies: [], username: '', password: '', clear: false })
const error = ref('')
const testingAll = ref(false)
const testing = ref<Record<number, boolean>>({})
// Results of tests run here, by endpoint key: unsaved endpoints have no record yet
const results = ref<Record<number, any>>({})

let nextKey = 1

// Host and port; the scheme is optional (the server takes none as http://)
const URL_PATTERN = /^((https?|socks5h?):\/\/)?[A-Za-z0-9.\-[\]:]+:\d{1,5}$/i

const status = computed(() => server.value?.status ?? null)
const records = computed<Record<string, any>>(() =>
  Object.fromEntries((server.value?.proxies ?? []).map((p: any) => [p.url, p])))

function fromServer(): Draft {
  return {
    proxies: (server.value?.proxies ?? []).map((p: any) => ({ key: nextKey++, url: p.url, enabled: p.enabled })),
    username: '', password: '', clear: false,
  }
}

const comparable = (d: Draft) => JSON.stringify({ ...d, proxies: d.proxies.map(({ url, enabled }) => ({ url, enabled })) })
const dirty = computed(() => !!server.value && comparable(draft.value) !== comparable(fromServer()))

// The URL as the server stores it: without a scheme, http://
const normalized = (url: string) => {
  const u = url.trim().replace(/\/$/, '')
  return /^[a-z0-9]+:\/\//i.test(u) ? u.replace(/^[a-z0-9]+/i, (s) => s.toLowerCase()) : `http://${u}`
}

function problemOf(p: Endpoint): string {
  const url = p.url.trim().replace(/\/$/, '')
  if (!url) return 'Enter host and port.'
  if (/^[a-z0-9+]+:\/\/[^/]*@/i.test(url)) return 'Put the username and password in their own fields, not in the URL.'
  if (!URL_PATTERN.test(url)) return 'The URL must look like host:port, optionally with http://, https:// or socks5:// in front.'
  const port = Number(url.split(':').pop())
  if (port < 1 || port > 65535) return `${port} is not a valid port.`
  if (draft.value.proxies.filter((o) => o.url.trim() && normalized(o.url) === normalized(url)).length > 1) return 'This endpoint is in the list twice.'
  return ''
}

const problems = computed(() => Object.fromEntries(draft.value.proxies.map((p) => [p.key, problemOf(p)])))
const firstProblem = computed(() => Object.values(problems.value).find(Boolean) ?? '')

async function load() {
  try {
    server.value = await api.get<any>('/v1/proxy')
    draft.value = fromServer()
  } catch (e: any) {
    error.value = e.message
  }
}

async function save() {
  if (firstProblem.value) throw new Error(`Proxy: ${firstProblem.value}`)
  const d = draft.value
  const body: Record<string, any> = {
    proxies: d.proxies.map((p) => ({ url: p.url.trim(), enabled: p.enabled })),
    clear_credentials: d.clear,
  }
  // Empty fields keep what is stored
  if (!d.clear && d.username) body.username = d.username
  if (!d.clear && d.password) body.password = d.password
  server.value = await api.put<any>('/v1/proxy', body)
  draft.value = fromServer()
  results.value = {}
}

function discard() {
  draft.value = fromServer()
  results.value = {}
}

useSettingsSection({ id: 'proxy', title: 'Proxy', dirty, save, discard })
onMounted(load)

function add() {
  draft.value.proxies.push({ key: nextKey++, url: '', enabled: true })
}

/** A copy right below, on the next port not in the list yet: a provider's endpoints
 * usually differ in the port only. */
function duplicate(p: Endpoint) {
  const url = p.url.trim().replace(/\/$/, '')
  const match = url.match(/^(.*):(\d{1,5})$/)
  let copy = url
  if (match) {
    const taken = new Set(draft.value.proxies.map((o) => normalized(o.url)))
    let port = Number(match[2])
    do { port++ } while (port <= 65535 && taken.has(normalized(`${match[1]}:${port}`)))
    if (port <= 65535) copy = `${match[1]}:${port}`
  }
  const i = draft.value.proxies.findIndex((o) => o.key === p.key)
  draft.value.proxies.splice(i + 1, 0, { key: nextKey++, url: copy, enabled: p.enabled })
}

function remove(p: Endpoint) {
  draft.value.proxies = draft.value.proxies.filter((o) => o.key !== p.key)
}

function credentialsBody(): Record<string, any> {
  const d = draft.value
  if (d.clear) return { username: '', password: '' }
  const body: Record<string, any> = {}
  if (d.username) body.username = d.username
  if (d.password) body.password = d.password
  return body
}

async function test(p: Endpoint) {
  if (problems.value[p.key]) return
  testing.value[p.key] = true
  delete results.value[p.key]
  try {
    results.value[p.key] = await api.post<any>('/v1/proxy/test', { url: p.url.trim(), ...credentialsBody() })
    // Kept as its health when it was tested as saved: show the record as it is now
    if (!dirty.value) await refresh()
  } catch (e: any) {
    results.value[p.key] = { ok: false, error: e.message }
  } finally {
    testing.value[p.key] = false
  }
}

async function testAll() {
  testingAll.value = true
  try {
    server.value = await api.post<any>('/v1/proxy/test-all', {})
    results.value = {}
  } catch (e: any) {
    error.value = e.message
  } finally {
    testingAll.value = false
  }
}

async function refresh() {
  try {
    const fresh = await api.get<any>('/v1/proxy')
    server.value = fresh
  } catch { /* Shown on the next load */ }
}

/** The record of a saved endpoint, if the draft still has it as saved. */
const recordOf = (p: Endpoint) => records.value[normalized(p.url)] ?? null
const testOf = (p: Endpoint) => results.value[p.key] ?? recordOf(p)?.test ?? null

const credentialsStored = computed(() => !draft.value.clear
  && (server.value?.username_set || server.value?.password_set))

const now = () => Date.now() / 1000

function health(p: Endpoint): { label: string, color: 'success' | 'error' | 'warning' | 'neutral' } {
  if (!p.enabled) return { label: 'Off', color: 'neutral' }
  const record = recordOf(p)
  const t = testOf(p)
  if (record?.down_until) return { label: 'Down', color: 'error' }
  if (t && !t.ok) return { label: 'Failed', color: 'error' }
  if (!t) return { label: 'Untested', color: 'neutral' }
  return { label: 'Works', color: 'success' }
}

const regionNames = typeof Intl !== 'undefined' && (Intl as any).DisplayNames
  ? new Intl.DisplayNames(['en'], { type: 'region' }) : null
const countryName = (code?: string) => {
  if (!code) return ''
  try { return regionNames?.of(code) ?? code } catch { return code }
}

const percent = (v: number) => `${Math.round(v * 100)}%`
const reputationColor = (v: number) => v >= 0.75 ? 'success' : v >= 0.4 ? 'warning' : 'error'
const clock = (at: number) => new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

const methodsOf = (p: Endpoint) => Object.entries(recordOf(p)?.methods ?? {}) as [string, any][]

const ago = (at?: number | null) => {
  if (!at) return ''
  const s = Math.max(0, now() - at)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return new Date(at * 1000).toLocaleDateString()
}

const usableCount = computed(() => status.value?.usable_count ?? 0)
</script>

<template>
  <!-- No title of its own: the Settings page heads the section -->
  <UCard>
    <UAlert v-if="error" color="error" variant="subtle" :description="error" class="mb-4" />

    <div class="space-y-4">
      <div v-if="status?.configured" class="flex flex-wrap items-center gap-x-3 gap-y-1">
        <UBadge :color="usableCount ? 'success' : status.enabled ? 'warning' : 'neutral'" variant="subtle" size="sm"
          :label="status.enabled ? `${usableCount} of ${status.count} usable` : 'Off'" />
        <span class="text-xs text-muted tabular-nums">
          {{ compact(status.requests_today) }} {{ status.requests_today === 1 ? 'attempt' : 'attempts' }} through them today
        </span>
        <span v-if="status.countries?.length" class="text-xs text-muted">
          Exits in {{ status.countries.map(countryName).join(', ') }}
        </span>
      </div>
      <p class="text-xs text-muted">
        When a method fails because a site refuses <em>this server's address</em> — YouTube refusing a
        stream or demanding its bot check, HTTP 403 or 451, a rate limit — it is retried through a proxy,
        and if the site refuses that one too, through the next. Hosts that refuse connections from here are
        fetched through one right away, and while YouTube is paused for this server, YouTube videos go
        through one directly. Results say which attempts used one ("browser (via proxy)"). Decodo, Firecrawl
        and the archives fetch from elsewhere and never use them.
      </p>
      <p class="text-xs text-muted">
        <strong class="font-medium">Which proxy:</strong> every endpoint keeps a record per method: how often
        it got through and how often it was refused (its <em>reputation</em>, older outcomes counting less).
        One the method refused rests from it for a while; one whose test failed, or that failed as a proxy,
        is left out. Of the rest, the best reputation wins, and among equals the one used longest ago, so
        that the load spreads across the addresses.
      </p>

      <div class="flex flex-wrap items-center gap-2">
        <UButton size="sm" variant="soft" icon="i-fa7-solid-plus" label="Add proxy" @click="add" />
        <UButton size="sm" variant="soft" icon="i-fa7-solid-satellite-dish" label="Test all"
          :loading="testingAll" :disabled="dirty || !server?.proxies?.length" @click="testAll" />
        <span v-if="dirty && server?.proxies?.length" class="text-xs text-muted">Save first to test all.</span>
        <span v-else-if="status?.last_full_test" class="text-xs text-muted">
          Last tested all {{ ago(status.last_full_test) }}
        </span>
      </div>

      <p v-if="!draft.proxies.length" class="text-sm text-muted">
        No proxy yet. Add one with its host and port.
      </p>

      <div v-for="(p, i) in draft.proxies" :key="p.key"
        class="rounded-md border border-default p-3 space-y-2" :class="p.enabled ? '' : 'opacity-70'">
        <div class="flex flex-wrap items-start gap-2">
          <USwitch v-model="p.enabled" class="mt-1.5" :aria-label="`Use proxy ${i + 1}`" />
          <UFormField class="flex-1 min-w-48" :error="p.url.trim() ? (problems[p.key] || undefined) : undefined">
            <UInput v-model="p.url" class="w-full font-mono" placeholder="proxy.example.com:8080"
              autocomplete="off" spellcheck="false" />
          </UFormField>
          <div class="flex items-center gap-1">
            <UTooltip text="Test this proxy">
              <UButton size="sm" variant="ghost" color="neutral" icon="i-fa7-solid-satellite-dish"
                :loading="testing[p.key]" :disabled="!!problems[p.key]" :aria-label="`Test proxy ${i + 1}`" @click="test(p)" />
            </UTooltip>
            <UTooltip text="Duplicate, on the next port">
              <UButton size="sm" variant="ghost" color="neutral" icon="i-fa7-solid-clone"
                :aria-label="`Duplicate proxy ${i + 1}`" @click="duplicate(p)" />
            </UTooltip>
            <UTooltip text="Remove">
              <UButton size="sm" variant="ghost" color="error" icon="i-fa7-solid-trash"
                :aria-label="`Remove proxy ${i + 1}`" @click="remove(p)" />
            </UTooltip>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs pl-11">
          <UBadge :color="health(p).color" variant="subtle" size="sm" :label="health(p).label" />
          <template v-if="testOf(p)?.ok">
            <span v-if="testOf(p).country" class="text-default">
              {{ countryName(testOf(p).country) }}<template v-if="testOf(p).city">, {{ testOf(p).city }}</template>
            </span>
            <code class="font-mono">{{ testOf(p).ip }}</code>
            <span v-if="testOf(p).org" class="text-muted truncate max-w-64" :title="testOf(p).org">{{ testOf(p).org }}</span>
            <span class="text-muted tabular-nums">{{ testOf(p).latency_ms }} ms</span>
          </template>
          <span v-else-if="testOf(p)" class="text-error">{{ testOf(p).error }}</span>
          <span v-if="testOf(p)?.at" class="text-muted">{{ ago(testOf(p).at) }}</span>
          <span v-if="recordOf(p)?.down_until" class="text-error">
            Left out until {{ clock(recordOf(p).down_until) }}: {{ recordOf(p).down_reason }}
          </span>
        </div>

        <div v-if="recordOf(p) && (methodsOf(p).length || recordOf(p).requests_today)"
          class="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs pl-11">
          <span class="text-muted">Reputation</span>
          <UTooltip v-for="[m, r] in methodsOf(p)" :key="m"
            :text="`${r.ok} through, ${r.refused} refused (recent ones counting most)${r.last_refusal ? ` — last refusal: ${r.last_refusal}` : ''}`">
            <UBadge :color="r.rest_until ? 'warning' : reputationColor(r.reputation)" variant="outline" size="sm">
              {{ m }} {{ percent(r.reputation) }}<template v-if="r.rest_until"> · rests until {{ clock(r.rest_until) }}</template>
            </UBadge>
          </UTooltip>
          <span v-if="recordOf(p).requests_today" class="text-muted tabular-nums">
            · {{ compact(recordOf(p).requests_today) }} today<template v-if="recordOf(p).last_used">, last {{ ago(recordOf(p).last_used) }}</template>
          </span>
        </div>
      </div>

      <div class="border-t border-default pt-4 space-y-3">
        <p class="text-xs text-muted">The login applies to every endpoint.</p>
        <div class="grid sm:grid-cols-2 gap-4" :class="draft.clear ? 'opacity-60' : ''">
          <UFormField label="Username" hint="Optional">
            <UInput v-model="draft.username" class="w-full" autocomplete="off" :disabled="draft.clear"
              :placeholder="server?.username_set && !draft.clear ? 'stored — type to replace' : ''" />
          </UFormField>
          <UFormField label="Password" hint="Optional">
            <UInput v-model="draft.password" type="password" class="w-full" autocomplete="new-password"
              :disabled="draft.clear"
              :placeholder="server?.password_set && !draft.clear ? 'stored — type to replace' : ''" />
          </UFormField>
        </div>
        <div v-if="server?.username_set || server?.password_set || draft.clear" class="flex items-center gap-2 text-xs">
          <UCheckbox v-model="draft.clear" label="Remove the stored username and password" />
        </div>
        <p v-if="credentialsStored" class="text-xs text-muted">
          The login is stored as a secret and never shown again. Leave the fields empty to keep it.
        </p>
      </div>

      <p v-if="status?.youtube_paused_until" class="text-xs text-warning">
        YouTube is paused for this server's address until
        {{ new Date(status.youtube_paused_until * 1000).toLocaleTimeString() }}<template v-if="status.usable_count">;
          YouTube retrievals go through the proxies meanwhile</template>.
      </p>
    </div>
  </UCard>
</template>
