<script setup lang="ts">
/**
 * Renders one retrieved page: its text with the media shown inline where the
 * `<image:3>` references sit, plus the raw formats behind tabs.
 *
 * Shared by the playground and the job-detail page, so a stored result looks exactly
 * like a fresh one.
 */
const props = defineProps<{
  url: string
  content: any | null
  method?: string | null
  errors?: Record<string, { type: string, message: string }>
  retrievalTime?: number | null
  queueTime?: number | null
  fromCache?: boolean
  success?: boolean
  // A screenshot's media descriptor; stored results keep it with their content
  screenshot?: any | null
  // For a stored result: the figures of its content, and how to get the content itself,
  // which then comes only once the card is opened (a job's results run to megabytes)
  stats?: any | null
  loadContent?: () => Promise<any>
  // The server's classification (see useOutcome.ts); derived here when absent
  outcome?: string | null
  outcomeKind?: string | null
  // With `collapsible`, the header toggles the content; `collapsed` is where it starts
  collapsible?: boolean
  collapsed?: boolean
}>()

const token = useToken()
const timeTitle = computed(() => retrievalTimeTitle(props.retrievalTime, props.queueTime))
const open = ref(!(props.collapsible && props.collapsed))

function toggle() {
  if (props.collapsible) open.value = !open.value
}

// The content as given, or as fetched on opening
const loaded = ref<any>(null)
const loadingContent = ref(false)
const loadError = ref('')
const shown = computed(() => props.content ?? loaded.value)

async function ensureContent() {
  if (!open.value || shown.value || !props.loadContent || loadingContent.value) return
  loadingContent.value = true
  loadError.value = ''
  try {
    loaded.value = await props.loadContent()
  } catch (e: any) {
    loadError.value = e.message
  } finally {
    loadingContent.value = false
  }
}
watch(open, ensureContent, { immediate: true })

/** Splits the sequence into text runs and media items, in order. */
const parts = computed(() => {
  const text: string = shown.value?.multimodal || ''
  if (!text) return []
  const items: any[] = shown.value?.items || []
  const byRef = new Map(items.map((item) => [item.ref, item]))
  const result: Array<{ kind: 'text', value: string } | { kind: 'media', item: any }> = []

  const pattern = /<(image|video|audio):(\d+)>/g
  let last = 0
  let match: RegExpExecArray | null
  const pushText = (value: string) => {
    // Media sitting next to media leaves empty runs between them; rendering those as
    // blank blocks only adds vertical gaps.
    if (value.trim()) result.push({ kind: 'text', value })
  }
  while ((match = pattern.exec(text))) {
    if (match.index > last) pushText(text.slice(last, match.index))
    const item = byRef.get(match[0])
    // An item missing from the manifest is shown as its reference rather than
    // silently dropped, so a gap in the manifest is visible instead of confusing.
    if (item) result.push({ kind: 'media', item })
    else result.push({ kind: 'text', value: match[0] })
    last = match.index + match[0].length
  }
  if (last < text.length) pushText(text.slice(last))
  return result
})

/** Media is fetched from the API, so the URL has to carry the token. */
function mediaUrl(item: any) {
  return `${apiBase()}${item.media_url}?token=${encodeURIComponent(token.value || '')}`
}

const screenshotItem = computed(() => props.screenshot || shown.value?.screenshot || null)

const tabs = computed(() => {
  const available = []
  if (shown.value?.multimodal) available.push({ label: 'Multimodal', slot: 'multimodal', icon: 'i-fa7-solid-photo-film' })
  if (shown.value?.markdown) available.push({ label: 'Markdown', slot: 'markdown', icon: 'i-fa7-solid-align-left' })
  if (shown.value?.html) available.push({ label: 'HTML', slot: 'html', icon: 'i-fa7-solid-code' })
  if (screenshotItem.value) available.push({ label: 'Screenshot', slot: 'screenshot', icon: 'i-fa7-solid-camera' })
  return available
})

const errorList = computed(() => Object.entries(props.errors || {}))

/** Green, yellow or red, with the kind of yellow: from the server, or derived alike. */
const look = computed(() => {
  const derived = classifyOutcome(!!props.success, props.errors)
  return outcomeLook(props.outcome || derived.outcome,
                     props.outcome ? props.outcomeKind : derived.kind)
})

/**
 * What actually came back, in numbers.
 *
 * "Retrieved" on its own does not say whether a page yielded three paragraphs or a
 * whole article with nine images, and that difference is usually the reason somebody
 * opened the playground. The media manifest already carries a size per item, so this
 * costs nothing to compute.
 */
