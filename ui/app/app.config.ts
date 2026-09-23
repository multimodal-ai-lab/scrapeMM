export default defineAppConfig({
  ui: {
    colors: {
      // Nuxt UI's default neutral is `slate`, which is a blue-tinted gray -- that tint
      // is what made the whole surface read as dark blue. `neutral` is a true gray, so
      // the only colour left on the page is colour that means something.
      neutral: 'neutral',
      // Interactive elements get one accent, and it is not a status colour.
      primary: 'blue',
      // Status colours, kept distinct from the accent so they still read as signals.
      success: 'green',
      warning: 'amber',
      error: 'red',
      info: 'sky',
    },
    // Cards read as solid panels rather than outlined frames, matching the dashboard.
    card: {
      defaultVariants: {
        variant: 'soft',
      },
    },
  },
})
