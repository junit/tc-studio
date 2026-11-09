import { create } from 'zustand'
import { readTcStorage, writeTcStorage } from '@/lib/storage'

export type ThemeMode = 'light' | 'dark' | 'system'

interface ThemeState {
  theme: ThemeMode
  isDark: boolean
  setTheme: (theme: ThemeMode) => void
  toggleTheme: () => void
}

function getInitialTheme(): ThemeMode {
  if (typeof window === 'undefined') return 'light'
  const saved = readTcStorage(localStorage, 'theme') as ThemeMode
  if (saved === 'light' || saved === 'dark' || saved === 'system') {
    return saved
  }
  return 'light'
}

function applyThemeToDocument(theme: ThemeMode): boolean {
  if (typeof window === 'undefined') return false
  const isDark =
    theme === 'dark' ||
    (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)

  if (isDark) {
    document.documentElement.classList.add('dark')
  } else {
    document.documentElement.classList.remove('dark')
  }
  return isDark
}

export const useThemeStore = create<ThemeState>((set, get) => {
  const initial = getInitialTheme()
  const isDark = applyThemeToDocument(initial)

  if (typeof window !== 'undefined') {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    media.addEventListener('change', (e) => {
      if (get().theme === 'system') {
        const dark = e.matches
        if (dark) {
          document.documentElement.classList.add('dark')
        } else {
          document.documentElement.classList.remove('dark')
        }
        set({ isDark: dark })
      }
    })
  }

  return {
    theme: initial,
    isDark,
    setTheme: (theme: ThemeMode) => {
      writeTcStorage(localStorage, 'theme', theme)
      const dark = applyThemeToDocument(theme)
      set({ theme, isDark: dark })
    },
    toggleTheme: () => {
      const current = get().theme
      const next: ThemeMode = current === 'dark' ? 'light' : 'dark'
      writeTcStorage(localStorage, 'theme', next)
      const dark = applyThemeToDocument(next)
      set({ theme: next, isDark: dark })
    },
  }
})
