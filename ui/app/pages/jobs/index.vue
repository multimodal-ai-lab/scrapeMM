<script setup lang="ts">
/** The jobs overview is the History's Jobs view now: old links and bookmarks lead there. */
const route = useRoute()
await navigateTo({ path: '/history', query: route.query }, { replace: true })
</script>

<template>
  <div />
</template>
