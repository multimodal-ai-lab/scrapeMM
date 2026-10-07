<script setup lang="ts">
/**
 * How a set of URLs came out, as one stacked bar: passed, partial, failed, and what is
 * still pending. Square at the baseline, a 4px rounded data end, and a 2px surface gap
 * between segments. Every segment names itself on hover; the legend (or the row it
 * sits in) carries the numbers, so colour never has to.
 */
const props = withDefaults(defineProps<{
  passed: number
  partial: number
  failed: number
  pending?: number
  /** The bar's height in px; the Test page's overview bar is thicker than a row's */
  height?: number
  /** Scale against this many URLs instead of the bar's own total (for aligned rows) */
  scale?: number
  /** Whether the pending URLs are being worked on: their segment shimmers */
  active?: boolean
}>(), { pending: 0, height: 12, active: false })

const SEGMENTS = [
  { key: 'passed', label: 'Passed', color: 'var(--viz-good)' },
  { key: 'partial', label: 'Partial — media missing', color: 'var(--viz-warning)' },
  { key: 'failed', label: 'Failed', color: 'var(--viz-critical)' },
  { key: 'pending', label: 'Pending', color: 'var(--viz-track)' },
] as const

const total = computed(() => props.passed + props.partial + props.failed + props.pending)

const segments = computed(() => SEGMENTS
  .map((s) => ({ ...s, value: props[s.key] as number }))
  .filter((s) => s.value > 0))

const width = computed(() => {
  const base = props.scale || total.value
  return base ? `${(total.value / base) * 100}%` : '0%'
})

function share(value: number) {
  return total.value ? `${Math.round((value / total.value) * 100)}%` : '0%'
}
</script>

<template>
  <div class="w-full" :style="{ height: `${height}px` }">
    <div
      class="flex h-full gap-[2px] rounded-r-[4px] overflow-hidden transition-[width] duration-500"
      :style="{ width }"
    >
      <UTooltip
        v-for="s in segments" :key="s.key"
        :text="`${s.label}: ${s.value} (${share(s.value)})`"
      >
        <div
          class="h-full min-w-[2px] transition-[flex-grow] duration-500"
          :class="{ 'viz-working': active && s.key === 'pending' }"
          :style="{ flexGrow: s.value, flexBasis: 0, background: s.color }"
          :aria-label="`${s.label}: ${s.value}`"
        />
      </UTooltip>
    </div>
  </div>
</template>

<style scoped>
/* What is still being worked on: a soft highlight sweeping across the gray, so a running
   test reads as alive without a single number changing */
.viz-working {
  position: relative;
  overflow: hidden;
}

.viz-working::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent 0%,
    color-mix(in oklab, var(--ui-text-highlighted) 16%, transparent) 50%, transparent 100%);
  transform: translateX(-100%);
  animation: viz-sweep 1.8s cubic-bezier(0.4, 0, 0.2, 1) infinite;
}

@keyframes viz-sweep {
  to { transform: translateX(100%); }
}

@media (prefers-reduced-motion: reduce) {
  .viz-working::after {
    animation: viz-breathe 2.4s ease-in-out infinite;
    transform: none;
  }

  @keyframes viz-breathe {
    50% { opacity: 0.4; }
  }
}
</style>
