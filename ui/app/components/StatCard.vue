<script setup lang="ts">
/**
 * One number on the dashboard's status row.
 *
 * Laid out so it can be read in one pass rather than parsed: the icon plate says what
 * kind of thing this is, the value is the largest element on the card, and the label
 * sits directly under it so the number is never orphaned from its meaning. The tone
 * colours the whole card, not just the figure, so a card that wants attention is
 * visible without reading any of it.
 *
 * Where a number is really a proportion -- a success rate, a disk filling up -- `ring`
 * draws it around the icon, so the shape carries the meaning before the digits do.
 */
const props = defineProps<{
  label: string
  value: string
  icon: string
  detail?: string
  tone?: 'neutral' | 'success' | 'warning' | 'error' | 'info'
  /** 0..1. Draws a progress ring around the icon when given. */
  ring?: number | null
  to?: string
}>()

// Resolved rather than named as a string: `<component :is="'NuxtLink'">` does not
// resolve the auto-imported component and silently renders an inert <nuxtlink> element,
// which is how these tiles ended up looking like links without being any.
const NuxtLink = resolveComponent('NuxtLink')

const surface = computed(() =>
  props.tone && props.tone !== 'neutral' ? `tone-${props.tone}` : '')

const iconColor = computed(() => ({
  success: 'text-success',
  warning: 'text-warning',
  error: 'text-error',
  info: 'text-info',
  neutral: 'text-dimmed',
}[props.tone || 'neutral']))

// Geometry for the ring. A 44px box with a 20px radius leaves room for the icon.
const RADIUS = 19
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

const dash = computed(() => {
  const fraction = Math.max(0, Math.min(1, props.ring ?? 0))
  return `${fraction * CIRCUMFERENCE} ${CIRCUMFERENCE}`
})

const hasRing = computed(() => props.ring != null)
</script>

<template>
  <component
    :is="to ? NuxtLink : 'div'" :to="to"
    class="surface-card rounded-xl p-3.5 flex items-center gap-3.5"
    :class="surface"
  >
    <!-- Ring form: the proportion is the plate. -->
    <div v-if="hasRing" class="relative shrink-0 size-11 grid place-items-center">
      <svg class="absolute inset-0 -rotate-90" viewBox="0 0 44 44" aria-hidden="true">
        <circle
          cx="22" cy="22" :r="RADIUS" fill="none" stroke-width="3.5"
          class="stroke-current opacity-15"
        />
        <circle
          cx="22" cy="22" :r="RADIUS" fill="none" stroke-width="3.5"
          stroke-linecap="round" :stroke-dasharray="dash"
          class="stroke-current transition-[stroke-dasharray] duration-500"
          :class="iconColor"
        />
      </svg>
      <UIcon :name="icon" class="size-4 relative" :class="iconColor" />
    </div>

    <!-- Plain form -->
    <div v-else class="icon-plate shrink-0 size-10 rounded-lg grid place-items-center">
      <UIcon :name="icon" class="size-5" :class="iconColor" />
    </div>

    <div class="min-w-0">
      <p class="text-2xl font-semibold leading-none tabular-nums">{{ value }}</p>
      <!-- The label wraps rather than truncating: a cut-off "Awaiting CA…" defeats the
           point of a card meant to be understood at a glance. -->
      <p class="text-sm font-medium mt-1.5 leading-tight">{{ label }}</p>
      <p
        v-if="detail" class="text-xs text-dimmed mt-0.5 leading-tight line-clamp-2"
        :title="detail"
      >
        {{ detail }}
      </p>
    </div>
  </component>
</template>
