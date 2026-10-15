'use client'

// components/ScenarioReview/ScenarioReview.jsx
// Two-pane (master-detail) review, resizable + collapsible.
//   LEFT  — filterable requirement list (drag the divider to resize, or use
//           the ‹ button to collapse it and give the detail the full width).
//   RIGHT — the selected requirement's test cases, with compact controls
//           (Replace · Automate · priority) and expandable steps.
// The component fills 100% of whatever width its parent gives it. If it still
// looks narrow, the parent page constrains it (see the CSS note that ships
// with this file — the .page max-width in new-test-suite.module.css).

import { useState, useRef } from 'react'
import styles from './ScenarioReview.module.css'

const PRIORITIES = [
  { value: 'P0', label: 'P0 — Blocker' },
  { value: 'P1', label: 'P1 — Critical' },
  { value: 'P2', label: 'P2 — High' },
  { value: 'P3', label: 'P3 — Medium' },
  { value: 'P4', label: 'P4 — Low' },
]

function defaultPriority(category) {
  if (category === 'negative' || category === 'security') return 'P1'
  if (category === 'boundary' || category === 'wrong_data') return 'P2'
  if (category === 'missing_field') return 'P3'
  return 'P2'
}

function groupRows(rows) {
  const map = new Map()
  for (const r of rows) {
    const name = r.scenario || (r.tags && r.tags.scenario) || 'General'
    const reqId = r.requirement_id || (r.tags && r.tags.requirement_id) || null
    if (!map.has(name)) map.set(name, { name, requirement_id: reqId, rows: [] })
    const g = map.get(name)
    if (!g.requirement_id && reqId) g.requirement_id = reqId
    g.rows.push(r)
  }
  return Array.from(map.values())
}

const CATEGORY_COLORS = {
  positive: { bg: 'rgba(34,197,94,0.14)',  fg: 'var(--green, #16a34a)' },
  negative: { bg: 'rgba(239,68,68,0.14)',  fg: 'var(--red, #dc2626)' },
  edge:     { bg: 'rgba(245,158,11,0.16)', fg: 'var(--amber, #d97706)' },
  boundary: { bg: 'rgba(245,158,11,0.16)', fg: 'var(--amber, #d97706)' },
  security: { bg: 'rgba(168,85,247,0.16)', fg: 'var(--purple-light, #9333ea)' },
}

function isNoMatch(r) {
  return Array.isArray(r.steps) && r.steps.some((s) => s.matched === false)
}

const FILTERS = [
  { key: 'all',      label: 'All' },
  { key: 'inferred', label: 'AI-inferred' },
  { key: 'neg',      label: 'Neg / edge' },
  { key: 'nomatch',  label: 'No UI match' },
]

function matchesFilter(r, f) {
  if (f === 'all') return true
  if (f === 'inferred') return r.derivation === 'inferred'
  if (f === 'neg') return ['negative', 'edge', 'boundary', 'security'].includes(r.category)
  if (f === 'nomatch') return isNoMatch(r)
  return true
}

function Badge({ children, bg, fg, title }) {
  return (
    <span title={title} style={{
      background: bg, color: fg, fontSize: 11, fontWeight: 700,
      padding: '2px 7px', borderRadius: 4, whiteSpace: 'nowrap',
      textTransform: 'capitalize',
    }}>{children}</span>
  )
}

