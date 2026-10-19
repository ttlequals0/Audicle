/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ["var(--audicle-font-mono)"],
        sans: ["var(--audicle-font-sans)"],
      },
      colors: {
        ink: "var(--audicle-color-background)",
        paper: "var(--audicle-color-paper)",
        surface: "var(--audicle-color-surface)",
        line: "var(--audicle-color-line)",
        mute: "var(--audicle-color-text-mute)",
        dim: "var(--audicle-color-text-dim)",
        fg: "var(--audicle-color-text)",
        accent: "var(--audicle-color-primary)",
        danger: "var(--audicle-color-danger)",
        warning: "var(--audicle-color-warning-text)",
      },
    },
  },
  plugins: [],
};
