<script setup lang="ts">
/**
 * A ranked list as horizontal bars: which methods carried the URLs, which errors ended
 * them. Name on the left, count on the right in text ink, the bar in one hue between
 * them. The bars share one scale, so the longest is the most frequent.
 */
const props = defineProps<{
  items: { label: string, value: number, hint?: string, color?: string }[]
  total?: number
  /** One colour for every bar, e.g. red for failures */
  color?: string
}>()

const top = computed(() => Math.max(1, ...props.items.map((i) => i.value)))
</script>

<template>
  <div class="space-y-2">
    <UTooltip
      v-for="item in items" :key="item.label"
      :text="item.hint || `${item.label}: ${item.value}${total ? ` of ${total}` : ''}`"
    >
      <div class="grid grid-cols-[minmax(0,10rem)_1fr_auto] items-center gap-3 text-sm">
        <span class="truncate text-muted" :title="item.label">{{ item.label }}</span>
        <div class="h-3">
          <div
            class="h-full rounded-r-[4px] transition-[width] duration-500"
            :style="{ width: `${(item.value / top) * 100}%`,
                      background: item.color || color || 'var(--viz-seq)' }"
          />
        </div>
        <span class="tabular-nums text-highlighted text-xs">{{ item.value }}</span>
      </div>
    </UTooltip>
    <p v-if="!items.length" class="text-sm text-dimmed">Nothing to show.</p>
  </div>
</template>
