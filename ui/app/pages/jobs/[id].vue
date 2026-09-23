<script setup lang="ts">
/** One job: its parameters and every URL it touched, rendered as the playground would. */
const api = useApi()
const route = useRoute()
const router = useRouter()

const job = ref<any>(null)
const error = ref('')
const loading = ref(true)
const copied = ref(false)

async function load() {
  loading.value = true
  try {
    job.value = await api.get<any>(`/v1/jobs/${route.params.id}`)
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function remove() {
  try {
    await api.del(`/v1/jobs/${route.params.id}`)
    router.push('/jobs')
  } catch (e: any) {
    error.value = e.message
  }
}

function copyId() {
  navigator.clipboard?.writeText(String(route.params.id))
  copied.value = true
  setTimeout(() => { copied.value = false }, 1500)
}

const totalTime = computed(() => {
  if (!job.value?.results) return null
  const times = job.value.results.map((r: any) => r.retrieval_time).filter(Boolean)
  return times.length ? times.reduce((a: number, b: number) => a + b, 0) : null
})

onMounted(load)
</script>

<template>
  <div class="space-y-5">
    <div class="flex items-start justify-between gap-4">
      <div class="min-w-0">
        <UButton
          class="-ml-3 mb-1 transition-transform duration-150 hover:-translate-x-0.5"
          variant="link" icon="i-fa7-solid-arrow-left" to="/jobs" label="All jobs"
        />
        <h1 class="text-2xl font-semibold truncate">
          {{ job?.results?.[0]?.url || 'Job' }}
          <span v-if="job && job.results?.length > 1" class="text-muted font-normal">
            and {{ job.results.length - 1 }} more
          </span>
        </h1>
        <div
          v-if="job"
          class="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5 text-sm text-muted"
        >
          <span class="inline-flex items-center gap-1.5" :title="absoluteTime(job.created_at)">
            <UIcon name="i-fa7-solid-clock" class="size-3" />
            {{ timeAgo(job.created_at) }}
          </span>
          <span v-if="totalTime" class="inline-flex items-center gap-1.5">
            <UIcon name="i-fa7-solid-stopwatch" class="size-3" />
            {{ seconds(totalTime) }}
          </span>
          <span class="text-success">{{ job.succeeded }} succeeded</span>
          <span v-if="job.failed" class="text-error">{{ job.failed }} failed</span>
        </div>
      </div>
      <div class="flex items-center gap-2 shrink-0">
        <UButton
          v-if="job" color="neutral" variant="ghost" icon="i-fa7-solid-trash"
          label="Delete" class="transition-transform duration-150 hover:scale-105"
          @click="remove"
        />
      </div>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <div v-if="loading && !job" class="space-y-4">
      <USkeleton class="h-24 w-full" />
      <USkeleton class="h-48 w-full" />
    </div>

    <template v-else-if="job">
      <div class="space-y-4">
        <ResultView
          v-for="result in job.results" :key="result.url"
          :url="result.url" :content="result.content" :method="result.method"
          :errors="result.errors" :retrieval-time="result.retrieval_time"
          :from-cache="result.from_cache" :success="result.success"
        />
      </div>

      <!-- Parameters and the id live at the bottom: useful when debugging, noise when
           you are just looking at what came back. -->
      <UCard>
        <template #header>
          <div class="flex items-center justify-between gap-2">
            <h2 class="font-medium text-sm flex items-center gap-2">
              <UIcon name="i-fa7-solid-sliders" class="size-3.5 text-dimmed" />
              Request
            </h2>
            <button
              type="button"
              class="font-mono text-[10px] text-dimmed/70 hover:text-dimmed transition-colors"
              :title="`${route.params.id} — click to copy`" @click="copyId"
            >
              {{ copied ? 'copied' : route.params.id }}
            </button>
          </div>
        </template>
        <pre class="text-xs overflow-x-auto">{{ JSON.stringify(job.params, null, 2) }}</pre>
      </UCard>
    </template>
  </div>
</template>
