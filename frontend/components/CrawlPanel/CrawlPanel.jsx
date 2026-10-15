'use client'
/**
 * components/CrawlPanel/CrawlPanel.jsx
 *
 * App Map crawler controls and live progress display.
 *
 * What it does
 * ─────────────
 * 1. Shows the date/time and summary of the last completed crawl (if any).
 * 2. Lets the user start a new crawl, optionally providing credentials for
 *    applications that are behind a login wall.
 * 3. Streams live crawl progress via SSE — each page discovered appears as a
 *    log line in real time.
 * 4. Shows the final summary once the crawl completes.
 *
 * Props
 * ─────
 * applicationId {number}  — the applications.id to crawl
 * baseUrl       {string}  — shown in the UI so the user knows what will be crawled
 */

import { useState, useEffect } from 'react'
import { useSSE } from '../../hooks/useSSE'
import { getLatestAppMap } from '../../lib/api'
import styles from './CrawlPanel.module.css'

export default function CrawlPanel({ applicationId, baseUrl }) {
  const [status, setStatus]         = useState('idle')   // idle | running | complete | error
  const [messages, setMessages]     = useState([])
  const [result, setResult]         = useState(null)     // {map_id, page_count, element_count}
  const [lastMap, setLastMap]       = useState(null)     // last completed map summary
  const [showCreds, setShowCreds]   = useState(false)
  const [username, setUsername]     = useState('')
  const [password, setPassword]     = useState('')
  const [maxPages, setMaxPages]     = useState(30)

  const { startStream, cancel } = useSSE()

  // Load the latest map summary when the panel first mounts.
  useEffect(() => {
    getLatestAppMap(applicationId)
      .then(setLastMap)
      .catch(() => setLastMap(null))   // 404 = no crawl yet, that's fine
  }, [applicationId])

  function handleEvent(payload) {
    if (payload.type === 'status') {
      setMessages(prev => [...prev, payload.data.message])
    } else if (payload.type === 'page') {
      const { url, title, element_count, page_number } = payload.data
      setMessages(prev => [
        ...prev,
        `Page ${page_number}: ${title || url} — ${element_count} elements`,
      ])
    }
  }

  function handleDone(data) {
    if (data?.error) {
      setStatus('error')
      setMessages(prev => [...prev, `❌ Error: ${data.error}`])
    } else {
      setStatus('complete')
      setResult(data)
      // Refresh the last-map summary with the new crawl
      getLatestAppMap(applicationId).then(setLastMap).catch(() => {})
    }
  }

  async function handleStartCrawl() {
    setStatus('running')
    setMessages([`Starting crawl of ${baseUrl || 'application'}…`])
    setResult(null)

    // useSSE.startStream POSTs FormData — consistent with all other SSE endpoints.
    const formData = new FormData()
    formData.append('max_pages', String(maxPages))
    if (showCreds && username) formData.append('username', username)
    if (showCreds && password) formData.append('password', password)

    await startStream(
      `/v1/applications/${applicationId}/crawl`,
      formData,
      handleEvent,
      handleDone,
    )
  }

  function handleCancel() {
    cancel()
    setStatus('idle')
    setMessages(prev => [...prev, 'Crawl cancelled.'])
  }

  const isRunning = status === 'running'

  return (
    <div className={styles.panel}>
      {/* Header */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.icon} aria-hidden="true">🗺</span>
          <div>
            <h3 className={styles.title}>App Map</h3>
            <p className={styles.subtitle}>
              Crawl all pages to build a persistent element map used by surveys and healing.
            </p>
          </div>
        </div>

        {lastMap && status === 'idle' && (
          <div className={styles.lastCrawl}>
            <span className={styles.lastCrawlLabel}>Last crawl</span>
            <span className={styles.lastCrawlMeta}>
              {new Date(lastMap.crawled_at).toLocaleString()} ·{' '}
              {lastMap.page_count} pages · {lastMap.element_count} elements
              {lastMap.was_authenticated && ' · 🔐 authenticated'}
            </span>
          </div>
        )}
      </div>

      {/* Credential toggle */}
      {status === 'idle' && (
        <div className={styles.controls}>
          <label className={styles.credToggle}>
            <input
              type="checkbox"
              checked={showCreds}
              onChange={e => setShowCreds(e.target.checked)}
            />
            Crawl with authentication
          </label>

          {showCreds && (
            <div className={styles.creds}>
              <input
                className={styles.input}
                type="text"
                placeholder="Username / email"
                value={username}
                onChange={e => setUsername(e.target.value)}
                autoComplete="username"
              />
              <input
                className={styles.input}
                type="password"
                placeholder="Password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </div>
          )}

          <div className={styles.pagesRow}>
            <label className={styles.pagesLabel} htmlFor="maxPages">Max pages</label>
            <input
              id="maxPages"
              className={styles.inputSmall}
              type="number"
              min={1}
              max={100}
              value={maxPages}
              onChange={e => setMaxPages(Number(e.target.value) || 30)}
            />
          </div>

          <button className={styles.startBtn} onClick={handleStartCrawl}>
            Start Crawl
          </button>
        </div>
      )}

      {/* Progress log */}
      {(isRunning || status === 'complete' || status === 'error') && (
        <div className={styles.log} role="log" aria-live="polite">
          {messages.map((msg, i) => (
            <div key={i} className={styles.logLine}>
              <span className={styles.logIdx}>{String(i + 1).padStart(2, '0')}</span>
              <span className={styles.logMsg}>{msg}</span>
            </div>
          ))}
        </div>
      )}

      {/* Running controls */}
      {isRunning && (
        <div className={styles.runningBar}>
          <span className={styles.spinner} aria-hidden="true" />
          <span>Crawling…</span>
          <button className={styles.cancelBtn} onClick={handleCancel}>
            Cancel
          </button>
        </div>
      )}

      {/* Completion summary */}
      {status === 'complete' && result && (
        <div className={styles.summary}>
          <span className={styles.summaryOk}>✓</span>
          <span>
            Crawl complete — <strong>{result.page_count}</strong> pages,{' '}
            <strong>{result.element_count}</strong> elements (map #{result.map_id})
          </span>
          <button
            className={styles.restartBtn}
            onClick={() => { setStatus('idle'); setMessages([]); setResult(null) }}
          >
            Crawl again
          </button>
        </div>
      )}

      {/* Error + restart */}
      {status === 'error' && (
        <div className={styles.errorBar}>
          <button
            className={styles.restartBtn}
            onClick={() => { setStatus('idle'); setMessages([]); setResult(null) }}
          >
            Try again
          </button>
        </div>
      )}
    </div>
  )
}
