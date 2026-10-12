<script setup lang="ts">
/** Server configuration, one section at a time: a panel on the left lists the sections,
 *  and the one picked is all the page shows -- the single long scroll it used to be had
 *  grown past finding anything on it. The section is kept in the URL, so a reload or a
 *  link lands on it.
 *
 *  Every section keeps a draft; the one save button at the bottom right saves all changed
 *  sections together -- except Secrets, saved one by one as they are entered. So all
 *  sections stay mounted, the hidden ones merely hidden: switching away from a section
 *  must neither lose its draft nor take it off the save button. The blacklist itself
 *  lives on its own page, the API keys too. */
const api = useApi()
const route = useRoute()
const router = useRouter()
const sections = provideSettingsSections()
const server = useServerConfig()

const error = ref('')
const notice = ref('')

// --- The sections -------------------------------------------------------------------

interface Entry { id: string, label: string, icon: string, blurb: string }

const GROUPS: { label: string, entries: Entry[] }[] = [
  { label: 'Retrieval', entries: [
    { id: 'general', label: 'General', icon: 'i-fa7-solid-sliders',
      blurb: 'What applies to every retrieval, whichever method runs it.' },
    { id: 'chain', label: 'Retrieval chain', icon: 'i-fa7-solid-list-ol',
      blurb: 'The methods scrapeMM tries for every URL, top to bottom, until one gets the page.' },
    { id: 'site-rules', label: 'Site rules', icon: 'i-fa7-solid-route',
      blurb: 'Sites that need particular methods: rules replace the chain\'s live methods for matching domains.' },
    { id: 'parallelism', label: 'Parallelism', icon: 'i-fa7-solid-layer-group',
      blurb: 'Slots: how many URLs a method retrieves at once. Cooldown: the least time between two of its retrievals starting (YouTube needs one).' },
  ] },
  { label: 'Sources', entries: [
    { id: 'integrations', label: 'Integrations', icon: 'i-fa7-solid-puzzle-piece',
      blurb: 'The integrations for particular platforms, which retrieve through their APIs.' },
    { id: 'archives', label: 'Archives', icon: 'i-fa7-solid-box-archive',
      blurb: 'Copies of a page captured earlier: Archive.today, the Wayback Machine and others.' },
    { id: 'browser', label: 'Browser', icon: 'i-fa7-solid-window-maximize',
      blurb: 'The server\'s own browser, which the Browser method and the archives retrieve with.' },
  ] },
  { label: 'Data', entries: [
    { id: 'cache', label: 'Cache', icon: 'i-fa7-solid-database',
      blurb: 'Recent results served again for the same URL, and archive captures kept for good.' },
    { id: 'media', label: 'Media', icon: 'i-fa7-solid-photo-film',
      blurb: 'Which images and videos a result keeps: small ones on web pages are left out as icons and logos, a post\'s own media are kept.' },
    { id: 'history', label: 'History', icon: 'i-fa7-solid-clock-rotate-left',
      blurb: 'How far back the Jobs and Statistics views reach. The Dashboard\'s totals count everything ever run.' },
  ] },
  { label: 'Access', entries: [
    { id: 'secrets', label: 'Secrets', icon: 'i-fa7-solid-lock',
      blurb: 'The credentials the integrations need.' },
    { id: 'proxy', label: 'Proxy', icon: 'i-fa7-solid-shuffle',
      blurb: 'A way around sites that refuse this server\'s address.' },
  ] },
]

const ENTRIES = GROUPS.flatMap((g) => g.entries)
const IDS = ENTRIES.map((e) => e.id)

// Anchors of the page as it was, still linked to from elsewhere (bookmarks, old links)
const OLD_ANCHORS: Record<string, string> = { exceptions: 'site-rules', retrieval: 'general' }

const sectionOf = (name: unknown) => {
  const id = typeof name === 'string' ? (OLD_ANCHORS[name] ?? name) : ''
  return IDS.includes(id) ? id : null
}

const current = computed(() =>
  sectionOf(route.query.section) ?? sectionOf(route.hash.slice(1)) ?? IDS[0]!)

// An old anchor link becomes the query it now is, so the URL says what is shown
onMounted(() => {
  const fromHash = sectionOf(route.hash.slice(1))
  if (fromHash && !route.query.section) router.replace({ query: { ...route.query, section: fromHash }, hash: '' })
})

// A new section starts at its top, not wherever the last one was scrolled to
watch(current, () => { if (window.scrollY > 0) window.scrollTo({ top: 0 }) })

