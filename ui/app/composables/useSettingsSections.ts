/**
 * The Settings page saves with one button for all of its sections. Each section keeps
 * its own draft and registers here what the button needs: whether it has unsaved
 * changes, how to save them and how to discard them.
 */
import type { ComputedRef, Ref } from 'vue'

export interface SettingsSection {
  id: string
  title: string
  /** The panel entry the section is shown under, for its unsaved-changes dot. Defaults
   *  to `id`: several drafts can share one entry (the parallelism table and its fields). */
  panel?: string
  dirty: ComputedRef<boolean> | Ref<boolean>
  /** Saves the section's changes. Throws with a message the button shows. */
  save: () => Promise<void>
  discard: () => void
}

const KEY = Symbol('settings-sections')

export function provideSettingsSections() {
  // Shallow: a deep `reactive()` unwraps each section's `dirty` ref, so the map would no
  // longer hold what `SettingsSection` says. Adding and removing sections stays reactive.
  const sections = shallowReactive(new Map<string, SettingsSection>())
  provide(KEY, sections)
  return sections
}

/** Registers a section with the page's save button; unregisters it on unmount. */
export function useSettingsSection(section: SettingsSection) {
  const sections = inject<Map<string, SettingsSection> | null>(KEY, null)
  if (!sections) return
  sections.set(section.id, section)
  onBeforeUnmount(() => sections.delete(section.id))
}

/** The drafts the chain preview reads: the chain and the site rules, as being edited. */
export const useChainDraft = () => useState<any[] | null>('chain-draft', () => null)
export const useSiteRulesDraft = () => useState<any[] | null>('site-rules-draft', () => null)

// --- The server's configuration ------------------------------------------------------

export type SettingType = 'int' | 'float' | 'bool' | 'str' | 'list' | 'dict'

/** One setting as the server describes it: enough to render a field for it. */
export interface SettingSchema {
  key: string
  type: SettingType
  default: any
  label: string
  description?: string
  /** The Settings panel entry it belongs to */
  section: string
  unit?: string
  min?: number
  max?: number
}

/** A retrieval method's parallelism: its slots and cooldown, with their defaults. */
export interface MethodLimits {
  key: string
  name: string
  kind: 'integration' | 'method' | 'archive'
  slots: number
  slots_default: number
  cooldown: number
  cooldown_default: number
}

export interface ServerConfig {
  /** Only what is set; a missing key is at its default */
  config: Record<string, any>
  schema: SettingSchema[]
  methods: MethodLimits[]
}

/** GET /v1/config, shared by the Settings sections that edit it. Null until loaded. An
 *  older server sends no schema and no methods: they are empty then, not missing. */
export const useServerConfig = () => useState<ServerConfig | null>('server-config', () => null)

/** Takes a response of GET or PATCH /v1/config into the shared state. Both are passed
 *  in, not looked up: a save runs outside of setup, where composables are not at hand. */
export function adoptServerConfig(state: Ref<ServerConfig | null>, response: any) {
  state.value = {
    config: response?.config || {},
    // The PATCH answers with the config alone; schema and methods stay as they were
    schema: Array.isArray(response?.schema) ? response.schema : (state.value?.schema || []),
    methods: Array.isArray(response?.methods) ? response.methods : (state.value?.methods || []),
  }
}

/** A value the server holds for a key, or null when it is at its default. */
export const setValue = (config: Record<string, any> | undefined, key: string) => config?.[key] ?? null
