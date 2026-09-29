<script setup lang="ts">
/**
 * What the chain does with a URL typed in: which integration or exception takes over, which
 * methods then run in order, and why the others are skipped. Resolves the drafts of the
 * chain and the exceptions, so unsaved changes show at once.
 */
const api = useApi()
const chainDraft = useChainDraft()
const exceptionsDraft = useExceptionsDraft()

const url = ref('')
const preview = ref<any>(null)
const error = ref('')
let timer: ReturnType<typeof setTimeout> | undefined

async function run() {
  const value = url.value.trim()
  if (!value) { preview.value = null; error.value = ''; return }
  try {
    preview.value = await api.post<any>('/v1/chain/preview', {
      url: value, chain: chainDraft.value, exceptions: exceptionsDraft.value,
    })
    error.value = ''
  } catch (e: any) {
    error.value = e.message
  }
}

watch([url, chainDraft, exceptionsDraft], () => {
  clearTimeout(timer)
  timer = setTimeout(run, 350)
}, { deep: true })

const EXAMPLES = [
  'https://www.politifact.com/factchecks/2016/apr/19/doug-ducey/are-90-percent-fires-arizona-caused-humans/',
  'https://x.com/PopBase/status/1938496291908030484',
  'https://www.washingtonpost.com/politics/2018/09/12/anatomy/',
  'https://web.archive.org/web/2023/https://example.com/',
]

const ICONS: Record<string, string> = {
  integrations: 'i-fa7-solid-puzzle-piece',
  browser: 'i-fa7-solid-window-maximize',
  firecrawl: 'i-fa7-solid-fire',
  decodo: 'i-fa7-solid-tower-broadcast',
  plain_http: 'i-fa7-solid-code',
  wayback: 'i-fa7-solid-building-columns',
  perma_cc: 'i-fa7-solid-link',
}
const icon = (key: string) => ICONS[key] || integrationIcon(key)
</script>

<template>
  <UCard>
    <template #header>
      <h2 class="font-medium flex items-center gap-2">
        <UIcon name="i-fa7-solid-eye" class="size-4 text-primary" />
        Preview
      </h2>
      <p class="text-sm text-muted mt-0.5">What the chain and the exceptions do with a URL, unsaved changes included.</p>
    </template>

    <div class="flex gap-2">
      <UInput
        v-model="url" icon="i-fa7-solid-link" placeholder="Paste a URL"
        class="flex-1 min-w-0" aria-label="URL to preview"
      />
      <UButton v-if="url" variant="ghost" color="neutral" icon="i-fa7-solid-xmark" aria-label="Clear the URL" @click="url = ''" />
    </div>
    <div v-if="!url" class="flex flex-wrap gap-1.5 mt-2">
      <button
        v-for="example in EXAMPLES" :key="example" type="button"
        class="text-xs rounded-full px-2 py-0.5 bg-(--surface-sunken) text-muted hover:text-default"
        @click="url = example"
      >
        {{ example.replace(/^https?:\/\/(www\.)?/, '').slice(0, 38) }}…
      </button>
    </div>
    <p v-if="error" class="text-sm text-error mt-2">{{ error }}</p>

    <div v-if="preview && url" class="mt-3 space-y-3">
      <p class="text-sm">
        <span class="text-muted">Domain</span> <code class="font-mono">{{ preview.domain }}</code>
        <template v-if="preview.integrations.length">
          · handled by <strong>{{ preview.integrations.join(', ') }}</strong>
        </template>
        <template v-if="preview.exception">
          · exception <code class="font-mono">{{ preview.exception.pattern }}</code>
        </template>
      </p>
      <ol v-if="preview.steps.length" class="flex flex-wrap items-center gap-1.5">
        <template v-for="(step, i) in preview.steps" :key="step.method">
          <UIcon v-if="i" name="i-fa7-solid-chevron-right" class="size-3 text-dimmed" />
          <li
            class="flex items-center gap-1.5 rounded-lg px-2 py-1 text-sm"
            :class="step.stage === 'archive' ? 'bg-info/10 text-info' : 'bg-primary/10 text-primary'"
          >
            <span class="text-xs opacity-70 tabular-nums">{{ i + 1 }}</span>
            <UIcon :name="icon(step.method)" class="size-3.5" />
            {{ step.name }}
            <UIcon v-if="step.stage === 'archive'" name="i-fa7-solid-box-archive" class="size-3 opacity-70" />
          </li>
        </template>
      </ol>
      <UAlert
        v-else color="warning" variant="subtle" icon="i-fa7-solid-triangle-exclamation"
        description="No method would run for this URL: every applicable one is switched off."
      />
      <details v-if="preview.skipped.length" class="text-xs text-muted">
        <summary class="cursor-pointer">Skipped ({{ preview.skipped.length }})</summary>
        <ul class="mt-1.5 space-y-0.5 pl-4 list-disc">
          <li v-for="s in preview.skipped" :key="s.method"><strong>{{ s.name }}</strong>: {{ s.reason }}</li>
        </ul>
      </details>
    </div>
  </UCard>
</template>
