/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Theme-aware tokens — every utility resolves through a CSS var
        // so the same JSX works in light + dark without a per-class
        // `dark:` prefix. See src/index.css for the variable bindings.
        bg:       "var(--bg)",
        paper:    "var(--bg-elev)",
        elev:     "var(--bg-elev)",
        sunken:   "var(--bg-sunken)",
        ink:      "var(--ink)",
        muted:    "var(--muted)",
        line:     "var(--line)",
        "line-soft": "var(--line-soft)",
        accent:   "var(--accent)",
        "accent-fg": "var(--accent-fg)",
        good:     "var(--good)",
        warn:     "var(--warn)",
        bad:      "var(--bad)",
        ghost:    "var(--ghost)",
        "code-bg":"var(--code-bg)",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo",
                "JetBrains Mono", "monospace"],
      },
      boxShadow: {
        soft: "0 1px 2px rgba(0,0,0,0.04), 0 4px 12px -4px rgba(0,0,0,0.06)",
      },
    },
  },
  plugins: [],
};
