export default defineAppConfig({
  ui: {
    colors: {
      // Nuxt UI's default neutral is `slate`, which is a blue-tinted gray -- that tint
      // is what made the whole surface read as dark blue. `neutral` is a true gray, so
      // the only colour left on the page is colour that means something.
      neutral: 'neutral',
      // Interactive elements get one accent, and it is not a status colour: the logo's
      // blue (see `brand` in assets/css/main.css).
      primary: 'brand',
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
    // Disabled buttons are gray, not a faded accent: blue says "you can click this".
    // Important (`!`) so they win over the theme's own `disabled:bg-primary`.
    button: {
      compoundVariants: [
        {
          color: 'primary',
          variant: 'solid',
          class: 'disabled:bg-accented! aria-disabled:bg-accented! disabled:text-muted! aria-disabled:text-muted!',
        },
        ...(['outline', 'subtle'] as const).map((variant) => ({
          color: 'primary' as const,
          variant,
          class: 'disabled:bg-transparent! aria-disabled:bg-transparent! disabled:text-dimmed! aria-disabled:text-dimmed! disabled:ring-accented! aria-disabled:ring-accented!',
        })),
        ...(['soft', 'ghost', 'link'] as const).map((variant) => ({
          color: 'primary' as const,
          variant,
          class: 'disabled:bg-transparent! aria-disabled:bg-transparent! disabled:text-dimmed! aria-disabled:text-dimmed!',
        })),
      ],
    },
    // The active tab's label sits beside the accent pill, not inside it: white by name
    // (see the white-on-accent rule in assets/css/main.css)
    tabs: {
      compoundVariants: [
        { color: 'primary', variant: 'pill', class: { trigger: 'data-[state=active]:text-white!' } },
      ],
    },
  },
})
