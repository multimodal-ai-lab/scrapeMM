<script setup lang="ts">
import type { Role } from '~/composables/useApi'

/**
 * The API keys: who may use this server, and with what role.
 *
 * Root is the server's own key (SCRAPEMM_API_KEY, or generated on first start). Admins
 * see and change everything; Clients retrieve and search, but see none of the server's
 * configuration or credentials. A key manages only keys below its own role, so an Admin
 * creates, renames and revokes Clients, and Root everything else.
 *
 * The server keeps a hash of each key, never the key: a new key is shown once, in the
 * dialog that created it, and is gone when that closes.
 */
const api = useApi()
const me = useMe()

const RANK: Record<Role, number> = { root: 3, admin: 2, client: 1 }
const ROLE_COLORS: Record<Role, 'error' | 'warning' | 'neutral'> = {
  root: 'error', admin: 'warning', client: 'neutral',
}
const ROLE_HINTS: Record<Role, string> = {
  root: "The server's own key. Only regenerated, never renamed or revoked.",
  admin: 'Everything, including Settings, Logs and the API keys.',
  client: 'Retrieval, search and the views around them. No Settings, Secrets, Logs or API keys.',
}

const keys = ref<any[]>([])
const loading = ref(true)
const error = ref('')

// Creating, in a dialog: first the name and role, then the new key
const dialogOpen = ref(false)
const name = ref('')
const role = ref<Role>('client')
const creating = ref(false)
const dialogError = ref('')
const roles = computed(() => (['client', 'admin'] as Role[])
  .filter((r) => me.value && RANK[me.value.role] > RANK[r])
  .map((r) => ({ label: ROLE_LABELS[r], value: r })))

// The key just made (or regenerated), shown once: only while the dialog is open
const revealed = ref<{ name: string, token: string } | null>(null)
const copied = ref(false)

function openDialog() {
  dialogOpen.value = true
}

/** Once the dialog has closed: nothing of the key is kept on the page */
function resetDialog() {
  revealed.value = null
  name.value = ''
  role.value = 'client'
  dialogError.value = ''
  copied.value = false
}

// Renaming
const editing = ref<string | null>(null)
const draft = ref('')
const busy = ref<string | null>(null)

const manageable = (key: any) => !!me.value && key.role !== 'root'
  && RANK[me.value.role] > RANK[key.role as Role]

