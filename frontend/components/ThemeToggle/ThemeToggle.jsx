'use client'

/**
 * components/ThemeToggle/ThemeToggle.jsx
 *
 * Light/dark switch. Sets data-theme on <html> and remembers the
 * choice in localStorage so it survives reloads. Defaults to dark.
 *
 * (This is a real Next.js app, not a sandboxed artifact — localStorage
 * is available and appropriate here.)
 */

import { useState, useEffect } from 'react'
import styles from './ThemeToggle.module.css'

export default function ThemeToggle() {
  const [theme, setThemeState] = useState('dark')

  useEffect(() => {
    const saved = typeof window !== 'undefined' ? localStorage.getItem('kavach-theme') : null
    const initial = saved || 'dark'
    setThemeState(initial)
    document.documentElement.setAttribute('data-theme', initial)
  }, [])

  function setTheme(next) {
    setThemeState(next)
    document.documentElement.setAttribute('data-theme', next)
    try { localStorage.setItem('kavach-theme', next) } catch {}
  }

  return (
    <div className={styles.toggle} role="group" aria-label="Theme">
      <button
        className={theme === 'light' ? styles.on : ''}
        onClick={() => setTheme('light')}
        aria-label="Light mode"
        title="Light mode"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4 12H2M22 12h-2M6 6L4.5 4.5M19.5 19.5L18 18M6 18l-1.5 1.5M19.5 4.5L18 6" />
        </svg>
      </button>
      <button
        className={theme === 'dark' ? styles.on : ''}
        onClick={() => setTheme('dark')}
        aria-label="Dark mode"
        title="Dark mode"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" />
        </svg>
      </button>
    </div>
  )
}
