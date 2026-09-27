<script setup lang="ts">
/**
 * Columns of one magnitude in one hue: a distribution (how many URLs took how long) or
 * a series over time (coverage per run). Columns are at most 24px wide, grow from one
 * square baseline and end in a 4px rounding; the grid is three faint lines. By default
 * only the highlighted column (the latest run, say) is labelled directly -- the rest say
 * their value on hover. With `labels="all"`, every column carries its value on top, and
 * the axis labels go: they would only repeat what the columns say.
 */
const props = withDefaults(defineProps<{
  /** `color` overrides the one hue, e.g. to mark a range as fast or slow */
  items: { label: string, value: number, tooltip?: string, color?: string }[]
  /** How to write a value: on the axis and in tooltips */
  format?: (value: number) => string
  /** A fixed top of the scale (1 for shares), otherwise the largest value */
  max?: number
  height?: number
  /** Index of the column to label directly, e.g. the latest run */
  highlight?: number | null
  /** Which columns carry their value on top: the highlighted one, or all */
  labels?: 'highlight' | 'all'
}>(), { height: 160, highlight: null, labels: 'highlight' })

// Room above the tallest column for its label, so it never runs into what is above
const LABEL_ROOM = 16

function labelled(index: number) {
  return props.labels === 'all' || index === props.highlight
}

const top = computed(() => props.max ?? Math.max(1, ...props.items.map((i) => i.value)))
const grid = computed(() => [1, 0.5].map((f) => ({ f, label: write(top.value * f) })))

function write(value: number) {
  return props.format ? props.format(value) : `${Math.round(value)}`
}
</script>

<template>
  <div class="w-full">
    <div
      class="relative"
      :style="{ height: `${height}px`, marginTop: labels === 'all' ? `${LABEL_ROOM}px` : undefined }"
    >
      <!-- Recessive grid: the top and the middle of the scale, plus the baseline -->
      <div
        v-for="g in grid" :key="g.f"
        class="absolute inset-x-0 border-t border-default/60"
        :style="{ bottom: `${g.f * 100}%` }"
      >
        <span
          v-if="labels !== 'all'"
          class="absolute -top-2.5 left-0 text-[10px] leading-none text-dimmed bg-default/0"
        >{{ g.label }}</span>
      </div>
      <div class="absolute inset-x-0 bottom-0 border-t border-default" />

      <div class="absolute inset-0 flex items-end gap-1" :class="labels === 'all' ? '' : 'pl-8'">
        <UTooltip
          v-for="(item, index) in items" :key="item.label + index"
          :text="item.tooltip || `${item.label}: ${write(item.value)}`"
        >
          <!-- The hit target is the whole column slot, not just the bar -->
          <div class="flex-1 h-full flex flex-col justify-end items-center min-w-0 group">
            <div
              class="relative w-full max-w-6 rounded-t-[4px] transition-[height,opacity] duration-500
                     group-hover:opacity-80"
              :style="{
                height: `${top ? Math.max(item.value > 0 ? 2 : 0, (item.value / top) * 100) : 0}%`,
                background: item.color || 'var(--viz-seq)',
              }"
            >
              <!-- On top of the column, outside the flow: it never squeezes the column -->
              <span
                v-if="labelled(index)"
                class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 whitespace-nowrap
                       text-[11px] leading-none font-medium text-highlighted tabular-nums"
              >{{ write(item.value) }}</span>
            </div>
          </div>
        </UTooltip>
      </div>
    </div>
    <div class="flex gap-1 mt-1.5" :class="labels === 'all' ? '' : 'pl-8'">
      <span
        v-for="(item, index) in items" :key="item.label + index"
        class="flex-1 min-w-0 text-center text-[10px] text-dimmed truncate"
      >{{ item.label }}</span>
    </div>
  </div>
</template>
