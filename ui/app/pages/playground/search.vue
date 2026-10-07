<script setup lang="ts">
/**
 * Web search through the server's search providers.
 *
 * Every provider has its own parameters and its own results, so this page is only the
 * frame: it lists the providers and hands the selected one to that provider's own
 * component, which knows its form and how to draw its answer.
 */
import type { Component } from 'vue'
import SerperSearch from '~/components/search/SerperSearch.vue'

const api = useApi()
const isAdmin = useIsAdmin()

// One component per provider, keyed by the provider's name in the API
const PROVIDERS: Record<string, Component> = {
  serper: SerperSearch,
}

const providers = ref<any[]>([])
const selected = ref('')
const loading = ref(true)
const error = ref('')

const current = computed(() => providers.value.find((p) => p.name === selected.value))
const options = computed(() =>
  providers.value.map((p) => ({ label: p.label, value: p.name })))

async function load() {
  loading.value = true
  try {
    const data = await api.get<any>('/v1/search')
    providers.value = data.providers
    if (!current.value) selected.value = providers.value[0]?.name ?? ''
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 class="text-2xl font-semibold">Search</h1>
        <p class="text-sm text-muted">
          Find pages and images through a search API, then retrieve what you found.
        </p>
      </div>
      <UFormField v-if="providers.length" label="Provider">
        <USelect v-model="selected" :items="options" class="w-44" />
      </UFormField>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <USkeleton v-if="loading" class="h-32 w-full rounded-xl" />

    <template v-else-if="current">
      <UAlert
        v-if="current.enabled === false" color="neutral" variant="subtle" icon="i-fa7-solid-ban"
        :title="`${current.label} is disabled`"
        description="It was switched off on the dashboard. Searches are refused until it is enabled again."
        :actions="[{ label: 'Open Dashboard', to: '/', color: 'neutral', variant: 'outline' }]"
      />
      <UAlert
        v-else-if="!current.configured" color="warning" variant="subtle" icon="i-fa7-solid-key"
        :title="`${current.label} is not set up yet`"
        :description="isAdmin
          ? `Set ${current.missing_secrets.join(', ')} under Secrets in the Settings to search with ${current.label}.`
          : `An admin of this server has to set ${current.missing_secrets.join(', ')} before you can search with ${current.label}.`"
        :actions="isAdmin ? [{ label: 'Open Secrets', to: '/settings#secrets', color: 'warning', variant: 'outline' }] : []"
      />
      <component :is="PROVIDERS[current.name]" v-if="PROVIDERS[current.name]" :provider="current" />
      <UCard v-else>
        <p class="text-sm text-muted">
          This UI cannot draw {{ current.label }}'s results yet. The provider is available
          through the API, at <code>POST /v1/search/{{ current.name }}</code>.
        </p>
      </UCard>
    </template>

    <UCard v-else-if="!error">
      <p class="text-sm text-muted">This server offers no search providers.</p>
    </UCard>
  </div>
</template>
