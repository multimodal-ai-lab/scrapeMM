<script setup lang="ts">
/**
 * Serper's search form and results, in Serper's own terms: the form holds Serper's
 * parameters under their own names, and the results are drawn from Serper's JSON as
 * the server passes it on (`organic[].link`, `images[].imageUrl`, ...).
 */
const props = defineProps<{
  provider: { name: string, label: string, configured: boolean, enabled: boolean }
}>()

const api = useApi()
const route = useRoute()

type SerperType = 'search' | 'images'

// Reka UI (behind USelect) refuses an empty-string option value, so "any" stands for
// Serper's default and is left out of the query
const ANY = 'any'
const TYPES = [
  { label: 'Web', value: 'search', icon: 'i-fa7-solid-globe' },
  { label: 'Images', value: 'images', icon: 'i-fa7-solid-image' },
]
const COUNTS = [10, 20, 50, 100].map((n) => ({ label: `${n} results`, value: n }))
const PERIODS = [
  { label: 'Any time', value: ANY },
  { label: 'Past hour', value: 'qdr:h' },
  { label: 'Past day', value: 'qdr:d' },
  { label: 'Past week', value: 'qdr:w' },
  { label: 'Past month', value: 'qdr:m' },
  { label: 'Past year', value: 'qdr:y' },
]

const q = ref((route.query.q as string) || '')
const type = ref<SerperType>(route.query.type === 'images' ? 'images' : 'search')
const num = ref(10)
const tbs = ref(ANY)
const gl = ref('')
const hl = ref('')
const location = ref('')
// Only results from before this day (YYYY-MM-DD, as the date input gives it)
const before = ref('')
// Websites to leave out, separated by commas or spaces
const excludeSites = ref('')
const page = ref(1)

const running = ref(false)
const error = ref('')
const response = ref<any>(null)
// What the results on screen answer, which the form may no longer say
const shown = ref<{ type: SerperType, num: number } | null>(null)
const elapsed = ref<number | null>(null)

const results = computed<any[]>(() => {
  if (!response.value || !shown.value) return []
  return (shown.value.type === 'images' ? response.value.images : response.value.organic) ?? []
})
const hasMore = computed(() => !!shown.value && results.value.length >= shown.value.num)

async function run(atPage = 1) {
  const terms = q.value.trim()
  if (!terms || running.value) return
  running.value = true
  error.value = ''

  const body: Record<string, unknown> = { q: terms, type: type.value, num: num.value, page: atPage }
  if (gl.value.trim()) body.gl = gl.value.trim().toLowerCase()
  if (hl.value.trim()) body.hl = hl.value.trim().toLowerCase()
  if (location.value.trim()) body.location = location.value.trim()
  // Both restrict the date, and Serper would get two contradicting ranges: the day wins
  const sites = excludeSites.value.split(/[\s,]+/).filter(Boolean)
  if (sites.length) body.exclude_sites = sites
  if (before.value) body.before = before.value
  else if (tbs.value !== ANY) body.tbs = tbs.value

  const started = performance.now()
  try {
    response.value = await api.post(`/v1/search/${props.provider.name}`, body)
    shown.value = { type: type.value, num: num.value }
    page.value = atPage
    elapsed.value = (performance.now() - started) / 1000
  } catch (e: any) {
    error.value = e.message
  } finally {
    running.value = false
  }
}

function searchFor(terms: string) {
  q.value = terms
  run(1)
}

// Switching between web and images answers the same question differently: do it at once
watch(type, () => {
  if (shown.value) run(1)
})

function retrieve(url: string) {
  navigateTo({ path: '/playground/retrieval', query: { url } })
}

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}
</script>

