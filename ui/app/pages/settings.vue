<script setup lang="ts">
/** Server configuration. Every section keeps a draft; the one save button at the bottom
 *  right saves all changed sections together -- except Secrets, saved one by one as they
 *  are entered. The blacklist itself lives on its own page; only how long its automatic
 *  entries last is a setting. The API keys have their own page too. */
const api = useApi()
const sections = provideSettingsSections()

const error = ref('')
const notice = ref('')

// --- Retrieval ----------------------------------------------------------------------

/** The Retrieval section's fields, as the form edits them (numbers as text) */
interface RetrievalForm {
  firecrawl_urls: string
  max_concurrency: string
  max_browser_pages: string
  max_search_concurrency: string
  youtube_min_interval: string
  youtube_cooldown: string
  blacklist_ttl: string
  archive_today_interactive_solve: boolean
  archive_today_screenshot_fallback: boolean
}

const NUMBERS = ['max_concurrency', 'max_browser_pages', 'max_search_concurrency',
  'youtube_min_interval', 'youtube_cooldown', 'blacklist_ttl'] as const

const retrievalSaved = ref<RetrievalForm | null>(null)
const retrieval = ref<RetrievalForm | null>(null)

function toForm(config: Record<string, any>): RetrievalForm {
  const text = (v: any) => (v === null || v === undefined ? '' : String(v))
  return {
    firecrawl_urls: (config.firecrawl_urls || []).join('\n'),
    max_concurrency: text(config.max_concurrency),
    max_browser_pages: text(config.max_browser_pages),
    max_search_concurrency: text(config.max_search_concurrency),
    youtube_min_interval: text(config.youtube_min_interval),
    youtube_cooldown: text(config.youtube_cooldown),
    blacklist_ttl: text(config.blacklist_ttl),
    archive_today_interactive_solve: !!config.archive_today_interactive_solve,
    archive_today_screenshot_fallback: !!config.archive_today_screenshot_fallback,
  }
}

const retrievalDirty = computed(() => !!retrieval.value
  && JSON.stringify(retrieval.value) !== JSON.stringify(retrievalSaved.value))

async function saveRetrieval() {
  const form = retrieval.value!
  const body: Record<string, any> = {
    firecrawl_urls: form.firecrawl_urls.split('\n').map((u) => u.trim()).filter(Boolean),
    archive_today_interactive_solve: form.archive_today_interactive_solve,
    archive_today_screenshot_fallback: form.archive_today_screenshot_fallback,
  }
  for (const key of NUMBERS) {
    const value = form[key].trim()
    // Empty means "keep the server's default", not "set it to zero"
    if (!value) continue
    if (Number.isNaN(Number(value)) || Number(value) < 0) throw new Error(`"${value}" is not a valid number.`)
    body[key] = Number(value)
  }
  const config = (await api.patch<any>('/v1/config', body)).config
  retrievalSaved.value = toForm(config)
  retrieval.value = toForm(config)
}

// Registered directly: `inject()` does not see what the same component provides
sections.set('retrieval', {
  id: 'retrieval', title: 'Retrieval', dirty: retrievalDirty, save: saveRetrieval,
  discard: () => { retrieval.value = { ...retrievalSaved.value! } },
})

// --- Cache --------------------------------------------------------------------------

interface CacheForm { enabled: boolean, ttl: string, max_entries: string, max_mb: string, immutable_max_mb: string }
const cacheStats = ref<any>(null)
const cacheSaved = ref<CacheForm | null>(null)
const cacheForm = ref<CacheForm | null>(null)

function toCacheForm(stats: any): CacheForm {
  return { enabled: !!stats.enabled, ttl: String(stats.ttl), max_entries: String(stats.max_entries),
    max_mb: String(stats.max_mb), immutable_max_mb: String(stats.immutable?.max_mb ?? 10240) }
}

const cacheDirty = computed(() => !!cacheForm.value
  && JSON.stringify(cacheForm.value) !== JSON.stringify(cacheSaved.value))

