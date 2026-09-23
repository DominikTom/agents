import twColors from 'tailwindcss/colors'

// Odcienie tint (50-300: tła/bordery) i tekstu (600-700) rodzin statusowych
// przechodzą przez zmienne CSS z src/index.css — dark mode podmienia je na
// ciemne tinty zamiast jasnych pasteli. Pozostałe odcienie: paleta Tailwinda.
const viaVars = (family, shades) =>
  Object.fromEntries(shades.map((s) => [s, `rgb(var(--tw-${family}-${s}) / <alpha-value>)`]))
const tinted = (family, shades = [50, 100, 200, 300, 500, 600, 700]) => ({
  ...twColors[family],
  ...viaVars(family, shades),
})

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/dashboard/templates/**/*.html', './src/dashboard/static/*.js', './src/mcp_server/oauth.py', './src/dashboard/*.py'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        // Design system (docs/DESIGN.md): Geist Sans dla UI, Geist Mono dla
        // liczb/tabel/kodu — pliki self-hosted przez @fontsource (main.jsx)
        sans: [
          'Geist Sans',
          'Geist',
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'Geist Mono',
          'JetBrains Mono',
          'ui-monospace',
          'SFMono-Regular',
          'Menlo',
          'Consolas',
          'monospace',
        ],
      },
      colors: {
        // App neutrals — wartości WYŁĄCZNIE ze zmiennych CSS w src/index.css
        // (tokeny design systemu z docs/DESIGN.md); nazwy bez zmian.
        // rgb(var()/<alpha-value>) zachowuje działanie modyfikatorów (bg-ink/90)
        canvas: 'rgb(var(--color-canvas) / <alpha-value>)',
        surface: {
          DEFAULT: 'rgb(var(--color-surface) / <alpha-value>)',
          dark: 'rgb(var(--color-surface-dark) / <alpha-value>)',
          2: 'rgb(var(--color-surface-2) / <alpha-value>)',
        },
        ink: {
          DEFAULT: 'rgb(var(--color-ink) / <alpha-value>)',
          soft: 'rgb(var(--color-ink-soft) / <alpha-value>)',
          muted: 'rgb(var(--color-ink-muted) / <alpha-value>)',
          faint: 'rgb(var(--color-ink-faint) / <alpha-value>)',
        },
        line: 'rgb(var(--color-line) / <alpha-value>)',
        // Kolory akcji z DESIGN.md
        primary: {
          DEFAULT: 'rgb(var(--color-primary) / <alpha-value>)',
          soft: 'rgb(var(--color-primary-soft) / <alpha-value>)',
          ink: 'rgb(var(--color-primary-ink) / <alpha-value>)',
        },
        accent: {
          DEFAULT: 'rgb(var(--color-accent) / <alpha-value>)',
          soft: 'rgb(var(--color-accent-soft) / <alpha-value>)',
          ink: 'rgb(var(--color-accent-ink) / <alpha-value>)',
        },
        // Rodziny statusowe z ciemnymi tintami w dark mode
        red: tinted('red'),
        orange: tinted('orange'),
        amber: tinted('amber'),
        emerald: tinted('emerald'),
        teal: tinted('teal'),
        cyan: tinted('cyan'),
        sky: tinted('sky'),
        blue: tinted('blue'),
        indigo: tinted('indigo'),
        violet: tinted('violet'),
        slate: tinted('slate', [50, 100, 200, 300]),
        gray: tinted('gray', [50, 100, 200, 300]),
        // Brand palettes
        mybed: {
          DEFAULT: '#FB7185',
          soft: '#FFF1F3',
          ink: '#9F1239',
        },
        mitto: {
          DEFAULT: '#F97316',
          soft: '#FFF4EB',
          ink: '#9A3412',
        },
        nomo: {
          DEFAULT: '#1E3A8A',
          soft: '#EEF2FB',
          ink: '#1E3A8A',
        },
        lepszysen: {
          DEFAULT: '#16A34A',
          soft: '#EDFaf1',
          ink: '#166534',
        },
        shared: {
          DEFAULT: '#CA8A04',
          soft: '#FEFAEB',
          ink: '#854D0E',
        },
        tech: {
          DEFAULT: '#3B82F6',
          soft: '#EFF5FF',
          ink: '#1D4ED8',
        },
      },
      boxShadow: {
        card: '0 1px 2px 0 rgba(15, 23, 42, 0.04), 0 1px 3px 0 rgba(15, 23, 42, 0.06)',
        soft: '0 2px 8px -2px rgba(15, 23, 42, 0.08), 0 4px 16px -4px rgba(15, 23, 42, 0.06)',
        panel: '-8px 0 30px -12px rgba(15, 23, 42, 0.18)',
        pop: '0 10px 40px -12px rgba(15, 23, 42, 0.22)',
      },
      borderRadius: {
        // Stonowane zaokrąglenia gęstego panelu (docs/DESIGN.md → rounded):
        // kontrolki/przyciski 8px, karty/popupy 10px — bez „pigułkowatych" kart
        xl: '0.5rem',
        '2xl': '0.625rem',
      },
      keyframes: {
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'slide-in': {
          '0%': { transform: 'translateX(100%)' },
          '100%': { transform: 'translateX(0)' },
        },
        'scale-in': {
          '0%': { opacity: '0', transform: 'scale(0.97)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 0.2s ease-out',
        'slide-in': 'slide-in 0.25s cubic-bezier(0.16, 1, 0.3, 1)',
        'scale-in': 'scale-in 0.15s ease-out',
      },
    },
  },
  plugins: [],
}
