<script setup lang="ts">
/**
 * Scraped Markdown, rendered.
 *
 * Reading a page as raw Markdown defeats the point of having scraped it -- the whole
 * value is seeing what the page said. Rendered it is, with two safeguards:
 *
 * * The Markdown comes from an arbitrary website, so it is **untrusted**. `marked` is
 *   told not to emit raw HTML, and the result is additionally stripped of scripts and
 *   event handlers before it reaches the DOM.
 * * Links open in a new tab with `rel="noopener noreferrer"`, so a scraped page cannot
 *   navigate this one away.
 */
import { marked } from 'marked'

const props = defineProps<{ source: string }>()

const html = computed(() => {
  const raw = marked.parse(props.source ?? '', {
    async: false,
    gfm: true,
    breaks: false,
  }) as string
  return sanitize(raw)
})

/**
 * A deliberately conservative pass over the rendered output. `marked` is configured not
 * to pass HTML through, so this is a second line of defence rather than the only one.
 */
function sanitize(input: string): string {
  if (!import.meta.client) return ''
  const template = document.createElement('template')
  template.innerHTML = input

  for (const element of [...template.content.querySelectorAll('*')]) {
    const tag = element.tagName.toLowerCase()
    if (tag === 'script' || tag === 'style' || tag === 'iframe' || tag === 'object'
        || tag === 'embed' || tag === 'form') {
      element.remove()
      continue
    }
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase()
      const value = attribute.value.trim().toLowerCase()
      // Event handlers, and any URL that could execute something
      if (name.startsWith('on')
          || ((name === 'href' || name === 'src')
              && (value.startsWith('javascript:') || value.startsWith('data:text/html')))) {
        element.removeAttribute(attribute.name)
      }
    }
    if (tag === 'a') {
      element.setAttribute('target', '_blank')
      element.setAttribute('rel', 'noopener noreferrer')
    }
  }
  return template.innerHTML
}
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- sanitized above -->
  <div class="scraped-markdown text-sm" v-html="html" />
</template>

<style scoped>
/* Typography for scraped content. Deliberately modest: this is someone else's page
   being previewed, not this app's own prose. */
.scraped-markdown :deep(h1),
.scraped-markdown :deep(h2),
.scraped-markdown :deep(h3),
.scraped-markdown :deep(h4) {
  font-weight: 600;
  line-height: 1.3;
  margin: 1.2em 0 0.5em;
}
.scraped-markdown :deep(h1) { font-size: 1.4em; }
.scraped-markdown :deep(h2) { font-size: 1.2em; }
.scraped-markdown :deep(h3) { font-size: 1.05em; }
.scraped-markdown :deep(p) { margin: 0.7em 0; line-height: 1.65; }
.scraped-markdown :deep(ul),
.scraped-markdown :deep(ol) { margin: 0.7em 0; padding-left: 1.4em; }
.scraped-markdown :deep(ul) { list-style: disc; }
.scraped-markdown :deep(ol) { list-style: decimal; }
.scraped-markdown :deep(li) { margin: 0.25em 0; }
.scraped-markdown :deep(a) {
  color: var(--ui-primary);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.scraped-markdown :deep(code) {
  font-size: 0.9em;
  padding: 0.1em 0.35em;
  border-radius: 0.25rem;
  background: var(--ui-bg-elevated);
}
.scraped-markdown :deep(pre) {
  overflow-x: auto;
  padding: 0.75rem;
  border-radius: 0.375rem;
  background: var(--ui-bg-elevated);
  margin: 0.8em 0;
}
.scraped-markdown :deep(pre code) { background: none; padding: 0; }
.scraped-markdown :deep(blockquote) {
  border-left: 3px solid var(--ui-border-accented);
  padding-left: 0.9em;
  margin: 0.8em 0;
  color: var(--ui-text-muted);
}
.scraped-markdown :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 0.375rem;
}
.scraped-markdown :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 0.8em 0;
  font-size: 0.95em;
}
.scraped-markdown :deep(th),
.scraped-markdown :deep(td) {
  border: 1px solid var(--ui-border);
  padding: 0.4em 0.6em;
  text-align: left;
}
.scraped-markdown :deep(hr) {
  border: 0;
  border-top: 1px solid var(--ui-border);
  margin: 1.2em 0;
}
.scraped-markdown :deep(> *:first-child) { margin-top: 0; }
.scraped-markdown :deep(> *:last-child) { margin-bottom: 0; }
</style>