const cacheProblem = computed(() => {
  const f = cacheForm.value
  if (!f) return ''
  const ttl = Number(f.ttl), entries = Number(f.max_entries), mb = Number(f.max_mb)
  if (f.ttl.trim() === '' || Number.isNaN(ttl) || ttl < 0) return 'The lifetime must be 0 or more seconds.'
  if (!Number.isInteger(entries) || entries < 1 || entries > 1_000_000) return 'Max entries must be a whole number from 1 to 1,000,000.'
  if (Number.isNaN(mb) || mb < 1 || mb > 65536) return 'Max size must be between 1 and 65,536 MB.'
  const permanentMb = Number(f.immutable_max_mb)
  if (Number.isNaN(permanentMb) || permanentMb < 1 || permanentMb > 1048576) return 'The permanent cache must be between 1 MB and 1 TB.'
  return ''
})

async function saveCache() {
  if (cacheProblem.value) throw new Error(cacheProblem.value)
  const f = cacheForm.value!
  const stats = await api.put<any>('/v1/cache/config', {
    enabled: f.enabled, ttl: Number(f.ttl), max_entries: Number(f.max_entries), max_mb: Number(f.max_mb),
    immutable_max_mb: Number(f.immutable_max_mb),
  })
  adoptCache(stats)
}

function adoptCache(stats: any) {
  cacheStats.value = stats
  cacheSaved.value = toCacheForm(stats)
  cacheForm.value = toCacheForm(stats)
}

sections.set('cache', {
  id: 'cache', title: 'Cache', dirty: cacheDirty, save: saveCache,
  discard: () => { cacheForm.value = { ...cacheSaved.value! } },
})

async function refreshCacheStats() {
  try {
    const stats = await api.get<any>('/v1/cache')
    cacheStats.value = stats
    if (!cacheDirty.value) adoptCache(stats)
  } catch { /* The figures are a convenience */ }
}

/** The permanent tier's entries, for the card: null while the server still counts them */
const permanentEntries = computed(() => {
  const e = cacheStats.value?.immutable?.entries
  if (!e) return null
  return { results: e.result ?? 0, media: e.medium ?? 0, pages: e.page ?? 0, snapshots: e.snapshot ?? 0 }
})

async function clearCache(tier: 'recent' | 'all' = 'recent') {
  if (tier === 'all' && !confirm('Also clear the permanent cache? Archive.today pages kept there '
    + 'can only be retrieved again after solving its CAPTCHA, and every archive capture is '
    + 'downloaded afresh.')) return
  try {
    const result = await api.post<any>(`/v1/cache/clear?tier=${tier}`)
    notice.value = tier === 'all'
      ? `Cleared ${result.cleared} recent results and ${result.cleared_permanent} permanent entries.`
      : `Cleared ${result.cleared} cached responses.`
    await refreshCacheStats()
  } catch (e: any) {
    error.value = e.message
  }
}

// --- Media -------------------------------------------------------------------------

const media = ref<any>(null)

let statsTimer: ReturnType<typeof setInterval> | undefined

onMounted(async () => {
  try {
    const [cfg, stats, mediaState] = await Promise.all([
      api.get<any>('/v1/config'), api.get<any>('/v1/cache'), api.get<any>('/v1/media'),
    ])
    retrievalSaved.value = toForm(cfg.config)
    retrieval.value = toForm(cfg.config)
    adoptCache(stats)
    media.value = mediaState
  } catch (e: any) {
    error.value = e.message
  }
  statsTimer = setInterval(refreshCacheStats, 10_000) // Live figures
})
onBeforeUnmount(() => clearInterval(statsTimer))
</script>

