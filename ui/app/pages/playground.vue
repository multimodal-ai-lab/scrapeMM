<script setup lang="ts">
/** Try a URL and watch the result arrive. Uses the very same streaming endpoint the
 *  Python client uses, so what you see here is what a script would get. */
const api = useApi()

const input = ref('')
const outputFormat = ref('multimodal')
const useCache = ref(true)
const prioritize = ref('completeness')
const running = ref(false)
const error = ref('')
const results = ref<any[]>([])
const progress = ref({ done: 0, total: 0 })
const summary = ref<any>(null)

const formats = [
  { label: 'Multimodal (text + media)', value: 'multimodal' },
  { label: 'Markdown (text only)', value: 'markdown' },
  { label: 'HTML (raw page)', value: 'html' },
]
const priorities = [
  { label: 'Completeness', value: 'completeness' },
  { label: 'Speed', value: 'speed' },
]

const urls = computed(() =>
  input.value.split('\n').map((line) => line.trim()).filter(Boolean))

async function run() {
  if (!urls.value.length) return
  running.value = true
  error.value = ''
  results.value = []
  summary.value = null
  progress.value = { done: 0, total: urls.value.length }

  try {
    await api.stream('/v1/retrieve', {
      urls: urls.value,
      output_format: outputFormat.value,
      use_cache: useCache.value,
      prioritize: prioritize.value,
    }, (message) => {
      if (message.type === 'header') progress.value.total = message.total
      else if (message.type === 'result') {
        results.value.push(message.payload)
        progress.value.done += 1
      } else if (message.type === 'summary') summary.value = message
      else if (message.type === 'error') error.value = message.message
    })
  } catch (e: any) {
    error.value = e.message
  } finally {
    running.value = false
  }
}

function succeeded(payload: any) {
  const content = payload.content
  if (!content) return false
  return content[payload.output_format] != null
}
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">Playground</h1>
      <p class="text-sm text-muted">One URL per line. Results stream in as they finish.</p>
    </div>

    <UCard>
      <div class="space-y-4">
        <UTextarea
          v-model="input" :rows="4" class="w-full"
          placeholder="https://example.com/article&#10;https://x.com/user/status/123"
        />
        <div class="flex flex-wrap items-end gap-4">
          <UFormField label="Output format">
            <USelect v-model="outputFormat" :items="formats" class="w-56" />
          </UFormField>
          <UFormField label="Prioritize">
            <USelect v-model="prioritize" :items="priorities" class="w-40" />
          </UFormField>
          <UCheckbox v-model="useCache" label="Use cache" class="mb-2" />
          <UButton
            class="ml-auto" size="lg" icon="i-fa7-solid-play" :loading="running"
            :disabled="!urls.length" label="Retrieve" @click="run"
          />
        </div>
      </div>
    </UCard>

    <div v-if="running || progress.total" class="space-y-2">
      <UProgress
        :model-value="progress.done" :max="progress.total || 1"
      />
      <p class="text-sm text-muted">
        {{ progress.done }} / {{ progress.total }} done
        <span v-if="summary">
          — {{ summary.succeeded }} succeeded, {{ summary.failed }} failed in
          {{ summary.duration.toFixed(1) }}s
        </span>
      </p>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <div class="space-y-4">
      <ResultView
        v-for="payload in results" :key="payload.url"
        :url="payload.url" :content="payload.content" :method="payload.method"
        :errors="payload.errors" :retrieval-time="payload.retrieval_time"
        :from-cache="payload.from_cache" :success="succeeded(payload)"
      />
    </div>
  </div>
</template>
