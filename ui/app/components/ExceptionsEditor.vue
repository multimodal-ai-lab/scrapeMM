<script setup lang="ts">
/**
 * Exceptions to the retrieval chain: domains whose live methods are fixed. A pattern is a
 * domain (the domain and its subdomains) or "*." and a domain (its subdomains only); the
 * most specific one matching a URL wins. Its methods replace the chain's live methods for
 * that domain, and the chain's archive methods apply as configured.
 *
 * Edited as a draft, saved with the page's save button.
 */
interface Rule { id?: number, pattern: string, methods: string[] }

let seq = 0

const api = useApi()
const draftForPreview = useExceptionsDraft()

const server = ref<any>(null)
const draft = ref<Rule[]>([])
const loading = ref(true)
const error = ref('')
const methodInfo = ref<Record<string, any>>({})

const PATTERN = /^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$/

const ICONS: Record<string, string> = {
  integrations: 'i-fa7-solid-puzzle-piece',
  browser: 'i-fa7-solid-window-maximize',
  firecrawl: 'i-fa7-solid-fire',
  decodo: 'i-fa7-solid-tower-broadcast',
  plain_http: 'i-fa7-solid-code',
}

const strip = (rules: Rule[]) => rules.map((r) => ({ pattern: r.pattern.trim().toLowerCase(), methods: [...r.methods] }))

const dirty = computed(() => !!server.value
  && JSON.stringify(strip(draft.value)) !== JSON.stringify(strip(server.value.exceptions)))

const defaults = computed<Record<string, string[]>>(() =>
  Object.fromEntries((server.value?.defaults || []).map((r: Rule) => [r.pattern, r.methods])))

function origin(rule: Rule): 'default' | 'modified' | 'custom' {
  const known = defaults.value[rule.pattern.trim().toLowerCase()]
  if (!known) return 'custom'
  return JSON.stringify(known) === JSON.stringify(rule.methods) ? 'default' : 'modified'
}

const removedDefaults = computed(() => {
  const present = new Set(draft.value.map((r) => r.pattern.trim().toLowerCase()))
  return Object.keys(defaults.value).filter((p) => !present.has(p))
})

function problem(rule: Rule, index: number): string {
  const pattern = rule.pattern.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/^www\./, '')
  if (!pattern) return 'Enter a domain, like example.com, or *.example.com for its subdomains.'
  if (!PATTERN.test(pattern)) return `"${rule.pattern}" is not a domain (like example.com) or *.domain.`
  if (draft.value.some((r, i) => i !== index && r.pattern.trim().toLowerCase() === pattern)) return 'This domain has another exception already.'
  if (!rule.methods.length) return 'Add at least one method.'
  return ''
}

const invalid = computed(() => draft.value.map((r, i) => problem(r, i)).filter(Boolean))

watch(draft, (value) => {
  // The preview follows the draft, but only once it is valid: a half-typed domain would
  // make the server reject the whole preview
  if (!invalid.value.length) draftForPreview.value = strip(value)
}, { deep: true })

function adopt(data: any) {
  server.value = data
  draft.value = data.exceptions.map((r: Rule) => ({ id: ++seq, pattern: r.pattern, methods: [...r.methods] }))
}

