<script setup lang="ts">
/**
 * A URL as a person scans it: the site first and prominent, the rest of the address
 * quieter, and no "https://" in front of every line. The full URL stays in the tooltip.
 */
const props = defineProps<{ url: string }>()

const parts = computed(() => {
  try {
    const u = new URL(props.url)
    const rest = `${u.pathname === '/' ? '' : u.pathname}${u.search}${u.hash}`
    return { host: u.host.replace(/^www\./, ''), rest: decodeSafely(rest) }
  } catch {
    return { host: props.url.replace(/^[a-z]+:\/\//i, ''), rest: '' }
  }
})

/** Percent-encoding is for machines; %C3%A4 reads better as ä */
function decodeSafely(value: string) {
  try {
    return decodeURI(value)
  } catch {
    return value
  }
}
</script>

<template>
  <span class="truncate min-w-0" :title="url">
    <span class="font-medium text-highlighted">{{ parts.host }}</span><span class="text-muted">{{ parts.rest }}</span>
  </span>
</template>