const ICONS: Record<string, string> = {
  image: 'i-fa7-solid-image',
  video: 'i-fa7-solid-film',
  audio: 'i-fa7-solid-volume-high',
}

const stats = computed(() => {
  // Not loaded (yet): the figures the server worked out for the header
  if (!shown.value && props.stats) {
    return {
      media: props.stats.media,
      bytes: props.stats.bytes,
      unsized: props.stats.unsized,
      characters: props.stats.characters,
      words: null as number | null,
      kinds: Object.entries(props.stats.kinds as Record<string, number>).map(([kind, count]) => ({
        kind, count, icon: ICONS[kind] || 'i-fa7-solid-file',
      })),
      htmlBytes: props.stats.html_bytes,
    }
  }
  const items: any[] = shown.value?.items || []

  const byKind: Record<string, number> = {}
  let bytes = 0
  let unsized = 0
  for (const item of items) {
    byKind[item.kind] = (byKind[item.kind] || 0) + 1
    if (typeof item.size === 'number') bytes += item.size
    else unsized++
  }

  // The longest format present stands in for "how much text": markdown and the
  // multimodal rendering are the same prose, and HTML inflates it with markup.
  const text = shown.value?.markdown || shown.value?.multimodal || ''

  return {
    media: items.length,
    bytes,
    unsized,
    characters: text.length,
    words: (text ? text.trim().split(/\s+/).length : 0) as number | null,
    kinds: Object.entries(byKind).map(([kind, count]) => ({
      kind, count, icon: ICONS[kind] || 'i-fa7-solid-file',
    })),
    htmlBytes: shown.value?.html ? new Blob([shown.value.html]).size : 0,
  }
})

/** A compact number: 18300 -> "18.3k". */
function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}
</script>

