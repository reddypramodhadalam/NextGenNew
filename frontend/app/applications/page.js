'use client'

/**
 * app/applications/page.js
 *
 * Real applications screen (replaces the stub).
 *   - Lists all applications from GET /v1/applications
 *   - "Add application" opens a form that POSTs to /v1/applications
 *   - Each row links to that application's detail
 *
 * Everything here is live against the backend — no placeholder data.
 */

import { useState, useEffect } from 'react'
import Link from 'next/link'
import { getApplications, createApplication, setApplicationSsoGateway, archiveApplication, deleteApplication } from '../../lib/api'

export default function ApplicationsPage() {
  const [removing, setRemoving] = useState(null)
  const [apps, setApps]       = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(null)

  // add-form state
  const [showForm, setShowForm]   = useState(false)
  const [name, setName]           = useState('')
  const [baseUrl, setBaseUrl]     = useState('')
  const [owner, setOwner]         = useState('')
  const [requiresSsoGateway, setRequiresSsoGateway] = useState(false)
  const [saving, setSaving]       = useState(false)
  const [ssoSavingId, setSsoSavingId] = useState(null)
  const [formError, setFormError] = useState(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await getApplications()
      setApps(Array.isArray(data) ? data : [])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  async function handleAdd() {
    if (!name.trim()) {
      setFormError('Application name is required.')
      return
    }
    setSaving(true)
    setFormError(null)
    try {
      await createApplication({
        name: name.trim(),
        base_url: baseUrl.trim() || null,
        owner: owner.trim() || null,
        requires_sso_gateway: requiresSsoGateway,
      })
      // reset + reload
      setName(''); setBaseUrl(''); setOwner(''); setRequiresSsoGateway(false)
      setShowForm(false)
      await load()
    } catch (err) {
      setFormError(err.message)
    } finally {
      setSaving(false)
    }
  }

  async function handleToggleSso(app, checked) {
    setSsoSavingId(app.id)
    try {
      await setApplicationSsoGateway(app.id, checked)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setSsoSavingId(null)
    }
  }

  return (
    <div style={{ padding: '32px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '24px' }}>
        <div>
          <h1 style={{ fontSize: '24px', fontWeight: 700, color: 'var(--text-primary)' }}>Applications</h1>
          <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginTop: '4px' }}>
            {loading ? 'Loading…' : `${apps.length} application${apps.length !== 1 ? 's' : ''}`}
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => setShowForm(v => !v)}>
          <i className="ti ti-plus" aria-hidden="true" /> Add application
        </button>
      </div>

      {/* Add form */}
      {showForm && (
        <div className="card" style={{ marginBottom: '24px', padding: '20px' }}>
          <div className="card-title" style={{ marginBottom: '16px' }}>New application</div>
          <div style={{ display: 'grid', gap: '12px', maxWidth: '480px' }}>
            <div>
              <label style={{ display: 'block', fontSize: '14px', color: 'var(--text-secondary)', marginBottom: '4px' }}>
                Name *
              </label>
              <input
                className="input" value={name} onChange={e => setName(e.target.value)}
                placeholder="e.g. FS, Netra, Baxter Portal"
                style={{ width: '100%' }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '14px', color: 'var(--text-secondary)', marginBottom: '4px' }}>
                Base URL
              </label>
              <input
                className="input" value={baseUrl} onChange={e => setBaseUrl(e.target.value)}
                placeholder="https://…"
                style={{ width: '100%' }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '14px', color: 'var(--text-secondary)', marginBottom: '4px' }}>
                Owner
              </label>
              <input
                className="input" value={owner} onChange={e => setOwner(e.target.value)}
                placeholder="Team or person"
                style={{ width: '100%' }}
              />
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14, color: 'var(--text-secondary)' }}>
              <input
                type="checkbox"
                checked={requiresSsoGateway}
                onChange={e => setRequiresSsoGateway(e.target.checked)}
              />
              This application requires SSO login gateway
            </label>
            {formError && (
              <div style={{ color: 'var(--red, #e5484d)', fontSize: '14px' }}>{formError}</div>
            )}
            <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
              <button className="btn btn-primary" onClick={handleAdd} disabled={saving}>
                {saving ? 'Saving…' : 'Create application'}
              </button>
              <button className="btn btn-ghost" onClick={() => { setShowForm(false); setFormError(null) }}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* States */}
      {loading && (
        <div className="card" style={{ padding: '20px' }}>
          {[1,2,3].map(i => <div key={i} className="skeleton" style={{ height: '48px', marginBottom: '8px', borderRadius: '6px' }} />)}
        </div>
      )}

      {!loading && error && (
        <div className="card" style={{ padding: '20px', color: 'var(--amber, #f5a623)' }}>
          <i className="ti ti-alert-circle" aria-hidden="true" /> Could not load applications: {error}
        </div>
      )}

      {!loading && !error && apps.length === 0 && (
        <div className="card" style={{ padding: '40px', textAlign: 'center', color: 'var(--text-muted)' }}>
          <i className="ti ti-apps" style={{ fontSize: '32px' }} aria-hidden="true" />
          <p style={{ margin: '12px 0' }}>No applications yet. Add your first one above.</p>
        </div>
      )}

      {!loading && !error && apps.length > 0 && (
        <div className="card">
          <div className="table-wrapper">
            <table className="table">
              <thead>
                <tr><th>Application</th><th>Base URL</th><th>SSO</th><th>Owner</th><th></th></tr>
              </thead>
              <tbody>
                {apps.map(app => (
                  <tr key={app.id}>
                    <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{app.name}</td>
                    <td className="text-muted">{app.base_url || '—'}</td>
                    <td className="text-secondary">
                      <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                        <input
                          type="checkbox"
                          checked={!!app.requires_sso_gateway}
                          disabled={ssoSavingId === app.id}
                          onChange={e => handleToggleSso(app, e.target.checked)}
                        />
                        {app.requires_sso_gateway ? 'Enabled' : 'Disabled'}
                      </label>
                    </td>
                    <td className="text-secondary">{app.owner || '—'}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      <Link href={`/applications/${app.id}`}
                            style={{ color: 'var(--accent)', fontWeight: 600, fontSize: 14, marginRight: 14 }}>
                        Open
                      </Link>
                      <Link href={`/applications/${app.id}/credentials`}
                            title="Set your own login for this application"
                            style={{ color: 'var(--accent)', fontWeight: 600, fontSize: 13, marginRight: 14 }}>
                        My login
                      </Link>
                      <button
                        onClick={() => setRemoving(app)}
                        title="Retire or delete this application"
                        style={{
                          background: 'none', border: '1px solid var(--border)',
                          color: 'var(--text-muted)', borderRadius: 7,
                          padding: '5px 11px', fontSize: 12, cursor: 'pointer',
                          fontFamily: 'var(--font-body)',
                        }}
                      >
                        Retire
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {removing && (
        <RetireDialog
          app={removing}
          onClose={() => setRemoving(null)}
          onDone={() => { setRemoving(null); load() }}
        />
      )}
    </div>
  )
}


/**
 * Retire an application.
 *
 * Archiving is offered first: a decommissioned system still needs its
 * test evidence retained. Deleting removes the application, every
 * scenario and script beneath it, and all execution history — so it is
 * the deliberate, secondary choice.
 */
function RetireDialog({ app, onClose, onDone }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  async function run(action) {
    setBusy(action); setError(null)
    try {
      await (action === 'archive' ? archiveApplication(app.id) : deleteApplication(app.id))
      onDone()
    } catch (e) { setError(e.message); setBusy(null) }
  }

  const box = { background: 'var(--bg-card)', border: '1px solid var(--border)',
                borderRadius: 12, padding: 22, width: 'min(520px, 100%)' }
  const opt = { border: '1px solid var(--border)', borderRadius: 10, padding: 14,
                marginBottom: 12, display: 'grid', gap: 6 }

  return (
    <div onClick={onClose}
         style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
                  display: 'grid', placeItems: 'center', zIndex: 300, padding: 40 }}>
      <div style={box} onClick={e => e.stopPropagation()}>
        <h3 style={{ marginBottom: 4 }}>Retire application?</h3>
        <p className="text-secondary" style={{ marginBottom: 18 }}>{app.name} · {app.base_url}</p>

        <div style={opt}>
          <strong style={{ fontSize: 14 }}>Archive</strong>
          <span className="text-muted" style={{ fontSize: 12, lineHeight: 1.5 }}>
            Removes it from the active list but keeps every scenario, script and
            execution record. Recommended when a system is decommissioned but its
            test evidence must be retained.
          </span>
          <button className="btn btn-secondary" style={{ justifySelf: 'start', marginTop: 6 }}
                  onClick={() => run('archive')} disabled={!!busy}>
            {busy === 'archive' ? 'Archiving…' : 'Archive'}
          </button>
        </div>

        <div style={{ ...opt, borderColor: 'var(--red-bg)' }}>
          <strong style={{ fontSize: 14 }}>Delete permanently</strong>
          <span className="text-muted" style={{ fontSize: 12, lineHeight: 1.5 }}>
            Deletes the application, every scenario and script beneath it, and all
            execution history and screenshots. This cannot be undone.
          </span>
          <button style={{ justifySelf: 'start', marginTop: 6, background: 'var(--red)',
                           color: '#fff', border: 'none', padding: '7px 14px',
                           borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: 'pointer' }}
                  onClick={() => run('delete')} disabled={!!busy}>
            {busy === 'delete' ? 'Deleting…' : 'Delete permanently'}
          </button>
        </div>

        {error && <div style={{ color: 'var(--red)', fontSize: 12, marginBottom: 10 }}>{error}</div>}
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button className="btn btn-ghost" onClick={onClose} disabled={!!busy}>Cancel</button>
        </div>
      </div>
    </div>
  )
}
