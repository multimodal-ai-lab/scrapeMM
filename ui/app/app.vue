<script setup lang="ts">
const token = useToken()
const entered = ref('')
const checking = ref(false)
const error = ref('')

// Remembered per browser, so the sidebar stays how you left it
const collapsed = ref(false)
onMounted(() => {
  try {
    collapsed.value = localStorage.getItem('scrapemm.sidebar') === 'collapsed'
  } catch { /* private window, or storage blocked */ }
})
watch(collapsed, (value) => {
  try {
    localStorage.setItem('scrapemm.sidebar', value ? 'collapsed' : 'expanded')
  } catch { /* nothing worth failing over */ }
})

const links = [
  { label: 'Dashboard', icon: 'i-fa7-solid-gauge-high', to: '/' },
  { label: 'Playground', icon: 'i-fa7-solid-play', to: '/playground' },
  { label: 'Jobs', icon: 'i-fa7-solid-clock-rotate-left', to: '/jobs' },
  { label: 'CAPTCHA', icon: 'i-fa7-solid-shield-halved', to: '/captcha' },
  { label: 'Blacklist', icon: 'i-fa7-solid-ban', to: '/blacklist' },
  { label: 'Secrets', icon: 'i-fa7-solid-key', to: '/secrets' },
  { label: 'Settings', icon: 'i-fa7-solid-gear', to: '/settings' },
]

const route = useRoute()

async function signIn() {
  error.value = ''
  checking.value = true
  try {
    // Verify before storing, so a typo does not leave the UI in a broken state
    await $fetch(`${apiBase()}/v1/version`, {
      headers: { Authorization: `Bearer ${entered.value.trim()}` },
    })
    setToken(entered.value.trim())
  } catch {
    error.value = 'That key was rejected. It is printed in the server log on first start, '
      + 'or set as SCRAPEMM_API_KEY in your .env.'
  } finally {
    checking.value = false
  }
}
</script>

<template>
  <UApp>
    <div v-if="!token" class="min-h-screen flex items-center justify-center p-4">
      <UCard class="w-full max-w-md">
        <template #header>
          <div class="flex items-center gap-3">
            <img src="/logo.webp" alt="" class="size-10 shrink-0">
            <div>
              <h1 class="text-lg font-semibold">scrapeMM</h1>
              <p class="text-sm text-muted">Enter the server's API key to continue.</p>
            </div>
          </div>
        </template>
        <form class="space-y-3" @submit.prevent="signIn">
          <UInput
            v-model="entered" type="password" placeholder="API key"
            autocomplete="off" size="lg" class="w-full"
          />
          <UAlert v-if="error" color="error" variant="subtle" :description="error" />
          <UButton
            type="submit" block size="lg" :loading="checking"
            :disabled="!entered.trim()" label="Unlock"
          />
        </form>
      </UCard>
    </div>

    <div v-else class="min-h-screen flex">
      <!-- min-w-0 and overflow-hidden are load-bearing: as a flex item the aside keeps
           `min-width: auto`, so without them its content's min-content width wins over
           the collapsed width and the panel never actually narrows. -->
      <aside
        class="fixed inset-y-0 left-0 z-40 shrink-0 min-w-0 overflow-hidden
               border-r border-default bg-default
               flex flex-col gap-2 p-3 transition-[width] duration-200 ease-out"
        :class="collapsed ? 'w-16' : 'w-56'"
      >
        <div class="flex items-center gap-2 px-1 h-9">
          <!-- Collapsed, the logo is all that remains of the brand, and it is what
               expands the sidebar again: the chevron no longer fits beside it. -->
          <NuxtLink
            v-if="!collapsed" to="/" class="flex items-center gap-2 min-w-0"
            aria-label="scrapeMM dashboard"
          >
            <img src="/logo.webp" alt="" class="size-7 shrink-0">
            <span class="font-semibold text-lg truncate">scrapeMM</span>
          </NuxtLink>
          <button
            v-else type="button" class="mx-auto transition-transform duration-200 hover:scale-110"
            aria-label="Expand the sidebar" title="Expand the sidebar"
            @click="collapsed = false"
          >
            <img src="/logo.webp" alt="" class="size-7">
          </button>
          <UButton
            v-if="!collapsed"
            class="ml-auto transition-transform duration-200 hover:scale-110"
            variant="ghost" color="neutral" size="sm" square
            :icon="collapsed ? 'i-fa7-solid-chevron-right' : 'i-fa7-solid-chevron-left'"
            :aria-label="collapsed ? 'Expand the sidebar' : 'Collapse the sidebar'"
            @click="collapsed = !collapsed"
          />
        </div>

        <nav class="flex flex-col gap-1">
          <UTooltip
            v-for="link in links" :key="link.to"
            :text="link.label" :disabled="!collapsed" :content="{ side: 'right' }"
          >
            <NuxtLink
              :to="link.to"
              class="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm
                     hover-row"
              :class="route.path === link.to
                ? 'bg-elevated text-highlighted font-medium'
                : 'text-muted hover:text-default'"
            >
              <UIcon :name="link.icon" class="size-4 shrink-0" />
              <span
                class="truncate transition-opacity duration-200"
                :class="collapsed ? 'opacity-0 w-0' : 'opacity-100'"
              >{{ link.label }}</span>
            </NuxtLink>
          </UTooltip>
        </nav>

        <div class="mt-auto">
          <UTooltip text="Log out" :disabled="!collapsed" :content="{ side: 'right' }">
            <UButton
              class="w-full justify-start transition-colors"
              variant="ghost" color="neutral" size="sm" icon="i-fa7-solid-right-from-bracket"
              :label="collapsed ? undefined : 'Log out'"
              :square="collapsed" @click="setToken(null)"
            />
          </UTooltip>
        </div>
      </aside>

      <!-- The sidebar is fixed, so it is out of the flow: the margin is what keeps the
           content beside it rather than underneath. -->
      <main
        class="flex-1 p-6 overflow-x-hidden min-w-0 transition-[margin] duration-200 ease-out"
        :class="collapsed ? 'ml-16' : 'ml-56'"
      >
        <NuxtPage />
      </main>
    </div>
  </UApp>
</template>
