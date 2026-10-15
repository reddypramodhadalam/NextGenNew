'use client'
/**
 * app/scenarios/page.js
 *
 * The test repository, across every application.
 *
 * The application page answers "tell me about Netra" — it has environments,
 * suites, run buttons and history, all scoped to one thing. This answers a
 * different kind of question, and one you cannot get at by clicking through
 * four applications in turn:
 *
 *     how much of the estate is automated
 *     which P0 and P1 tests still have no script
 *     what came from an import versus what the AI proposed
 *     which tests cover a given requirement
 *
 * Acting on a selection reuses the endpoints that already exist, so there is
 * no second way to add something to a suite or generate a script.
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import Link from 'next/link'
import {
  Search, CheckCircle2, XCircle, MinusCircle, Zap, Layers, RefreshCw,
  ExternalLink,
} from 'lucide-react'
import {
  listRepositoryTestCases, getRepositorySummary,
  getSuites, setSuiteMembers, getSuite, generateScriptsFor,
} from '../../lib/api'
import styles from './Scenarios.module.css'

export default function ScenariosPage() {
  const [summary, setSummary] = useState(null)
  const [cases, setCases] = useState(null)
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState(() => new Set())
  const [toast, setToast] = useState(null)
  const [adding, setAdding] = useState(false)
  const [filters, setFilters] = useState({
    application_id: '', priority: '', automated: '', source: '', scenario: '', search: '',
  })

  const load = useCallback(async () => {
    setBusy(true)
    try {
      const d = await listRepositoryTestCases(filters)
      setCases(d.test_cases || [])
      setTotal(d.total || 0)
    } catch {
      setCases([])
    } finally { setBusy(false) }
  }, [filters])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    getRepositorySummary().then(setSummary).catch(() => setSummary(null))
  }, [])

  function set(k, v) {
    setFilters(f => ({ ...f, [k]: v }))
    setSelected(new Set())          // a filter change makes the old selection meaningless
  }

  function toggle(id) {
    setSelected(prev => {
      const n = new Set(prev)
      n.has(id) ? n.delete(id) : n.add(id)
      return n
    })
  }

  const chosen = useMemo(
    () => (cases || []).filter(c => selected.has(c.id)),
    [cases, selected]
  )

  // Suites belong to one application, so adding across applications is not a
  // thing that exists. Said plainly rather than silently doing something odd.
  const chosenApps = useMemo(
    () => Array.from(new Set(chosen.map(c => c.application_id))),
    [chosen]
  )
  const oneApplication = chosenApps.length === 1

  /**
   * Generate a script for everything selected.
   *
   * Generation is per application only because the endpoint takes an
   * application id — each test case already knows which one it belongs to,
   * so a selection spanning several is simply several calls. Blocking it
   * was a restriction I had no reason to impose; a suite is a different
   * matter, because a suite genuinely belongs to one application.
   */
  async function generateForSelected() {
    if (!chosen.length) return
    setBusy(true)
    const byApplication = chosen.reduce((acc, c) => {
      (acc[c.application_id] = acc[c.application_id] || []).push(c.id)
      return acc
    }, {})
    const failed = []
    for (const [appId, ids] of Object.entries(byApplication)) {
      try {
        await generateScriptsFor(Number(appId), ids)
      } catch (e) {
        const name = chosen.find(c => String(c.application_id) === appId)?.application_name || appId
        failed.push(`${name}: ${e.message}`)
      }
    }
    const apps = Object.keys(byApplication).length
    setToast(
      failed.length
        ? { message: `Some did not start — ${failed.join('; ')}`, tone: 'error' }
        : { message: `Generating ${chosen.length} script${chosen.length === 1 ? '' : 's'}`
                     + (apps > 1 ? ` across ${apps} applications` : '')
                     + ' — about a minute each.' }
    )
    setSelected(new Set())
    setBusy(false)
    setTimeout(load, 2000)
  }

  return (
    <div>
      <div className={styles.head}>
        <div>
          <h2 className={styles.title}>Scenarios</h2>
          <p className={styles.sub}>
            Every test case across every application — what is covered, what
            is not, and where it came from.
          </p>
        </div>
        <button className={styles.refresh} onClick={load} disabled={busy}>
          <RefreshCw size={14} strokeWidth={2} className={busy ? styles.spin : ''} />
          Refresh
        </button>
      </div>

      {summary && <SummaryStrip summary={summary} onFilter={set} />}

      <div className={styles.filters}>
        <div className={styles.searchBox}>
          <Search size={14} strokeWidth={2} />
          <input
            placeholder="Search test cases…"
            value={filters.search}
            onChange={e => set('search', e.target.value)}
          />
        </div>
        <select className={styles.select} value={filters.application_id}
                onChange={e => set('application_id', e.target.value)}>
          <option value="">All applications</option>
          {(summary?.applications || []).map(a => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
        <select className={styles.select} value={filters.priority}
                onChange={e => set('priority', e.target.value)}>
          <option value="">Any priority</option>
          {['P0', 'P1', 'P2', 'P3'].map(p => <option key={p} value={p}>{p}</option>)}
        </select>
        <select className={styles.select} value={filters.automated}
                onChange={e => set('automated', e.target.value)}>
          <option value="">Automated or not</option>
          <option value="yes">Automated</option>
          <option value="no">Not automated</option>
        </select>
        <select className={styles.select} value={filters.source}
                onChange={e => set('source', e.target.value)}>
          <option value="">Any source</option>
          <option value="author_agent">Proposed by AI</option>
          <option value="bulk_import">Imported</option>
          <option value="alm">ALM</option>
          <option value="jira">Jira</option>
          <option value="brd">BRD document</option>
          <option value="crawl">Crawl-only</option>
        </select>
        {(summary?.scenarios || []).length > 0 && (
          <select className={styles.select} value={filters.scenario}
                  onChange={e => set('scenario', e.target.value)}>
            <option value="">Any scenario</option>
            {summary.scenarios.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        )}
      </div>

      {cases === null && <div className="skeleton" style={{ height: 240 }} />}

      {cases && cases.length === 0 && (
        <div className={styles.empty}>Nothing matches these filters.</div>
      )}

      {cases && cases.length > 0 && (
        <>
          <div className={styles.countLine}>
            Showing {cases.length} of {total}
            {total > cases.length && ' — narrow the filters to see the rest'}
          </div>
          <div className="card" style={{ padding: 0 }}>
            {cases.map(c => (
              <label key={c.id} className={styles.row}>
                <input
                  type="checkbox"
                  className={styles.checkbox}
                  checked={selected.has(c.id)}
                  onChange={() => toggle(c.id)}
                />
                {c.priority && (
                  <span className={styles.prio} data-level={c.priority}>{c.priority}</span>
                )}
                <span className={styles.rowMain}>
                  <span className={styles.rowTitle}>{c.title}</span>
                  <span className={styles.rowMeta}>
                    <Link href={`/applications/${c.application_id}`} className={styles.appLink}>
                      {c.application_name}
                    </Link>
                    {c.scenario && <> · {c.scenario}</>}
                    {c.requirement_id && <> · <span className={styles.req}>{c.requirement_id}</span></>}
                    {c.source === 'bulk_import' && <> · <span className={styles.imported}>imported</span></>}
                    {c.origin && (
                      <>
                        {' '}· <span className={styles.origin} data-origin={String(c.origin).toLowerCase()}>
                          {String(c.origin).toLowerCase() === 'jira'
                            ? 'Jira'
                            : String(c.origin).toLowerCase() === 'brd'
                              ? 'BRD'
                              : String(c.origin).toLowerCase() === 'alm'
                                ? 'ALM'
                                : 'Crawl'}
                        </span>
                      </>
                    )}
                  </span>
                </span>
                {c.automated
                  ? <span className={styles.auto}><CheckCircle2 size={13} strokeWidth={2} /> automated</span>
                  : <span className={styles.notAuto}><MinusCircle size={13} strokeWidth={2} /> no script</span>}
                {c.run_count > 0 && (
                  <span className={styles.runs} data-result={c.last_result}>
                    {c.run_count} run{c.run_count === 1 ? '' : 's'}
                  </span>
                )}
              </label>
            ))}
          </div>
        </>
      )}

      {selected.size > 0 && (
        <div className={styles.actionBar}>
          <span className={styles.selCount}>
            {selected.size} selected
            {!oneApplication && (
              <span className={styles.selWarn}>
                {' '}across {chosenApps.length} applications — a suite belongs to one
              </span>
            )}
          </span>
          <button className={styles.barBtn} disabled={busy}
                  onClick={generateForSelected}
                  title="Generate a script for each selected test case">
            <Zap size={14} strokeWidth={2} /> Generate scripts
          </button>
          <button className={styles.barBtn} disabled={busy || !oneApplication}
                  onClick={() => setAdding(true)}
                  title={oneApplication
                    ? 'Add these to a suite'
                    : 'A suite belongs to one application — select from a single one'}>
            <Layers size={14} strokeWidth={2} /> Add to suite
          </button>
          <button className={styles.barGhost} onClick={() => setSelected(new Set())}>
            Clear
          </button>
        </div>
      )}

      {adding && oneApplication && (
        <AddToSuite
          applicationId={chosenApps[0]}
          caseIds={chosen.map(c => c.id)}
          onClose={() => setAdding(false)}
          onDone={(msg) => { setAdding(false); setSelected(new Set()); setToast({ message: msg }) }}
        />
      )}

      {toast && (
        <div className={`${styles.toast} ${toast.tone === 'error' ? styles.toastError : ''}`}>
          {toast.message}
          <button className={styles.toastClose} onClick={() => setToast(null)}>✕</button>
        </div>
      )}
    </div>
  )
}

/** Coverage at a glance, with each number acting as a filter. */
function SummaryStrip({ summary, onFilter }) {
  return (
    <div className={styles.summary}>
      <Stat label="Test cases" value={summary.total} />
      <Stat label="Automated" value={`${summary.automated} · ${summary.coverage_pct}%`}
            onClick={() => onFilter('automated', 'yes')} />
      <Stat label="Imported" value={summary.imported}
            onClick={() => onFilter('source', 'bulk_import')} />
      {/* The number that actually matters: critical tests with no automation. */}
      <Stat label="P0/P1 without a script" value={summary.critical_gap}
            tone={summary.critical_gap > 0 ? 'warn' : undefined}
            onClick={() => onFilter('automated', 'no')} />
    </div>
  )
}

function Stat({ label, value, tone, onClick }) {
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag className={`${styles.stat} ${tone === 'warn' ? styles.statWarn : ''}`} onClick={onClick}>
      <span className={styles.statValue}>{value}</span>
      <span className={styles.statLabel}>{label}</span>
    </Tag>
  )
}

/** Add the selection to one of that application's suites. */
function AddToSuite({ applicationId, caseIds, onClose, onDone }) {
  const [suites, setSuites] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    getSuites(applicationId)
      .then(d => setSuites(d.suites || []))
      .catch(() => setSuites([]))
  }, [applicationId])

  async function add(suite) {
    setBusy(true); setError(null)
    try {
      // Membership is set as a whole list, so the existing members have to be
      // read first — otherwise adding would quietly remove everything else.
      const full = await getSuite(suite.id)
      const existing = (full.suite?.members || []).map(m => m.id)
      const merged = Array.from(new Set([...existing, ...caseIds]))
      const res = await setSuiteMembers(suite.id, merged)
      if (res.error) { setError(res.error); return }
      const added = merged.length - existing.length
      onDone(`${added} test case${added === 1 ? '' : 's'} added to ${suite.name}.`)
    } catch (e) {
      setError(e.message)
    } finally { setBusy(false) }
  }

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()}>
        <div className={styles.modalHead}>
          <span className={styles.modalTitle}>
            Add {caseIds.length} test case{caseIds.length === 1 ? '' : 's'} to a suite
          </span>
          <button className={styles.ghost} onClick={onClose}>Close</button>
        </div>
        <div className={styles.modalBody}>
          {suites === null && <div className="skeleton" style={{ height: 80 }} />}
          {suites && suites.length === 0 && (
            <p className={styles.hint}>
              This application has no suites yet. Create one on its page first.
            </p>
          )}
          {suites && suites.map(s => (
            <button key={s.id} className={styles.suiteOption} disabled={busy}
                    onClick={() => add(s)}>
              <span className={styles.suiteKind} data-kind={s.kind}>{s.kind}</span>
              <span className={styles.suiteName}>{s.name}</span>
              <span className={styles.hint}>{s.member_count} already in it</span>
            </button>
          ))}
          {error && <div className={styles.error}>{error}</div>}
        </div>
      </div>
    </div>
  )
}