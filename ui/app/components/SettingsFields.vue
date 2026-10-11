<script setup lang="ts">
/**
 * The plain settings of one Settings section -- numbers, switches, short texts --
 * rendered from the schema the server sends with its configuration, so a new setting
 * shows up here without anyone writing a form for it. Lists and dicts have editors of
 * their own and are left out.
 *
 * Only what is edited is kept as a draft; every other field shows what the server holds,
 * and keeps doing so when another section's save changes it. An empty field is the
 * default, which the placeholder shows; saving sends the changed keys alone, null for a
 * key reset to its default.
 */
import type { SettingSchema } from '~/composables/useSettingsSections'

const props = defineProps<{
  section: string
  /** Shown to the save button when this section fails to save */
  title: string
  /** Keys rendered elsewhere in the section */
  exclude?: string[]
  /** Given, the sections the page knows: settings of any other section show here too,
   *  so one the UI has not heard of yet is not left out of sight */
  known?: string[]
}>()

const api = useApi()
const server = useServerConfig()

const SCALAR = new Set(['int', 'float', 'bool', 'str'])

const fields = computed(() => (server.value?.schema || []).filter((s) =>
  (s.section === props.section || (props.known && !props.known.includes(s.section)))
  && SCALAR.has(s.type) && !props.exclude?.includes(s.key)))
const inputs = computed(() => fields.value.filter((f) => f.type !== 'bool'))
const switches = computed(() => fields.value.filter((f) => f.type === 'bool'))

/** Edited keys only: text for inputs, a boolean for switches, null for "reset" */
const edits = reactive<Record<string, any>>({})

const saved = (key: string) => setValue(server.value?.config, key)

/** What an edit means to the server: null for an empty field, a number for a number */
function parsed(field: SettingSchema, value: any) {
  if (value === null || value === undefined) return null
  if (field.type === 'bool') return value
  const text = String(value).trim()
  if (!text) return null
  if (field.type === 'str') return text
  return Number(text) // NaN is caught by `problem`
}

const changed = computed(() => fields.value.filter((f) =>
  f.key in edits && parsed(f, edits[f.key]) !== saved(f.key)))
const dirty = computed(() => changed.value.length > 0)

function problem(field: SettingSchema): string {
  if (!(field.key in edits) || field.type === 'bool' || field.type === 'str') return ''
  const value = parsed(field, edits[field.key])
  if (value === null) return ''
  if (Number.isNaN(value)) return 'Enter a number, or leave it empty for the default.'
  if (field.type === 'int' && !Number.isInteger(value)) return 'Enter a whole number.'
  if (field.min !== undefined && value < field.min) return `At least ${field.min}.`
  if (field.max !== undefined && value > field.max) return `At most ${field.max}.`
  return ''
}

async function save() {
  const bad = changed.value.find((f) => problem(f))
  if (bad) throw new Error(`${bad.label}: ${problem(bad)}`)
  const body = Object.fromEntries(changed.value.map((f) => [f.key, parsed(f, edits[f.key])]))
  adoptServerConfig(server, await api.patch<any>('/v1/config', body))
  discard()
}

function discard() {
  for (const key of Object.keys(edits)) delete edits[key]
}

useSettingsSection({ id: `fields-${props.section}`, panel: props.section, title: props.title, dirty, save, discard })

// --- Fields -------------------------------------------------------------------------

const text = (f: SettingSchema) => f.key in edits ? (edits[f.key] ?? '') : String(saved(f.key) ?? '')

function setText(f: SettingSchema, value: string | number) {
  edits[f.key] = String(value ?? '')
}

const checked = (f: SettingSchema) => {
  const value = f.key in edits ? edits[f.key] : saved(f.key)
  return value === null ? !!f.default : !!value
}

function setChecked(f: SettingSchema, value: boolean) {
  // Switched back to what the server holds: no longer an edit at all
  const current = saved(f.key)
  if (value === (current === null ? !!f.default : current)) delete edits[f.key]
  else edits[f.key] = value
}

/** Set to something other than the default, saved or in the draft */
function overridden(f: SettingSchema) {
  return (f.key in edits ? parsed(f, edits[f.key]) : saved(f.key)) !== null
}

function reset(f: SettingSchema) {
  if (saved(f.key) === null) delete edits[f.key]
  else edits[f.key] = null
}

const defaultText = (f: SettingSchema) =>
  f.default === null || f.default === undefined || f.default === '' ? 'None' : String(f.default)
const step = (f: SettingSchema) => f.type === 'int' ? 1 : 'any'
</script>

<template>
  <UCard v-if="fields.length">
    <div class="space-y-5">
      <div v-if="inputs.length" class="grid sm:grid-cols-2 gap-x-6 gap-y-4">
        <UFormField
          v-for="f in inputs" :key="f.key" :label="f.label" :description="f.description"
          :error="problem(f) || undefined"
        >
          <template #hint>
            <UButton
              v-if="overridden(f)" size="xs" variant="link" color="neutral"
              icon="i-fa7-solid-arrow-rotate-left" class="text-dimmed hover:text-default p-0"
              :title="`Back to the default (${defaultText(f)})`" :aria-label="`Reset ${f.label} to its default`"
              @click="reset(f)"
            />
          </template>
          <UInput
            :model-value="text(f)" :type="f.type === 'str' ? 'text' : 'number'"
            :min="f.min" :max="f.max" :step="f.type === 'str' ? undefined : step(f)"
            :placeholder="`${defaultText(f)} (default)`" class="w-full"
            :ui="{ base: overridden(f) ? 'font-medium' : '' }"
            @update:model-value="setText(f, $event)"
          >
            <template v-if="f.unit" #trailing>
              <span class="text-xs text-dimmed">{{ f.unit }}</span>
            </template>
          </UInput>
        </UFormField>
      </div>

      <div v-if="switches.length" class="space-y-3" :class="inputs.length ? 'border-t border-default pt-4' : ''">
        <div v-for="f in switches" :key="f.key" class="flex items-start gap-2">
          <USwitch
            :model-value="checked(f)" :label="f.label" :description="f.description" class="flex-1"
            @update:model-value="setChecked(f, $event)"
          />
          <UButton
            v-if="overridden(f)" size="xs" variant="link" color="neutral"
            icon="i-fa7-solid-arrow-rotate-left" class="text-dimmed hover:text-default shrink-0"
            :title="`Back to the default (${f.default ? 'on' : 'off'})`" :aria-label="`Reset ${f.label} to its default`"
            @click="reset(f)"
          />
        </div>
      </div>
    </div>
  </UCard>
</template>
