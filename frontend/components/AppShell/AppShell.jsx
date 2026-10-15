'use client'

/**
 * components/AppShell/AppShell.jsx
 * Persistent shell: Kavach shield sidebar, topbar with search +
 * light/dark toggle, primary actions. Uses Tabler icon classes.
 *
 * ChatPanel ("Ask Kavach") is mounted here so it is available on every
 * screen — it answers questions about coverage and failures using the real
 * database, and those questions come up wherever you happen to be.
 */

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { useState, useEffect } from 'react'
// V2 adds the ability to act on instructions, with a confirmation step.
// Swap this import back to ChatPanel to revert entirely.
import ChatPanel from '../ChatPanel/ChatPanel'
import { getCurrentUser } from '../../lib/api'
import styles from './AppShell.module.css'
import { useAuth } from '../../hooks/useAuth'

const NAV_ITEMS = [
  { href: '/dashboard',    label: 'Dashboard',    icon: 'ti-layout-dashboard' },
  { href: '/applications', label: 'Applications', icon: 'ti-apps' },
  { href: '/scenarios',    label: 'Scenarios',    icon: 'ti-clipboard-list' },
  { href: '/executions',   label: 'Executions',   icon: 'ti-player-play' },
  { href: '/schedules',    label: 'Schedules',    icon: 'ti-calendar-time' },
  { href: '/reports',      label: 'Reports',      icon: 'ti-chart-bar' },
]
const RESOURCE_ITEMS = [
  { href: '/knowledge-base', label: 'Testing standards', icon: 'ti-book' },
  // Separate from the knowledge base on purpose: that holds the testing
  // standards that feed generation, this holds how to use Kavach. Different
  // audience, different lifecycle — merging them would have the assistant
  // answering "how do I schedule a run" out of a testing standard.
  { href: '/help', label: 'Help & support', icon: 'ti-lifebuoy' },
]


/**
 * Reduce a name to at most two initials for the avatar.
 *
 * @param {string} name - The user's name, possibly empty.
 * @returns {string} Initials, or a dash when no name is set.
 */
function initials(name) {
  const parts = (name || '').trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return '—'
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase()
}


