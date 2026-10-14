<script setup lang="ts">
/**
 * How hard scrapeMM leans on each retrieval method: its slots (URLs it retrieves at
 * once) and its cooldown (the least time between two of its retrievals starting). One
 * row per method, defaults as placeholders, so that only what differs from them is
 * typed -- and saved: `method_slots` and `method_cooldowns` hold the overrides alone.
 *
 * A PATCH replaces a dict as a whole, so saving sends each changed dict complete.
 */
import type { MethodLimits } from '~/composables/useSettingsSections'

const api = useApi()
const server = useServerConfig()

type Overrides = Record<string, string>
const slots = ref<Overrides>({})
const cooldowns = ref<Overrides>({})

const methods = computed(() => server.value?.methods || [])

/** The overrides the server holds (or held, in `config`), as the inputs edit them */
function savedOf(key: 'method_slots' | 'method_cooldowns', config = server.value?.config): Overrides {
  const held = config?.[key] || {}
  return Object.fromEntries(Object.entries(held)
    .filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => [k, String(v)]))
}

/** A field's text, trimmed: a number input may hand over a number */
const text = (o: Overrides, key: string) => String(o[key] ?? '').trim()

/** The draft without its empty fields: those are at their defaults */
const compact = (o: Overrides) => Object.fromEntries(Object.keys(o)
  .map((k) => [k, text(o, k)]).filter(([, v]) => v))

const same = (a: Overrides, b: Overrides) => {
  const x = compact(a), y = compact(b)
  return Object.keys(x).length === Object.keys(y).length
    && Object.entries(x).every(([k, v]) => Number(v) === Number(y[k]))
}

const slotsDirty = computed(() => !same(slots.value, savedOf('method_slots')))
const cooldownsDirty = computed(() => !same(cooldowns.value, savedOf('method_cooldowns')))
const dirty = computed(() => slotsDirty.value || cooldownsDirty.value)

function adopt() {
  slots.value = savedOf('method_slots')
  cooldowns.value = savedOf('method_cooldowns')
}

// Taken over once the configuration is there, and after any save that changed it --
// unless the table holds edits of its own, which a save elsewhere must not wipe out
watch(() => server.value?.config, (_, before) => {
  // `?? {}`: undefined would stand for the current config
  if (same(slots.value, savedOf('method_slots', before ?? {}))
    && same(cooldowns.value, savedOf('method_cooldowns', before ?? {}))) adopt()
}, { immediate: true })

function slotProblem(m: MethodLimits) {
  const v = text(slots.value, m.key)
  if (!v) return ''
  return Number.isInteger(Number(v)) && Number(v) >= 1 ? '' : `${m.name}: slots must be a whole number, 1 or more.`
}

function cooldownProblem(m: MethodLimits) {
  const v = text(cooldowns.value, m.key)
  if (!v) return ''
  return !Number.isNaN(Number(v)) && Number(v) >= 0 ? '' : `${m.name}: the cooldown must be 0 or more seconds.`
}

const problems = computed(() => methods.value.flatMap((m) => [slotProblem(m), cooldownProblem(m)]).filter(Boolean))

const numbers = (o: Overrides) => Object.fromEntries(Object.entries(compact(o)).map(([k, v]) => [k, Number(v)]))

async function save() {
  if (problems.value.length) throw new Error(problems.value[0]!)
  const body: Record<string, any> = {}
  if (slotsDirty.value) body.method_slots = numbers(slots.value)
  if (cooldownsDirty.value) body.method_cooldowns = numbers(cooldowns.value)
  adoptServerConfig(server, await api.patch<any>('/v1/config', body))
  adopt()
}

useSettingsSection({ id: 'parallelism', title: 'Parallelism', dirty, save, discard: adopt })

const overridden = (m: MethodLimits) => !!(text(slots.value, m.key) || text(cooldowns.value, m.key))

function reset(m: MethodLimits) {
  const { [m.key]: _s, ...s } = slots.value
  const { [m.key]: _c, ...c } = cooldowns.value
  slots.value = s
  cooldowns.value = c
}

const KIND = { integration: 'Integration', method: 'Method', archive: 'Archive' } as const
</script>

<template>
  <UCard v-if="methods.length" :ui="{ body: 'p-0 sm:p-0' }">
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-left text-xs font-semibold uppercase tracking-wider text-dimmed">
            <th class="font-semibold pl-5.5 pr-1 sm:pl-7.5 py-2.5">Method</th>
            <th class="font-semibold px-2 py-2.5 w-20 sm:w-28">Slots</th>
            <th class="font-semibold px-2 py-2.5 w-24 sm:w-32">Cooldown</th>
            <th class="w-9" />
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in methods" :key="m.key" class="border-t border-default">
            <td class="pl-2 pr-1 sm:px-4 py-1.5">
              <div class="flex items-center gap-2">
                <span
                  class="size-1.5 rounded-full shrink-0" :class="overridden(m) ? 'bg-primary' : 'bg-transparent'"
                  :title="overridden(m) ? 'Set here, not the default' : undefined"
                />
                <span class="flex flex-wrap items-baseline gap-x-2">
                  <span :class="overridden(m) ? 'font-medium text-highlighted' : ''">{{ m.name }}</span>
                  <span class="text-xs text-dimmed">{{ KIND[m.kind] || m.kind }}</span>
                </span>
              </div>
            </td>
            <td class="px-2 py-1.5">
              <UInput
                v-model="slots[m.key]" type="number" min="1" step="1" size="sm" class="w-full"
                :placeholder="String(m.slots_default)" :color="slotProblem(m) ? 'error' : undefined"
                :aria-label="`${m.name}: slots`" :title="slotProblem(m) || undefined"
              />
              <p v-if="m.extra_slots" class="text-xs text-dimmed mt-0.5" :title="`One per usable proxy, unpaced: each is an address of its own`">
                + {{ m.extra_slots }} via proxies
              </p>
            </td>
            <td class="px-2 py-1.5">
              <UInput
                v-model="cooldowns[m.key]" type="number" min="0" step="any" size="sm" class="w-full"
                :placeholder="m.cooldown_default ? String(m.cooldown_default) : 'None'"
                :color="cooldownProblem(m) ? 'error' : undefined"
                :aria-label="`${m.name}: cooldown in seconds`" :title="cooldownProblem(m) || undefined"
              >
                <template #trailing><span class="text-xs text-dimmed">s</span></template>
              </UInput>
            </td>
            <td class="pr-2 py-1.5 text-right">
              <UButton
                v-if="overridden(m)" size="xs" variant="ghost" color="neutral"
                icon="i-fa7-solid-arrow-rotate-left" class="text-dimmed hover:text-default"
                :title="`Back to the defaults (${m.slots_default} slots, ${m.cooldown_default ? `${m.cooldown_default} s` : 'no'} cooldown)`"
                :aria-label="`Reset ${m.name} to its defaults`" @click="reset(m)"
              />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="problems.length" class="text-xs text-error px-4 pb-3">{{ problems[0] }}</p>
  </UCard>
</template>
