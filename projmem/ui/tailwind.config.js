/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Minimal palette aligned with the brief's "shadcn/ui look".
        // Real shadcn primitives land in Step 7 when the graph + inspector
        // need dialogs / popovers / tabs. Step 6 stays headless-light.
        ink:    "#0a0a0a",
        paper:  "#fafafa",
        muted:  "#737373",
        line:   "#e5e5e5",
        accent: "#2563eb",
        warn:   "#d97706",
        bad:    "#dc2626",
        good:   "#16a34a",
        ghost:  "#a3a3a3",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