async function load() {
  loading.value = true
  try {
    const [exceptions, chain] = await Promise.all([
      api.get<any>('/v1/chain/exceptions'), api.get<any>('/v1/chain')])
    methodInfo.value = Object.fromEntries(chain.methods.map((m: any) => [m.key, m]))
    adopt(exceptions)
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function save() {
  if (invalid.value.length) throw new Error(invalid.value[0]!)
  adopt(await api.put<any>('/v1/chain/exceptions', { exceptions: strip(draft.value) }))
}

function discard() {
  if (server.value) adopt(server.value)
}

useSettingsSection({ id: 'exceptions', title: 'Exceptions', dirty, save, discard })
onMounted(load)

// --- Editing ------------------------------------------------------------------------

const filter = ref('')
const showAll = ref(false)
const COLLAPSED = 6 // Rows shown before "Show all"; new and invalid rows always show
const matching = computed(() => draft.value
  .map((rule, index) => ({ rule, index }))
  .filter(({ rule }) => !filter.value.trim() || rule.pattern.includes(filter.value.trim().toLowerCase())
    || rule.methods.some((m) => name(m).toLowerCase().includes(filter.value.trim().toLowerCase()))))
const shown = computed(() => showAll.value || filter.value.trim()
  ? matching.value
  : matching.value.filter(({ rule, index }, i) => i < COLLAPSED || !rule.pattern || problem(rule, index)))

const name = (key: string) => methodInfo.value[key]?.name || key

function add() {
  draft.value = [{ id: ++seq, pattern: '', methods: ['browser'] }, ...draft.value]
  filter.value = ''
  nextTick(() => document.getElementById('exception-pattern-0')?.focus())
}

function remove(index: number) {
  draft.value = draft.value.filter((_, i) => i !== index)
}

function restore(pattern: string) {
  draft.value = [...draft.value, { id: ++seq, pattern, methods: [...defaults.value[pattern]!] }]
}

function resetToDefaults() {
  draft.value = (server.value?.defaults || []).map((r: Rule) => ({ id: ++seq, pattern: r.pattern, methods: [...r.methods] }))
}

function addable(rule: Rule) {
  return (server.value?.live_methods || []).filter((m: string) => !rule.methods.includes(m))
    .map((m: string) => ({ label: name(m), icon: ICONS[m], onSelect: () => { rule.methods.push(m) } }))
}

function removeMethod(rule: Rule, m: string) {
  rule.methods = rule.methods.filter((x) => x !== m)
}

function shift(rule: Rule, i: number, delta: number) {
  const j = i + delta
  if (j < 0 || j >= rule.methods.length) return
  const list = [...rule.methods]
  ;[list[i], list[j]] = [list[j]!, list[i]!]
  rule.methods = list
}

const ORIGIN = {
  default: { label: 'Default', color: 'neutral' },
  modified: { label: 'Changed default', color: 'warning' },
  custom: { label: 'Custom', color: 'primary' },
} as const
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex flex-col sm:flex-row sm:items-start gap-x-3 gap-y-2">
        <div class="min-w-0 flex-1">
          <h2 class="font-medium flex items-center gap-2">
            <UIcon name="i-fa7-solid-route" class="size-4 text-primary" />
            Exceptions
            <UBadge v-if="dirty" color="warning" variant="subtle" size="sm" label="Unsaved" />
          </h2>
          <p class="text-sm text-muted mt-0.5">
            Domains that use fixed live methods instead of the chain above: social platforms
            only their own integration, some sites a particular scraper. The chain's archive
            methods still apply. <code class="font-mono">example.com</code> covers its
            subdomains too, <code class="font-mono">*.example.com</code> only them.
          </p>
        </div>
        <div class="flex gap-1 self-start">
          <UButton size="xs" icon="i-fa7-solid-plus" label="Add exception" :disabled="loading" @click="add" />
          <UButton
            size="xs" variant="ghost" color="neutral" icon="i-fa7-solid-arrow-rotate-left"
            label="Reset to defaults" :disabled="loading" @click="resetToDefaults"
          />
        </div>
      </div>
    </template>

    <div v-if="loading && !server" class="py-6 text-center text-sm text-muted">
      <UIcon name="i-fa7-solid-spinner" class="size-4 animate-spin" /> Loading the exceptions…
    </div>

    <div v-else-if="server" class="space-y-3">
      <UAlert v-if="error" color="error" variant="subtle" :description="error" />
      <UInput
        v-model="filter" icon="i-fa7-solid-magnifying-glass" size="sm" class="w-full sm:w-72"
        :placeholder="`Filter ${draft.length} exceptions`" aria-label="Filter the exceptions"
      />

      <ul class="grid gap-2">
        <li
          v-for="{ rule, index } in shown" :key="rule.id"
          class="surface-card rounded-xl p-3 flex flex-col sm:flex-row sm:items-start gap-2 sm:gap-3"
          :class="problem(rule, index) ? 'ring ring-error/50' : ''"
        >
          <div class="sm:w-56 shrink-0 space-y-1">
            <UInput
              :id="`exception-pattern-${index}`" v-model="rule.pattern" size="sm" class="w-full font-mono"
              placeholder="example.com" :aria-label="`Domain of exception ${index + 1}`"
              :color="problem(rule, index) && rule.pattern ? 'error' : undefined"
            />
            <UBadge
              :color="ORIGIN[origin(rule)].color" variant="subtle" size="sm"
              :label="ORIGIN[origin(rule)].label"
            />
          </div>

          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-1.5">
              <template v-for="(m, i) in rule.methods" :key="m">
                <UIcon v-if="i" name="i-fa7-solid-chevron-right" class="size-3 text-dimmed" />
                <span class="method-chip">
                  <UButton
                    v-if="rule.methods.length > 1 && i" size="xs" variant="link" color="neutral" square
                    icon="i-fa7-solid-chevron-left" :aria-label="`Try ${name(m)} earlier`" @click="shift(rule, i, -1)"
                  />
                  <UIcon :name="ICONS[m] || 'i-fa7-solid-globe'" class="size-3.5" />
                  {{ name(m) }}
                  <UButton
                    size="xs" variant="link" color="neutral" square icon="i-fa7-solid-xmark"
                    :aria-label="`Remove ${name(m)}`" @click="removeMethod(rule, m)"
                  />
                </span>
              </template>
              <UDropdownMenu v-if="addable(rule).length" :items="addable(rule)">
                <UButton size="xs" variant="soft" color="neutral" icon="i-fa7-solid-plus" label="Method" />
              </UDropdownMenu>
            </div>
            <p v-if="problem(rule, index)" class="text-xs text-error mt-1.5">{{ problem(rule, index) }}</p>
          </div>

          <UButton
            size="xs" variant="ghost" color="error" icon="i-fa7-solid-trash" class="self-end sm:self-start"
            :aria-label="`Delete the exception for ${rule.pattern || 'a new domain'}`" @click="remove(index)"
          />
        </li>
      </ul>
      <p v-if="!shown.length" class="text-sm text-muted">No exception matches.</p>
      <UButton
        v-if="!filter.trim() && matching.length > shown.length || showAll" size="xs" variant="soft" color="neutral"
        :icon="showAll ? 'i-fa7-solid-chevron-up' : 'i-fa7-solid-chevron-down'"
        :label="showAll ? 'Show fewer' : `Show all ${matching.length}`" @click="showAll = !showAll"
      />

      <p v-if="removedDefaults.length" class="text-xs text-muted flex flex-wrap items-center gap-1.5">
        Removed defaults:
        <UButton
          v-for="p in removedDefaults" :key="p" size="xs" variant="soft" color="neutral"
          icon="i-fa7-solid-arrow-rotate-left" :label="p" @click="restore(p)"
        />
      </p>
    </div>
  </UCard>
</template>

<style scoped>
.method-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.8125rem;
  padding: 0.1rem 0.15rem 0.1rem 0.55rem;
  border-radius: 0.5rem;
  background-color: var(--surface-sunken, rgb(127 127 127 / 0.12));
}
</style>
