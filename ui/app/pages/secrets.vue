<script setup lang="ts">
/**
 * The API credentials the integrations need.
 *
 * One compact row each rather than a card each: fourteen cards made a page you had to
 * scroll to find anything on, when all most rows need to say is "set" or "not set".
 * Values go one way only -- the server reports whether a secret exists, never what it
 * is, so every field starts empty and saving replaces.
 */
const api = useApi()

const secrets = ref<any[]>([])
const drafts = reactive<Record<string, string>>({})
const open = ref<string | null>(null)
const busy = ref<string | null>(null)
const error = ref('')
const notice = ref('')
const loading = ref(true)

const counts = computed(() => ({
  set: secrets.value.filter((s) => s.is_set).length,
  total: secrets.value.length,
}))

async function load() {
  try {
    secrets.value = (await api.get<any>('/v1/secrets')).secrets
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function toggle(name: string) {
  open.value = open.value === name ? null : name
  if (open.value) drafts[name] = ''
}

async function save(name: string) {
  const value = (drafts[name] || '').trim()
  if (!value) return
  busy.value = name
  error.value = ''
  notice.value = ''
  try {
    await api.put(`/v1/secrets/${name}`, { value })
    drafts[name] = ''
    open.value = null
    notice.value = `${name} saved. The dashboard reflects it straight away.`
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

async function clear(name: string) {
  busy.value = name
  error.value = ''
  notice.value = ''
  try {
    await api.del(`/v1/secrets/${name}`)
    notice.value = `${name} removed.`
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-5 max-w-3xl">
    <div>
      <h1 class="text-2xl font-semibold">Secrets</h1>
      <p class="text-sm text-muted mt-1">
        {{ counts.set }} of {{ counts.total }} set. Stored encrypted on the server and
        never sent back to this page — you can replace a secret, not read it.
      </p>
    </div>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UAlert
      v-if="notice" color="success" variant="subtle" :description="notice"
      close @update:open="notice = ''"
    />

    <div v-if="loading" class="space-y-1.5">
      <div v-for="n in 8" :key="n" class="surface-card rounded-lg px-3 py-3">
        <USkeleton class="h-4 w-56" />
      </div>
    </div>

    <ul v-else class="space-y-1.5">
      <li v-for="secret in secrets" :key="secret.name" class="surface-card rounded-lg">
        <!-- The whole row opens the editor, so hiding the buttons until hover costs
             no discoverability: there is a target either way. -->
        <div
          class="group flex items-center gap-3 px-3 py-2.5"
          :class="secret.managed ? '' : 'cursor-pointer'"
          @click="secret.managed || toggle(secret.name)"
        >
          <!-- Status as a mark rather than a word: a column of "Set"/"Not set" badges
               is harder to scan than a column of ticks. -->
          <UIcon
            :name="secret.is_set ? 'i-fa7-solid-circle-check' : 'i-fa7-regular-circle'"
            class="size-4 shrink-0"
            :class="secret.is_set ? 'text-success' : 'text-dimmed'"
            :title="secret.is_set ? 'Set' : 'Not set'"
          />

          <div class="min-w-0 flex-1">
            <p class="font-mono text-sm truncate">{{ secret.name }}</p>
            <p class="text-xs text-dimmed truncate" :title="secret.description">
              {{ secret.description }}
            </p>
          </div>

          <UBadge
            v-if="secret.managed" color="neutral" variant="subtle" size="sm"
            label="managed" title="Written by the server when you solve a CAPTCHA"
          />

          <!-- Actions appear on hover. Fourteen rows of permanently visible buttons
               made the page about the buttons rather than about what is configured. -->
          <template v-else>
            <div
              class="reveal-on-hover flex items-center gap-1 opacity-0
                     transition-opacity duration-150 group-hover:opacity-100
                     focus-within:opacity-100"
              :class="open === secret.name ? 'opacity-100' : ''"
            >
              <UButton
                size="xs" color="neutral" variant="link"
                :icon="open === secret.name ? 'i-fa7-solid-xmark' : 'i-fa7-solid-pen'"
                :label="open === secret.name ? undefined : (secret.is_set ? 'Replace' : 'Set')"
                :square="open === secret.name"
                class="text-dimmed hover:text-highlighted transition-colors"
                @click.stop="toggle(secret.name)"
              />
              <!-- Neutral until you reach for it: a permanently red bin on every row
                   reads as a warning about the row rather than an action on it. -->
              <UButton
                v-if="secret.is_set" size="xs" color="neutral" variant="link"
                icon="i-fa7-solid-trash" :loading="busy === secret.name"
                aria-label="Remove this secret" title="Remove this secret"
                class="text-dimmed hover:text-error transition-colors"
                @click.stop="clear(secret.name)"
              />
            </div>
          </template>
        </div>

        <div
          v-if="open === secret.name" class="px-3 pb-3 flex items-end gap-2"
          @click.stop
        >
          <UTextarea
            v-if="secret.multiline" v-model="drafts[secret.name]" :rows="3"
            class="flex-1" placeholder="Paste the value" autofocus
          />
          <UInput
            v-else v-model="drafts[secret.name]" type="password" class="flex-1"
            placeholder="Enter the value" autocomplete="off" autofocus
            @keyup.enter="save(secret.name)"
          />
          <UButton
            :loading="busy === secret.name"
            :disabled="!(drafts[secret.name] || '').trim()"
            label="Save" @click="save(secret.name)"
          />
        </div>
      </li>
    </ul>
  </div>
</template>
