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
  fromCache?: boolean
  success?: boolean
}>()

const token = useToken()

/** Splits the sequence into text runs and media items, in order. */
const parts = computed(() => {
  const text: string = props.content?.multimodal || ''
  if (!text) return []
  const items: any[] = props.content?.items || []
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

const tabs = computed(() => {
  const available = []
  if (props.content?.multimodal) available.push({ label: 'Multimodal', slot: 'multimodal', icon: 'i-fa7-solid-photo-film' })
  if (props.content?.markdown) available.push({ label: 'Markdown', slot: 'markdown', icon: 'i-fa7-solid-align-left' })
  if (props.content?.html) available.push({ label: 'HTML', slot: 'html', icon: 'i-fa7-solid-code' })
  return available
})

const errorList = computed(() => Object.entries(props.errors || {}))

/**
 * What actually came back, in numbers.
 *
 * "Retrieved" on its own does not say whether a page yielded three paragraphs or a
 * whole article with nine images, and that difference is usually the reason somebody
 * opened the playground. The media manifest already carries a size per item, so this
 * costs nothing to compute.
 */
const stats = computed(() => {
  const items: any[] = props.content?.items || []

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
  const text = props.content?.markdown || props.content?.multimodal || ''

  const ICONS: Record<string, string> = {
    image: 'i-fa7-solid-image',
    video: 'i-fa7-solid-film',
    audio: 'i-fa7-solid-volume-high',
  }

  return {
    media: items.length,
    bytes,
    unsized,
    characters: text.length,
    words: text ? text.trim().split(/\s+/).length : 0,
    kinds: Object.entries(byKind).map(([kind, count]) => ({
      kind, count, icon: ICONS[kind] || 'i-fa7-solid-file',
    })),
    htmlBytes: props.content?.html ? new Blob([props.content.html]).size : 0,
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
  <UCard>
    <template #header>
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <a
            :href="url" target="_blank" rel="noopener noreferrer"
            class="font-medium hover:underline break-all inline-flex items-center gap-1.5
                   transition-colors hover:text-primary"
          >
            <UIcon name="i-fa7-solid-link" class="size-3 shrink-0 text-dimmed" />
            {{ url }}
          </a>
          <div class="flex flex-wrap items-center gap-2 mt-1.5">
            <UBadge
              :color="success ? 'success' : 'error'" variant="subtle"
              :icon="success ? 'i-fa7-solid-circle-check' : 'i-fa7-solid-circle-xmark'"
              :label="success ? 'Retrieved' : 'Failed'"
            />
            <UBadge v-if="method" color="neutral" variant="outline" :label="method" />
            <UBadge
              v-if="fromCache" color="info" variant="outline"
              icon="i-fa7-solid-bolt" label="from cache"
            />
            <span
              v-if="retrievalTime != null"
              class="text-xs text-dimmed inline-flex items-center gap-1"
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
              :title="`${stats.characters.toLocaleString()} characters, ${stats.words.toLocaleString()} words`"
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
      </div>
    </template>

    <div v-if="errorList.length" class="space-y-2 mb-4">
      <UAlert
        v-for="[method_, error] in errorList" :key="method_"
        color="error" variant="subtle" icon="i-fa7-solid-triangle-exclamation"
        :title="`${method_}: ${error.type}`" :description="error.message"
      />
    </div>

    <p v-if="content?.truncated?.length" class="text-xs text-dimmed mb-3">
      Stored content was truncated ({{ content.truncated.join(', ') }}) to keep the job
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
              class="max-w-full rounded border border-default" :alt="part.item.ref"
              loading="lazy"
            >
            <video
              v-else-if="part.item.kind === 'video'" :src="mediaUrl(part.item)" controls
              class="max-w-full rounded border border-default"
            />
            <audio v-else :src="mediaUrl(part.item)" controls class="w-full" />
          </template>
        </div>
      </template>
      <template #markdown>
        <div class="pt-3 max-w-prose">
          <MarkdownView :source="content.markdown" />
        </div>
      </template>
      <template #html>
        <pre class="whitespace-pre-wrap text-xs pt-3 overflow-x-auto">{{ content.html }}</pre>
      </template>
    </UTabs>

    <p v-else-if="!errorList.length" class="text-sm text-muted">No content.</p>
  </UCard>
</template>