export default function ScenarioReview({ proposals, onConfirm }) {
  const [rows, setRows] = useState(
    proposals.map((p) => ({
      ...p, included: true, automate: true, replace: false,
      priority: p.priority || defaultPriority(p.category),
    }))
  )
  const [expanded, setExpanded] = useState(() => new Set())
  const [filter, setFilter] = useState('all')
  const [selectedGroup, setSelectedGroup] = useState(null)

  // Resizable / collapsible left pane.
  const [navWidth, setNavWidth] = useState(288)
  const [navCollapsed, setNavCollapsed] = useState(false)
  const bodyRef = useRef(null)
  function startDrag(e) {
    e.preventDefault()
    const onMove = (ev) => {
      const rect = bodyRef.current?.getBoundingClientRect()
      if (!rect) return
      let w = ev.clientX - rect.left
      w = Math.max(200, Math.min(w, rect.width - 340))
      setNavWidth(w)
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.userSelect = ''
    }
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  const allIncluded = rows.filter((r) => r.included)

  function toggleExpanded(id) {
    setExpanded((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n })
  }
  function toggleIncluded(id) {
    setRows((prev) => prev.map((r) =>
      r.id === id ? { ...r, included: !r.included, automate: !r.included ? r.automate : false } : r))
  }
  function toggleAutomate(id) {
    setRows((prev) => prev.map((r) => r.id === id ? { ...r, automate: !r.automate } : r))
  }
  function toggleReplace(id) {
    setRows((prev) => prev.map((r) => r.id === id ? { ...r, replace: !r.replace } : r))
  }
  function automateAll(on) {
    setRows((prev) => prev.map((r) => r.included ? { ...r, automate: on } : r))
  }
  function setPriority(id, priority) {
    setRows((prev) => prev.map((r) => r.id === id ? { ...r, priority } : r))
  }
  function selectAll() { setRows((prev) => prev.map((r) => ({ ...r, included: true }))) }
  function selectNone() { setRows((prev) => prev.map((r) => ({ ...r, included: false }))) }
  function handleConfirm() {
    const approved = rows.filter((r) => r.included)
    if (approved.length === 0) return
    onConfirm(approved.map((r) => ({ ...r, on_duplicate: r.replace ? 'replace' : 'skip' })))
  }

  const filteredRows = rows.filter((r) => matchesFilter(r, filter))
  const groups = groupRows(filteredRows)
  const activeName =
    (selectedGroup && groups.some((g) => g.name === selectedGroup))
      ? selectedGroup : (groups[0] ? groups[0].name : null)
  const activeGroup = groups.find((g) => g.name === activeName) || null

  const totalReqs = groupRows(rows).length
  const totalTests = rows.length
  const inferredCount = rows.filter((r) => r.derivation === 'inferred').length
  const nomatchCount = rows.filter(isNoMatch).length

  return (
    <div className={styles.container} style={{ width: '100%' }}>
      {/* ── Top bar ─────────────────────────────────────────────────── */}
      <div className={styles.header}>
        <div>
          <h3 className={styles.title}>Review generated scenarios</h3>
          <p className={styles.subtitle}>
            {totalReqs} requirement{totalReqs === 1 ? '' : 's'} · {totalTests} test case{totalTests === 1 ? '' : 's'}
            {inferredCount > 0 && <> · <span style={{ color: 'var(--amber)' }}>{inferredCount} AI-inferred</span></>}
            {nomatchCount > 0 && <> · {nomatchCount} no UI match</>}
          </p>
        </div>
        <div className={styles.headerActions}>
          <button className="btn btn-ghost"
                  onClick={() => (allIncluded.length === rows.length ? selectNone() : selectAll())}>
            {allIncluded.length === rows.length ? 'Keep none' : 'Keep all'}
          </button>
          <button className="btn btn-ghost"
                  onClick={() => automateAll(rows.filter((r) => r.included && r.automate).length !== allIncluded.length)}>
            {rows.filter((r) => r.included && r.automate).length === allIncluded.length && allIncluded.length > 0
              ? 'Automate none' : 'Automate all'}
          </button>
        </div>
      </div>

      {/* ── Two-pane body ───────────────────────────────────────────── */}
      {rows.length === 0 ? (
        <div className={styles.empty}>
          No scenarios were generated. Try providing a URL or a requirements document.
        </div>
      ) : (
        <div ref={bodyRef} style={{ display: 'flex', height: '64vh', minHeight: 440, width: '100%' }}>

          {/* Collapsed rail: a thin bar with a show button */}
          {navCollapsed ? (
            <button type="button" onClick={() => setNavCollapsed(false)} title="Show requirement list"
              style={{
                width: 30, flexShrink: 0, border: 'none', borderRight: '1px solid var(--border)',
                background: 'transparent', cursor: 'pointer', color: 'var(--text-muted)', fontSize: 16,
              }}>›</button>
          ) : (
            <>
              {/* LEFT — filters + requirement list */}
              <div style={{
                width: navWidth, flexShrink: 0, display: 'flex', flexDirection: 'column', minHeight: 0,
                background: 'var(--bg-subtle, rgba(120,120,140,0.04))',
                borderRight: '1px solid var(--border)',
              }}>
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 6, padding: '10px 12px',
                  borderBottom: '1px solid var(--border)',
                }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, flex: 1 }}>
                    {FILTERS.map((f) => {
                      const active = filter === f.key
                      return (
                        <button key={f.key} type="button" onClick={() => setFilter(f.key)}
                          style={{
                            fontSize: 11, fontWeight: 600, padding: '3px 8px', borderRadius: 5, cursor: 'pointer',
                            transition: 'all .12s', whiteSpace: 'nowrap',
                            background: active ? 'var(--accent-glow, rgba(99,102,241,0.12))' : 'transparent',
                            color: active ? 'var(--accent, #6366f1)' : 'var(--text-muted)',
                            border: `1px solid ${active ? 'var(--accent, #6366f1)' : 'transparent'}`,
                          }}>{f.label}</button>
                      )
                    })}
                  </div>
                  <button type="button" onClick={() => setNavCollapsed(true)} title="Hide list"
                    style={{ border: '1px solid var(--border)', background: 'transparent', cursor: 'pointer',
                      color: 'var(--text-muted)', fontSize: 12, lineHeight: 1, width: 20, height: 20,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      borderRadius: 5, padding: 0, flexShrink: 0 }}>‹</button>
                </div>

                <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
                  {groups.length === 0 ? (
                    <div style={{ padding: 18, fontSize: 12, color: 'var(--text-muted)' }}>Nothing matches this filter.</div>
                  ) : groups.map((g) => {
                    const inf = g.rows.filter((r) => r.derivation === 'inferred').length
                    const active = g.name === activeName
                    return (
                      <button key={g.name} type="button" onClick={() => setSelectedGroup(g.name)}
                        style={{
                          display: 'block', width: '100%', textAlign: 'left', cursor: 'pointer',
                          padding: '11px 14px', border: 'none', borderBottom: '1px solid var(--border)',
                          borderLeft: active ? '3px solid var(--accent, #6366f1)' : '3px solid transparent',
                          background: active ? 'var(--accent-glow, rgba(99,102,241,0.10))' : 'transparent',
                          transition: 'background .12s',
                        }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
                          {g.requirement_id && (
                            <span style={{
                              background: active ? 'var(--accent, #6366f1)' : 'var(--bg-accent, rgba(99,102,241,0.12))',
                              color: active ? '#fff' : 'var(--accent, #6366f1)',
                              fontSize: 11, fontWeight: 700, padding: '1px 6px', borderRadius: 4, whiteSpace: 'nowrap', flexShrink: 0,
                            }}>{g.requirement_id}</span>
                          )}
                          <span style={{
                            fontSize: 14, fontWeight: active ? 700 : 600, color: 'var(--text-primary)',
                            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                          }}>{g.name}</span>
                        </div>
                        <div style={{ display: 'flex', gap: 10, marginTop: 3, fontSize: 11 }}>
                          <span style={{ color: 'var(--text-muted)' }}>{g.rows.length} test{g.rows.length === 1 ? '' : 's'}</span>
                          {inf > 0 && <span style={{ color: 'var(--amber)', fontWeight: 600 }}>{inf} AI</span>}
                        </div>
                      </button>
                    )
                  })}
                </div>
              </div>

              {/* Drag handle */}
              <div onMouseDown={startDrag} title="Drag to resize"
                style={{
                  width: 6, flexShrink: 0, cursor: 'col-resize',
                  borderLeft: '1px solid var(--border)', borderRight: '1px solid var(--border)',
                  background: 'transparent',
                }} />
            </>
          )}

          {/* RIGHT — detail */}
          <div style={{ flex: 1, overflowY: 'auto', minHeight: 0, minWidth: 0 }}>
            {!activeGroup ? (
              <div style={{ padding: 28, fontSize: 14, color: 'var(--text-muted)' }}>Select a requirement on the left.</div>
            ) : (
              <div>
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '14px 22px',
                  borderBottom: '1px solid var(--border)', position: 'sticky', top: 0,
                  background: 'var(--bg-card)', zIndex: 1,
                }}>
                  {activeGroup.requirement_id && (
                    <span style={{
                      background: 'var(--accent, #6366f1)', color: '#fff',
                      fontSize: 12, fontWeight: 700, padding: '3px 9px', borderRadius: 5, flexShrink: 0,
                    }}>{activeGroup.requirement_id}</span>
                  )}
                  <span style={{
                    fontSize: 16, fontWeight: 700, color: 'var(--text-primary)', letterSpacing: '-0.2px',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>{activeGroup.name}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-muted)', flexShrink: 0 }}>
                    {activeGroup.rows.length} test case{activeGroup.rows.length === 1 ? '' : 's'}
                  </span>
                </div>

                {activeGroup.rows.map((row) => {
                  const cat = CATEGORY_COLORS[row.category] || null
                  return (
                    <div key={row.id} style={{ borderBottom: '1px solid var(--border)', opacity: row.included ? 1 : 0.45 }}>
                      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, padding: '12px 20px' }}>
                        <input type="checkbox" className={styles.checkbox} style={{ marginTop: 3, flexShrink: 0 }}
                          checked={row.included} onChange={() => toggleIncluded(row.id)}
                          aria-label={`Include: ${row.title}`} />

                        {/* title with an inline category label; provenance badge sits below */}
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                            <span style={{ fontSize: 14, color: 'var(--text-primary)', lineHeight: 1.4 }}>{row.title}</span>
                            {cat && (
                              <span style={{
                                fontSize: 11, fontWeight: 600, color: cat.fg,
                                border: `1px solid ${cat.bg}`, background: 'transparent',
                                padding: '1px 6px', borderRadius: 4, textTransform: 'capitalize',
                                flexShrink: 0, letterSpacing: 0,
                              }}>{row.category}</span>
                            )}
                            {row.derivation === 'inferred' && (
                              <span title="Reasoned by the AI — not stated in the requirement. Review it."
                                style={{
                                  fontSize: 11, fontWeight: 600, color: 'var(--amber, #d97706)',
                                  border: '1px solid rgba(245,158,11,0.35)', background: 'transparent',
                                  padding: '1px 6px', borderRadius: 4, flexShrink: 0, letterSpacing: 0,
                                }}>AI</span>
                            )}
                            {Array.isArray(row.steps) && row.steps.length > 0 && (
                              <button type="button" onClick={() => toggleExpanded(row.id)}
                                style={{
                                  background: 'none', border: 'none', padding: 0, marginLeft: 2,
                                  color: 'var(--text-dimmer)', cursor: 'pointer', fontSize: 11,
                                  display: 'inline-flex', alignItems: 'center', gap: 3, flexShrink: 0,
                                }}>
                                <span style={{ fontSize: 9 }}>{expanded.has(row.id) ? '▾' : '▸'}</span>
                                {row.steps.length} step{row.steps.length === 1 ? '' : 's'}
                              </button>
                            )}
                          </div>
                        </div>

                        {/* compact controls, single row, never wraps */}
                        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0 }}>
                          {row.duplicate_of && (
                            <label title="Replace the existing test case, keeping its run history"
                              style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12,
                                color: row.replace ? 'var(--amber)' : 'var(--text-muted)', cursor: 'pointer', whiteSpace: 'nowrap' }}>
                              <input type="checkbox" checked={!!row.replace} onChange={() => toggleReplace(row.id)}
                                style={{ accentColor: 'var(--amber)', width: 14, height: 14 }} />
                              Replace
                            </label>
                          )}
                          <label title="Generate a Selenium script for this test case now"
                            style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12,
                              color: row.included ? 'var(--text-secondary)' : 'var(--text-dimmer)',
                              cursor: row.included ? 'pointer' : 'default', whiteSpace: 'nowrap' }}>
                            <input type="checkbox" checked={row.automate && row.included} disabled={!row.included}
                              onChange={() => toggleAutomate(row.id)} style={{ accentColor: 'var(--accent)', width: 14, height: 14 }} />
                            Automate
                          </label>
                          <select value={row.priority} onChange={(e) => setPriority(row.id, e.target.value)}
                            disabled={!row.included} title="Priority" aria-label={`Priority for: ${row.title}`}
                            style={{
                              background: 'var(--bg-input)', border: '1px solid var(--border-strong)',
                              borderRadius: 'var(--radius-sm)', color: 'var(--text-secondary)', fontFamily: 'inherit',
                              fontSize: 12, padding: '5px 6px', cursor: 'pointer', width: 62,
                            }}>
                            {PRIORITIES.map((p) => (
                              <option key={p.value} value={p.value} title={p.label}>{p.value}</option>
                            ))}
                          </select>
                        </div>
                      </div>

                      {expanded.has(row.id) && Array.isArray(row.steps) && row.steps.length > 0 && (
                        <div className={styles.stepTableWrap}>
                          <table className={styles.stepTable}>
                            <thead>
                              <tr>
                                <th scope="col">Step No</th>
                                <th scope="col">Step Description</th>
                                <th scope="col">Expected Result</th>
                              </tr>
                            </thead>
                            <tbody>
                              {row.steps.map((s, i) => (
                                <tr key={s.n ?? i}>
                                  <td className={styles.stepNumberCell}>{s.n ?? i + 1}</td>
                                  <td className={styles.stepDescriptionCell}>
                                    {s.step || '—'}
                                    {s.matched === false && (
                                      <span className={styles.stepUnmatched}>
                                        no matching UI element
                                      </span>
                                    )}
                                  </td>
                                  <td className={styles.stepExpectedCell}>
                                    {s.expected || '—'}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Footer ──────────────────────────────────────────────────── */}
      <div className={styles.footer}>
        <span className={styles.selectedCount}>
          {(() => {
            const keep = allIncluded.length
            const auto = rows.filter((r) => r.included && r.automate).length
            const drop = rows.length - keep
            const parts = [`${keep} saved as test case${keep === 1 ? '' : 's'}`]
            if (auto) parts.push(`${auto} scripted now`)
            if (keep - auto > 0) parts.push(`${keep - auto} to script later`)
            if (drop) parts.push(`${drop} discarded`)
            return parts.join(' · ')
          })()}
        </span>
        <button className="btn btn-primary" onClick={handleConfirm} disabled={allIncluded.length === 0}>
          <i className="ti ti-wand" aria-hidden="true" />
          {(() => {
            const keep = allIncluded.length
            const auto = rows.filter((r) => r.included && r.automate).length
            if (auto === 0) return `Save ${keep} test case${keep === 1 ? '' : 's'}`
            return `Save ${keep}, automate ${auto}`
          })()}
        </button>
      </div>
    </div>
  )
}