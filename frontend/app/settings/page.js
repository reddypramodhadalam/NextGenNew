'use client'

/**
 * app/settings/page.js
 *
 * Local settings.
 *
 * Only one thing lives here today, and it matters more than it looks: the
 * name Kavach attributes actions to.
 *
 * Kavach has no authentication yet. Every request carries an X-Triggered-By
 * header, and the backend writes that value into the training record, the
 * approval fields on a test case, the healing decision log and the support
 * ticket. If it is always "anonymous", those records exist but say nothing —
 * a training record that cannot name who was trained is not evidence.
 *
 * So this is deliberately honest about what it is. It is a label, not a
 * login. Anyone can change it and nothing verifies it. What it buys is an
 * audit trail with real names in it before SSO lands, and exactly one place
 * to change when SSO does land: lib/api.js::getCurrentUser.
 */

import { useState, useEffect } from 'react'
import { Check, UserCircle, AlertTriangle } from 'lucide-react'
import { getCurrentUser, setCurrentUser, API_BASE } from '../../lib/api'

export default function SettingsPage() {
  const [name, setName] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    const current = getCurrentUser()
    setName(current === 'anonymous' ? '' : current)
  }, [])

  function save() {
    setCurrentUser(name.trim())
    setSaved(true)
    setTimeout(() => setSaved(false), 2500)
    // The sidebar chip reads the same value, so tell it to re-read rather
    // than letting the two disagree until the next full page load.
    window.dispatchEvent(new Event('kavach-user-changed'))
  }

  return (
    <div style={{ maxWidth: 640 }}>
      <h1 style={{ fontSize: 26, fontWeight: 600, margin: '0 0 4px' }}>Settings</h1>
      <p style={{ fontSize: 14, color: 'var(--text-muted)', margin: '0 0 22px' }}>
        Preferences stored in this browser.
      </p>

      <section className="card" style={{ padding: '20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
          <UserCircle size={17} strokeWidth={1.9} style={{ color: 'var(--accent)' }} />
          <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>Your name</h2>
        </div>
        <p style={{ fontSize: 14, color: 'var(--text-muted)', margin: '0 0 16px', lineHeight: 1.6 }}>
          Used to attribute what you do in NextGen2 &mdash; training records, approvals,
          healing decisions and support tickets. Without it, those entries read
          &ldquo;anonymous&rdquo; and cannot show who did what.
        </p>

        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
          <input
            className="form-input"
            value={name}
            onChange={e => { setName(e.target.value); setSaved(false) }}
            onKeyDown={e => { if (e.key === 'Enter') save() }}
            placeholder="e.g. User Name"
            style={{ flex: 1 }}
          />
          <button className="btn btn-primary" onClick={save}>
            {saved ? <Check size={15} /> : null}
            {saved ? 'Saved' : 'Save'}
          </button>
        </div>

        {/* Said plainly rather than implied. Someone reading a training record
            needs to know what level of assurance it carries. */}
        <div style={{
          display: 'flex', gap: 9, marginTop: 16, padding: '11px 13px',
          background: 'var(--amber-bg)', color: 'var(--amber)',
          borderRadius: 'var(--radius-sm)', fontSize: 12, lineHeight: 1.55,
        }}>
          <AlertTriangle size={15} strokeWidth={2} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>
            This is a label, not a login. Nothing verifies it, and anyone using
            this browser can change it. It gives the audit trail real names
            ahead of single sign-on, which will replace it.
          </span>
        </div>
      </section>

      <section className="card" style={{ padding: '20px', marginTop: 14 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: '0 0 12px' }}>Connection</h2>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 14 }}>
          <span style={{ color: 'var(--text-muted)' }}>Backend</span>
          <code style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>{API_BASE}</code>
        </div>
        <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: '10px 0 0', lineHeight: 1.55 }}>
          Set by NEXT_PUBLIC_API_URL at build time.
        </p>
      </section>
    </div>
  )
}