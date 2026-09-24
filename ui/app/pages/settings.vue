<script setup lang="ts">
/** Server configuration. The blacklist itself lives on its own page; only how long
 *  its automatic entries last is a setting. */
const api = useApi()

const config = ref<Record<string, any>>({})
const schema = ref<Record<string, string>>({})
const cache = ref<any>({ entries: 0, ttl: 0 })
const media = ref<any>(null)
const error = ref('')
const notice = ref('')
const saving = ref(false)


// Shown as a textarea because several Firecrawl instances are the normal case
const firecrawlUrls = ref('')

async function load() {
  try {
    const [cfg, cacheState, mediaState] = await Promise.all([
      api.get<any>('/v1/config'),
      api.get<any>('/v1/cache'),
      api.get<any>('/v1/media'),
    ])
    config.value = cfg.config
    schema.value = cfg.settings
    cache.value = cacheState
    media.value = mediaState
    firecrawlUrls.value = (cfg.config.firecrawl_urls || []).join('\n')
  } catch (e: any) {
    error.value = e.message
  }
}

async function save() {
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const body: Record<string, any> = {
      firecrawl_urls: firecrawlUrls.value.split('\n').map((u) => u.trim()).filter(Boolean),
      hedging_delay: numberOrNull(config.value.hedging_delay),
      cache_ttl: numberOrNull(config.value.cache_ttl),
      blacklist_ttl: numberOrNull(config.value.blacklist_ttl),
      max_concurrency: numberOrNull(config.value.max_concurrency),
      archive_today_interactive_solve: !!config.value.archive_today_interactive_solve,
      archive_today_screenshot_fallback: !!config.value.archive_today_screenshot_fallback,
    }
    // Leaving a field empty means "keep the server's default", not "set it to zero"
    for (const key of Object.keys(body)) if (body[key] === null) delete body[key]
    config.value = (await api.patch<any>('/v1/config', body)).config
    notice.value = 'Configuration saved.'
  } catch (e: any) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

function numberOrNull(value: any) {
  if (value === '' || value === null || value === undefined) return null
  const parsed = Number(value)
  return Number.isNaN(parsed) ? null : parsed
}

async function clearCache() {
  try {
    const result = await api.post<any>('/v1/cache/clear')
    notice.value = `Cleared ${result.cleared} cached responses.`
    await load()
  } catch (e: any) {
    error.value = e.message
  }
}

// null until known; the key cannot be regenerated here when .env sets it
const keyFromEnvironment = ref<boolean | null>(null)
const regenerating = ref(false)
const newKey = ref('')
const keyCopied = ref(false)

async function loadKeyState() {
  try {
    keyFromEnvironment.value = (await api.get<any>('/v1/api-key')).from_environment
  } catch (e: any) {
    error.value = e.message
  }
}

async function regenerateKey() {
  if (!confirm('Regenerate the API key? Every client using the current key is rejected '
    + 'until it is given the new one.')) return
  regenerating.value = true
  error.value = ''
  notice.value = ''
  try {
    const { api_key } = await api.post<any>('/v1/api-key/regenerate')
    // This browser switches over at once; otherwise its next call would be rejected
    setToken(api_key)
    newKey.value = api_key
  } catch (e: any) {
    error.value = e.message
  } finally {
    regenerating.value = false
  }
}

async function copyNewKey() {
  if (!(await copyText(newKey.value))) {
    error.value = 'Copying to the clipboard failed. Select the key and copy it by hand.'
    return
  }
  keyCopied.value = true
  setTimeout(() => { keyCopied.value = false }, 1500)
}

onMounted(() => {
  load()
  loadKeyState()
})
</script>

