<script setup lang="ts">
/**
 * The Settings page's one save button: fixed at the bottom right, shown only while some
 * section has unsaved changes. Saves the changed sections one after another, so that one
 * section failing validation does not keep the others from being saved; the failures
 * are listed by section. Also guards against leaving with unsaved changes.
 */
import type { SettingsSection } from '~/composables/useSettingsSections'

const props = defineProps<{ sections: Map<string, SettingsSection> }>()

const changed = computed(() => [...props.sections.values()].filter((s) => unref(s.dirty)))
const saving = ref(false)
const failures = ref<{ title: string, message: string }[]>([])
const saved = ref(false)

async function saveAll() {
  saving.value = true
  failures.value = []
  for (const section of changed.value) {
    try {
      await section.save()
    } catch (e: any) {
      failures.value.push({ title: section.title, message: e?.message || String(e) })
    }
  }
  saving.value = false
  if (!failures.value.length) {
    saved.value = true
    setTimeout(() => { saved.value = false }, 2500)
  }
}

function discardAll() {
  for (const section of changed.value) section.discard()
  failures.value = []
}

// Unsaved changes are not lost by accident: not on reload or closing the tab ...
function beforeUnload(event: BeforeUnloadEvent) {
  if (!changed.value.length) return
  event.preventDefault()
  event.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
// ... nor on leaving the page within the app
onBeforeRouteLeave(() => {
  if (changed.value.length
    && !confirm(`${changed.value.length === 1 ? 'A section has' : `${changed.value.length} sections have`} `
      + 'unsaved changes. Leave without saving?')) return false
})
</script>

<template>
  <div
    class="fixed z-50 flex flex-col items-end gap-2 pointer-events-none"
    style="right: max(1rem, env(safe-area-inset-right)); bottom: max(1rem, env(safe-area-inset-bottom))"
  >
    <Transition
      enter-from-class="opacity-0 translate-y-2" leave-to-class="opacity-0 translate-y-2"
      enter-active-class="transition duration-200" leave-active-class="transition duration-150"
    >
      <div
        v-if="failures.length" role="alert"
        class="pointer-events-auto max-w-sm rounded-xl bg-default ring ring-error/40 shadow-lg p-3 text-sm space-y-1"
      >
        <p class="font-medium text-error flex items-center gap-2">
          <UIcon name="i-fa7-solid-triangle-exclamation" class="size-4" />
          {{ failures.length === 1 ? 'A section was not saved' : `${failures.length} sections were not saved` }}
        </p>
        <p v-for="f in failures" :key="f.title"><strong>{{ f.title }}:</strong> {{ f.message }}</p>
      </div>
    </Transition>

    <Transition
      enter-from-class="opacity-0 translate-y-3 scale-95" leave-to-class="opacity-0 translate-y-3 scale-95"
      enter-active-class="transition duration-200 ease-out" leave-active-class="transition duration-150 ease-in"
    >
      <div
        v-if="changed.length || saving"
        class="pointer-events-auto flex items-center gap-1 rounded-full bg-default shadow-xl ring ring-default p-1.5"
      >
        <UTooltip :text="changed.map((s) => s.title).join(', ')">
          <span class="px-3 text-sm text-muted tabular-nums" aria-live="polite">
            {{ changed.length }} unsaved {{ changed.length === 1 ? 'section' : 'sections' }}
          </span>
        </UTooltip>
        <UButton
          variant="ghost" color="neutral" size="lg" label="Discard" class="rounded-full"
          :disabled="saving" @click="discardAll"
        />
        <UButton
          size="lg" icon="i-fa7-solid-floppy-disk" label="Save" class="rounded-full"
          :loading="saving" @click="saveAll"
        />
      </div>
      <div
        v-else-if="saved"
        class="pointer-events-auto flex items-center gap-2 rounded-full bg-default shadow-xl ring ring-success/40 px-4 py-2.5 text-sm text-success"
        role="status"
      >
        <UIcon name="i-fa7-solid-circle-check" class="size-4" /> Saved
      </div>
    </Transition>
  </div>
</template>