async function load() {
  try {
    keys.value = (await api.get<any>('/v1/api-keys')).keys
    error.value = ''
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function create() {
  if (!name.value.trim()) return
  creating.value = true
  dialogError.value = ''
  try {
    const { key, token } = await api.post<any>('/v1/api-keys', { name: name.value.trim(), role: role.value })
    revealed.value = { name: key.name, token }
    await load()
  } catch (e: any) {
    dialogError.value = e.message
  } finally {
    creating.value = false
  }
}

function startRename(key: any) {
  editing.value = key.id
  draft.value = key.name
}

async function rename(key: any) {
  const value = draft.value.trim()
  if (!value || value === key.name) {
    editing.value = null
    return
  }
  busy.value = key.id
  error.value = ''
  try {
    await api.patch(`/v1/api-keys/${key.id}`, { name: value })
    editing.value = null
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

async function revoke(key: any) {
  if (!confirm(`Revoke the key "${key.name}"? Whoever uses it is rejected from now on. `
    + 'This cannot be undone.')) return
  busy.value = key.id
  error.value = ''
  try {
    await api.del(`/v1/api-keys/${key.id}`)
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

async function regenerateRoot() {
  if (!confirm('Regenerate the root key? Every client using it is rejected until it is '
    + 'given the new one. This browser switches over by itself.')) return
  busy.value = 'root'
  error.value = ''
  try {
    const { token } = await api.post<any>('/v1/api-keys/root/regenerate')
    setToken(token) // Otherwise this browser's next call would be rejected
    revealed.value = { name: 'Root', token }
    dialogOpen.value = true
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

async function copy() {
  if (!revealed.value || !(await copyText(revealed.value.token))) {
    dialogError.value = 'Copying to the clipboard failed. Select the key and copy it by hand.'
    return
  }
  copied.value = true
  setTimeout(() => { copied.value = false }, 1500)
}

onMounted(load)
</script>

<template>
  <div class="space-y-5 max-w-3xl">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h1 class="text-2xl font-semibold">API Keys</h1>
        <p class="text-sm text-muted mt-1">
          Who may use this server. Admins see and change everything; Clients retrieve and
          search, without access to Settings, Secrets, Logs or these keys.
          <template v-if="me?.role === 'admin'">As an Admin, you manage the Client keys.</template>
        </p>
      </div>
      <UButton
        v-if="roles.length" icon="i-fa7-solid-plus" label="New key" class="shrink-0"
        @click="openDialog"
      />
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <!-- Creating a key: its name and role, then the key itself, once. Closing the dialog
         is what puts the key out of sight for good. -->
    <UModal
      v-model:open="dialogOpen"
      :title="revealed ? `The key for ${revealed.name}` : 'New API key'"
      :description="revealed ? 'Copy it now: it cannot be shown again.' : 'Name it after who or what uses it.'"
      @after:leave="resetDialog"
    >
      <template #body>
        <div class="space-y-4">
          <div v-if="revealed" class="flex items-center gap-2">
            <UInput
              :model-value="revealed.token" readonly class="flex-1 font-mono"
              @focus="($event.target as HTMLInputElement).select()"
            />
            <UButton
              :icon="copied ? 'i-fa7-solid-check' : 'i-fa7-regular-copy'"
              color="neutral" variant="subtle" :label="copied ? 'Copied' : 'Copy'" @click="copy"
            />
          </div>
          <form v-else id="new-key" class="space-y-4" @submit.prevent="create">
            <UFormField label="Name">
              <UInput v-model="name" placeholder="e.g. Fact-checking pipeline" :maxlength="64" class="w-full" autofocus />
            </UFormField>
            <UFormField label="Role" :description="ROLE_HINTS[role]">
              <USelect v-model="role" :items="roles" class="w-40" />
            </UFormField>
          </form>
          <UAlert v-if="dialogError" color="error" variant="subtle" :description="dialogError" />
        </div>
      </template>
      <template #footer="{ close }">
        <div class="flex justify-end gap-2 w-full">
          <UButton v-if="revealed" label="Done" @click="close" />
          <template v-else>
            <UButton color="neutral" variant="ghost" label="Cancel" @click="close" />
            <UButton
              type="submit" form="new-key" icon="i-fa7-solid-plus" label="Create"
              :loading="creating" :disabled="!name.trim()"
            />
          </template>
        </div>
      </template>
    </UModal>

    <div v-if="loading" class="space-y-1.5">
      <div v-for="n in 3" :key="n" class="surface-card rounded-lg px-3 py-3">
        <USkeleton class="h-4 w-56" />
      </div>
    </div>

    <ul v-else class="space-y-1.5">
      <li v-for="key in keys" :key="key.id" class="surface-card rounded-lg group">
        <div class="flex items-center gap-3 px-3 py-2.5">
          <UIcon
            :name="key.role === 'root' ? 'i-fa7-solid-crown' : 'i-fa7-solid-key'"
            class="size-4 shrink-0 text-dimmed"
          />

          <div class="min-w-0 flex-1">
            <form v-if="editing === key.id" class="flex items-center gap-2" @submit.prevent="rename(key)">
              <UInput v-model="draft" size="sm" :maxlength="64" class="flex-1" autofocus @keyup.escape="editing = null" />
              <UButton type="submit" size="xs" label="Save" :loading="busy === key.id" />
              <UButton size="xs" color="neutral" variant="ghost" label="Cancel" @click="editing = null" />
            </form>
            <template v-else>
              <div class="flex items-center gap-2 min-w-0">
                <span class="text-sm font-medium truncate">{{ key.name }}</span>
                <UBadge
                  :color="ROLE_COLORS[key.role as Role]" variant="subtle" size="sm" class="shrink-0"
                  :label="ROLE_LABELS[key.role as Role]" :title="ROLE_HINTS[key.role as Role]"
                />
                <span v-if="key.id === me?.id" class="text-xs text-dimmed shrink-0">this browser</span>
              </div>
              <p class="text-xs text-dimmed truncate">
                <template v-if="key.role === 'root'">
                  {{ key.from_environment ? 'Set by SCRAPEMM_API_KEY in the .env' : 'Generated by the server' }}
                </template>
                <template v-else>
                  <span class="font-mono">{{ key.hint }}…</span>
                  · created <span :title="absoluteTime(key.created_at)">{{ timeAgo(key.created_at) }}</span>
                  · <template v-if="key.last_used_at">
                    last used <span :title="absoluteTime(key.last_used_at)">{{ timeAgo(key.last_used_at) }}</span>
                  </template>
                  <template v-else>never used</template>
                </template>
              </p>
            </template>
          </div>

          <!-- Actions on hover, as on the Secrets rows -->
          <div
            v-if="editing !== key.id"
            class="flex items-center gap-1 opacity-0 transition-opacity duration-150
                   group-hover:opacity-100 focus-within:opacity-100"
          >
            <template v-if="manageable(key)">
              <UButton
                size="xs" color="neutral" variant="link" icon="i-fa7-solid-pen" label="Rename"
                class="text-dimmed hover:text-highlighted transition-colors" @click="startRename(key)"
              />
              <UButton
                size="xs" color="neutral" variant="link" icon="i-fa7-solid-ban" label="Revoke"
                :loading="busy === key.id"
                class="text-dimmed hover:text-error transition-colors" @click="revoke(key)"
              />
            </template>
            <UButton
              v-else-if="key.role === 'root' && me?.role === 'root' && !key.from_environment"
              size="xs" color="neutral" variant="link" icon="i-fa7-solid-rotate" label="Regenerate"
              :loading="busy === 'root'"
              class="text-dimmed hover:text-error transition-colors" @click="regenerateRoot"
            />
          </div>
        </div>
      </li>
    </ul>
  </div>
</template>
