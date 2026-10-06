<script setup lang="ts">
const token = useToken()
const me = useMe()
const isAdmin = useIsAdmin()
const api = useApi()
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

interface NavLink {
  label: string
  icon: string
  to: string
  children?: NavLink[]
  /** Root and Admin only: what configures the server or reveals its credentials */
  admin?: boolean
}

const links: NavLink[] = [
  { label: 'Dashboard', icon: 'i-fa7-solid-gauge-high', to: '/' },
  {
    label: 'Playground',
    icon: 'i-fa7-solid-play',
    to: '/playground',
    children: [
      { label: 'Retrieval', icon: 'i-fa7-solid-download', to: '/playground/retrieval' },
      { label: 'Search', icon: 'i-fa7-solid-magnifying-glass', to: '/playground/search' },
    ],
  },
  { label: 'Jobs', icon: 'i-fa7-solid-clock-rotate-left', to: '/jobs' },
  { label: 'Statistics', icon: 'i-fa7-solid-chart-column', to: '/statistics' },
  { label: 'Test', icon: 'i-fa7-solid-flask', to: '/test' },
  { label: 'CAPTCHA', icon: 'i-fa7-solid-shield-halved', to: '/captcha' },
  { label: 'Blacklist', icon: 'i-fa7-solid-ban', to: '/blacklist' },
  { label: 'Logs', icon: 'i-fa7-solid-terminal', to: '/logs', admin: true },
  { label: 'API Keys', icon: 'i-fa7-solid-key', to: '/api-keys', admin: true },
  { label: 'Settings', icon: 'i-fa7-solid-gear', to: '/settings', admin: true },
]

const visibleLinks = computed(() => links.filter((link) => !link.admin || isAdmin.value))

const route = useRoute()

/** An Admin page opened with a Client key (a bookmark, a typed URL): said so plainly
 *  rather than left to fail call by call. Undecided until the key's role is known. */
const adminPage = computed(() => links.some((l) => l.admin && route.path.startsWith(l.to)))

// Who this browser is signed in as, whenever the key changes (sign-in, regeneration)
watch(token, async (value) => {
  if (!value) return
  try {
    me.value = await api.get<any>('/v1/me')
  } catch { /* A rejected key signs out by itself (see useApi) */ }
}, { immediate: true })

/** A group is current while one of its pages is; it is set apart from the page itself,
 *  which alone gets the highlighted row. */
function inGroup(link: NavLink) {
  return route.path === link.to || route.path.startsWith(`${link.to}/`)
}

async function signIn() {
  error.value = ''
  checking.value = true
  try {
    // Verify before storing, so a typo does not leave the UI in a broken state
    await $fetch(`${apiBase()}/v1/me`, {
      headers: { Authorization: `Bearer ${entered.value.trim()}` },
    })
    setToken(entered.value.trim())
  } catch {
    error.value = 'That key was rejected. Ask an admin of this server for one. The root key '
      + 'is printed in the server log on start, or set as SCRAPEMM_API_KEY in your .env.'
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
              <p class="text-sm text-muted">Enter your API key to continue.</p>
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
          <template v-for="link in visibleLinks" :key="link.to">
            <UTooltip :text="link.label" :disabled="!collapsed" :content="{ side: 'right' }">
              <NuxtLink
                :to="link.children ? link.children[0]!.to : link.to"
                class="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm
                       hover-row"
                :class="route.path === link.to
                  ? 'bg-elevated text-highlighted font-medium'
                  : link.children && inGroup(link)
                    ? 'text-highlighted font-medium'
                    : 'text-muted hover:text-default'"
              >
                <UIcon :name="link.icon" class="size-4 shrink-0" />
                <span
                  class="truncate transition-opacity duration-200"
                  :class="collapsed ? 'opacity-0 w-0' : 'opacity-100'"
                >{{ link.label }}</span>
              </NuxtLink>
            </UTooltip>

            <!-- A group's pages, always shown: two entries do not need a toggle. Indented
                 along a guide line when expanded; collapsed, only their icons remain. -->
            <div
              v-if="link.children" class="flex flex-col gap-1"
              :class="collapsed ? '' : 'ml-[1.2rem] pl-2 border-l border-default'"
            >
              <UTooltip
                v-for="child in link.children" :key="child.to"
                :text="`${link.label}: ${child.label}`" :disabled="!collapsed"
                :content="{ side: 'right' }"
              >
                <NuxtLink
                  :to="child.to"
                  class="flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm hover-row"
                  :class="route.path === child.to
                    ? 'bg-elevated text-highlighted font-medium'
                    : 'text-muted hover:text-default'"
                >
                  <UIcon :name="child.icon" class="shrink-0" :class="collapsed ? 'size-4' : 'size-3.5'" />
                  <span
                    class="truncate transition-opacity duration-200"
                    :class="collapsed ? 'opacity-0 w-0' : 'opacity-100'"
                  >{{ child.label }}</span>
                </NuxtLink>
              </UTooltip>
            </div>
          </template>
        </nav>

        <div class="mt-auto space-y-1">
          <!-- Which key this browser uses: with several keys about, worth knowing -->
          <div
            v-if="me && !collapsed" class="px-2.5 text-xs text-dimmed truncate"
            :title="`Signed in with the ${ROLE_LABELS[me.role]} key '${me.name}'`"
          >
            {{ me.name }} · {{ ROLE_LABELS[me.role] }}
          </div>
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
        <!-- The jobs overview is kept alive: coming back from a job shows it exactly as
             it was left -- filters, sorting, everything loaded and the scroll position,
             which the router restores once the page is there again -->
        <template v-if="adminPage && !isAdmin">
          <UAlert
            v-if="me" color="neutral" variant="subtle" icon="i-fa7-solid-lock" class="max-w-xl"
            title="Admins only"
            description="This page needs an Admin or Root API key. Ask an admin of this server for one, or sign in with one."
          />
        </template>
        <NuxtPage v-else :keepalive="{ include: ['JobsOverview'] }" />
      </main>
    </div>
  </UApp>
</template>
