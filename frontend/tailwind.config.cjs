/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
        sans: ["Satoshi", "system-ui", "sans-serif"],
      },
      colors: {
        ink: "#040405",
        paper: "#0a0a0c",
        surface: "#15151a",
        line: "#26262e",
        mute: "#6b6b78",
        dim: "#9a9aaa",
        fg: "#f5f5f5",
        accent: "#1ce783",
        danger: "#ff5252",
      },
    },
  },
  plugins: [],
};
