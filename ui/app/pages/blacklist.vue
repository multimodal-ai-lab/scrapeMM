<script setup lang="ts">
/**
 * Domains scrapeMM will not retrieve.
 *
 * Two kinds end up here and they behave differently, which the page makes visible:
 * entries added automatically after a CAPTCHA expire, because those gates are often
 * transient and excluding a domain forever would quietly erode coverage; entries added
 * by hand are permanent, because they express a decision rather than an observation.
 */
const api = useApi()

const domains = ref<Record<string, string>>({})
const ttl = ref<number | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const busy = ref<string | null>(null)

const newDomain = ref('')
const newReason = ref('')
const filter = ref('')

const entries = computed(() => {
  const term = filter.value.trim().toLowerCase()
  return Object.entries(domains.value)
    .filter(([domain, reason]) => !term
      || domain.toLowerCase().includes(term)
      || reason.toLowerCase().includes(term))
    .sort(([a], [b]) => a.localeCompare(b))
})

async function load() {
  loading.value = true
  try {
    const [list, env] = await Promise.all([
      api.get<any>('/v1/blacklist'),
      api.get<any>('/v1/environment'),
    ])
    domains.value = list.domains
    ttl.value = env.blacklist?.ttl ?? null
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function add() {
  const domain = newDomain.value.trim()
  if (!domain) return
  busy.value = domain
  error.value = ''
  try {
    await api.post(`/v1/blacklist/${encodeURIComponent(domain)}`,
                   { reason: newReason.value.trim() || 'Blacklisted manually.' })
    notice.value = `${domain} will no longer be retrieved.`
    newDomain.value = ''
    newReason.value = ''
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

async function remove(domain: string) {
  busy.value = domain
  error.value = ''
  try {
    await api.del(`/v1/blacklist/${encodeURIComponent(domain)}`)
    notice.value = `${domain} can be retrieved again.`
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

function lifetime(seconds: number | null) {
  if (seconds == null) return ''
  if (seconds <= 0) return 'Automatic entries never expire.'
  const days = seconds / 86400
  if (days >= 1) return `Automatic entries expire after ${Math.round(days)} day(s).`
  return `Automatic entries expire after ${Math.round(seconds / 3600)} hour(s).`
}

onMounted(load)
</script>

<template>
  <div class="space-y-5 max-w-3xl">
    <div>
      <h1 class="text-2xl font-semibold">Blacklist</h1>
      <p class="text-sm text-muted mt-1">
        Domains scrapeMM refuses to retrieve. A domain lands here automatically when
        every method hit a CAPTCHA on it, or by hand.
        <span v-if="ttl != null">{{ lifetime(ttl) }} Entries you add yourself are permanent.</span>
      </p>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UAlert
      v-if="notice" color="success" variant="subtle" :description="notice"
      close @update:open="notice = ''"
    />

    <UCard>
      <template #header><h2 class="font-medium text-sm">Exclude a domain</h2></template>
      <form class="flex flex-wrap gap-2" @submit.prevent="add">
        <UInput
          v-model="newDomain" placeholder="example.com" class="flex-1 min-w-48"
          icon="i-fa7-solid-globe"
        />
        <UInput v-model="newReason" placeholder="Reason (optional)" class="flex-1 min-w-48" />
        <UButton
          type="submit" icon="i-fa7-solid-ban" label="Exclude"
          :loading="busy === newDomain.trim()" :disabled="!newDomain.trim()"
        />
      </form>
    </UCard>

    <div class="flex items-center justify-between gap-3">
      <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">
        Excluded domains
      </h2>
      <UInput
        v-if="Object.keys(domains).length" v-model="filter" placeholder="Filter…"
        icon="i-fa7-solid-magnifying-glass" size="sm" class="w-56"
      />
    </div>

    <div v-if="loading" class="space-y-2">
      <div v-for="n in 3" :key="n" class="surface-card rounded-xl p-3 space-y-2">
        <USkeleton class="h-4 w-48" />
        <USkeleton class="h-3 w-2/3" />
      </div>
    </div>

    <UCard v-else-if="!Object.keys(domains).length">
      <p class="text-sm text-muted">
        Nothing excluded. Domains appear here on their own when every retrieval method
        hits a CAPTCHA.
      </p>
    </UCard>

    <p v-else-if="!entries.length" class="text-sm text-muted">
      No domain matches “{{ filter }}”.
    </p>

    <ul v-else class="space-y-2">
      <li
        v-for="[domain, reason] in entries" :key="domain"
        class="group surface-card rounded-xl p-3 flex items-start justify-between gap-3"
      >
        <div class="min-w-0">
          <p class="font-mono text-sm">{{ domain }}</p>
          <p class="text-xs text-dimmed break-words mt-0.5">{{ reason }}</p>
        </div>
        <!-- Shown on hover, like every other row action in the app. -->
        <UButton
          size="xs" color="neutral" variant="link" icon="i-fa7-solid-trash"
          :loading="busy === domain" aria-label="Allow this domain again"
          title="Allow this domain again"
          class="reveal-on-hover shrink-0 opacity-0 text-dimmed
                 transition-[opacity,color] duration-150 hover:text-error
                 group-hover:opacity-100 focus-visible:opacity-100"
          @click="remove(domain)"
        />
      </li>
    </ul>
  </div>
</template>
