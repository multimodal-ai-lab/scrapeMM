<script setup lang="ts">
/** The Playground has two tabs; its own address opens the first. The query comes along,
 *  so an older link like /playground?url=... still prefills the retrieval. */
definePageMeta({
  redirect: (to) => ({ path: '/playground/retrieval', query: to.query }),
})
</script>

<template>
  <div />
</template>