<template>
  <div class="space-y-6 max-w-3xl pb-24">
    <h1 class="text-2xl font-semibold">Settings</h1>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UAlert v-if="notice" color="success" variant="subtle" :description="notice" />

    <!-- The most consequential settings first -->
    <ChainEditor />
    <ExceptionsEditor />
    <ChainPreview />
    <SecretsSection />

    <UCard v-if="retrieval">
      <template #header>
        <h2 class="font-medium flex items-center gap-2">
          <UIcon name="i-fa7-solid-sliders" class="size-4 text-primary" />
          Retrieval
          <UBadge v-if="retrievalDirty" color="warning" variant="subtle" size="sm" label="Unsaved" />
        </h2>
      </template>
      <div class="space-y-4">
        <UFormField
          label="Firecrawl instances" hint="One URL per line"
          description="scrapeMM spreads its scrapes across every instance that responds."
        >
          <UTextarea v-model="retrieval.firecrawl_urls" :rows="3" class="w-full" placeholder="http://firecrawl:3002" />
        </UFormField>
        <div class="grid sm:grid-cols-2 gap-4">
          <UFormField label="Max concurrent URLs">
            <UInput v-model="retrieval.max_concurrency" type="number" min="1" placeholder="40 (default)" />
          </UFormField>
          <UFormField
            label="Max concurrent browser pages"
            description="Retrievals in the server's browser (the Browser method, the archives) at once; more wait. Lower it if the server runs short of memory."
          >
            <UInput v-model="retrieval.max_browser_pages" type="number" min="1" placeholder="32 (default)" />
          </UFormField>
          <UFormField
            label="Max concurrent searches"
            description="Searches with a search provider at once, across all clients; more wait. Keeps bursts within the provider's rate limit."
          >
            <UInput v-model="retrieval.max_search_concurrency" type="number" min="1" placeholder="10 (default)" />
          </UFormField>
          <UFormField
            label="YouTube pace (s)"
            description="Seconds between two YouTube retrievals. YouTube starts its bot check after bursts; 12 s stays under its ~300 videos an hour."
          >
            <UInput v-model="retrieval.youtube_min_interval" type="number" min="0" step="1" placeholder="12" />
          </UFormField>
          <UFormField
            label="YouTube pause after a bot check (s)"
            description="How long YouTube is left alone once it asked to prove this is no bot. Asking again sooner only extends the block."
          >
            <UInput v-model="retrieval.youtube_cooldown" type="number" min="0" step="60" placeholder="1800" />
          </UFormField>
          <UFormField
            label="Blacklist lifetime (s)"
            description="How long automatic CAPTCHA blacklistings last. 0 never expires."
            hint="Manage entries under Blacklist"
          >
            <UInput v-model="retrieval.blacklist_ttl" type="number" min="0" />
          </UFormField>
        </div>
        <div class="space-y-2">
          <UCheckbox
            v-model="retrieval.archive_today_interactive_solve"
            label="Archive.today: ask for a CAPTCHA at the moment of a gated request"
            description="Off by default. Sensible only for one-off, attended retrievals."
          />
          <UCheckbox
            v-model="retrieval.archive_today_screenshot_fallback"
            label="Archive.today: serve the snapshot's screenshot when it is gated"
            description="Gives an unattended run something rather than an error — but a screenshot is not the page text."
          />
        </div>
      </div>
    </UCard>

    <UCard v-if="cacheForm">
      <template #header>
        <div class="flex items-center justify-between gap-2">
          <h2 class="font-medium flex items-center gap-2">
            <UIcon name="i-fa7-solid-database" class="size-4 text-primary" />
            Cache
            <UBadge v-if="cacheDirty" color="warning" variant="subtle" size="sm" label="Unsaved" />
          </h2>
          <div class="flex items-center gap-1">
            <UButton size="xs" variant="ghost" icon="i-fa7-solid-broom" label="Clear recent"
              title="Empties the recent results; the permanent archive tier stays" @click="clearCache('recent')" />
            <UButton size="xs" variant="ghost" color="error" icon="i-fa7-solid-trash" label="Clear all"
              title="Also empties the permanent tier (archive captures, their media, Archive.today pages)"
              @click="clearCache('all')" />
          </div>
        </div>
      </template>
      <div class="space-y-4">
        <div class="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <USwitch v-model="cacheForm.enabled" label="Re-use recent results" />
          <span class="text-muted tabular-nums">
            {{ cacheStats?.entries ?? 0 }} entries · {{ cacheStats?.size_mb ?? 0 }} MB in memory
          </span>
        </div>
        <p class="text-xs text-muted -mt-2">
          A successful retrieval is served again for the same URL and settings until it
          expires. Clients can ask for a fresh one anyway (<code class="font-mono">use_cache=False</code>).
          Off, no recent result is kept; the permanent archive tier below stays on.
        </p>
        <div class="grid sm:grid-cols-3 gap-4" :class="cacheForm.enabled ? '' : 'opacity-60'">
          <UFormField label="Lifetime (s)" description="0 turns the cache off too.">
            <UInput v-model="cacheForm.ttl" type="number" min="0" step="3600" :disabled="!cacheForm.enabled" />
          </UFormField>
          <UFormField label="Max entries" description="The oldest go first.">
            <UInput v-model="cacheForm.max_entries" type="number" min="1" step="100" :disabled="!cacheForm.enabled" />
          </UFormField>
          <UFormField label="Max size (MB)" description="Page text in memory; media stay on disk.">
            <UInput v-model="cacheForm.max_mb" type="number" min="1" step="64" :disabled="!cacheForm.enabled" />
          </UFormField>
        </div>
        <div class="border-t border-default pt-4 space-y-3">
          <div class="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm">
            <span class="font-medium">Permanent archive tier</span>
            <UBadge color="success" variant="subtle" size="sm" label="always on" />
            <span class="text-muted tabular-nums">
              <template v-if="permanentEntries">
                {{ permanentEntries.results }} results · {{ permanentEntries.media }} media ·
                {{ permanentEntries.pages }} Archive.today pages · {{ permanentEntries.snapshots }} snapshot records
                · {{ cacheStats?.immutable?.size_mb ?? 0 }} of {{ cacheStats?.immutable?.max_mb }} MB on disk
              </template>
              <template v-else>counting…</template>
            </span>
          </div>
          <p class="text-xs text-muted">
            Archive captures never change: an Archive.today snapshot, a Wayback capture with its
            timestamp, a Perma.cc record, a Ghostarchive archive. Their complete results, and which
            media-registry file each archived medium became, are kept on disk without expiry, so asking again needs no network, and Archive.today
            pages retrieved during a solved CAPTCHA stay available. The switch above does not turn
            this tier off; the least recently used entries go once it is full.
          </p>
          <div class="grid sm:grid-cols-3 gap-4">
            <UFormField label="Permanent tier size (MB)" description="On disk, besides the media registry.">
              <UInput v-model="cacheForm.immutable_max_mb" type="number" min="1" step="1024" />
            </UFormField>
          </div>
        </div>
        <p v-if="cacheProblem" class="text-xs text-error">{{ cacheProblem }}</p>
      </div>
    </UCard>

    <UCard v-if="media">
      <template #header>
        <h2 class="font-medium flex items-center gap-2">
          <UIcon name="i-fa7-solid-photo-film" class="size-4 text-primary" />
          Media registry
        </h2>
      </template>
      <p class="text-sm text-muted">
        {{ media.files ?? 'Still counting the' }} files at <code class="font-mono">{{ media.root }}</code>
        <span v-if="media.host_root"> (host path <code class="font-mono">{{ media.host_root }}</code>)</span>.
      </p>
      <p class="text-xs text-muted mt-2">
        The server never deletes media on its own: clients that share this machine hold
        references straight into this directory, and removing a file would break
        sequences handed out earlier. Prune it yourself when you decide to.
      </p>
    </UCard>

    <SaveFab :sections="sections" />
  </div>
</template>
