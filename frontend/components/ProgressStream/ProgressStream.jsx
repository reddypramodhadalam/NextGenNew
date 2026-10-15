'use client'

// components/ProgressStream/ProgressStream.jsx
// Renders live progress events from the backend SSE stream.
// Used during survey, generation, and execution phases.

import { useEffect, useRef } from 'react'
import styles from './ProgressStream.module.css'

export default function ProgressStream({ title, description, messages }) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <div className={styles.spinner} aria-hidden="true" />
        <div>
          <h3 className={styles.title}>{title}</h3>
          <p className={styles.description}>{description}</p>
        </div>
      </div>

      <div className={styles.log} role="log" aria-live="polite" aria-label="Progress log">
        {messages.length === 0 && (
          <div className={styles.logEmpty}>Starting…</div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={styles.logLine}>
            <span className={styles.logStep}>{String(i + 1).padStart(2, '0')}</span>
            <span className={styles.logMsg}>{msg}</span>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
