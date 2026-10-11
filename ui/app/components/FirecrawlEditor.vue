<script setup lang="ts">
/**
 * The Firecrawl instances scrapeMM spreads its scrapes across: a list, so one URL per
 * line rather than a field the schema could render. Saved with the page's save button.
 */
const props = defineProps<{ panel: string }>()

const api = useApi()
const server = useServerConfig()

const saved = computed(() => ((server.value?.config?.firecrawl_urls as string[] | null) || []).join('\n'))
const text = ref<string | null>(null) // Null: not edited, shows what the server holds

const urls = (value: string) => value.split('\n').map((u) => u.trim()).filter(Boolean)

const dirty = computed(() => text.value !== null
  && JSON.stringify(urls(text.value)) !== JSON.stringify(urls(saved.value)))

async function save() {
  const list = urls(text.value ?? '')
  const bad = list.find((u) => !/^https?:\/\/\S+$/.test(u))
  if (bad) throw new Error(`"${bad}" is not an http(s) URL.`)
  adoptServerConfig(server, await api.patch<any>('/v1/config', { firecrawl_urls: list }))
  text.value = null
}

useSettingsSection({ id: 'firecrawl', panel: props.panel, title: 'Firecrawl instances', dirty, save,
  discard: () => { text.value = null } })
</script>

<template>
  <UCard>
    <UFormField
      label="Firecrawl instances" hint="One URL per line"
      description="scrapeMM spreads its scrapes across every instance that responds."
    >
      <UTextarea
        :model-value="text ?? saved" :rows="3" class="w-full font-mono" placeholder="http://firecrawl:3002"
        @update:model-value="text = String($event ?? '')"
      />
    </UFormField>
  </UCard>
</template>
