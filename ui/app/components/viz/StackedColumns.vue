<script setup lang="ts">
/**
 * Counts over time, stacked by group: one column per time bucket, one segment per
 * series. Segments are separated by a 2px surface gap, the column ends in a 4px rounding
 * and grows from one square baseline. The grid is recessive: the top of the scale, its
 * middle and the baseline. Hovering a column (its whole slot, not just the bar) shows
 * every segment's count; the legend and a table view elsewhere carry the numbers, so
 * colour never has to.
 */
const props = withDefaults(defineProps<{
  /** The label of each bucket, in order */
  labels: string[]
  /** A longer label for the tooltip (e.g. with the date), defaults to `labels` */
  titles?: string[]
  /** Bottom to top; each with one count per bucket */
  series: { key: string, label: string, color: string, counts: number[] }[]
  height?: number
  /** At most this many axis labels, evenly spaced */
  maxLabels?: number
}>(), { height: 240, maxLabels: 8 })

const totals = computed(() => props.labels.map((_, i) =>
  props.series.reduce((sum, s) => sum + (s.counts[i] || 0), 0)))

/** A round top for the scale: 1, 2, 2.5 or 5 times a power of ten */
const top = computed(() => {
  const max = Math.max(1, ...totals.value)
  const power = 10 ** Math.floor(Math.log10(max))
  return [1, 1.5, 2, 2.5, 5, 10].map((f) => f * power).find((v) => v >= max) || max
})

const grid = computed(() => [1, 0.5].map((f) => ({ f, label: compact(top.value * f) })))

// Every k-th label, so they never collide however many buckets there are -- and
// however narrow the chart is: about one label per 72px of width
const root = ref<HTMLElement | null>(null)
const width = ref(0)
let observer: ResizeObserver | null = null
onMounted(() => {
  observer = new ResizeObserver(([entry]) => { width.value = entry!.contentRect.width })
  if (root.value) observer.observe(root.value)
})
onBeforeUnmount(() => observer?.disconnect())
const step = computed(() => {
  const fit = width.value ? Math.max(2, Math.floor(width.value / 72)) : props.maxLabels
  return Math.max(1, Math.ceil(props.labels.length / Math.min(props.maxLabels, fit)))
})

function compact(n: number): string {
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${+(n / 1_000).toFixed(2)}k`
  return String(Math.round(n))
}

/** The segments of one column, top first (the DOM order of a column stacked downward) */
function segments(i: number) {
  return props.series
    .map((s) => ({ key: s.key, label: s.label, color: s.color, value: s.counts[i] || 0 }))
    .filter((s) => s.value > 0)
    .reverse()
}
</script>

<template>
  <div ref="root" class="w-full">
    <div class="relative" :style="{ height: `${height}px` }">
      <div
        v-for="g in grid" :key="g.f"
        class="absolute inset-x-0 border-t border-default/60"
        :style="{ bottom: `${g.f * 100}%` }"
      >
        <span class="absolute -top-2.5 left-0 text-[10px] leading-none text-dimmed tabular-nums">
          {{ g.label }}
        </span>
      </div>
      <div class="absolute inset-x-0 bottom-0 border-t border-default" />

      <div class="absolute inset-0 pl-8 flex items-end gap-[2px] sm:gap-1">
        <UTooltip
          v-for="(label, i) in labels" :key="i"
          :content="{ side: 'top' }" :delay-duration="0"
          :ui="{ content: 'h-auto px-3 py-2' }"
        >
          <!-- The hit target is the whole slot, so thin columns are easy to hover -->
          <div class="flex-1 h-full min-w-0 flex flex-col justify-end items-center group">
            <div
              class="w-full max-w-7 flex flex-col gap-[2px] overflow-hidden rounded-t-[4px]
                     transition-[height] duration-500 group-hover:opacity-85"
              :style="{ height: `${(totals[i]! / top) * 100}%` }"
            >
              <div
                v-for="s in segments(i)" :key="s.key"
                class="w-full min-h-[2px]"
                :style="{ flexGrow: s.value, flexBasis: 0, background: s.color }"
              />
            </div>
          </div>
          <template #content>
            <div class="text-xs space-y-1 py-0.5 min-w-40">
              <p class="font-medium text-highlighted">{{ (titles || labels)[i] }}</p>
              <p v-if="!totals[i]" class="text-dimmed">No retrievals</p>
              <template v-else>
                <div
                  v-for="s in [...segments(i)].reverse()" :key="s.key"
                  class="flex items-center gap-2"
                >
                  <span class="size-2.5 rounded-sm shrink-0" :style="{ background: s.color }" />
                  <span class="text-muted flex-1">{{ s.label }}</span>
                  <span class="tabular-nums text-highlighted">{{ s.value }}</span>
                </div>
                <div class="flex items-center gap-2 border-t border-default pt-1">
                  <span class="text-muted flex-1">Total</span>
                  <span class="tabular-nums text-highlighted">{{ totals[i] }}</span>
                </div>
              </template>
            </div>
          </template>
        </UTooltip>
      </div>
    </div>
    <div class="pl-8 flex gap-[2px] sm:gap-1 mt-1.5">
      <span
        v-for="(label, i) in labels" :key="i"
        class="flex-1 min-w-0 text-[10px] text-dimmed whitespace-nowrap overflow-visible"
        :class="i % step === 0 ? '' : 'invisible'"
      >{{ label }}</span>
    </div>
  </div>
</template>