function show(id: string) {
  router.replace({ query: { ...route.query, section: id }, hash: '' })
}

/** The panel entries with unsaved changes, for their dots */
const unsaved = computed(() => new Set([...sections.values()]
  .filter((s) => unref(s.dirty)).map((s) => s.panel ?? s.id)))

// The narrow layout's select: the same groups, as Nuxt UI's grouped items
const selectItems = GROUPS.map((g) => [
  { type: 'label' as const, label: g.label },
  ...g.entries.map((e) => ({ label: e.label, value: e.id, icon: e.icon })),
])

const entry = (id: string) => ENTRIES.find((e) => e.id === id)!

/** How many plain settings the server describes for a section, to say so when none */
const scalarCount = (id: string) => (server.value?.schema || []).filter((s) =>
  s.section === id && ['int', 'float', 'bool', 'str'].includes(s.type)).length

// --- Cache usage --------------------------------------------------------------------

const cacheStats = ref<any>(null)

async function refreshCacheStats() {
  try {
    cacheStats.value = await api.get<any>('/v1/cache')
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

// --- Media --------------------------------------------------------------------------

const media = ref<any>(null)

let statsTimer: ReturnType<typeof setInterval> | undefined

onMounted(async () => {
  // Each on its own: the figures failing must not keep the settings from loading
  api.get<any>('/v1/media').then((m) => { media.value = m }).catch(() => {})
  refreshCacheStats()
  try {
    adoptServerConfig(server, await api.get<any>('/v1/config'))
  } catch (e: any) {
    error.value = e.message
  }
  statsTimer = setInterval(refreshCacheStats, 10_000) // Live figures
})
onBeforeUnmount(() => clearInterval(statsTimer))
</script>

<template>
  <div class="pb-24">
    <h1 class="text-2xl font-semibold mb-6">Settings</h1>

    <div class="flex flex-col md:flex-row gap-4 md:gap-8">
      <!-- Wide: a panel beside the content, staying in view as a long section scrolls
           (which takes <main> not being a scroll container, see app.vue). In a window
           too short for it, the panel scrolls by itself. -->
      <nav
        class="hidden md:block w-48 shrink-0 sticky top-6 self-start space-y-4
               max-h-[calc(100vh-3rem)] overflow-y-auto"
        aria-label="Settings sections"
      >
        <div v-for="group in GROUPS" :key="group.label" class="space-y-1">
          <p class="px-2.5 text-xs font-semibold uppercase tracking-wider text-dimmed">{{ group.label }}</p>
          <NuxtLink
            v-for="e in group.entries" :key="e.id" :to="{ query: { section: e.id } }" replace
            class="flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm hover-row"
            :class="current === e.id ? 'bg-elevated text-highlighted font-medium' : 'text-muted hover:text-default'"
            :aria-current="current === e.id ? 'page' : undefined"
          >
            <UIcon :name="e.icon" class="size-3.5 shrink-0" />
            <span class="truncate flex-1">{{ e.label }}</span>
            <span
              v-if="unsaved.has(e.id)" class="size-1.5 rounded-full bg-warning shrink-0"
              title="Unsaved changes" aria-label="Unsaved changes"
            />
          </NuxtLink>
        </div>
      </nav>

      <!-- Narrow: the same sections in a select above the content -->
      <USelect
        :model-value="current" :items="selectItems" :icon="entry(current).icon"
        class="md:hidden w-full" aria-label="Settings section"
        @update:model-value="show(String($event))"
      >
        <template #item-trailing="{ item }">
          <span
            v-if="unsaved.has((item as any).value)" class="size-1.5 rounded-full bg-warning"
            aria-label="Unsaved changes"
          />
        </template>
      </USelect>

      <div class="flex-1 min-w-0 max-w-3xl space-y-4">
        <UAlert v-if="error" color="error" variant="subtle" :description="error" close @update:open="error = ''" />
        <UAlert v-if="notice" color="success" variant="subtle" :description="notice" close @update:open="notice = ''" />

        <!-- v-show, never v-if: see the top -->
        <section
          v-for="e in ENTRIES" v-show="current === e.id" :key="e.id"
          :aria-labelledby="`settings-${e.id}`" class="space-y-4"
        >
          <header>
            <h2 :id="`settings-${e.id}`" class="text-lg font-semibold flex items-center gap-2">
              <UIcon :name="e.icon" class="size-4 text-primary" />
              {{ e.label }}
            </h2>
            <p class="text-sm text-muted mt-0.5">{{ e.blurb }}</p>
          </header>

          <template v-if="e.id === 'general'">
            <FirecrawlEditor panel="general" />
            <SettingsFields section="general" title="General" :known="IDS" />
          </template>

          <template v-else-if="e.id === 'chain'">
            <ChainEditor />
            <SettingsFields section="chain" title="Retrieval chain: settings" />
            <ChainPreview />
          </template>

          <template v-else-if="e.id === 'site-rules'">
            <SiteRulesEditor />
            <SettingsFields section="site-rules" title="Site rules: settings" />
            <ChainPreview />
          </template>

          <template v-else-if="e.id === 'parallelism'">
            <ParallelismEditor />
            <!-- The one place where two limits stack, which the table alone does not show -->
            <UAlert
              color="neutral" variant="subtle" icon="i-fa7-solid-circle-info"
              title="Browser slots and browser pages"
              description="Slots are per method; pages are the shared browser's. A Browser
                retrieval needs a Browser slot, then a free page — and the archive replays
                (Wayback, Perma.cc, Ghostarchive, Archive.today) take pages from the same
                pool. With as many Browser slots as pages, the Browser method alone can fill
                the browser and leave the archives waiting: give it fewer slots than there
                are pages to keep some for them."
            />
            <SettingsFields section="parallelism" title="Parallelism: settings" />
            <p v-if="server && !server.methods.length && !scalarCount('parallelism')" class="text-sm text-muted">
              This server does not tell the methods' limits yet.
            </p>
          </template>

          <template v-else-if="e.id === 'cache'">
            <UCard>
              <div class="space-y-4">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <span class="text-sm text-muted tabular-nums">
                    <template v-if="cacheStats">
                      {{ cacheStats.enabled ? 'On' : 'Off' }} · {{ compact(cacheStats.entries ?? 0) }} entries ·
                      {{ cacheStats.size_mb ?? 0 }} MB in memory
                    </template>
                    <template v-else>…</template>
                  </span>
                  <div class="flex items-center gap-1">
                    <UButton size="xs" variant="ghost" icon="i-fa7-solid-broom" label="Clear recent"
                      title="Empties the recent results; the permanent archive tier stays" @click="clearCache('recent')" />
                    <UButton size="xs" variant="ghost" color="error" icon="i-fa7-solid-trash" label="Clear all"
                      title="Also empties the permanent tier (archive captures, their media, Archive.today pages)"
                      @click="clearCache('all')" />
                  </div>
                </div>
                <p class="text-xs text-muted">
                  A successful retrieval is served again for the same URL and settings until it
                  expires. Clients can ask for a fresh one anyway (<code class="font-mono">use_cache=False</code>).
                </p>
                <div class="border-t border-default pt-4 space-y-2">
                  <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
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
                    media-registry file each archived medium became, are kept on disk without expiry, so
                    asking again needs no network, and Archive.today pages retrieved during a solved CAPTCHA
                    stay available. Turning the cache off does not turn this tier off; the least recently
                    used entries go once it is full.
                  </p>
                </div>
              </div>
            </UCard>
            <SettingsFields section="cache" title="Cache" />
            <UCard v-if="media">
              <h3 class="text-sm font-medium flex items-center gap-2">
                <UIcon name="i-fa7-solid-photo-film" class="size-3.5 text-dimmed" />
                Media registry
              </h3>
              <p class="text-sm text-muted mt-2">
                {{ media.files == null ? 'Still counting the' : compact(media.files) }} files at <code class="font-mono">{{ media.root }}</code>
                <span v-if="media.host_root"> (host path <code class="font-mono">{{ media.host_root }}</code>)</span>.
              </p>
              <p class="text-xs text-muted mt-2">
                The server never deletes media on its own: clients that share this machine hold
                references straight into this directory, and removing a file would break
                sequences handed out earlier. Prune it yourself when you decide to.
              </p>
            </UCard>
          </template>

          <SecretsSection v-else-if="e.id === 'secrets'" />
          <ProxySection v-else-if="e.id === 'proxy'" />

          <template v-else>
            <p v-if="e.id === 'integrations'" class="text-xs text-muted">
              Their API keys and logins are under
              <NuxtLink to="/settings?section=secrets" class="text-primary hover:underline">Secrets</NuxtLink>.
            </p>
            <SettingsFields :section="e.id" :title="e.label" />
            <p v-if="server && !scalarCount(e.id)" class="text-sm text-muted">
              Nothing to set here on this server.
            </p>
          </template>
        </section>
      </div>
    </div>

    <SaveFab :sections="sections" />
  </div>
</template>