<template>
  <div class="space-y-5">
    <UCard>
      <form class="space-y-4" @submit.prevent="run(1)">
        <div class="flex gap-2">
          <UInput
            v-model="q" size="lg" icon="i-fa7-solid-magnifying-glass" class="flex-1"
            placeholder="What are you looking for?" autofocus
          />
          <UButton
            type="submit" size="lg" icon="i-fa7-solid-magnifying-glass" label="Search"
            :loading="running" :disabled="!q.trim() || !provider.configured || !provider.enabled"
          />
        </div>
        <div class="flex flex-wrap items-end gap-4">
          <UTabs v-model="type" :items="TYPES" :content="false" size="sm" class="mb-0.5" />
          <UFormField label="Results">
            <USelect v-model="num" :items="COUNTS" class="w-32" />
          </UFormField>
          <UFormField label="Time">
            <USelect
              v-model="tbs" :items="PERIODS" class="w-36" :disabled="!!before"
              :title="before ? 'Replaced by the Before date' : undefined"
            />
          </UFormField>
          <UFormField label="Before" hint="excl.">
            <UInput
              v-model="before" type="date" class="w-40"
              title="Only results from before this day; the day itself is left out"
            />
          </UFormField>
          <UFormField label="Country" hint="gl">
            <UInput v-model="gl" placeholder="us" maxlength="8" class="w-20" />
          </UFormField>
          <UFormField label="Language" hint="hl">
            <UInput v-model="hl" placeholder="en" maxlength="8" class="w-20" />
          </UFormField>
          <UFormField label="Location">
            <UInput v-model="location" placeholder="Berlin, Germany" class="w-48" />
          </UFormField>
          <UFormField label="Exclude sites" class="flex-1 min-w-56">
            <UInput
              v-model="excludeSites" icon="i-fa7-solid-ban" class="w-full"
              placeholder="snopes.com, politifact.com"
              title="Results from these websites and their subdomains are left out"
            />
          </UFormField>
        </div>
      </form>
    </UCard>

    <UAlert v-if="error" color="error" variant="subtle" :description="error" />

    <template v-if="response && shown">
      <p class="text-xs text-dimmed">
        {{ plural(results.length, 'result') }} on page {{ page }}
        <span v-if="elapsed != null"> · {{ elapsed.toFixed(1) }}s</span>
        <span v-if="response.credits != null"> · {{ plural(response.credits, 'credit') }}</span>
      </p>

      <!-- Web: Google's own blocks first, as it shows them -->
      <template v-if="shown.type === 'search'">
        <div v-if="response.answerBox" class="surface-card rounded-xl p-3 space-y-1">
          <p class="text-xs font-semibold uppercase tracking-wider text-dimmed">Answer</p>
          <p v-if="response.answerBox.title" class="font-medium">{{ response.answerBox.title }}</p>
          <p class="text-sm">{{ response.answerBox.answer || response.answerBox.snippet }}</p>
          <a
            v-if="response.answerBox.link" :href="response.answerBox.link"
            target="_blank" rel="noopener noreferrer" class="inline-flex max-w-full text-xs hover:underline"
          >
            <UrlLabel :url="response.answerBox.link" />
          </a>
        </div>

        <div v-if="response.knowledgeGraph" class="surface-card rounded-xl p-3 flex gap-3">
          <img
            v-if="response.knowledgeGraph.imageUrl" :src="response.knowledgeGraph.imageUrl"
            alt="" referrerpolicy="no-referrer" class="size-20 rounded-lg object-cover shrink-0"
          >
          <div class="min-w-0 space-y-1">
            <p class="font-medium">
              {{ response.knowledgeGraph.title }}
              <span v-if="response.knowledgeGraph.type" class="text-xs font-normal text-dimmed">
                {{ response.knowledgeGraph.type }}
              </span>
            </p>
            <p v-if="response.knowledgeGraph.description" class="text-sm text-muted">
              {{ response.knowledgeGraph.description }}
            </p>
            <dl
              v-if="response.knowledgeGraph.attributes"
              class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-xs"
            >
              <template v-for="(value, key) in response.knowledgeGraph.attributes" :key="key">
                <dt class="text-dimmed">{{ key }}</dt>
                <dd class="truncate" :title="String(value)">{{ value }}</dd>
              </template>
            </dl>
            <a
              v-if="response.knowledgeGraph.website" :href="response.knowledgeGraph.website"
              target="_blank" rel="noopener noreferrer" class="inline-flex max-w-full text-xs hover:underline"
            >
              <UrlLabel :url="response.knowledgeGraph.website" />
            </a>
          </div>
        </div>

        <ul class="space-y-2">
          <li
            v-for="(r, i) in results" :key="i"
            class="group surface-card rounded-xl p-3 flex items-start justify-between gap-3"
          >
            <div class="min-w-0 space-y-1">
              <a
                :href="r.link" target="_blank" rel="noopener noreferrer"
                class="block font-medium text-highlighted hover:underline"
              >{{ r.title || r.link }}</a>
              <div class="flex items-center gap-1.5 text-xs min-w-0">
                <UrlLabel :url="r.link" />
                <span v-if="r.date" class="text-dimmed shrink-0">· {{ r.date }}</span>
              </div>
              <p v-if="r.snippet" class="text-sm text-muted">{{ r.snippet }}</p>
              <div v-if="r.sitelinks?.length" class="flex flex-wrap gap-x-3 gap-y-1 text-xs">
                <a
                  v-for="s in r.sitelinks" :key="s.link" :href="s.link"
                  target="_blank" rel="noopener noreferrer" class="text-primary hover:underline"
                >{{ s.title }}</a>
              </div>
            </div>
            <!-- Shown on hover, like every other row action in the app. -->
            <UButton
              size="xs" color="neutral" variant="soft" icon="i-fa7-solid-play" label="Retrieve"
              title="Retrieve this page"
              class="reveal-on-hover shrink-0 opacity-0 transition-opacity duration-150
                     group-hover:opacity-100 focus-visible:opacity-100"
              @click="retrieve(r.link)"
            />
          </li>
        </ul>

        <div v-if="response.topStories?.length" class="space-y-2">
          <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">Top stories</h2>
          <ul class="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            <li v-for="(s, i) in response.topStories" :key="i">
              <a
                :href="s.link" target="_blank" rel="noopener noreferrer"
                class="surface-card rounded-xl p-3 flex gap-3 h-full"
              >
                <img
                  v-if="s.imageUrl" :src="s.imageUrl" alt="" loading="lazy"
                  referrerpolicy="no-referrer" class="size-14 rounded-md object-cover shrink-0"
                >
                <div class="min-w-0">
                  <p class="text-sm font-medium line-clamp-2">{{ s.title }}</p>
                  <p class="text-xs text-dimmed truncate">
                    {{ s.source }}<span v-if="s.date"> · {{ s.date }}</span>
                  </p>
                </div>
              </a>
            </li>
          </ul>
        </div>

        <div v-if="response.peopleAlsoAsk?.length" class="space-y-2">
          <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">People also ask</h2>
          <details
            v-for="(item, i) in response.peopleAlsoAsk" :key="i"
            class="surface-card rounded-xl px-3 py-2 text-sm"
          >
            <summary class="font-medium">{{ item.question }}</summary>
            <p v-if="item.snippet" class="text-muted mt-1">{{ item.snippet }}</p>
            <a
              v-if="item.link" :href="item.link" target="_blank" rel="noopener noreferrer"
              class="inline-flex max-w-full text-xs mt-1 hover:underline"
            >
              <UrlLabel :url="item.link" />
            </a>
          </details>
        </div>

        <div v-if="response.relatedSearches?.length" class="space-y-2">
          <h2 class="text-xs font-semibold uppercase tracking-wider text-dimmed">Related searches</h2>
          <div class="flex flex-wrap gap-2">
            <UButton
              v-for="(r, i) in response.relatedSearches" :key="i"
              size="xs" color="neutral" variant="soft" icon="i-fa7-solid-magnifying-glass"
              :label="r.query" @click="searchFor(r.query)"
            />
          </div>
        </div>
      </template>

      <!-- Images: the picture first, where it comes from underneath -->
      <ul v-else class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
        <li
          v-for="(img, i) in results" :key="i"
          class="surface-card rounded-xl overflow-hidden flex flex-col"
        >
          <a
            :href="img.link || img.imageUrl" target="_blank" rel="noopener noreferrer"
            class="block bg-(--surface-sunken)"
          >
            <img
              :src="img.thumbnailUrl || img.imageUrl" :alt="img.title || ''" loading="lazy"
              referrerpolicy="no-referrer" class="w-full aspect-[4/3] object-cover"
            >
          </a>
          <div class="p-2.5 min-w-0 flex-1 flex flex-col gap-1">
            <p class="text-sm font-medium line-clamp-2" :title="img.title">{{ img.title }}</p>
            <p class="text-xs text-dimmed truncate">
              {{ img.source || img.domain }}<span v-if="img.imageWidth && img.imageHeight">
                · {{ img.imageWidth }}×{{ img.imageHeight }}</span>
            </p>
            <div class="flex flex-wrap gap-x-2 mt-auto pt-1">
              <UButton
                v-if="img.imageUrl" size="xs" color="neutral" variant="link" class="px-0"
                icon="i-fa7-solid-image" label="Image" :to="img.imageUrl" target="_blank"
              />
              <UButton
                v-if="img.link" size="xs" color="neutral" variant="link" class="px-0"
                icon="i-fa7-solid-play" label="Retrieve" title="Retrieve the page"
                @click="retrieve(img.link)"
              />
            </div>
          </div>
        </li>
      </ul>

      <UCard v-if="!results.length">
        <p class="text-sm text-muted">No results. Try other terms, or fewer filters.</p>
      </UCard>

      <div v-if="page > 1 || hasMore" class="flex items-center justify-center gap-3">
        <UButton
          color="neutral" variant="soft" icon="i-fa7-solid-chevron-left" label="Previous"
          :disabled="page <= 1 || running" @click="run(page - 1)"
        />
        <span class="text-sm text-muted">Page {{ page }}</span>
        <UButton
          color="neutral" variant="soft" trailing-icon="i-fa7-solid-chevron-right" label="Next"
          :disabled="!hasMore || running" @click="run(page + 1)"
        />
      </div>
    </template>
  </div>
</template>
