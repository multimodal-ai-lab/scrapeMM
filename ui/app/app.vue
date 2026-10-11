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

// --- Health and news ----------------------------------------------------------------

const environment = useEnvironment()
const { captchaNew, blacklistNew, test, statuses, health } = useAttention()

watch(token, (value) => { if (value) followEnvironment() })
onMounted(() => { if (token.value) followEnvironment() })

/** What the logo says when hovered: which cards make it yellow or red, if any */
const healthText = computed(() => {
  const off = statuses.value.filter((s) => s.tone === health.value).map((s) => s.label)
  return health.value === 'error' ? `Needs attention now: ${off.join(', ')}`
    : health.value === 'warning' ? `Wants a look: ${off.join(', ')}`
      : health.value === 'success' ? 'All good' : 'scrapeMM dashboard'
})

/** A page counts as seen while it is open, including what turns up meanwhile */
watch([() => route.path, environment], () => {
  const env = environment.value
  if (!env) return
  if (route.path === '/captcha') markSeen('captcha', env.captcha?.latest)
  else if (route.path === '/blacklist') markSeen('blacklist', env.blacklist?.latest)
  else if (route.path === '/test' && env.test && !env.test.running) markSeen('test', env.test.key)
}, { immediate: true })

interface Indicator { tone: 'running' | 'success' | 'warning' | 'error', text: string }

/** The sidebar's indicators, by page. Yellow: something new to look at some time.
 *  Red: something went wrong. A test run is a spinner, then the colour of its results. */
const indicators = computed<Record<string, Indicator | undefined>>(() => ({
  '/captcha': captchaNew.value
    ? { tone: 'warning', text: 'New URLs are waiting for a CAPTCHA' } : undefined,
  '/blacklist': blacklistNew.value
    ? { tone: 'warning', text: 'New domains were blacklisted' } : undefined,
  '/test': test.value === 'running' ? { tone: 'running', text: 'A test is running' }
    : test.value && test.value !== 'neutral'
      ? { tone: test.value, text: {
          success: 'The last test went well', warning: 'The last test fell short',
          error: 'The last test went badly' }[test.value] }
      : undefined,
}))

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
        class="group/sidebar fixed inset-y-0 left-0 z-40 shrink-0 min-w-0 overflow-hidden
               border-r border-default bg-default
               flex flex-col gap-2 p-3 transition-[width] duration-200 ease-out"
        :class="collapsed ? 'w-16' : 'w-56'"
      >
        <div class="flex items-center gap-2 px-1 h-9">
          <!-- Collapsed, the logo is all that remains of the brand, and it is what
               expands the sidebar again: the chevron no longer fits beside it. -->
          <!-- Tinted by health: green, or yellow or red as the worst status card is -->
          <NuxtLink
            v-if="!collapsed" to="/" class="brand flex items-center gap-2 min-w-0"
            :class="`brand-${health}`" aria-label="scrapeMM dashboard" :title="healthText"
          >
            <BrandMark class="size-7 shrink-0" />
            <span class="brand-name font-semibold text-lg whitespace-nowrap">scrapeMM</span>
          </NuxtLink>
          <button
            v-else type="button"
            class="brand mx-auto transition-transform duration-200 hover:scale-110"
            :class="`brand-${health}`" aria-label="Expand the sidebar"
            :title="`${healthText} · Expand the sidebar`"
            @click="collapsed = false"
          >
            <BrandMark class="size-7" />
          </button>
          <!-- Only while the pointer is over the sidebar (or the button has the keyboard's
               focus): otherwise it is clutter beside the brand -->
          <UButton
            v-if="!collapsed"
            class="ml-auto opacity-0 transition duration-200 hover:scale-110
                   group-hover/sidebar:opacity-100 focus-visible:opacity-100"
            variant="ghost" color="neutral" size="sm" square
            :icon="collapsed ? 'i-fa7-solid-chevron-right' : 'i-fa7-solid-chevron-left'"
            :aria-label="collapsed ? 'Expand the sidebar' : 'Collapse the sidebar'"
            @click="collapsed = !collapsed"
          />
        </div>

        <nav class="flex flex-col gap-1">
          <template v-for="link in visibleLinks" :key="link.to">
            <UTooltip
              :text="indicators[link.to] ? `${link.label} · ${indicators[link.to]!.text}` : link.label"
              :disabled="!collapsed" :content="{ side: 'right' }"
            >
              <NuxtLink
                :to="link.children ? link.children[0]!.to : link.to"
                :title="collapsed ? undefined : indicators[link.to]?.text"
                class="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm
                       hover-row"
                :class="route.path === link.to
                  ? 'bg-elevated text-highlighted font-medium'
                  : link.children && inGroup(link)
                    ? 'text-highlighted font-medium'
                    : 'text-muted hover:text-default'"
              >
                <span class="relative shrink-0 flex">
                  <UIcon :name="link.icon" class="size-4" />
                  <!-- On the icon's corner, so it stays when the sidebar is collapsed -->
                  <span
                    v-if="indicators[link.to]?.tone === 'running'"
                    class="absolute -top-1.5 -right-1.5 size-2.5 rounded-full border-2
                           border-info border-t-transparent animate-spin"
                  />
                  <span
                    v-else-if="indicators[link.to]"
                    class="absolute -top-1 -right-1 size-2 rounded-full ring-2 ring-(--ui-bg)"
                    :class="{ success: 'bg-success', warning: 'bg-warning', running: '',
                              error: 'bg-error animate-pulse' }[indicators[link.to]!.tone]"
                  />
                </span>
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
          <!-- Styled like the navigation's entries above, which it sits among -->
          <UTooltip text="Log out" :disabled="!collapsed" :content="{ side: 'right' }">
            <button
              type="button"
              class="w-full flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm
                     hover-row text-muted hover:text-default"
              @click="setToken(null)"
            >
              <UIcon name="i-fa7-solid-right-from-bracket" class="size-4 shrink-0" />
              <span
                class="truncate transition-opacity duration-200"
                :class="collapsed ? 'opacity-0 w-0' : 'opacity-100'"
              >Log out</span>
            </button>
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
