<script setup lang="ts">
/**
 * One retrieval method on the dashboard.
 *
 * Built like the status cards above it: a prominent icon plate carries the identity,
 * the name is the largest text, and the whole card is tinted by state so the health of
 * the deployment reads without any of it being read.
 *
 * The verdict lives in the bottom-right corner, on its own row, with the retrieval
 * count beside it. Putting them in a row of their own is what keeps them from colliding
 * with a long integration name or a wrapped list of missing credentials. The reason
 * behind a verdict -- "Connected.", or a paragraph about a CAPTCHA -- is a tooltip on
 * the chip rather than a third block of text on every card.
 *
 * Every action lives in the hover menu, including Configure: seventeen cards with their
 * own buttons is a wall, and the colour already says which ones want attention.
 */
const props = defineProps<{ item: any, busy?: boolean, checking?: boolean }>()
const emit = defineEmits<{
  recheck: [name: string]
  toggle: [name: string, enabled: boolean]
}>()

const tone = computed(() => toneFor(props.item.state))
const icon = computed(() => integrationIcon(props.item.key))

// Required credentials are a blocker (red); optional ones only narrow what the
// integration can reach (amber), so they must not look like the same problem.
const chips = computed(() => [
  ...(props.item.missing_secrets || []).map((name: string) => ({ name, color: 'error' })),
  ...(props.item.missing_optional_secrets || [])
    .map((name: string) => ({ name, color: 'warning' })),
])

const menuItems = computed(() => {
  const actions = [
    {
      label: 'Re-check',
      icon: 'i-fa7-solid-rotate',
      onSelect: () => emit('recheck', props.item.key),
    },
  ]
  if (props.item.configurable) {
    actions.push({ label: 'Configure', icon: 'i-fa7-solid-sliders', to: '/secrets' })
  }
  actions.push({
    label: props.item.enabled ? 'Disable' : 'Enable',
    icon: props.item.enabled ? 'i-fa7-solid-ban' : 'i-fa7-solid-circle-play',
    onSelect: () => emit('toggle', props.item.key, !props.item.enabled),
  })
  return [actions]
})

const retrievals = computed(() => {
  const n = props.item.retrievals || 0
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
})

/** What the chip's tooltip says. Falls back to the state's own label. */
const explanation = computed(() => props.item.detail || tone.value.label)
</script>

<template>
  <div
    class="group relative surface-card rounded-xl p-3.5 flex flex-col h-full"
    :class="tone.surface"
  >
    <div class="flex items-start gap-3.5">
      <div class="icon-plate shrink-0 size-10 rounded-lg grid place-items-center">
        <UIcon :name="icon" class="size-5" :class="tone.text" />
      </div>

      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-2">
          <p class="font-semibold truncate flex-1" :title="item.name">{{ item.name }}</p>

          <!-- `variant="link"` keeps the neutral hover *plate* off a card that is
               already tinted by status; the icon brightens instead. -->
          <UDropdownMenu :items="menuItems" :content="{ align: 'end' }">
            <UButton
              icon="i-fa7-solid-ellipsis-vertical" color="neutral" variant="link"
              size="xs" square :loading="busy" aria-label="Actions"
              class="reveal-on-hover -mr-1 -my-1 opacity-0 text-dimmed
                     transition-[opacity,color] duration-150
                     hover:text-highlighted group-hover:opacity-100
                     focus-visible:opacity-100 data-[state=open]:opacity-100
                     data-[state=open]:text-highlighted"
            />
          </UDropdownMenu>
        </div>

        <p
          v-if="item.domains.length" class="text-xs text-dimmed truncate mt-1"
          :title="item.domains.join(', ')"
        >
          {{ item.domains.join(', ') }}
        </p>
        <p v-else class="text-xs text-dimmed capitalize mt-1">{{ item.kind }}</p>
      </div>
    </div>

    <!-- Missing credentials, one chip each: shorter to read than a sentence, and it
         names exactly what to go and set. -->
    <div v-if="chips.length && !checking" class="mt-2.5 flex flex-wrap gap-1">
      <UBadge
        v-for="chip in chips" :key="chip.name"
        :color="chip.color" variant="subtle" size="sm" class="font-mono"
        :title="chip.color === 'error' ? 'Required' : 'Optional — unlocks more content'"
      >
        <UIcon name="i-fa7-solid-xmark" class="size-2.5 mr-1" />
        {{ chip.name }}
      </UBadge>
    </div>

    <!-- The verdict, pinned to the bottom right. `mt-auto` keeps it there whatever the
         card above it contains, so a row of cards lines its chips up. -->
    <div class="mt-auto pt-2.5 flex items-center justify-end gap-2">
      <span
        v-if="item.retrievals && !checking"
        class="inline-flex items-center gap-1 text-xs text-dimmed tabular-nums"
        :title="`${item.retrievals} URL(s) retrieved with this method`"
      >
        <UIcon name="i-fa7-solid-arrow-down-long" class="size-2.5" />
        {{ retrievals }}
      </span>

      <USkeleton v-if="checking" class="h-5 w-24 rounded-full" />
      <!-- Nuxt UI's tooltip is built for one short line: the container is a fixed 24px
           (`h-6`, `items-center`) and the text span inside carries `truncate`. Both have
           to give, or a three-sentence explanation is clipped after a few words -- and
           reading it is the entire reason the detail lives here. -->
      <UTooltip
        v-else :text="explanation"
        :ui="{
          content: 'h-auto max-w-sm items-start py-1.5 leading-snug text-left',
          text: 'whitespace-normal overflow-visible text-clip break-words',
        }"
      >
        <UBadge
          :color="tone.color" variant="subtle" size="sm" :icon="tone.icon"
          :label="tone.label" class="cursor-help"
        />
      </UTooltip>
    </div>
  </div>
</template>
