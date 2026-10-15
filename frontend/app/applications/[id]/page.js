'use client'
/**
 * app/applications/[id]/page.js
 *
 * Application detail with the full hierarchy:
 *   Application -> Scenarios -> Scripts
 *
 * Click a scenario to expand its scripts. Execute the whole scenario
 * (all its scripts) or a single script. Toggle headed/headless — headed
 * opens Chrome visibly (the live demo). Results appear inline and the
 * dashboard reflects them on next load.
 *
 * Test data lives in ONE place per application — the "Test data" panel
 * opened from the toolChips row below (see AppTestData). Flat keys
 * (username, password, ItemNumber, ...) are the application-wide default;
 * a "Per-form overrides" section inside that same panel lets a reviewer
 * set different values for the same key, keyed by the exact target_flow
 * text typed on the Configure step (e.g. "Sales Order Entry") — see
 * FormOverridesEditor below.
 *
 * There is deliberately no second file, no per-scenario override, and no
 * manual "link this scenario to a file" step: test_data/loader.py resolves
 * a form's own values automatically from this one file, by target_flow.
 */
import { useState, useEffect, useRef } from 'react'
import {
  Eye, Pencil, Download, Trash2, Clock, Play, Zap, MoreHorizontal,
  Layers, Plus, Check, Upload, ChevronDown, ChevronRight,
} from 'lucide-react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import {
  getApplication, getProposals, getScenarioScripts, runScript, runScenario,
  getExecutionHistory, screenshotUrl,
  getAppTestData, saveAppTestData, revealAppTestDataField,
  getScript, saveScript,
  archiveScript, restoreScript, deleteScript, archiveScenario, deleteScenario,
  generateScriptsFor,
  diagnoseFailure, approveHealing, rejectHealing,
  getEnvironments, createEnvironment, updateEnvironment, deleteEnvironment,
  getSuites, getSuiteCandidates, createSuite, getSuite, setSuiteMembers,
  deleteSuite, runSuite,
} from '../../../lib/api'
import styles from './AppDetail.module.css'
export default function ApplicationDetailPage() {
  const { id } = useParams()
  const [app, setApp] = useState(null)
  const [scenarios, setScenarios] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [headed, setHeaded] = useState(true) // demo default: watch the browser
  const [environments, setEnvironments] = useState([])
  const [envName, setEnvName] = useState(null)      // null = application default
  const [customUrl, setCustomUrl] = useState('')
  const [showEnvs, setShowEnvs] = useState(false)
  const [showTestData, setShowTestData] = useState(false)
  const [showSuites, setShowSuites] = useState(false)
  const [suiteCount, setSuiteCount] = useState(null)
  const [scriptModal, setScriptModal] = useState(null)
  const [removing, setRemoving] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [bulkBusy, setBulkBusy] = useState(null)
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false)
  const [toast, setToast] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [activeScenario, setActiveScenario] = useState(null)
  const [scenarioFilter, setScenarioFilter] = useState('')
  const runTarget = () => ({
    environment: envName === '__custom__' ? null : envName,
    target_url:  envName === '__custom__' ? customUrl : null,
  })
  useEffect(() => {
    if (!id) return
    getEnvironments(id)
      .then(d => {
        const list = d.environments || []
        setEnvironments(list)
        const def = list.find(e => e.is_default) || list[0]
        if (def && envName === null) setEnvName(def.name)
      })
      .catch(() => setEnvironments([]))
  }, [id, refreshKey]) // eslint-disable-line
  function toggleSelect(proposalId) {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(proposalId) ? next.delete(proposalId) : next.add(proposalId)
      return next
    })
  }
  function toggleSelectGroup(ids, select) {
    setSelected(prev => {
      const next = new Set(prev)
      ids.forEach(id => select ? next.add(id) : next.delete(id))
      return next
    })
  }
  async function runSelected() {
    const ids = Array.from(selected)
    setBulkBusy(`Running 0/${ids.length}`)
    let done = 0
    for (const pid of ids) {
      try { await runScenario(pid, headed, runTarget()) } catch {}
      done += 1
      setBulkBusy(`Running ${done}/${ids.length}`)
    }
    setBulkBusy(null)
    setRefreshKey(k => k + 1)
  }
  async function downloadSelected() {
    const ids = Array.from(selected)
    setBulkBusy(`Downloading 0/${ids.length}`)
    let done = 0
    for (const pid of ids) {
      try {
        const resp = await getScenarioScripts(pid)
        for (const s of (resp.scripts || [])) {
          await downloadScript(s)
        }
      } catch {}
      done += 1
      setBulkBusy(`Downloading ${done}/${ids.length}`)
    }
    setBulkBusy(null)
  }
  async function removeSelected(action) {
    const ids = Array.from(selected)
    setBulkBusy(`${action === 'archive' ? 'Archiving' : 'Deleting'} 0/${ids.length}`)
    let done = 0
    for (const pid of ids) {
      try {
        await (action === 'archive' ? archiveScenario(pid) : deleteScenario(pid))
      } catch {}
      done += 1
      setBulkBusy(`${action === 'archive' ? 'Archiving' : 'Deleting'} ${done}/${ids.length}`)
    }
    setBulkBusy(null)
    setSelected(new Set())
    setRefreshKey(k => k + 1)
  }
  async function openScript(script, readOnly) {
    setScriptModal({ script, readOnly, content: null, loading: true })
    try {
      const data = await getScript(script.generation_id)
      setScriptModal({ script, readOnly, content: data.script_content || '', title: data.title, loading: false })
    } catch (e) {
      setScriptModal({ script, readOnly, content: '', error: e.message, loading: false })
    }
  }
  async function downloadScript(script) {
    try {
      const data = await getScript(script.generation_id)
      const blob = new Blob([data.script_content || ''], { type: 'text/x-python' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = data.filename || `test_${script.generation_id}.py`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setToast({ message: 'Could not download: ' + e.message, tone: 'error' })
    }
  }
  useEffect(() => {
    (async () => {
      try {
        setApp(await getApplication(id))
        try { setScenarios(await getProposals(id) || []) } catch { setScenarios([]) }
      } catch (e) { setError(e.message) } finally { setLoading(false) }
    })()
  }, [id, refreshKey])

  if (loading) return <div className={styles.pad}><div className="skeleton" style={{ height: 40, width: 260, marginBottom: 20 }} /><div className="skeleton" style={{ height: 240 }} /></div>
  if (error) return <div className={styles.pad}><Link href="/applications" className={styles.back}>← Applications</Link><div className={styles.errBox}>Could not load: {error}</div></div>
  return (
    <div className={styles.pad}>
      <div className={styles.header}>
        <div className={styles.headLeft}>
          <div className={styles.logo}>{(app?.name || '?').slice(0, 2).toUpperCase()}</div>
          <div>
            <h1>{app?.name}</h1>
            <div className={styles.url}>{app?.base_url || app?.normalized_url || 'No URL'}</div>
          </div>
        </div>
        <div className={styles.envPicker} title="Which environment tests run against">
          <span className={styles.envLabel}>Environment</span>
          <select
            className={styles.envSelect}
            value={envName || ''}
            onChange={e => setEnvName(e.target.value)}
          >
            {environments.map(env => (
              <option key={env.id} value={env.name}>
                {env.name}{env.is_default ? ' (default)' : ''}
              </option>
            ))}
            <option value="__custom__">Custom URL…</option>
          </select>
          {envName === '__custom__' && (
            <input
              className={styles.envUrlInput}
              placeholder="https://…"
              value={customUrl}
              onChange={e => setCustomUrl(e.target.value)}
            />
          )}
          <button className={styles.envManage} onClick={() => setShowEnvs(true)}>Manage</button>
        </div>
        <div className={styles.toolChips}>
          <button className={styles.toolChip} onClick={() => setShowTestData(true)}>
            Test data
          </button>
          <button className={styles.toolChip} onClick={() => setShowSuites(true)}>
            Suites
            {suiteCount !== null && <span className={styles.chipCount}>{suiteCount}</span>}
          </button>
        </div>
        <label className={styles.headedToggle} title="Headed opens Chrome visibly">
          <input type="checkbox" checked={headed} onChange={e => setHeaded(e.target.checked)} />
          <span>{headed ? 'Headed (visible)' : 'Headless'}</span>
        </label>
      </div>
      {selected.size > 0 && (
        <div className={styles.bulkBar}>
          <span className={styles.bulkCount}>{selected.size} selected</span>
          <div className={styles.bulkActions}>
            <Act icon="play" tone="run" disabled={!!bulkBusy} onClick={runSelected}
                 label={bulkBusy && bulkBusy.startsWith('Running') ? bulkBusy : 'Run selected'} />
            <Act icon="download" disabled={!!bulkBusy} onClick={downloadSelected} label="Download selected" />
            <Act icon="clock" disabled={!!bulkBusy} onClick={() => removeSelected('archive')}
                 label={bulkBusy && bulkBusy.startsWith('Archiving') ? bulkBusy : 'Archive selected'} />
            <Act icon="trash" tone="danger" disabled={!!bulkBusy}
                 onClick={() => setConfirmBulkDelete(true)}
                 label={bulkBusy && bulkBusy.startsWith('Deleting') ? bulkBusy : 'Delete selected'} />
            <Act icon="eye" disabled={!!bulkBusy} onClick={() => setSelected(new Set())} label="Clear" />
          </div>
        </div>
      )}
      {scenarios.length === 0 ? (
        <div className="card">
          <div className={styles.empty}>
            <p>No scenarios yet.</p>
            <Link href={`/new-test-suite?app_id=${encodeURIComponent(String(id || ''))}`}><button className="btn btn-primary">Generate scenarios</button></Link>
          </div>
        </div>
      ) : (() => {
        const groups = groupScenarios(scenarios)
        const automated = scenarios.filter(s => (s.success_script_count || 0) > 0).length
        const incomplete = scenarios.filter(
          s => (s.success_script_count || 0) === 0 && (s.incomplete_script_count || 0) > 0
        ).length
        const notAutomated = scenarios.length - automated - incomplete
        const runs = scenarios.reduce((n, s) => n + (s.run_count || 0), 0)
        const neverRun = scenarios.filter(s => (s.run_count || 0) === 0).length
        const failing = scenarios.filter(s => s.last_result === 'fail').length
        const q = scenarioFilter.trim().toLowerCase()
        const shown = q
          ? groups.filter(g =>
              g.name.toLowerCase().includes(q) ||
              (g.requirement_id || '').toLowerCase().includes(q))
          : groups
        const active =
          (activeScenario && shown.some(g => g.name === activeScenario))
            ? shown.find(g => g.name === activeScenario)
            : shown[0] || null
        const activeIds = active ? active.cases.map(p => p.id) : []
        const activeSelected = activeIds.filter(i => selected.has(i)).length
        return (
          <>
            <div className={styles.summaryCardGrid}>
              <SummaryKpi
                label="Test Cases"
                value={scenarios.length}
                sub={`across ${groups.length} module${groups.length === 1 ? '' : 's'}`}
              />
              <SummaryKpi
                label="Automation Coverage"
                value={`${scenarios.length ? Math.round((automated / scenarios.length) * 100) : 0}%`}
                sub={`${automated} automated · ${incomplete} incomplete · ${notAutomated} manual`}
                tone="accent"
                progressPct={scenarios.length ? (automated / scenarios.length) * 100 : 0}
              />
              <SummaryKpi
                label="Executions"
                value={runs}
                sub={neverRun > 0 ? `${neverRun} never run` : 'all test cases have run'}
                tone={neverRun > 0 ? 'warn' : 'ok'}
              />
              <SummaryKpi
                label="Failing"
                value={failing}
                sub={failing > 0 ? 'test cases need attention' : 'all tests passing'}
                tone={failing > 0 ? 'danger' : 'ok'}
              />
            </div>
            <div className={styles.split}>
              <div className={styles.navPane}>
                <div className={styles.navSearch}>
                  <input
                    className={styles.navInput}
                    placeholder="Filter scenarios…"
                    value={scenarioFilter}
                    onChange={e => setScenarioFilter(e.target.value)}
                  />
                </div>
                <div className={styles.navList}>
                  {shown.length === 0 ? (
                    <div className={styles.navEmpty}>Nothing matches that filter.</div>
                  ) : shown.map(g => {
                    const auto = g.cases.filter(c => (c.success_script_count || 0) > 0).length
                    const incompleteOnly = g.cases.filter(c =>
                      (c.success_script_count || 0) === 0 && (c.incomplete_script_count || 0) > 0
                    ).length
                    const isActive = active && g.name === active.name
                    return (
                      <button
                        key={g.name}
                        type="button"
                        className={`${styles.navItem} ${isActive ? styles.navItemActive : ''}`}
                        onClick={() => setActiveScenario(g.name)}
                      >
                        <div className={styles.navItemTop}>
                          {g.origin && (
                            <span className={styles.provOrigin} data-origin={String(g.origin).toLowerCase()}
                                  title={`Source: ${g.origin}`}>
                              {String(g.origin).toLowerCase() === 'jira' ? 'Jira'
                                : String(g.origin).toLowerCase() === 'brd' ? 'BRD'
                                : String(g.origin).toLowerCase() === 'alm' ? 'ALM' : 'Crawl'}
                            </span>
                          )}
                          {g.requirement_id && !isPlaceholderRequirement(g.requirement_id) && (
                            <span className={styles.reqBadge}>{g.requirement_id}</span>
                          )}
                          <span className={styles.navItemName}>{g.name}</span>
                        </div>
                        <div className={styles.navItemMeta}>
                          <span>{g.cases.length} test{g.cases.length === 1 ? '' : 's'}</span>
                          <span className={auto === g.cases.length ? styles.navMetaOk : styles.navMetaWarn}>
                            {auto}/{g.cases.length} automated
                          </span>
                          {incompleteOnly > 0 && (
                            <span className={styles.navMetaIncomplete}>
                              {incompleteOnly} incomplete
                            </span>
                          )}
                        </div>
                      </button>
                    )
                  })}
                </div>
              </div>
              <div className={styles.detailPane}>
                {!active ? (
                  <div className={styles.navEmpty}>Select a scenario on the left.</div>
                ) : (
                  <>
                    <div className={styles.detailHead}>
                      <input
                        type="checkbox"
                        className={styles.checkbox}
                        checked={activeSelected === activeIds.length && activeIds.length > 0}
                        ref={el => { if (el) el.indeterminate = activeSelected > 0 && activeSelected !== activeIds.length }}
                        onChange={e => toggleSelectGroup(activeIds, e.target.checked)}
                        title="Select all in this scenario"
                      />
                      {active.origin && (
                        <span className={styles.provOrigin} data-origin={String(active.origin).toLowerCase()}
                              title={`Source: ${active.origin}`}>
                          {String(active.origin).toLowerCase() === 'jira' ? 'Jira'
                            : String(active.origin).toLowerCase() === 'brd' ? 'BRD'
                            : String(active.origin).toLowerCase() === 'alm' ? 'ALM' : 'Crawl'}
                        </span>
                      )}
                      {active.requirement_id && !isPlaceholderRequirement(active.requirement_id) && (
                        <span className={styles.reqBadge}>{active.requirement_id}</span>
                      )}
                      <span className={styles.detailTitle}>{active.name}</span>
                      <span className={styles.detailCount}>
                        {active.cases.length} test case{active.cases.length === 1 ? '' : 's'}
                        {active.scriptTotal > 0 && ` · ${active.scriptTotal} script${active.scriptTotal === 1 ? '' : 's'}`}
                      </span>
                    </div>
                    <div className={styles.detailBody}>
                      {active.cases.map(p => (
                        <ScenarioRow
                          key={p.id}
                          scenario={p}
                          headed={headed}
                          onOpenScript={openScript}
                          onDownload={downloadScript}
                          onRemove={setRemoving}
                          onRemoveScenario={setRemoving}
                          isSelected={selected.has(p.id)}
                          onToggleSelect={toggleSelect}
                          onAutomated={() => setRefreshKey(k => k + 1)}
                          runTarget={runTarget}
                          onNotify={(message, tone) => setToast({ message, tone })}
                        />
                      ))}
                    </div>
                  </>
                )}
              </div>
            </div>
          </>
        )
      })()}
      {confirmBulkDelete && (
        <ConfirmDialog
          title={`Delete ${selected.size} test case${selected.size === 1 ? '' : 's'}?`}
          body="This removes the test cases, their scripts, and all run history and screenshots. It cannot be undone. Archiving keeps the evidence instead."
          confirmLabel="Delete permanently"
          danger
          busy={!!bulkBusy}
          onConfirm={() => { setConfirmBulkDelete(false); removeSelected('delete') }}
          onCancel={() => setConfirmBulkDelete(false)}
        />
      )}
      {toast && <Toast message={toast.message} tone={toast.tone} onDone={() => setToast(null)} />}
      {showTestData && (
        <div className={styles.modalBackdrop} onClick={() => setShowTestData(false)}>
          <div className={styles.modal} onClick={e => e.stopPropagation()}
               style={{ maxWidth: 900, width: '100%', maxHeight: '84vh', display: 'flex', flexDirection: 'column' }}>
            <div className={styles.modalHead}>
              <span className={styles.modalTitle}>Test data</span>
              <button className={styles.historyBtn}
                      onClick={() => setShowTestData(false)}>Close</button>
            </div>
            <div className={styles.modalScrollBody}>
              <AppTestData
                applicationId={id}
                environments={environments}
                selectedEnvironment={
                  envName && envName !== '__custom__'
                    ? envName
                    : (environments.find(e => e.is_default)?.name || environments[0]?.name || 'default')
                }
                onEnvironmentChange={(nextEnv) => setEnvName(nextEnv)}
                open
                onToggle={() => setShowTestData(false)}
              />
            </div>
          </div>
        </div>
      )}
      {showSuites && (
        <div className={styles.modalBackdrop} onClick={() => setShowSuites(false)}>
          <div className={styles.modal} onClick={e => e.stopPropagation()}
               style={{ maxWidth: 820, width: '100%' }}>
            <div className={styles.modalHead}>
              <span className={styles.modalTitle}>Suites</span>
              <button className={styles.historyBtn}
                      onClick={() => setShowSuites(false)}>Close</button>
            </div>
            <SuitesCard
              applicationId={id}
              environment={envName === '__custom__' ? null : envName}
              headed={headed}
              open
              onToggle={() => setShowSuites(false)}
              onCount={setSuiteCount}
              onNotify={(message, tone) => setToast({ message, tone })}
            />
          </div>
        </div>
      )}
      {showEnvs && (
        <EnvironmentsModal
          applicationId={id}
          onClose={() => setShowEnvs(false)}
          onChanged={() => setRefreshKey(k => k + 1)}
        />
      )}
      {removing && (
        <RemoveDialog
          target={removing}
          onClose={() => setRemoving(null)}
          onDone={() => { setRemoving(null); setRefreshKey(k => k + 1) }}
        />
      )}
      {scriptModal && (
        <ScriptModal
          state={scriptModal}
          onClose={() => setScriptModal(null)}
          onSaved={(content) => setScriptModal(m => ({ ...m, content, readOnly: true }))}
        />
      )}
    </div>
  )
}
function SuitesCard({ applicationId, environment, headed, open = false, onToggle, onNotify, onCount }) {
  const [suites, setSuites] = useState(null)
  const [picker, setPicker] = useState(null)
  const [runningId, setRunningId] = useState(null)
  async function load() {
    try {
      const d = await getSuites(applicationId)
      const list = d.suites || []
      setSuites(list)
      onCount?.(list.length)
    } catch { setSuites([]); onCount?.(0) }
  }
  useEffect(() => { load() }, [applicationId]) // eslint-disable-line
  async function run(suite) {
    setRunningId(suite.id)
    try {
      const r = await runSuite(suite.id, { environment, headed })
      if (r.error) { onNotify?.(r.error, 'error'); return }
      onNotify?.(
        `${r.suite}: ${r.passed} passed, ${r.failed} failed` +
        (r.skipped ? `, ${r.skipped} skipped (no script)` : ''),
        r.failed ? 'error' : undefined
      )
    } catch (e) {
      onNotify?.('Suite run failed: ' + e.message, 'error')
    } finally { setRunningId(null) }
  }
  return (
    <div>
      <p className={styles.cardSub} style={{ padding: '14px 20px 0', margin: 0 }}>
        Named selections of existing test cases — smoke runs the critical
        path, regression runs everything.
      </p>
      {open && (
        <div className={styles.suiteBody}>
          {suites === null && <div className="skeleton" style={{ height: 60 }} />}
          {suites && suites.length === 0 && (
            <div className={styles.noScripts}>No suites yet.</div>
          )}
          {suites && suites.map(s => (
            <div key={s.id} className={styles.suiteRow}>
              <span className={styles.suiteKind} data-kind={s.kind}>{s.kind}</span>
              <span className={styles.suiteName}>{s.name}</span>
              <span className={styles.metaText}>
                {s.member_count} test case{s.member_count === 1 ? '' : 's'}
                {' · '}{s.automated_count} automated
                {s.member_count - s.automated_count > 0 &&
                  ` · ${s.member_count - s.automated_count} without a script`}
              </span>
              <div className={styles.suiteActions}>
                <Act icon="play" tone="run" disabled={runningId === s.id}
                     label={runningId === s.id ? 'Running…' : 'Run'}
                     onClick={() => run(s)} />
                <Act icon="edit" label="Edit" onClick={() => setPicker({ suite: s })} />
                <Act icon="trash" label="Delete" tone="danger"
                     onClick={async () => {
                       await deleteSuite(s.id); load()
                     }} />
              </div>
            </div>
          ))}
          <div className={styles.suiteAdd}>
            <Act icon="plus" label="New smoke suite" onClick={() => setPicker({ kind: 'smoke' })} />
            <Act icon="plus" label="New regression suite" onClick={() => setPicker({ kind: 'regression' })} />
            <Act icon="plus" label="Custom" onClick={() => setPicker({ kind: 'custom' })} />
          </div>
        </div>
      )}
      {picker && (
        <SuitePicker
          applicationId={applicationId}
          kind={picker.kind}
          suite={picker.suite}
          onClose={() => setPicker(null)}
          onSaved={() => { setPicker(null); load() }}
          onNotify={onNotify}
        />
      )}
    </div>
  )
}
function SuitePicker({ applicationId, kind, suite, onClose, onSaved, onNotify }) {
  const editing = !!suite
  const [name, setName] = useState(suite?.name || '')
  const [rows, setRows] = useState(null)
  const [chosen, setChosen] = useState(() => new Set())
  const [filter, setFilter] = useState('all')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  useEffect(() => {
    (async () => {
      try {
        const d = await getSuiteCandidates(applicationId, suite?.kind || kind || 'custom')
        setRows(d.candidates || [])
        if (editing) {
          const full = await getSuite(suite.id)
          setChosen(new Set((full.suite?.members || []).map(m => m.id)))
        } else {
          setChosen(new Set((d.candidates || []).filter(r => r.suggested).map(r => r.id)))
          setName(suggestName(kind))
        }
      } catch (e) { setError(e.message); setRows([]) }
    })()
  }, []) // eslint-disable-line
  function suggestName(k) {
    if (k === 'smoke') return 'Smoke Suite 1'
    if (k === 'regression') return 'Regression Suite 1'
    return ''
  }
  function toggle(id) {
    setChosen(prev => {
      const n = new Set(prev)
      n.has(id) ? n.delete(id) : n.add(id)
      return n
    })
  }
  async function save() {
    if (!name.trim()) { setError('A suite name is required.'); return }
    setSaving(true); setError(null)
    try {
      if (editing) {
        await setSuiteMembers(suite.id, Array.from(chosen))
      } else {
        const res = await createSuite(applicationId, {
          name: name.trim(),
          kind: kind || 'custom',
          member_ids: Array.from(chosen),
        })
        if (res.error) { setError(res.error); return }
      }
      onSaved()
    } catch (e) { setError(e.message) } finally { setSaving(false) }
  }
  const visible = (rows || []).filter(r =>
    filter === 'all' ? true : filter === 'automated' ? r.automated : !r.automated
  )
  const chosenAutomated = (rows || []).filter(r => chosen.has(r.id) && r.automated).length
  return (
    <div className={styles.modalBackdrop} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()} style={{ maxWidth: 780 }}>
        <div className={styles.modalHead}>
          <span className={styles.modalTitle}>
            {editing ? `Edit ${suite.name}` : `New ${kind} suite`}
          </span>
          <button className={styles.historyBtn} onClick={onClose}>Close</button>
        </div>
        <div style={{ padding: '16px 20px', overflowY: 'auto' }}>
          {!editing && (
            <div className={styles.suiteNameRow}>
              <label className={styles.envLabel}>Name</label>
              <input className={styles.envInput} value={name}
                     onChange={e => setName(e.target.value)}
                     placeholder="e.g. Smoke Suite 1" />
            </div>
          )}
          <p className={styles.envHelp}>
            {kind === 'smoke' && 'P0 and P1 automated tests are pre-selected — the critical path, meant to finish in minutes. Adjust as you like.'}
            {kind === 'regression' && 'Every automated test is pre-selected — regression means "did anything break".'}
            {(!kind || kind === 'custom') && 'Choose the test cases this suite should run.'}
          </p>
          <div className={styles.suiteFilters}>
            {['all', 'automated', 'not automated'].map(f => (
              <button key={f}
                      className={`${styles.filterChip} ${filter === f.split(' ')[0] || (f === 'not automated' && filter === 'not') ? styles.filterOn : ''}`}
                      onClick={() => setFilter(f === 'not automated' ? 'not' : f)}>
                {f}
              </button>
            ))}
            <span className={styles.metaText} style={{ marginLeft: 'auto' }}>
              {chosen.size} selected · {chosenAutomated} automated
            </span>
          </div>
          {rows === null ? (
            <div className="skeleton" style={{ height: 200 }} />
          ) : (
            <div className={styles.candidateList}>
              {visible.map(r => (
                <label key={r.id} className={styles.candidateRow}>
                  <input type="checkbox" className={styles.checkbox}
                         checked={chosen.has(r.id)} onChange={() => toggle(r.id)} />
                  {r.priority && (
                    <span className={styles.prio} data-level={r.priority}>
                      <span className={styles.prioDot} />{r.priority}
                    </span>
                  )}
                  <span className={styles.candidateTitle}>{r.title}</span>
                  {r.automated
                    ? <span className={styles.metaText}>automated</span>
                    : <span className={styles.metaMuted}>no script</span>}
                </label>
              ))}
              {visible.length === 0 && <div className={styles.noScripts}>Nothing matches this filter.</div>}
            </div>
          )}
          {error && <div className={styles.modalErr}>{error}</div>}
        </div>
        <div className={styles.modalFoot}>
          <span className={styles.metaText}>
            {chosen.size - chosenAutomated > 0 &&
              `${chosen.size - chosenAutomated} selected test case(s) have no script yet and will be skipped when the suite runs.`}
          </span>
          <div style={{ display: 'flex', gap: 9 }}>
            <button className={styles.historyBtn} onClick={onClose}>Cancel</button>
            <button className="btn btn-primary" onClick={save} disabled={saving || chosen.size === 0}>
              {saving ? 'Saving…' : editing ? 'Save members' : 'Create suite'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
function EnvironmentsModal({ applicationId, onClose, onChanged }) {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState({ name: '', base_url: '' })
  async function load() {
    try {
      const d = await getEnvironments(applicationId)
      setRows(d.environments || [])
    } catch (e) { setError(e.message); setRows([]) }
  }
  useEffect(() => { load() }, []) // eslint-disable-line
  async function add() {
    if (!draft.name.trim() || !draft.base_url.trim()) {
      setError('Both a name and a URL are required.'); return
    }
    setBusy(true); setError(null)
    try {
      const res = await createEnvironment(applicationId, {
        name: draft.name.trim(),
        base_url: draft.base_url.trim(),
        is_default: (rows || []).length === 0,
      })
      if (res.error) setError(res.error)
      else { setDraft({ name: '', base_url: '' }); await load(); onChanged && onChanged() }
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  async function saveRow(env, patch) {
    setBusy(true); setError(null)
    try {
      const res = await updateEnvironment(env.id, patch)
      if (res.error) setError(res.error)
      else { await load(); onChanged && onChanged() }
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  async function remove(env) {
    setBusy(true)
    try { await deleteEnvironment(env.id); await load(); onChanged && onChanged() }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  return (
    <div className={styles.modalBackdrop} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()} style={{ maxWidth: 720 }}>
        <div className={styles.modalHead}>
          <span className={styles.modalTitle}>Environments</span>
          <button className={styles.historyBtn} onClick={onClose}>Close</button>
        </div>
        <div style={{ padding: '16px 20px' }}>
          <p className={styles.envHelp}>
            A test case carries no URL — it is written once and run anywhere.
            The environment supplies the URL when the test runs, and each result
            records which environment it ran against.
          </p>
          {rows === null ? (
            <div className="skeleton" style={{ height: 90 }} />
          ) : (
            <div className={styles.envTable}>
              {rows.map(env => (
                <EnvRow key={env.id} env={env} busy={busy}
                        onSave={saveRow} onRemove={remove} />
              ))}
              {rows.length === 0 && (
                <div className={styles.noScripts}>No environments yet. Add one below.</div>
              )}
            </div>
          )}
          <div className={styles.envAdd}>
            <input className={styles.envInput} placeholder="name (dev, test, stage, prod)"
                   value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} />
            <input className={styles.envInput} style={{ flex: 2 }} placeholder="https://…"
                   value={draft.base_url} onChange={e => setDraft({ ...draft, base_url: e.target.value })} />
            <button className="btn btn-primary" onClick={add} disabled={busy}>Add</button>
          </div>
          {error && <div className={styles.modalErr}>{error}</div>}
        </div>
      </div>
    </div>
  )
}
function EnvRow({ env, busy, onSave, onRemove }) {
  const [url, setUrl] = useState(env.base_url)
  const [confirmRemove, setConfirmRemove] = useState(false)
  const dirty = url !== env.base_url
  return (
    <div className={styles.envRow}>
      <span className={styles.envName}>
        {env.name}
        {env.is_default && <span className={styles.envDefault}>default</span>}
      </span>
      <input className={styles.envInput} style={{ flex: 2 }} value={url}
             onChange={e => setUrl(e.target.value)} />
      {dirty && (
        <button className="btn btn-secondary" disabled={busy}
                onClick={() => onSave(env, { base_url: url })}>Save</button>
      )}
      {!env.is_default && (
        <button className={styles.historyBtn} disabled={busy}
                onClick={() => onSave(env, { is_default: true })}>Make default</button>
      )}
      {confirmRemove ? (
        <>
          <span className={styles.envConfirm}>Remove?</span>
          <button className={styles.deleteBtn} disabled={busy}
                  onClick={() => { setConfirmRemove(false); onRemove(env) }}>Yes</button>
          <button className={styles.historyBtn} disabled={busy}
                  onClick={() => setConfirmRemove(false)}>No</button>
        </>
      ) : (
        <button className={styles.dangerBtn} disabled={busy}
                onClick={() => setConfirmRemove(true)}>Remove</button>
      )}
    </div>
  )
}
function ScriptHistory({ script, result }) {
  const [history, setHistory] = useState(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getExecutionHistory(script.generation_id)
      .then(h => { if (!cancelled) { setHistory(h); setLoading(false) } })
      .catch(() => { if (!cancelled) { setHistory({ run_count: 0, history: [] }); setLoading(false) } })
    return () => { cancelled = true }
  }, [script.generation_id, result])
  if (loading) return <div className="skeleton" style={{ height: 48, margin: '8px 0' }} />
  if (!history) return null
  const runs = history.history || []
  const latest = runs[0]
  const recent = runs.slice(0, 7)
  const passedRecent = recent.filter(r => r.result === 'pass').length
  return (
    <div className={styles.historyBox}>
      <div className={styles.historyHead}>
        {script.generation_status === 'incomplete' && (
          <span className={styles.metaIncomplete} title={script.notes || 'Generated script failed verification'}>
            Incomplete
          </span>
        )}
        {' '}
        Executed <b>{history.run_count}</b> time{history.run_count !== 1 ? 's' : ''}
        {recent.length > 1 && (
          <>
            {' · '}
            <span className={styles.trend}>
              {recent.slice().reverse().map((r, i) => (
                <span key={i}
                      className={`${styles.trendBar} ${r.result === 'pass' ? styles.trendPass : styles.trendFail}`}
                      title={`${r.result} · ${r.executed_at}`} />
              ))}
            </span>
            {' '}
            <span className={styles.trendText}>
              {passedRecent} of last {recent.length} passed
            </span>
          </>
        )}
        <a href={`/executions?test=${encodeURIComponent(script.title || '')}&application_id=${script.application_id || ''}`}
           className={styles.allRuns}>see all runs →</a>
      </div>
      {runs.length === 0 && (
        <div className={styles.noScripts}>No runs recorded yet. Click Run.</div>
      )}
      {(latest ? [latest] : []).map(run => (
        <div key={run.execution_id} className={styles.runRow}>
          <div className={styles.runMeta}>
            <ResultPill result={run.result} />
            <span className={styles.runTime}>{run.executed_at}</span>
            <span className={styles.runDur}>
              {run.duration_ms ? (run.duration_ms / 1000).toFixed(1) + 's' : ''}
            </span>
            {run.environment && <span className={styles.metaText}>{run.environment}</span>}
          </div>
          {run.output && (
            <details className={styles.failDetails}>
              <summary>Why it failed</summary>
              <pre className={styles.failOutput}>{run.output}</pre>
            </details>
          )}
          {(run.result === 'fail' || run.result === 'error') && (
            <HealPanel
              generationId={script.generation_id}
              executionId={run.execution_id}
            />
          )}
          {run.screenshots && run.screenshots.length > 0 && (
            <div className={styles.shots}>
              {run.screenshots.map((s, i) => (
                <a key={i} href={screenshotUrl(s)} target="_blank" rel="noreferrer">
                  <img src={screenshotUrl(s)} alt={`step ${i + 1}`} className={styles.shot} />
                </a>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
function HealPanel({ generationId, executionId, onApplied, onOpenScript }) {
  const [busy, setBusy] = useState(false)
  const [proposal, setProposal] = useState(null)
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)
  async function diagnose() {
    setBusy(true); setError(null)
    try {
      const res = await diagnoseFailure(generationId, executionId)
      if (res.error) setError(res.error)
      else setProposal(res)
    } catch (e) {
      setError(e.message)
    } finally { setBusy(false) }
  }
  async function decide(approve) {
    setBusy(true); setError(null)
    try {
      const res = approve
        ? await approveHealing(proposal.healing_id)
        : await rejectHealing(proposal.healing_id)
      if (res.error) { setError(res.error); return }
      setDone(approve ? 'applied' : 'rejected')
      if (approve && onApplied) onApplied()
    } catch (e) {
      setError(e.message)
    } finally { setBusy(false) }
  }
  if (done === 'applied') {
    return (
      <div className={styles.healDone}>
        Fix applied. Run the test again to confirm it passes.
      </div>
    )
  }
  if (done === 'rejected') {
    return <div className={styles.healNote}>Proposal rejected. The script is unchanged.</div>
  }
  if (!proposal) {
    return (
      <div className={styles.healBar}>
        <Act icon="bolt" label={busy ? 'Diagnosing…' : 'Diagnose'} onClick={diagnose}
             disabled={busy} tone="run"
             title="Compare the script against the page as it is now" />
        <span className={styles.healHint}>
          {busy
            ? 'Opening the page and comparing it with the script — this takes 30-60s.'
            : 'Check whether a selector no longer matches the page.'}
        </span>
        {error && <span className={styles.healError}>{error}</span>}
      </div>
    )
  }
  if (!proposal.new_selector) {
    return (
      <div className={styles.healBox}>
        <div className={styles.healHead}>
          Diagnosis — not a selector problem
          <span className={styles.healConf} data-c={proposal.confidence}>
            {proposal.confidence} confidence
          </span>
        </div>
        <div className={styles.healRow}>
          <span className={styles.healLabel}>selector</span>
          <code className={styles.healNew}>{proposal.old_selector}</code>
        </div>
        <p className={styles.healWhy}>{proposal.reasoning}</p>
        <div className={styles.healActions}>
          <Act icon="edit" label="Edit the script" onClick={() => onOpenScript && onOpenScript()} />
          <Act icon="trash" label="Dismiss" tone="danger" disabled={busy}
               onClick={() => decide(false)} />
        </div>
      </div>
    )
  }
  return (
    <div className={styles.healBox}>
      <div className={styles.healHead}>
        Proposed fix
        <span className={styles.healConf} data-c={proposal.confidence}>
          {proposal.confidence} confidence
        </span>
      </div>
      {proposal.new_selector ? (
        <div className={styles.healDiff}>
          <div className={styles.healRow}>
            <span className={styles.healLabel}>was</span>
            <code className={styles.healOld}>{proposal.old_selector}</code>
          </div>
          <div className={styles.healRow}>
            <span className={styles.healLabel}>now</span>
            <code className={styles.healNew}>{proposal.new_selector}</code>
          </div>
        </div>
      ) : (
        <div className={styles.healDiff}>
          <div className={styles.healRow}>
            <span className={styles.healLabel}>selector</span>
            <code className={styles.healNew}>{proposal.old_selector}</code>
          </div>
          <div className={styles.healNoFix}>
            Not a selector problem — no change proposed.
          </div>
        </div>
      )}
      {proposal.reasoning && <p className={styles.healWhy}>{proposal.reasoning}</p>}
      {error && <div className={styles.healError}>{error}</div>}
      <div className={styles.healActions}>
        {proposal.new_selector && (
          <Act icon="play" label="Approve and apply" tone="run" disabled={busy}
               onClick={() => decide(true)} />
        )}
        <Act icon="trash" label={proposal.new_selector ? 'Reject' : 'Dismiss'} tone="danger"
             disabled={busy} onClick={() => decide(false)} />
      </div>
    </div>
  )
}
function ConfirmDialog({ title, body, confirmLabel, danger, busy, onConfirm, onCancel }) {
  return (
    <div className={styles.modalBackdrop} onClick={onCancel}>
      <div className={styles.confirmBox} onClick={e => e.stopPropagation()}>
        <h3 className={styles.confirmTitle}>{title}</h3>
        {body && <p className={styles.confirmBody}>{body}</p>}
        <div className={styles.confirmFoot} style={{ gap: 9 }}>
          <button className={styles.historyBtn} onClick={onCancel} disabled={busy}>Cancel</button>
          <button
            className={danger ? styles.deleteBtn : 'btn btn-primary'}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? 'Working…' : (confirmLabel || 'Confirm')}
          </button>
        </div>
      </div>
    </div>
  )
}
function Toast({ message, tone, onDone }) {
  useEffect(() => {
    const t = setTimeout(onDone, 5000)
    return () => clearTimeout(t)
  }, [onDone])
  return (
    <div className={`${styles.toast} ${tone === 'error' ? styles.toastError : ''}`}>
      {message}
      <button className={styles.toastClose} onClick={onDone}>✕</button>
    </div>
  )
}
function RemoveDialog({ target, onClose, onDone }) {
  const isScenario = target.kind === 'scenario'
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const name = target.title || `Script ${target.generation_id}`
  async function run(action) {
    setBusy(action); setError(null)
    try {
      if (isScenario) {
        await (action === 'archive' ? archiveScenario(target.id) : deleteScenario(target.id))
      } else {
        await (action === 'archive' ? archiveScript(target.generation_id) : deleteScript(target.generation_id))
      }
      onDone()
    } catch (e) {
      setError(e.message); setBusy(null)
    }
  }
  return (
    <div className={styles.modalBackdrop} onClick={onClose}>
      <div className={styles.confirmBox} onClick={e => e.stopPropagation()}>
        <h3 className={styles.confirmTitle}>Remove {isScenario ? 'scenario' : 'script'}?</h3>
        <p className={styles.confirmName}>{name}</p>
        <div className={styles.confirmOption}>
          <strong>Archive</strong>
          <span>
            Keeps the record and its run history, but stops offering it for
            execution. Recommended — audit evidence is preserved.
          </span>
          <button className="btn btn-secondary" onClick={() => run('archive')} disabled={!!busy}>
            {busy === 'archive' ? 'Archiving…' : 'Archive'}
          </button>
        </div>
        <div className={`${styles.confirmOption} ${styles.confirmDanger}`}>
          <strong>Delete permanently</strong>
          <span>
            {isScenario
              ? 'Deletes the scenario, every script under it, and all their execution history and screenshots. This cannot be undone.'
              : 'Deletes the script and all of its execution history and screenshots. This cannot be undone.'}
          </span>
          <button className={styles.deleteBtn} onClick={() => run('delete')} disabled={!!busy}>
            {busy === 'delete' ? 'Deleting…' : 'Delete permanently'}
          </button>
        </div>
        {error && <div className={styles.modalErr}>{error}</div>}
        <div className={styles.confirmFoot}>
          <button className={styles.historyBtn} onClick={onClose} disabled={!!busy}>Cancel</button>
        </div>
      </div>
    </div>
  )
}
function ScriptModal({ state, onClose, onSaved }) {
  const { script, readOnly, content, loading, error, title } = state
  const [draft, setDraft] = useState(content || '')
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState(null)
  useEffect(() => { setDraft(content || '') }, [content])
  async function save() {
    setSaving(true); setSaveErr(null)
    try {
      const res = await saveScript(script.generation_id, draft)
      if (res.error) { setSaveErr(res.error); return }
      onSaved(draft)
    } catch (e) {
      setSaveErr(e.message)
    } finally { setSaving(false) }
  }
  return (
    <div className={styles.modalBackdrop} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()}>
        <div className={styles.modalHead}>
          <span className={styles.modalTitle}>
            {readOnly ? 'View script' : 'Edit script'} — {title || script.title}
          </span>
          <div className={styles.modalActions}>
            {!readOnly && (
              <button className="btn btn-primary" onClick={save} disabled={saving}>
                {saving ? 'Saving…' : 'Save'}
              </button>
            )}
            <button className={styles.historyBtn} onClick={onClose}>Close</button>
          </div>
        </div>
        {saveErr && <div className={styles.modalErr}>{saveErr}</div>}
        {error && <div className={styles.modalErr}>{error}</div>}
        {loading ? (
          <div className="skeleton" style={{ height: 300, margin: 16 }} />
        ) : readOnly ? (
          <pre className={styles.codeView}>{draft}</pre>
        ) : (
          <textarea className={styles.codeEdit} value={draft} onChange={e => setDraft(e.target.value)} spellCheck={false} />
        )}
      </div>
    </div>
  )
}
const ICONS = {
  eye: Eye, edit: Pencil, download: Download, trash: Trash2,
  clock: Clock, play: Play, bolt: Zap, layers: Layers, plus: Plus, check: Check,
}
function Icon({ name, size = 14 }) {
  const C = ICONS[name] || Eye
  return <C size={size} strokeWidth={1.9} className={styles.icon} />
}
function Act({ icon, label, onClick, tone, disabled, title }) {
  return (
    <button
      className={`${styles.act} ${tone === 'run' ? styles.actRun : ''} ${tone === 'danger' ? styles.actDanger : ''}`}
      onClick={onClick}
      disabled={disabled}
      title={title || label}
    >
      <Icon name={icon} />{label}
    </button>
  )
}
/**
 * Top-of-page summary tile — 4 cards instead of the previous 9 separate
 * numbers (Test cases/Automated/Incomplete/Not automated/Runs/Never
 * run/Failing/Scenarios/Coverage%). Each card leads with ONE primary
 * metric and folds the rest into a compact sub-line, the same
 * information-density pattern used by enterprise dashboards (Datadog,
 * Vercel, Linear) — a handful of confident numbers rather than a long
 * row of equally-weighted stats. No data is dropped: every original
 * number still appears, just grouped under the metric it belongs to
 * (e.g. Incomplete/Not automated live inside the Coverage card's
 * sub-line instead of standing alone).
 *
 * @param {string} [tone] - 'accent' | 'ok' | 'warn' | 'danger' — colors
 *   the value, mirroring the Dashboard Kpi tone convention so the two
 *   pages read consistently.
 * @param {number} [progressPct] - When provided (Coverage card only),
 *   renders the existing coverage progress bar under the sub-line.
 */
function SummaryKpi({ label, value, sub, tone, progressPct }) {
  const toneClass =
    tone === 'accent' ? styles.summaryCardValAccent :
    tone === 'ok' ? styles.summaryCardValOk :
    tone === 'warn' ? styles.summaryCardValWarn :
    tone === 'danger' ? styles.summaryCardValDanger : ''
  return (
    <div className={styles.summaryCard}>
      <div className={styles.summaryCardLabel}>{label}</div>
      <div className={`${styles.summaryCardVal} ${toneClass}`}>{value}</div>
      {sub && <div className={styles.summaryCardSub}>{sub}</div>}
      {typeof progressPct === 'number' && (
        <div className={styles.coverageTrack}>
          <div className={styles.coverageFill} style={{ width: `${progressPct}%` }} />
        </div>
      )}
    </div>
  )
}
function MoreMenu({ items }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])
  return (
    <div className={styles.moreWrap} ref={ref}>
      <button
        className={styles.moreBtn}
        onClick={(e) => { e.stopPropagation(); setOpen(o => !o) }}
        title="More actions"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <MoreHorizontal size={15} strokeWidth={2} className={styles.icon} />
      </button>
      {open && (
        <div className={styles.moreMenu} role="menu">
          {items.map((it, i) =>
            it.divider ? (
              <div key={i} className={styles.moreDivider} />
            ) : (
              <button
                key={i}
                className={`${styles.moreItem} ${it.danger ? styles.moreDanger : ''}`}
                onClick={(e) => { e.stopPropagation(); setOpen(false); it.onClick() }}
                role="menuitem"
              >
                <Icon name={it.icon} />{it.label}
              </button>
            )
          )}
        </div>
      )}
    </div>
  )
}
function groupScenarios(proposals) {
  const map = new Map()
  for (const p of proposals) {
    const tags = p.tags || {}
    const name = tags.scenario || 'General'
    if (!map.has(name)) {
      map.set(name, { name, requirement_id: tags.requirement_id || null, origin: p.origin || null, cases: [] })
    }
    const g = map.get(name)
    if (!g.requirement_id && tags.requirement_id) g.requirement_id = tags.requirement_id
    if (!g.origin && p.origin) g.origin = p.origin
    g.cases.push(p)
  }
  for (const g of map.values()) {
    g.cases.sort((a, b) => (b.script_count || 0) - (a.script_count || 0))
    g.scriptTotal = g.cases.reduce((n, p) => n + (p.script_count || 0), 0)
  }
  return Array.from(map.values())
}

/**
 * True when a requirement_id is the raw "TARGET_FLOW" placeholder rather
 * than a real requirement reference (e.g. a Jira ticket or BRD section
 * number). Crawl-originated scenarios carry this literal tag value
 * instead of an actual requirement — surfacing it as a badge next to
 * every scenario added no information, so it is hidden both in the
 * scenario nav list and the detail header (see the two reqBadge render
 * sites above). A genuine requirement_id from Jira/BRD-origin scenarios
 * is unaffected and still renders normally.
 */
function isPlaceholderRequirement(requirementId) {
  return typeof requirementId === 'string' && requirementId.trim().toUpperCase() === 'TARGET_FLOW'
}

function ScenarioGroup({ group, headed, onOpenScript, onDownload, onRemove,
                         selected, onToggleSelect, onToggleGroup, onAutomated, runTarget, onNotify }) {
  const [open, setOpen] = useState(true)
  const ids = group.cases.map(p => p.id)
  const selectedCount = ids.filter(id => selected.has(id)).length
  const allSelected = selectedCount === ids.length && ids.length > 0
  const someSelected = selectedCount > 0
  return (
    <div className={styles.group}>
      <div className={styles.groupHead} onClick={() => setOpen(!open)}>
        <input
          type="checkbox"
          className={styles.checkbox}
          checked={allSelected}
          ref={el => { if (el) el.indeterminate = someSelected && !allSelected }}
          onClick={e => e.stopPropagation()}
          onChange={e => onToggleGroup(group.cases.map(p => p.id), e.target.checked)}
          title="Select all in this scenario"
        />
        <span className={`${styles.chevron} ${open ? styles.chevronOpen : ''}`}>▶</span>
        {group.origin && (
          <span className={styles.provOrigin} data-origin={String(group.origin).toLowerCase()}
                title={`Source: ${group.origin}`}>
            {String(group.origin).toLowerCase() === 'jira' ? 'Jira'
              : String(group.origin).toLowerCase() === 'brd' ? 'BRD'
              : String(group.origin).toLowerCase() === 'alm' ? 'ALM' : 'Crawl'}
          </span>
        )}
        {group.requirement_id && (
          <span className={styles.reqBadge} title="Requirement this scenario verifies">
            {group.requirement_id}
          </span>
        )}
        <span className={styles.groupName}>{group.name}</span>
        <span className={styles.groupCount}>
          {group.cases.length} test case{group.cases.length !== 1 ? 's' : ''}
          {group.scriptTotal > 0 && ` · ${group.scriptTotal} script${group.scriptTotal === 1 ? '' : 's'}`}
        </span>
      </div>
      {open && (
        <div className={styles.groupBody}>
          {group.cases.map(p => (
            <ScenarioRow
              key={p.id}
              scenario={p}
              headed={headed}
              onOpenScript={onOpenScript}
              onDownload={onDownload}
              onRemove={onRemove}
              onRemoveScenario={onRemove}
              isSelected={selected.has(p.id)}
              onToggleSelect={onToggleSelect}
              onAutomated={onAutomated}
              runTarget={runTarget}
              onNotify={onNotify}
            />
          ))}
        </div>
      )}
    </div>
  )
}


function ScenarioRow({ scenario, headed, onOpenScript, onDownload, onRemove, onRemoveScenario, isSelected, onToggleSelect, onAutomated, runTarget, onNotify }) {
  const hasScript = (scenario.script_count || 0) > 0
  const hasAutomatedSuccess = (scenario.success_script_count || 0) > 0
  const hasIncompleteOnly = !hasAutomatedSuccess && (scenario.incomplete_script_count || 0) > 0
  const [automating, setAutomating] = useState(false)
  const [regenerating, setRegenerating] = useState(false)
  const [confirmRegen, setConfirmRegen] = useState(false)
  async function regenerate() {
    setConfirmRegen(false)
    setRegenerating(true)
    try {
      await generateScriptsFor(scenario.application_id, [scenario.id], { replaceExisting: true })
      onAutomated && onAutomated()
      await refreshOwnScripts()
    } catch (err) {
      onNotify && onNotify('Could not regenerate: ' + err.message, 'error')
      onAutomated && onAutomated()
      await refreshOwnScripts()
    } finally {
      setRegenerating(false)
    }
  }
  async function automate(e) {
    e?.stopPropagation?.()
    setAutomating(true)
    try {
      await generateScriptsFor(scenario.application_id, [scenario.id])
      onAutomated && onAutomated()
      await refreshOwnScripts()
    } catch (err) {
      onNotify && onNotify('Could not generate a script: ' + err.message, 'error')
      onAutomated && onAutomated()
      await refreshOwnScripts()
    } finally {
      setAutomating(false)
    }
  }
  const [open, setOpen] = useState(false)
  const [scripts, setScripts] = useState(null)
  const [loadingScripts, setLoadingScripts] = useState(false)
  const [running, setRunning] = useState(false)
  const [results, setResults] = useState({})
  async function refreshOwnScripts() {
    try {
      const resp = await getScenarioScripts(scenario.id)
      setScripts(resp.scripts || [])
    } catch { /* keep existing scripts if the refresh fails */ }
  }
  async function toggle() {
    const next = !open
    setOpen(next)
    if (next && scripts === null) {
      setLoadingScripts(true)
      try {
        const resp = await getScenarioScripts(scenario.id)
        setScripts(resp.scripts || [])
      } catch { setScripts([]) }
      finally { setLoadingScripts(false) }
    }
  }
  async function runAll() {
    setRunning(true)
    try {
      const res = await runScenario(scenario.id, headed, runTarget())
      const map = {}
      ;(res.results || []).forEach(r => { map[r.generation_id] = r.result })
      setResults(map)
    } catch (e) {
      onNotify && onNotify('Run failed: ' + e.message, 'error')
    } finally { setRunning(false) }
  }
  async function runOne(genId) {
    setResults(r => ({ ...r, [genId]: 'running' }))
    try {
      const res = await runScript(genId, headed, runTarget())
      setResults(r => ({ ...r, [genId]: res.result }))
    } catch (e) {
      setResults(r => ({ ...r, [genId]: 'error' }))
    }
  }
  return (
    <div className={styles.scenario}>
      {confirmRegen && (
        <ConfirmDialog
          title="Regenerate this script?"
          body={`A new script will be generated for "${scenario.title}" from the page as it is now. The previous script remains available until its replacement succeeds. If generation fails, the previous script is kept. Its run history is preserved either way. This takes about a minute.`}
          confirmLabel="Regenerate"
          busy={regenerating}
          onConfirm={regenerate}
          onCancel={() => setConfirmRegen(false)}
        />
      )}
      <div className={styles.scenarioHead}>
        <input
          type="checkbox"
          className={styles.checkbox}
          checked={!!isSelected}
          onChange={() => onToggleSelect && onToggleSelect(scenario.id)}
          onClick={e => e.stopPropagation()}
          title="Select this test case"
        />
        <button className={styles.scenarioToggle} onClick={toggle}>
          <span className={`${styles.chevron} ${open ? styles.chevronOpen : ''}`}>▶</span>
          <span className={styles.scenarioTitle}>{scenario.title}</span>
        </button>
        {(scenario.derivation === 'inferred' || scenario.source_sync_status === 'requirement_changed') && (
          <div className={styles.provRow}>
            {scenario.derivation === 'inferred' && (
              <span className={styles.provInferred}
                    title="Reasoned by the AI - not stated in the requirement.">AI-inferred</span>
            )}
            {scenario.source_sync_status === 'requirement_changed' && (
              <span className={styles.provChanged}
                    title="The source requirement changed after this test was generated - review it.">
                Requirement changed
              </span>
            )}
          </div>
        )}
        <div className={styles.scenarioMeta}>
          <span
            className={styles.statusDot}
            data-result={scenario.last_result || 'none'}
            title={scenario.last_result
              ? `Last run: ${scenario.last_result}`
              : 'Never run'}
          />
          {scenario.priority && (
            <span className={styles.prio} data-level={String(scenario.priority).toUpperCase()}>
              <span className={styles.prioDot} />{scenario.priority}
            </span>
          )}
          {scenario.category && (
            <span className={styles.catDot} data-cat={String(scenario.category).toLowerCase()}
                  title={scenario.category} aria-label={scenario.category} />
          )}
          {typeof scenario.script_count === 'number' && (
            hasAutomatedSuccess
              ? <span className={styles.metaText}>
                  {scenario.success_script_count} automated script{scenario.success_script_count === 1 ? '' : 's'}
                  {scenario.run_count > 0 && ` · ${scenario.run_count} run${scenario.run_count === 1 ? '' : 's'}`}
                </span>
              : hasIncompleteOnly
                ? <span className={styles.metaIncomplete} title={scenario.latest_generation_notes || 'Script generation failed verification'}>
                    Incomplete (needs regenerate)
                  </span>
              : <span className={styles.metaMuted} title="This test case has no automation yet">
                  Not automated
                </span>
          )}
          
          {hasScript ? (
            <Act icon="play" label={running ? 'Running…' : 'Run'} onClick={runAll}
                 disabled={running} tone="run" title="Run every script under this test case" />
          ) : (
            <Act icon="bolt" label={automating ? 'Generating…' : 'Generate script'}
                 onClick={automate} disabled={automating} tone="run"
                 title="Generate a Selenium script for this test case" />
          )}
          <MoreMenu items={[
            ...(scripts && scripts.length === 1 ? [
              { icon: 'eye',      label: 'View script',
                onClick: () => onOpenScript(scripts[0], true) },
              { icon: 'edit',     label: 'Edit script',
                onClick: () => onOpenScript(scripts[0], false) },
              { icon: 'download', label: 'Download',
                onClick: () => onDownload(scripts[0]) },
            ] : []),
            ...(hasScript ? [
              { icon: 'bolt', label: regenerating ? 'Regenerating…' : 'Regenerate script',
                onClick: () => setConfirmRegen(true) },
            ] : []),
            { divider: true },
            { icon: 'trash', label: 'Archive or delete', danger: true,
              onClick: () => onRemoveScenario({ kind: 'scenario', id: scenario.id, title: scenario.title }) },
          ]} />
        </div>
      </div>
      {open && (
        <div className={styles.scripts}>
          {Array.isArray(scenario.steps) && scenario.steps.length > 0 && (
            <div className={styles.manualBox}>
              <div className={styles.manualHead}>
                Steps ({scenario.steps.length})
              </div>
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
                    {scenario.steps.map((s, i) => (
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
            </div>
          )}
          {!hasScript && !(Array.isArray(scenario.steps) && scenario.steps.length > 0) && (
            <div className={styles.manualBox}>
              <div className={styles.manualHead}>What this test case verifies</div>
              <p className={styles.manualBody}>
                {scenario.description || scenario.title}
              </p>
              <div className={styles.manualHint}>
                Generate a script to automate this test case.
              </div>
            </div>
          )}
          {loadingScripts && <div className="skeleton" style={{ height: 44, margin: '8px 0' }} />}
          {scripts && scripts.length === 0 && (
            <div className={styles.noScripts}>
              No scripts generated for this scenario yet.
              <Link href={`/new-test-suite?app_id=${encodeURIComponent(String(scenario.application_id || ''))}`} className={styles.genLink}>Generate scripts →</Link>
            </div>
          )}
          {scripts && scripts.length === 1 && (
            <ScriptHistory
              script={scripts[0]}
              result={results[scripts[0].generation_id]}
            />
          )}
          {scripts && scripts.length > 1 && scripts.map(s => (
            <ScriptRow
              key={s.generation_id}
              script={s}
              result={results[s.generation_id]}
              onRun={() => runOne(s.generation_id)}
              onOpenScript={onOpenScript}
              onDownload={onDownload}
              onRemove={onRemove}
            />
          ))}
        </div>
      )}
    </div>
  )
}
function ScriptRow({ script, result, onRun, onOpenScript, onDownload, onRemove }) {
  const [showHistory, setShowHistory] = useState(false)
  const [history, setHistory] = useState(null)
  const [loadingH, setLoadingH] = useState(false)
  const [runCount, setRunCount] = useState(null)
  useEffect(() => {
    let cancelled = false
    getExecutionHistory(script.generation_id)
      .then(h => { if (!cancelled) setRunCount(h.run_count ?? 0) })
      .catch(() => { if (!cancelled) setRunCount(0) })
    return () => { cancelled = true }
  }, [script.generation_id, result])
  async function toggleHistory() {
    const next = !showHistory
    setShowHistory(next)
    if (next) {
      setLoadingH(true)
      try { setHistory(await getExecutionHistory(script.generation_id)) }
      catch { setHistory({ run_count: 0, history: [] }) }
      finally { setLoadingH(false) }
    }
  }
  useEffect(() => {
    if (showHistory && (result === 'pass' || result === 'fail')) {
      getExecutionHistory(script.generation_id).then(setHistory).catch(() => {})
    }
  }, [result]) // eslint-disable-line
  return (
    <div className={styles.scriptWrap}>
      <div className={styles.scriptRow}>
        <div className={styles.scriptLeft}>
          <span className={styles.kindTag} data-kind={script.test_kind}>{script.test_kind === 'ui' ? 'UI' : 'API'}</span>
          {script.generation_status === 'incomplete' && (
            <span className={styles.metaIncomplete} title={script.notes || 'Generated script failed verification'}>
              Incomplete
            </span>
          )}
          {script.priority && (
            <span className={styles.prio} data-level={String(script.priority).toUpperCase()}>
              <span className={styles.prioDot} />{script.priority}
            </span>
          )}
          {script.category && (
            <span className={styles.catDot} data-cat={String(script.category).toLowerCase()}
                  title={script.category} aria-label={script.category} />
          )}
          <span className={styles.scriptTitle}>{script.title}</span>
        </div>
        <div className={styles.scriptRight}>
          <ResultPill result={result} />
          <button className={styles.runsLink} onClick={toggleHistory}
                  title="Show run history and screenshots">
            {runCount === null ? '' : `${runCount} run${runCount === 1 ? '' : 's'}`}
          </button>
          <Act icon="play" label={result === 'running' ? 'Running…' : 'Run'} onClick={onRun}
               disabled={result === 'running'} tone="run" />
          <MoreMenu items={[
            { icon: 'eye',      label: 'View script',     onClick: () => onOpenScript(script, true) },
            { icon: 'edit',     label: 'Edit script',     onClick: () => onOpenScript(script, false) },
            { icon: 'download', label: 'Download',        onClick: () => onDownload(script) },
            { divider: true },
            { icon: 'trash',    label: 'Archive or delete', onClick: () => onRemove(script), danger: true },
          ]} />
        </div>
      </div>
      {showHistory && (
        <div className={styles.history}>
          {loadingH && <div className="skeleton" style={{ height: 40 }} />}
          {history && (
            <>
              <div className={styles.historyHead}>
                Executed <b>{history.run_count}</b> time{history.run_count !== 1 ? 's' : ''} · audit trail
              </div>
              {history.history.length === 0 && <div className={styles.noScripts}>No runs recorded yet. Click Run.</div>}
              {history.history.map(run => (
                <div key={run.execution_id} className={styles.runRow}>
                  <div className={styles.runMeta}>
                    <ResultPill result={run.result} />
                    <span className={styles.runTime}>{run.executed_at}</span>
                    <span className={styles.runDur}>{run.duration_ms ? (run.duration_ms/1000).toFixed(1)+'s' : ''}</span>
                  </div>
                  {run.output && (
                    <details className={styles.failDetails}>
                      <summary>Why it failed</summary>
                      <pre className={styles.failOutput}>{run.output}</pre>
                    </details>
                  )}
                  {(run.result === 'fail' || run.result === 'error') && (
                    <HealPanel
                      generationId={script.generation_id}
                      executionId={run.execution_id}
                      onApplied={onRun}
                      onOpenScript={() => onOpenScript(script, false)}
                    />
                  )}
                  {run.screenshots && run.screenshots.length > 0 && (
                    <div className={styles.shots}>
                      {run.screenshots.map((sc, i) => (
                        <a key={i} href={screenshotUrl(sc)} target="_blank" rel="noreferrer">
                          <img src={screenshotUrl(sc)} alt={`step ${i+1}`} className={styles.shot} />
                        </a>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  )
}
function prioBadge(p) {
  const x = (p || '').toUpperCase()
  if (x === 'P0' || x === 'P1') return 'red'
  if (x === 'P2') return 'amber'
  return 'gray'
}
function catBadge(cat) {
  const c = (cat || '').toLowerCase()
  if (c === 'positive') return 'green'
  if (c === 'negative') return 'red'
  if (c === 'edge' || c === 'boundary') return 'amber'
  if (c === 'security') return 'purple'
  return 'gray'
}
function AppTestData({
  applicationId,
  environments = [],
  selectedEnvironment,
  onEnvironmentChange,
  open = false,
  onToggle,
}) {
  const activeEnvironment = selectedEnvironment || 'default'
  const [data, setData] = useState(null)
  useEffect(() => {
    if (!open || !applicationId) return
    let cancelled = false
    setData(null)
    getAppTestData({ applicationId, environment: activeEnvironment })
      .then(d => { if (!cancelled) setData(d || {}) })
      .catch(() => { if (!cancelled) setData({}) })
    return () => { cancelled = true }
  }, [open, applicationId, activeEnvironment])
  async function revealField(key) {
    const r = await revealAppTestDataField({ applicationId, environment: activeEnvironment, key })
    return r.value
  }
  async function revealPersonaField(persona, key) {
    const r = await revealAppTestDataField({ applicationId, environment: activeEnvironment, key, persona })
    return r.value
  }
  async function saveForEnvironment(updatedFlat) {
    // updatedFlat comes from the flat key/value table below and never
    // contains "forms" or "personas" (both are stripped out before being
    // passed to that table — see flatData). Re-attach both unchanged so a
    // save here can never wipe out per-form overrides OR multi-actor
    // persona credentials (see test_data/loader.py's "personas" block,
    // added for multi-actor procedures — a script that must run as more
    // than one identity, e.g. requester -> approver -> admin, within one
    // continuous run). Without re-attaching "personas" here the same way
    // "forms" already is, saving a single flat-table edit would silently
    // delete every persona's stored credentials for this environment.
    const merged = { ...updatedFlat, forms: data?.forms || {}, personas: data?.personas || {} }
    await saveAppTestData(merged, { applicationId, environment: activeEnvironment })
    const refreshed = await getAppTestData({ applicationId, environment: activeEnvironment })
    setData(refreshed || {})
  }
  async function saveForms(updatedForms) {
    // Mirror image of the above: keep every flat key AND personas
    // untouched, replace only "forms". Spreading ...(data || {}) first
    // already preserves "personas" automatically — nothing to change here.
    const merged = { ...(data || {}), forms: updatedForms }
    await saveAppTestData(merged, { applicationId, environment: activeEnvironment })
    const refreshed = await getAppTestData({ applicationId, environment: activeEnvironment })
    setData(refreshed || {})
  }
  async function savePersonas(updatedPersonas) {
    // Mirror image of saveForms: keep every flat key AND forms untouched,
    // replace only "personas".
    const merged = { ...(data || {}), personas: updatedPersonas }
    await saveAppTestData(merged, { applicationId, environment: activeEnvironment })
    const refreshed = await getAppTestData({ applicationId, environment: activeEnvironment })
    setData(refreshed || {})
  }
  // The flat table must never see "forms" or "personas" — it renders every
  // top-level key as a plain string row, which is exactly how "forms" used
  // to show up as "[object Object]" and risked being overwritten with that
  // literal string on save. The same applies to "personas" (a dict of
  // named identities, each {username, password}) now that it exists.
  // FormOverridesEditor is the only thing that reads/writes "forms";
  // PersonaOverridesEditor is the only thing that reads/writes "personas".
  const flatData = data ? (({ forms, personas, ...rest }) => rest)(data) : null
  const envOptions = environments.length
    ? environments
    : [{ id: 'fallback-default', name: 'default', is_default: true }]
  return (
    <div className="card" style={{ marginBottom: 15 }}>
      <div className="card-header" style={{ cursor: 'default' }}>
        <div>
          <h3>Test Data <span style={{ fontWeight: 400, color: 'var(--text-muted)', fontSize: 12 }}>· environment scoped</span></h3>
          <div className="card-subtitle">Password/secret values are encrypted at save and masked on load.</div>
        </div>
        <div className={styles.testDataHeadRight}>
          <label className={styles.envLabel}>Environment</label>
          <select
            className={styles.envSelect}
            value={activeEnvironment}
            onChange={e => onEnvironmentChange && onEnvironmentChange(e.target.value)}
          >
            {envOptions.map(env => (
              <option key={env.id || env.name} value={env.name}>
                {env.name}{env.is_default ? ' (default)' : ''}
              </option>
            ))}
          </select>
          <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>edit values below</span>
        </div>
      </div>
      {open && (
        <div style={{ padding: '16px 20px' }}>
          {flatData === null ? <div className="skeleton" style={{ height: 80 }} />
            : <TestDataTable data={flatData} appLevel onSave={saveForEnvironment} onReveal={revealField} />}
        </div>
      )}
      {open && data !== null && (
        <FormOverridesEditor forms={data.forms || {}} onSave={saveForms} />
      )}
      {open && data !== null && (
        <PersonaOverridesEditor personas={data.personas || {}} onSave={savePersonas} onReveal={revealPersonaField} />
      )}
    </div>
  )
}
/**
 * Per-form overrides — the ONE way a value can differ from the
 * application-wide default above, for a single flow only.
 *
 * A "form" here is keyed by the exact target_flow text typed on the
 * Configure step when surveying (e.g. "Sales Order Entry"). At crawl and
 * run time, test_data/loader.py resolves a scenario's data by looking up
 * environments.<env>.forms.<target_flow> and merging it on top of the
 * flat application-level values — so a key set here overrides that same
 * key above, but only for scripts belonging to that flow. Nothing else
 * needs to be created or linked: no separate file, no per-scenario
 * override, no manual step after accepting a survey.
 *
 * Saving writes the WHOLE "forms" object back in one PUT (via onSave,
 * wired to AppTestData.saveForms), which merges it back on top of the
 * unedited flat keys — see saveForms above.
 */
function FormOverridesEditor({ forms, onSave }) {
  const [editing, setEditing] = useState(false)
  const [groups, setGroups] = useState([])
  const [newFormName, setNewFormName] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const formNames = Object.keys(forms || {})
  function startEdit() {
    setError(null)
    setGroups(formNames.map((name, i) => ({
      id: `form-${i}-${name}`,
      name,
      rows: Object.entries(forms[name] || {}).map(([k, v], j) => ({
        id: `row-${i}-${j}`,
        key: k,
        value: v == null ? '' : String(v),
      })),
    })))
    setEditing(true)
  }
  function addForm() {
    const name = newFormName.trim()
    if (!name) return
    if (groups.some(g => g.name === name)) {
      setError(`A form called "${name}" already exists below.`)
      return
    }
    setGroups(prev => [...prev, { id: `form-new-${Date.now()}`, name, rows: [] }])
    setNewFormName('')
    setError(null)
  }
  function removeForm(id) {
    setGroups(prev => prev.filter(g => g.id !== id))
  }
  function addRow(groupId) {
    setGroups(prev => prev.map(g => (g.id === groupId
      ? { ...g, rows: [...g.rows, { id: `row-${Date.now()}`, key: '', value: '' }] }
      : g)))
  }
  function updateRow(groupId, rowId, field, value) {
    setGroups(prev => prev.map(g => (g.id === groupId
      ? { ...g, rows: g.rows.map(r => (r.id === rowId ? { ...r, [field]: value } : r)) }
      : g)))
  }
  function removeRow(groupId, rowId) {
    setGroups(prev => prev.map(g => (g.id === groupId
      ? { ...g, rows: g.rows.filter(r => r.id !== rowId) }
      : g)))
  }
  async function save() {
    setError(null)
    const seen = new Set()
    const updated = {}
    for (const g of groups) {
      const name = g.name.trim()
      if (!name) { setError('Every form needs a name.'); return }
      if (seen.has(name)) { setError(`Duplicate form name: ${name}`); return }
      seen.add(name)
      const fields = {}
      for (const row of g.rows) {
        const key = row.key.trim()
        if (key) fields[key] = row.value
      }
      updated[name] = fields
    }
    setSaving(true)
    try {
      await onSave(updated)
      setEditing(false)
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className={styles.formOverrides}>
      <button
        type="button"
        className={styles.scenarioDataToggle}
        onClick={() => (editing ? setEditing(false) : startEdit())}
      >
        {editing ? <ChevronDown size={15} strokeWidth={2} /> : <ChevronRight size={15} strokeWidth={2} />}
        <span>Per-form overrides</span>
        <span className={styles.scenarioDataToggleHint}>
          {formNames.length > 0
            ? `${formNames.length} form${formNames.length === 1 ? '' : 's'} configured — override the application data above, for that flow only`
            : 'values for one flow only (e.g. "Sales Order Entry") — override the application data above'}
        </span>
      </button>
      {editing && (
        <div style={{ padding: '0 20px 16px' }}>
          <p className={styles.envHelp}>
            Use the exact flow name typed on the Configure step when surveying
            (e.g. &quot;Sales Order Entry&quot;, &quot;Inventory Adjustment&quot;).
            A key set here overrides the same key above — only for scripts
            belonging to that flow.
          </p>
          {groups.map(g => (
            <div key={g.id} className={styles.formGroup}>
              <div className={styles.formGroupHead}>
                <span className={styles.formGroupName}>{g.name}</span>
                <button
                  type="button"
                  className={styles.kvRemove}
                  onClick={() => removeForm(g.id)}
                  disabled={saving}
                  title="Remove this form's overrides"
                >
                  <Trash2 size={13} strokeWidth={2} />
                </button>
              </div>
              {g.rows.map(row => (
                <div key={row.id} className={styles.kvRow}>
                  <input
                    className={styles.kvKey}
                    placeholder="key, e.g. ItemNumber"
                    value={row.key}
                    onChange={e => updateRow(g.id, row.id, 'key', e.target.value)}
                    disabled={saving}
                  />
                  <input
                    className={styles.kvValue}
                    placeholder="value"
                    value={row.value}
                    onChange={e => updateRow(g.id, row.id, 'value', e.target.value)}
                    disabled={saving}
                  />
                  <button
                    type="button"
                    className={styles.kvRemove}
                    onClick={() => removeRow(g.id, row.id)}
                    disabled={saving}
                    title="Remove this key"
                  >
                    <Trash2 size={13} strokeWidth={2} />
                  </button>
                </div>
              ))}
              <button type="button" className={styles.kvAdd} onClick={() => addRow(g.id)} disabled={saving}>
                <Plus size={13} strokeWidth={2} /> Add a value
              </button>
            </div>
          ))}
          <div className={styles.filenameRow}>
            <input
              className={styles.filenameInput}
              placeholder='new form name, e.g. "Sales Order Entry"'
              value={newFormName}
              onChange={e => setNewFormName(e.target.value)}
              disabled={saving}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addForm() } }}
            />
            <button type="button" className={styles.historyBtn} onClick={addForm} disabled={saving}>
              <Plus size={13} strokeWidth={2} /> Add form
            </button>
          </div>
          {error && <div className={styles.modalErr}>{error}</div>}
          <div className={styles.modalActions}>
            <button className={styles.historyBtn} disabled={saving} onClick={() => setEditing(false)}>Cancel</button>
            <button className="btn btn-primary" disabled={saving} onClick={save}>
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}
      {!editing && formNames.length > 0 && (
        <div style={{ padding: '0 20px 16px' }}>
          {formNames.map(name => (
            <div key={name} className={styles.scenarioOverrideRow}>
              <span className={styles.scenarioOverrideTitle}>{name}</span>
              <span className={styles.metaText}>
                {Object.keys(forms[name] || {}).length} field{Object.keys(forms[name] || {}).length === 1 ? '' : 's'}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
/**
 * Named-identity credentials for a multi-actor procedure — e.g. a
 * requester creates a record, an approver later approves it, an admin
 * performs a final override, all within ONE continuous script run (see
 * service/grounded_runtime.py's _login_as). Each persona here is just a
 * {username, password} pair under environments.<env>.personas.<name> —
 * test_data/loader.py flattens each one into "<name>_username" /
 * "<name>_password" at load time, which the generated script reads via
 * get_data("<name>_username") / get_data("<name>_password").
 *
 * There is NO fixed list of role names: "requester"/"approver"/"admin"
 * are just examples — add, rename, or remove persona entries freely, and
 * scenario authors reference whichever name they typed as a step's
 * "actor"/"login_as" value. Adding a 4th or 5th role needs no code
 * change anywhere, only a new entry here.
 *
 * Mirrors FormOverridesEditor's exact UX (collapsed-by-default, same
 * card classes) but each group has exactly two fixed fields —
 * username and password — rather than arbitrary key/value rows, since
 * that is the only shape test_data/loader.py's personas block supports
 * today.
 *
 * Every persona's password is ALWAYS encrypted at save (backend:
 * _encrypt_personas in api/routes/test_data.py) — unlike the flat
 * table's per-row Encrypt checkbox, there is no opt-out, since a
 * password must be encrypted irrespective of where it is stored. GET
 * returns it masked as the same "********" string token the flat table
 * already uses (api/routes/test_data.py's MASKED_SECRET) — NOT a nested
 * {_encrypted,...} object — so the "is this already stored, encrypted"
 * check below is a plain string comparison, exactly like
 * TestDataTable's encryptedKeys check.
 */
function PersonaOverridesEditor({ personas, onSave, onReveal }) {
  const MASKED = '********'
  const [editing, setEditing] = useState(false)
  const [groups, setGroups] = useState([])
  const [newPersonaName, setNewPersonaName] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [revealingId, setRevealingId] = useState(null)
  const personaNames = Object.keys(personas || {})
  function startEdit() {
    setError(null)
    setGroups(personaNames.map((name, i) => ({
      id: `persona-${i}-${name}`,
      name,
      username: personas[name]?.username == null ? '' : String(personas[name].username),
      // A previously-saved password comes back from GET as the literal
      // mask token "********" (api/routes/test_data.py's MASKED_SECRET),
      // never the real value — pre-fill with that token, exactly like
      // TestDataTable seeds an encrypted row, rather than blanking it.
      // Leaving it as-is on Save tells the backend "unchanged"; typing a
      // new value replaces and re-encrypts it; Reveal below swaps the
      // token for the real decrypted value so it can be edited in place.
      password: personas[name]?.password == null ? '' : String(personas[name].password),
      passwordExisting: personas[name]?.password === MASKED,
    })))
    setEditing(true)
  }
  function addPersona() {
    const name = newPersonaName.trim()
    if (!name) return
    if (groups.some(g => g.name === name)) {
      setError(`A persona called "${name}" already exists below.`)
      return
    }
    setGroups(prev => [...prev, {
      id: `persona-new-${Date.now()}`, name, username: '', password: '', passwordExisting: false,
    }])
    setNewPersonaName('')
    setError(null)
  }
  function removePersona(id) {
    setGroups(prev => prev.filter(g => g.id !== id))
  }
  function updateGroup(id, field, value) {
    setGroups(prev => prev.map(g => (g.id === id ? { ...g, [field]: value } : g)))
  }
  async function revealPassword(g) {
    if (!onReveal) return
    setRevealingId(g.id)
    try {
      const plain = await onReveal(g.name, 'password')
      updateGroup(g.id, 'password', plain)
    } catch (e) {
      setError(e.message)
    } finally {
      setRevealingId(null)
    }
  }
  async function save() {
    setError(null)
    const seen = new Set()
    const updated = {}
    for (const g of groups) {
      const name = g.name.trim()
      if (!name) { setError('Every persona needs a name.'); return }
      if (seen.has(name)) { setError(`Duplicate persona name: ${name}`); return }
      seen.add(name)
      if (!g.username.trim()) { setError(`Persona "${name}" needs a username.`); return }
      if (!g.password.trim()) { setError(`Persona "${name}" needs a password.`); return }
      updated[name] = {
        username: g.username.trim(),
        // Left as the mask token -> backend keeps the existing encrypted
        // value unchanged. Anything else -> backend encrypts the new
        // value. Either way, plaintext is never stored unencrypted — see
        // _encrypt_personas in api/routes/test_data.py.
        password: g.password,
      }
    }
    setSaving(true)
    try {
      await onSave(updated)
      setEditing(false)
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className={styles.formOverrides}>
      <button
        type="button"
        className={styles.scenarioDataToggle}
        onClick={() => (editing ? setEditing(false) : startEdit())}
      >
        {editing ? <ChevronDown size={15} strokeWidth={2} /> : <ChevronRight size={15} strokeWidth={2} />}
        <span>Multi-actor personas</span>
        <span className={styles.scenarioDataToggleHint}>
          {personaNames.length > 0
            ? `${personaNames.length} persona${personaNames.length === 1 ? '' : 's'} configured — for procedures that log in as more than one identity (e.g. requester, approver, admin)`
            : 'named identities for one continuous script (e.g. requester, approver, admin) — add as many as the procedure needs'}
        </span>
      </button>
      {editing && (
        <div style={{ padding: '0 20px 16px' }}>
          <p className={styles.envHelp}>
            Each persona is a separate login this application supports (any
            name — not limited to requester/approver/admin). A scenario step
            that switches identity mid-script references the exact name
            typed here. Passwords are always encrypted at save.
          </p>
          {groups.map(g => (
            <div key={g.id} className={styles.formGroup}>
              <div className={styles.formGroupHead}>
                <span className={styles.formGroupName}>{g.name}</span>
                <button
                  type="button"
                  className={styles.kvRemove}
                  onClick={() => removePersona(g.id)}
                  disabled={saving}
                  title="Remove this persona"
                >
                  <Trash2 size={13} strokeWidth={2} />
                </button>
              </div>
              <div className={styles.kvRow}>
                <input
                  className={styles.kvKey}
                  placeholder="username"
                  value={g.username}
                  onChange={e => updateGroup(g.id, 'username', e.target.value)}
                  disabled={saving}
                />
                <input
                  className={styles.kvValue}
                  type={g.password === MASKED ? 'text' : 'password'}
                  placeholder="password"
                  value={g.password}
                  onChange={e => updateGroup(g.id, 'password', e.target.value)}
                  disabled={saving}
                />
                {g.passwordExisting && g.password === MASKED && onReveal && (
                  <button
                    type="button"
                    className={styles.revealBtn}
                    onClick={() => revealPassword(g)}
                    disabled={saving || revealingId === g.id}
                    title="Reveal and load current value"
                  >
                    <Eye size={13} />
                    {revealingId === g.id ? 'Revealing…' : 'Reveal'}
                  </button>
                )}
              </div>
            </div>
          ))}
          <div className={styles.filenameRow}>
            <input
              className={styles.filenameInput}
              placeholder='new persona name, e.g. "approver"'
              value={newPersonaName}
              onChange={e => setNewPersonaName(e.target.value)}
              disabled={saving}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addPersona() } }}
            />
            <button type="button" className={styles.historyBtn} onClick={addPersona} disabled={saving}>
              <Plus size={13} strokeWidth={2} /> Add persona
            </button>
          </div>
          {error && <div className={styles.modalErr}>{error}</div>}
          <div className={styles.modalActions}>
            <button className={styles.historyBtn} disabled={saving} onClick={() => setEditing(false)}>Cancel</button>
            <button className="btn btn-primary" disabled={saving} onClick={save}>
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}
      {!editing && personaNames.length > 0 && (
        <div style={{ padding: '0 20px 16px' }}>
          {personaNames.map(name => (
            <div key={name} className={styles.scenarioOverrideRow}>
              <span className={styles.scenarioOverrideTitle}>{name}</span>
              <span className={styles.metaText}>{personas[name]?.username || '(no username set)'}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
function TestDataTable({ data, appLevel, onSave, onReveal }) {
  const [editing, setEditing] = useState(false)
  const [rows, setRows] = useState([])
  const [saveErr, setSaveErr] = useState(null)
  const [saving, setSaving] = useState(false)
  const [revealed, setRevealed] = useState({})
  const [revealingKey, setRevealingKey] = useState(null)
  const entries = Object.entries(data).filter(([k]) => k !== '_comment' && !k.startsWith('__'))
  const encryptedKeys = new Set(Array.isArray(data?.__encrypted_keys) ? data.__encrypted_keys : [])
  if (entries.length === 0 && !editing) {
    return (
      <div>
        <div className={styles.noScripts}>No test data yet.</div>
        <div className={styles.dataActions} style={{ marginTop: 10 }}>
          <button className="btn btn-secondary" onClick={startEdit}>Add first entry</button>
          {appLevel && (
            <span className={styles.uploadHintInline}>Versioned in <code>test_data/</code> · upload coming soon</span>
          )}
        </div>
      </div>
    )
  }
  function startEdit() {
    setSaveErr(null)
    setRevealed({})
    const seeded = entries
      .map(([k, v], i) => ({
        id: `existing-${k}-${i}`,
        key: k,
        value: encryptedKeys.has(k) ? '' : (typeof v === 'boolean' ? String(v) : String(v)),
        encryptedExisting: encryptedKeys.has(k),
        encrypt: encryptedKeys.has(k),
        keyLocked: true,
      }))
    setRows(seeded.length ? seeded : [{ id: `new-${Date.now()}`, key: '', value: '', keyLocked: false, encryptedExisting: false, encrypt: false }])
    setEditing(true)
  }
  function addRow() {
    setRows(prev => [...prev, {
      id: `new-${Date.now()}-${prev.length}`,
      key: '',
      value: '',
      keyLocked: false,
      encryptedExisting: false,
      encrypt: false,
    }])
  }
  function updateRow(id, field, value) {
    setRows(prev => prev.map(r => (r.id === id ? { ...r, [field]: value } : r)))
  }
  function removeRow(id) {
    setRows(prev => prev.filter(r => r.id !== id))
  }
  function isSensitiveKey(key) {
    const k = (key || '').toLowerCase()
    return k.includes('password') || k.includes('secret')
  }
  async function revealValue(key, rowId = null) {
    if (!onReveal) return
    try {
      setRevealingKey(`${key}:${rowId || 'view'}`)
      const plain = await onReveal(key)
      if (rowId) {
        updateRow(rowId, 'value', plain)
      } else {
        setRevealed(prev => ({ ...prev, [key]: plain }))
      }
    } catch (e) {
      setSaveErr(e.message)
    } finally {
      setRevealingKey(null)
    }
  }
  async function save() {
    setSaveErr(null)
    setSaving(true)
    try {
      const updated = {}
      const encryptKeys = new Set()
      const seen = new Set()
      for (let i = 0; i < rows.length; i += 1) {
        const row = rows[i]
        const key = (row.key || '').trim()
        if (!key) {
          throw new Error(`Row ${i + 1}: key is required.`)
        }
        if (key === '_comment') {
          throw new Error('Key name "_comment" is reserved.')
        }
        if (seen.has(key)) {
          throw new Error(`Duplicate key: ${key}`)
        }
        seen.add(key)
        const original = entries.find(([k]) => k === key)
        let val = row.value
        if (row.encrypt) {
          encryptKeys.add(key)
          if (row.encryptedExisting && !String(row.value || '').trim()) {
            val = '********'
          }
        } else {
          if (row.encryptedExisting && !String(row.value || '').trim()) {
            throw new Error(`Row ${i + 1}: enter a value (or turn Encrypt back on) before saving.`)
          }
        }
        if (original && typeof original[1] === 'boolean' && !row.encrypt) {
          val = String(row.value).toLowerCase() === 'true'
        }
        updated[key] = val
      }
      updated.__encrypt_keys = Array.from(encryptKeys)
      await onSave(updated)
      setEditing(false)
      setRows([])
      setRevealed({})
    } catch (e) {
      setSaveErr(e.message)
    } finally { setSaving(false) }
  }
  return (
    <div className={styles.dataTable}>
      <div className={styles.dataRows}>
      {!editing && entries.map(([k, v]) => (
        <div key={k} className={styles.dataRow}>
          <span className={styles.dataKey}>{k}</span>
          <span className={styles.dataValueWrap}>
            <span className={styles.dataVal}>
              <code>{typeof v === 'boolean' ? String(v) : (v === '' ? '(empty)' : String(revealed[k] ?? v))}</code>
            </span>
            {(encryptedKeys.has(k) || isSensitiveKey(k)) && onReveal && (
              <button
                className={styles.revealBtn}
                onClick={() => {
                  if (revealed[k] !== undefined) {
                    setRevealed(prev => {
                      const next = { ...prev }
                      delete next[k]
                      return next
                    })
                  } else {
                    revealValue(k)
                  }
                }}
                disabled={revealingKey === `${k}:view`}
                title={revealed[k] !== undefined ? 'Hide value' : 'Reveal value'}
              >
                <Eye size={13} />
                {revealed[k] !== undefined ? 'Hide' : 'Show'}
              </button>
            )}
          </span>
        </div>
      ))}
      {editing && rows.map((row) => (
        <div key={row.id} className={styles.dataRow}>
          <span className={styles.dataKey}>
            <input
              className={styles.dataInput}
              placeholder="key"
              value={row.key}
              disabled={row.keyLocked}
              onChange={e => updateRow(row.id, 'key', e.target.value)}
            />
          </span>
          <span className={styles.dataVal} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              className={styles.dataInput}
              placeholder="value"
              value={row.value}
              onChange={e => updateRow(row.id, 'value', e.target.value)}
              style={{ flex: 1 }}
            />
            <label className={styles.encryptToggle} title="Encrypt this field at save">
              <input
                type="checkbox"
                checked={!!row.encrypt}
                onChange={e => updateRow(row.id, 'encrypt', e.target.checked)}
                disabled={saving}
              />
              Encrypt
            </label>
            {row.encryptedExisting && row.encrypt && onReveal && (
              <button
                className={styles.revealBtn}
                onClick={() => revealValue(row.key, row.id)}
                disabled={saving || revealingKey === `${row.key}:${row.id}`}
                title="Reveal and load current value"
              >
                <Eye size={13} />
                Reveal
              </button>
            )}
            <button className="btn btn-ghost" onClick={() => removeRow(row.id)} disabled={saving}>Remove</button>
          </span>
        </div>
      ))}
      </div>

      <div className={styles.dataActions}>
        {editing ? (
          <>
            <button className="btn btn-secondary" onClick={addRow} disabled={saving}>Add key-value</button>
            <button className="btn btn-primary" onClick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
            <button className="btn btn-ghost" onClick={() => { setEditing(false); setRows([]); setSaveErr(null) }} disabled={saving}>Cancel</button>
          </>
        ) : (
          <button className="btn btn-secondary" onClick={startEdit}>Edit</button>
        )}
        {appLevel && !editing && (
          <span className={styles.uploadHintInline}>Versioned in <code>test_data/</code> · upload coming soon</span>
        )}
      </div>
      {editing && (
        <div className={styles.encryptHint}>
          Only rows with <b>Encrypt</b> checked are encrypted at save.
        </div>
      )}
      {saveErr && <div className={styles.modalErr}>{saveErr}</div>}
    </div>
  )
}
function ResultPill({ result }) {
  if (!result) return null
  if (result === 'running') return <span className={`badge badge-gray`}>running…</span>
  if (result === 'pass') return <span className="badge badge-green">passed</span>
  if (result === 'fail') return <span className="badge badge-red">failed</span>
  return <span className="badge badge-amber">{result}</span>
}