/**
 * The Settings page saves with one button for all of its sections. Each section keeps
 * its own draft and registers here what the button needs: whether it has unsaved
 * changes, how to save them and how to discard them.
 */
import type { ComputedRef, Ref } from 'vue'

export interface SettingsSection {
  id: string
  title: string
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

/** The drafts the chain preview reads: the chain and the exceptions, as being edited. */
export const useChainDraft = () => useState<any[] | null>('chain-draft', () => null)
export const useExceptionsDraft = () => useState<any[] | null>('exceptions-draft', () => null)