<template>
  <div class="space-y-6 max-w-3xl">
    <h1 class="text-2xl font-semibold">Settings</h1>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UAlert v-if="notice" color="success" variant="subtle" :description="notice" />

    <UCard>
      <template #header><h2 class="font-medium">Retrieval</h2></template>
      <div class="space-y-4">
        <UFormField
          label="Firecrawl instances" hint="One URL per line"
          description="scrapeMM spreads its scrapes across every instance that responds."
        >
          <UTextarea v-model="firecrawlUrls" :rows="3" class="w-full" placeholder="http://firecrawl:3002" />
        </UFormField>
        <div class="grid sm:grid-cols-2 gap-4">
          <UFormField
            label="Hedging delay (s)"
            description="Head start each method gets before the next runs alongside it. Empty disables hedging."
          >
            <UInput v-model="config.hedging_delay" type="number" step="0.5" min="0" />
          </UFormField>
          <UFormField label="Max concurrent URLs">
            <UInput v-model="config.max_concurrency" type="number" min="1" />
          </UFormField>
          <UFormField label="Cache lifetime (s)" description="0 disables the cache.">
            <UInput v-model="config.cache_ttl" type="number" min="0" />
          </UFormField>
          <UFormField
            label="Blacklist lifetime (s)"
            description="How long automatic CAPTCHA blacklistings last. 0 never expires."
            hint="Manage entries under Blacklist"
          >
            <UInput v-model="config.blacklist_ttl" type="number" min="0" />
          </UFormField>
        </div>
        <div class="space-y-2">
          <UCheckbox
            v-model="config.archive_today_interactive_solve"
            label="Archive.today: ask for a CAPTCHA at the moment of a gated request"
            description="Off by default. Sensible only for one-off, attended retrievals."
          />
          <UCheckbox
            v-model="config.archive_today_screenshot_fallback"
            label="Archive.today: serve the snapshot's screenshot when it is gated"
            description="Gives an unattended run something rather than an error — but a screenshot is not the page text."
          />
        </div>
        <UButton :loading="saving" label="Save" @click="save" />
      </div>
    </UCard>

    <UCard>
      <template #header>
        <div class="flex items-center justify-between">
          <h2 class="font-medium">Cache</h2>
          <UButton size="xs" variant="ghost" label="Clear" @click="clearCache" />
        </div>
      </template>
      <p class="text-sm text-muted">
        {{ cache.entries }} responses cached, lifetime {{ cache.ttl }}s.
      </p>
    </UCard>

    <UCard>
      <template #header>
        <div class="flex items-center justify-between">
          <h2 class="font-medium">API key</h2>
          <UButton
            size="xs" variant="ghost" color="error" icon="i-fa7-solid-rotate"
            label="Regenerate" :loading="regenerating"
            :disabled="keyFromEnvironment !== false" @click="regenerateKey"
          />
        </div>
      </template>
      <p v-if="keyFromEnvironment" class="text-sm text-muted">
        The key is set by <code class="font-mono">SCRAPEMM_API_KEY</code> in the server's
        <code class="font-mono">.env</code>. Change it there, or empty it to manage the key here.
      </p>
      <p v-else class="text-sm text-muted">
        A new key replaces the current one at once: clients that still use the old key are
        rejected until they are given the new one. This browser switches over by itself.
      </p>
      <div v-if="newKey" class="mt-3 flex items-center gap-2">
        <UInput :model-value="newKey" readonly class="flex-1 font-mono" />
        <UButton
          :icon="keyCopied ? 'i-fa7-solid-check' : 'i-fa7-regular-copy'"
          color="neutral" variant="subtle" :label="keyCopied ? 'Copied' : 'Copy'"
          @click="copyNewKey"
        />
      </div>
    </UCard>

    <UCard v-if="media">
      <template #header><h2 class="font-medium">Media registry</h2></template>
      <p class="text-sm text-muted">
        {{ media.files }} files at <code class="font-mono">{{ media.root }}</code>
        <span v-if="media.host_root"> (host path <code class="font-mono">{{ media.host_root }}</code>)</span>.
      </p>
      <p class="text-xs text-muted mt-2">
        The server never deletes media on its own: clients that share this machine hold
        references straight into this directory, and removing a file would break
        sequences handed out earlier. Prune it yourself when you decide to.
      </p>
    </UCard>
  </div>
</template>