<template>
  <!-- No divider between header and body: the spacing separates them well enough -->
  <UCard
    :ui="{ root: `group/result divide-y-0 transition-[background-color,transform,box-shadow] duration-200
                  ${collapsible ? 'hover:bg-elevated hover:-translate-y-px hover:shadow-md' : ''}`,
           header: open ? 'pb-0 sm:pb-0' : '',
           body: open ? '' : 'hidden' }"
  >
    <template #header>
      <div
        class="flex items-start justify-between gap-3"
        :class="collapsible ? 'cursor-pointer select-none' : ''"
        @click="toggle"
      >
        <div class="min-w-0">
          <a
            :href="url" target="_blank" rel="noopener noreferrer" @click.stop
            class="inline-flex items-center gap-1.5 max-w-full hover:underline"
          >
            <UIcon name="i-fa7-solid-link" class="size-3 shrink-0 text-dimmed" />
            <UrlLabel :url="url" />
          </a>
          <div class="flex flex-wrap items-center gap-2 mt-1.5">
            <UBadge :color="look.color" variant="subtle" :icon="look.icon" :label="look.label" />
            <UBadge v-if="method" color="neutral" variant="outline" :label="method" />
            <UBadge
              v-if="fromCache" color="info" variant="outline"
              icon="i-fa7-solid-bolt" label="from cache"
            />
            <span
              v-if="retrievalTime != null"
              class="text-xs text-dimmed inline-flex items-center gap-1"
              :title="timeTitle"
            >
              <UIcon name="i-fa7-solid-stopwatch" class="size-3" />
              {{ seconds(retrievalTime) }}
            </span>
          </div>

          <!-- What came back, in numbers -->
          <div
            v-if="success"
            class="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 text-xs text-dimmed"
          >
            <span
              v-for="kind in stats.kinds" :key="kind.kind"
              class="inline-flex items-center gap-1 tabular-nums"
              :title="`${kind.count} ${kind.kind}(s)`"
            >
              <UIcon :name="kind.icon" class="size-3" />
              {{ kind.count }} {{ kind.kind }}{{ kind.count === 1 ? '' : 's' }}
            </span>

            <span
              v-if="stats.bytes" class="inline-flex items-center gap-1 tabular-nums"
              :title="stats.unsized
                ? `${stats.unsized} item(s) of unknown size are not counted`
                : 'Total size of the downloaded media'"
            >
              <UIcon name="i-fa7-solid-hard-drive" class="size-3" />
              {{ bytes(stats.bytes) }}{{ stats.unsized ? '+' : '' }}
            </span>

            <span
              v-if="stats.characters" class="inline-flex items-center gap-1 tabular-nums"
              :title="`${stats.characters.toLocaleString()} characters${stats.words != null ? `, ${stats.words.toLocaleString()} words` : ''}`"
            >
              <UIcon name="i-fa7-solid-align-left" class="size-3" />
              {{ compact(stats.characters) }} chars
            </span>

            <span
              v-if="stats.htmlBytes" class="inline-flex items-center gap-1 tabular-nums"
              title="Size of the raw HTML"
            >
              <UIcon name="i-fa7-solid-code" class="size-3" />
              {{ bytes(stats.htmlBytes) }}
            </span>

            <span v-if="!stats.media" class="inline-flex items-center gap-1">
              <UIcon name="i-fa7-solid-image" class="size-3" />
              no media
            </span>
          </div>
        </div>

        <div class="flex items-center gap-1 shrink-0">
          <!-- For the page that shows the result: icons that appear on hovering the card -->
          <slot name="actions" />
          <UButton
            v-if="collapsible" color="neutral" variant="ghost" size="sm" square
            class="shrink-0" :aria-expanded="open"
            :aria-label="open ? 'Collapse this result' : 'Expand this result'"
            icon="i-fa7-solid-chevron-down"
            :ui="{ leadingIcon: `transition-transform duration-200 ${open ? 'rotate-180' : ''}` }"
            @click.stop="toggle"
          />
        </div>
      </div>
    </template>

    <!-- Rendered only while open: a job of many results renders none of their text until
         somebody looks at one -->
    <template v-if="open">
    <div v-if="errorList.length" class="space-y-2 mb-4">
      <!-- Yellow where the target was to blame, red where scrapeMM was. A method's error
           that did not decide the outcome -- another method retrieved the page, or found
           the target unavailable -- is only worth a quiet note. -->
      <UAlert
        v-for="[method_, error] in errorList" :key="method_"
        :color="isUnavailableError(error.type) && !success ? 'warning'
          : success || look.color !== 'error' ? 'neutral' : 'error'"
        variant="subtle"
        :icon="isUnavailableError(error.type) ? 'i-fa7-solid-circle-minus' : 'i-fa7-solid-triangle-exclamation'"
        :title="`${method_}: ${error.type}`" :description="error.message"
      />
    </div>

    <p v-if="shown?.truncated?.length" class="text-xs text-dimmed mb-3">
      Stored content was truncated ({{ shown.truncated.join(', ') }}) to keep the job
      history small. Retrieve the URL again for the full version.
    </p>

    <UTabs v-if="tabs.length" :items="tabs" class="w-full">
      <!-- max-w-prose keeps the line length readable: scraped body text at the full
           width of a desktop window is close to unreadable. -->
      <template #multimodal>
        <div class="space-y-3 pt-3 max-w-prose">
          <template v-for="(part, index) in parts" :key="index">
            <MarkdownView v-if="part.kind === 'text'" :source="part.value" />
            <img
              v-else-if="part.item.kind === 'image'" :src="mediaUrl(part.item)"
              class="max-w-full rounded-lg" :alt="part.item.ref"
              loading="lazy"
            >
            <video
              v-else-if="part.item.kind === 'video'" :src="mediaUrl(part.item)" controls
              class="max-w-full rounded-lg"
            />
            <audio v-else :src="mediaUrl(part.item)" controls class="w-full" />
          </template>
        </div>
      </template>
      <template #markdown>
        <div class="pt-3 max-w-prose">
          <MarkdownView :source="shown.markdown" />
        </div>
      </template>
      <template #html>
        <pre class="whitespace-pre-wrap text-xs pt-3 overflow-x-auto">{{ shown.html }}</pre>
      </template>
      <template #screenshot>
        <!-- No link to open it on its own: its address carries the API key, which would
             then stand in the address bar -->
        <div class="pt-3">
          <img
            :src="mediaUrl(screenshotItem)" class="max-w-full rounded-lg border border-default"
            alt="The page as the server's browser showed it" loading="lazy"
          >
        </div>
      </template>
    </UTabs>

    <div v-else-if="loadingContent" class="space-y-2">
      <USkeleton class="h-8 w-64" />
      <USkeleton class="h-4 w-full" />
      <USkeleton class="h-4 w-5/6" />
    </div>
    <UAlert v-else-if="loadError" color="error" variant="subtle" :description="loadError" />
    <p v-else-if="!errorList.length" class="text-sm text-muted">No content.</p>
    </template>
  </UCard>
</template>
