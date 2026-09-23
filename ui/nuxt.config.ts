// The UI is a static single-page app. The Python server serves the built files and
// the API from the same origin, so there is one container, one port and no CORS.
export default defineNuxtConfig({
  compatibilityDate: '2026-09-01',
  ssr: false,
  modules: ['@nuxt/ui'],
  css: ['~/assets/css/main.css'],

  // Font Awesome 7, bundled rather than fetched: the server may sit on a network with
  // no outbound access, and an icon set that silently fails to load is worse than none.
  ui: {
    fonts: false,
  },
  icon: {
    mode: 'svg',
    clientBundle: {
      scan: true,
      includeCustomCollections: true,
    },
  },

  devtools: { enabled: false },

  app: {
    head: {
      title: 'scrapeMM',
      meta: [{ name: 'viewport', content: 'width=device-width, initial-scale=1' }],
    },
  },

  runtimeConfig: {
    public: {
      // Empty means "same origin", which is how the built UI is served. Point it
      // elsewhere with NUXT_PUBLIC_API_BASE when running the UI separately.
      apiBase: '',
    },
  },

  // `nuxt dev` serves the UI on 3000 and forwards the API to the Python server, so
  // the UI can be edited with hot reload against a real backend. `ws: true` is what
  // lets the Archive.today CAPTCHA panel's VNC socket through.
  nitro: {
    devProxy: {
      '/v1': {
        target: process.env.SCRAPEMM_DEV_API || 'http://localhost:8080',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