export default function AppShell({ children }) {
  const pathname = usePathname()
  const { user, isAuthenticated } = useAuth()
  const [theme, setThemeState] = useState('dark')

  const signedInUserName = user?.name || user?.username || ''
  const signedInUserEmail = user?.username || ''

  // The name shown here is the same one sent as X-Triggered-By, so what the
  // sidebar says and what the audit trail records cannot disagree. Read on
  // mount rather than at module load, because localStorage does not exist
  // during server rendering.
  // The sidebar collapses to an icon rail. On a 1280px laptop the shell,
  // the content and an open assistant panel compete for the same width, and
  // the navigation is the part a user needs least while reading an answer.
  const [navCollapsed, setNavCollapsed] = useState(false)
  useEffect(() => {
    try { setNavCollapsed(localStorage.getItem('kavach-nav-collapsed') === '1') } catch {}
  }, [])
  function toggleNav() {
    setNavCollapsed(v => {
      const next = !v
      try { localStorage.setItem('kavach-nav-collapsed', next ? '1' : '0') } catch {}
      return next
    })
  }

  const [userName, setUserName] = useState('')
  useEffect(() => {
    const read = () => {
      const who = getCurrentUser()
      setUserName(who === 'anonymous' ? '' : who)
    }
    read()
    // Settings dispatches this after a save, so the chip updates without a
    // page reload.
    window.addEventListener('kavach-user-changed', read)
    return () => window.removeEventListener('kavach-user-changed', read)
  }, [])

  useEffect(() => {
    const saved = (typeof window !== 'undefined' && localStorage.getItem('kavach-theme')) || 'dark'
    setThemeState(saved)
    document.documentElement.setAttribute('data-theme', saved)
  }, [])

  function setTheme(t) {
    setThemeState(t)
    document.documentElement.setAttribute('data-theme', t)
    try { localStorage.setItem('kavach-theme', t) } catch {}
  }

  const isActive = (href) => pathname === href || pathname.startsWith(href + '/')

  const titleMap = {
    '/dashboard':'Home','/applications':'Applications','/scenarios':'Scenarios',
    '/executions':'Executions','/schedules':'Schedules','/reports':'Reports','/knowledge-base':'Knowledge base',
    '/help':'Help & support',
    '/settings':'Settings','/new-test-suite':'New Test Suite',
  }
  const pageTitle = titleMap[Object.keys(titleMap).find(k => isActive(k)) || ''] || 'NextGen2'

  return (
    <div className={styles.shell}>
      <aside
        className={`${styles.sidebar} ${navCollapsed ? styles.sidebarNarrow : ''}`}
        aria-label="Main navigation">
        <button
          className={styles.navToggle}
          onClick={toggleNav}
          title={navCollapsed ? 'Expand navigation' : 'Collapse navigation'}
          aria-label={navCollapsed ? 'Expand navigation' : 'Collapse navigation'}>
          {navCollapsed ? '›' : '‹'}
        </button>
        <div className={styles.logoArea}>
          <div className={styles.logoRow}>
            <div className={styles.logoMark} aria-hidden="true">
              <svg viewBox="0 0 32 32" fill="none" width="34" height="34">
                <path d="M16 2L28 7v9c0 7.5-5.1 12.5-12 14C9.1 28.5 4 23.5 4 16V7l12-5z" fill="url(#kg)" stroke="url(#kg2)" strokeWidth="1"/>
                <path d="M11 16.5l3.2 3.2L21 12" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
                <defs>
                  <linearGradient id="kg" x1="4" y1="2" x2="28" y2="30"><stop stopColor="#6366f1"/><stop offset="1" stopColor="#4f7cff"/></linearGradient>
                  <linearGradient id="kg2" x1="4" y1="2" x2="28" y2="30"><stop stopColor="#818cf8"/><stop offset="1" stopColor="#4f7cff"/></linearGradient>
                </defs>
              </svg>
            </div>
            <div className={styles.brandText}>
              <div className={styles.brandName}>NextGen2</div>
              <div className={styles.brandSub}>Test Insights & Actions</div>
            </div>
          </div>
        </div>

        <nav className={styles.nav}>
          {NAV_ITEMS.map(item => (
            <Link key={item.href} href={item.href}
              title={navCollapsed ? item.label : undefined}
              className={`${styles.navItem} ${isActive(item.href) ? styles.navItemActive : ''}`}>
              <i className={`ti ${item.icon} ${styles.navIcon}`} aria-hidden="true" />
              {/* Wrapped so the collapsed rail can hide it. A bare text node
                  cannot be targeted by CSS, which is why collapsing used to
                  shrink the sidebar and leave the words overflowing it. */}
              <span className={styles.navText}>{item.label}</span>
            </Link>
          ))}
          <div className={styles.navLabel}>Resources</div>
          {RESOURCE_ITEMS.map(item => (
            <Link key={item.href} href={item.href}
              title={navCollapsed ? item.label : undefined}
              className={`${styles.navItem} ${isActive(item.href) ? styles.navItemActive : ''}`}>
              <i className={`ti ${item.icon} ${styles.navIcon}`} aria-hidden="true" />
              {/* Wrapped so the collapsed rail can hide it. A bare text node
                  cannot be targeted by CSS, which is why collapsing used to
                  shrink the sidebar and leave the words overflowing it. */}
              <span className={styles.navText}>{item.label}</span>
            </Link>
          ))}
        </nav>

        <div className={styles.sidebarFoot}>
          <Link href="/settings"
            title={navCollapsed ? 'Settings' : undefined}
            className={`${styles.navItem} ${isActive('/settings') ? styles.navItemActive : ''}`}>
            <i className={`ti ti-settings ${styles.navIcon}`} aria-hidden="true" />
            <span className={styles.navText}>Settings</span>
          </Link>
          <div className={styles.userChip}>
            <div className={styles.avatar}>
              {initials(isAuthenticated ? signedInUserName : userName)}
            </div>
            <div className={styles.userMeta}>
              <div className={styles.userName}>
                {isAuthenticated ? signedInUserName : userName || 'Not signed in'}
              </div>
              <div
                className={styles.userRole}
                title={isAuthenticated ? signedInUserEmail : undefined}
              >
                {isAuthenticated ? (
                  signedInUserEmail
                ) : userName ? (
                  'Quality Lead'
                ) : (
                  <Link href="/settings">Set your name</Link>
                )}
              </div>
            </div>
          </div>
        </div>
      </aside>

      <div className={styles.main}>
        <div className={styles.topbar}>
          <div className={styles.pageTitle}>{pageTitle}</div>
          <div className={styles.spacer} />
          <div className={styles.search}>
            <i className="ti ti-search" aria-hidden="true" />
            <input placeholder="Search applications, scenarios…" />
          </div>
          <div className={styles.themeToggle}>
            <button className={theme === 'light' ? styles.themeOn : ''} onClick={() => setTheme('light')} aria-label="Light mode"><i className="ti ti-sun" /></button>
            <button className={theme === 'dark' ? styles.themeOn : ''} onClick={() => setTheme('dark')} aria-label="Dark mode"><i className="ti ti-moon" /></button>
          </div>
          <Link href="/new-test-suite"><button className="btn btn-primary"><i className="ti ti-plus" /> New Test Suite</button></Link>
        </div>
        <div className={styles.content}>{children}</div>
      </div>

      {/* Ask NextGen2 — available on every screen, answering from the database */}
      <ChatPanel />
    </div>
  )
}
