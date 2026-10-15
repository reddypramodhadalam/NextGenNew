'use client'

/**
 * app/applications/[id]/credentials/page.js
 *
 * "My application login" — the per-user credential screen.
 *
 * Each signed-in Kavach user stores THEIR OWN username + password for this
 * application here. When that user later triggers a crawl, generation, or
 * execution, the run signs in as them — not as a shared account.
 *
 * Everything on this page is scoped to the signed-in user by the backend
 * (api/routes/app_credentials.py). This page never sends a user id; the
 * backend derives it from the identity header apiFetch already stamps. So a
 * user only ever sees or edits their own credential — opening this page as a
 * different user shows that user's own (or empty) credential.
 *
 * This is a self-contained sub-route, intentionally separate from the large
 * application detail page, so it cannot regress existing behaviour there.
 */

import { useState, useEffect } from 'react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import { ArrowLeft, Eye, EyeOff, Save, Trash2, ShieldCheck } from 'lucide-react'
import {
  getApplication,
  getMyAppCredential,
  saveMyAppCredential,
  revealMyAppCredential,
  deleteMyAppCredential,
} from '../../../../lib/api'
import styles from './credentials.module.css'

const MASKED = '********'

export default function MyAppCredentialPage() {
  const params = useParams()
  const applicationId = params?.id

  const [appName, setAppName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [hasPassword, setHasPassword] = useState(false)
  const [revealed, setRevealed] = useState(false)

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const [app, cred] = await Promise.all([
        getApplication(applicationId).catch(() => null),
        getMyAppCredential(applicationId),
      ])
      if (app?.name) setAppName(app.name)
      setUsername(cred.username || '')
      setHasPassword(!!cred.has_password)
      setPassword(cred.has_password ? MASKED : '')
      setRevealed(false)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (applicationId) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applicationId])

  async function handleSave() {
    if (!username.trim()) {
      setError('Username is required.')
      return
    }
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const cred = await saveMyAppCredential(applicationId, {
        username: username.trim(),
        password,
      })
      setUsername(cred.username || '')
      setHasPassword(!!cred.has_password)
      setPassword(cred.has_password ? MASKED : '')
      setRevealed(false)
      setNotice('Your login was saved.')
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  async function handleReveal() {
    if (revealed) {
      // Hide again — restore the mask.
      setPassword(hasPassword ? MASKED : '')
      setRevealed(false)
      return
    }
    setError(null)
    try {
      const cred = await revealMyAppCredential(applicationId)
      setPassword(cred.password || '')
      setRevealed(true)
    } catch (err) {
      setError(err.message)
    }
  }

  async function handleDelete() {
    setError(null)
    setNotice(null)
    try {
      await deleteMyAppCredential(applicationId)
      setUsername('')
      setPassword('')
      setHasPassword(false)
      setRevealed(false)
      setNotice('Your login was removed.')
    } catch (err) {
      setError(err.message)
    }
  }

  // When the user starts typing in a masked password field, clear the mask so
  // they are entering a real new value (not editing "********").
  function handlePasswordFocus() {
    if (!revealed && password === MASKED) {
      setPassword('')
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <Link href={`/applications/${applicationId}`} className={styles.backLink}>
          <ArrowLeft size={16} /> Back to application
        </Link>
        <h1 className={styles.title}>My application login</h1>
        <p className={styles.subtitle}>
          {appName ? <>For <strong>{appName}</strong>. </> : null}
          This login is yours alone. Runs you trigger sign in as you — other
          users have their own separate login for this application.
        </p>
      </div>

      <div className={styles.card}>
        <div className={styles.privacyRow}>
          <ShieldCheck size={16} className={styles.privacyIcon} />
          <span>Stored encrypted. Only you can see or reveal it.</span>
        </div>

        {loading ? (
          <p className={styles.muted}>Loading…</p>
        ) : (
          <>
            <label className={styles.field}>
              <span className={styles.label}>Username</span>
              <input
                className="input"
                type="text"
                value={username}
                autoComplete="off"
                placeholder="e.g. your application login id"
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>

            <label className={styles.field}>
              <span className={styles.label}>Password</span>
              <div className={styles.passwordRow}>
                <input
                  className="input"
                  type={revealed ? 'text' : 'password'}
                  value={password}
                  autoComplete="new-password"
                  placeholder={hasPassword ? '' : 'enter your password'}
                  onFocus={handlePasswordFocus}
                  onChange={(e) => {
                    setPassword(e.target.value)
                    setRevealed(true)
                  }}
                />
                {hasPassword && (
                  <button
                    type="button"
                    className="btn-icon"
                    onClick={handleReveal}
                    title={revealed ? 'Hide' : 'Reveal'}
                  >
                    {revealed ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                )}
              </div>
              {hasPassword && (
                <span className={styles.hint}>
                  A password is already stored. Leave it untouched to keep it,
                  or type a new one to replace it.
                </span>
              )}
            </label>

            {error && <div className={styles.error}>{error}</div>}
            {notice && <div className={styles.notice}>{notice}</div>}

            <div className={styles.actions}>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleSave}
                disabled={saving}
              >
                <Save size={16} /> {saving ? 'Saving…' : 'Save login'}
              </button>
              {hasPassword && (
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={handleDelete}
                  disabled={saving}
                >
                  <Trash2 size={16} /> Remove
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
