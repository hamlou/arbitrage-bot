import type { Config } from "tailwindcss";

/**
 * Design tokens — the entire visual language lives here and in globals.css.
 * Every color is a CSS variable so dark/light mode is a single class flip.
 */
const config: Config = {
  darkMode: ["class"],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "rgb(var(--bg) / <alpha-value>)",
        surface: "rgb(var(--surface) / <alpha-value>)",
        elevated: "rgb(var(--elevated) / <alpha-value>)",
        raised: "rgb(var(--raised) / <alpha-value>)",
        line: "rgb(var(--line) / <alpha-value>)",
        edge: "rgb(var(--edge) / <alpha-value>)",
        ink: {
          DEFAULT: "rgb(var(--ink) / <alpha-value>)",
          muted: "rgb(var(--ink-muted) / <alpha-value>)",
          faint: "rgb(var(--ink-faint) / <alpha-value>)",
        },
        brand: {
          DEFAULT: "rgb(var(--brand) / <alpha-value>)",
          soft: "rgb(var(--brand-soft) / <alpha-value>)",
        },
        ok: {
          DEFAULT: "rgb(var(--ok) / <alpha-value>)",
          soft: "rgb(var(--ok-soft) / <alpha-value>)",
        },
        warn: {
          DEFAULT: "rgb(var(--warn) / <alpha-value>)",
          soft: "rgb(var(--warn-soft) / <alpha-value>)",
        },
        danger: {
          DEFAULT: "rgb(var(--danger) / <alpha-value>)",
          soft: "rgb(var(--danger-soft) / <alpha-value>)",
        },
        info: {
          DEFAULT: "rgb(var(--info) / <alpha-value>)",
          soft: "rgb(var(--info-soft) / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Cascadia Code",
          "Consolas",
          "Liberation Mono",
          "monospace",
        ],
      },
      borderRadius: {
        sm: "6px",
        DEFAULT: "10px",
        md: "12px",
        lg: "16px",
        xl: "20px",
      },
      boxShadow: {
        card: "0 1px 0 0 rgb(255 255 255 / 0.03) inset, 0 1px 2px 0 rgb(0 0 0 / 0.4)",
        pop: "0 12px 40px -8px rgb(0 0 0 / 0.5), 0 2px 8px rgb(0 0 0 / 0.3)",
        glow: "0 0 0 1px rgb(var(--brand) / 0.35), 0 0 24px -4px rgb(var(--brand) / 0.45)",
        "glow-ok": "0 0 0 1px rgb(var(--ok) / 0.3), 0 0 20px -6px rgb(var(--ok) / 0.4)",
      },
      letterSpacing: {
        tightest: "-0.04em",
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-400px 0" },
          "100%": { backgroundPosition: "400px 0" },
        },
        "pulse-dot": {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.45", transform: "scale(0.8)" },
        },
        "slide-in-right": {
          "0%": { transform: "translateX(16px)", opacity: "0" },
          "100%": { transform: "translateX(0)", opacity: "1" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.4s cubic-bezier(0.16, 1, 0.3, 1) both",
        shimmer: "shimmer 1.6s linear infinite",
        "pulse-dot": "pulse-dot 1.6s ease-in-out infinite",
        "slide-in-right": "slide-in-right 0.28s cubic-bezier(0.16, 1, 0.3, 1) both",
      },
    },
  },
  plugins: [],
};

export default config;
