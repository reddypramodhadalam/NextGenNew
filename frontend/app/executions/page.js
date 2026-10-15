'use client'

/**
 * app/executions/page.js
 *
 * Every test run, in one place.
 *
 * Run history was previously only reachable by opening an application,
 * finding the test case and expanding it — fine when you already know which
 * test you care about, useless when the question is "what ran overnight and
 * what broke". Schedules and suites link here rather than each rendering
 * their own version of the same thing.
 */

import { useState, useEffect, useCallback, Suspense } from 'react'
import { useSearchParams } from 'next/navigation'
import Link from 'next/link'
import {
  CheckCircle2, XCircle, MinusCircle, Clock, User, CalendarClock,
  RefreshCw, ChevronRight, Layers,
} from 'lucide-react'
import {
  listExecutions, getExecution, listScheduleRuns, getApplications,
} from '../../lib/api'
import { screenshotUrl } from '../../lib/api'
import styles from './Executions.module.css'

export default function ExecutionsPage() {
  return (
    <Suspense fallback={<div className="skeleton" style={{ height: 200 }} />}>
      <ExecutionsInner />
    </Suspense>
  )
}

function ExecutionsInner() {
  // Landing here from a test case should show THAT test, not the top of an
  // unfiltered list — otherwise the link only saves half the journey.
  const params = useSearchParams()
  const focusTest = params.get('test')
  const focusApp = params.get('application_id')
  // A link can land on either tab — the dashboard's suite summary points here.
  const [tab, setTab] = useState(params.get('tab') === 'suites' ? 'suites' : 'tests')
  const [executions, setExecutions] = useState(null)
  const [suiteRuns, setSuiteRuns] = useState(null)
  const [apps, setApps] = useState([])
  const [filters, setFilters] = useState({
    application_id: focusApp || '', result: '', triggered_by: '',
  })
  const [selected, setSelected] = useState(null)
  const [groupBy, setGroupBy] = useState('test')  // test | day | scenario
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async () => {
    setRefreshing(true)
    try {
      if (tab === 'tests') {
        const d = await listExecutions(filters)
        setExecutions(d.executions || [])
      } else {
        const d = await listScheduleRuns()
        setSuiteRuns(d.runs || [])
      }
    } catch {
      tab === 'tests' ? setExecutions([]) : setSuiteRuns([])
    } finally { setRefreshing(false) }
  }, [tab, filters])

  useEffect(() => { load() }, [load])

  // Bring the focused test into view once its group has rendered.
  useEffect(() => {
    if (!focusTest || !executions) return
    const el = document.getElementById('focus-group')
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [focusTest, executions])
  useEffect(() => { getApplications().then(setApps).catch(() => setApps([])) }, [])

  return (
    <div>
      <div className={styles.head}>
        <div>
          <h2 className={styles.title}>Executions</h2>
          <p className={styles.sub}>
            Every run, whoever or whatever started it — with the evidence it produced.
          </p>
        </div>
        <button className={styles.refresh} onClick={load} disabled={refreshing}>
          <RefreshCw size={14} strokeWidth={2} className={refreshing ? styles.spin : ''} />
          Refresh
        </button>
      </div>

      {focusTest && (
        <div className={styles.focusBanner}>
          Showing runs for <b>{focusTest}</b>
          <a href="/executions" className={styles.clearFocus}>show everything</a>
        </div>
      )}

      <div className={styles.tabs}>
        <button className={`${styles.tab} ${tab === 'tests' ? styles.tabOn : ''}`}
                onClick={() => setTab('tests')}>
          Test runs
        </button>
        <button className={`${styles.tab} ${tab === 'suites' ? styles.tabOn : ''}`}
                onClick={() => setTab('suites')}>
          Suite runs
        </button>
      </div>

      {tab === 'tests' && (
        <>
          <div className={styles.filters}>
            <select className={styles.select} value={filters.application_id}
                    onChange={e => setFilters(f => ({ ...f, application_id: e.target.value }))}>
              <option value="">All applications</option>
              {apps.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
            <select className={styles.select} value={filters.result}
                    onChange={e => setFilters(f => ({ ...f, result: e.target.value }))}>
              <option value="">Any result</option>
              <option value="pass">Passed</option>
              <option value="fail">Failed</option>
              <option value="error">Error</option>
            </select>
            <select className={styles.select} value={filters.triggered_by}
                    onChange={e => setFilters(f => ({ ...f, triggered_by: e.target.value }))}>
              <option value="">Anyone</option>
              <option value="ui">Started by hand</option>
              <option value="scheduler">Scheduled</option>
            </select>
            <select className={styles.select} value={groupBy}
                    onChange={e => setGroupBy(e.target.value)}
                    title="A flat list answers nothing; grouping decides which question this page answers">
              <option value="test">Group by test case</option>
              <option value="scenario">Group by scenario</option>
              <option value="day">Group by day</option>
            </select>
          </div>

          {executions === null && <div className="skeleton" style={{ height: 200 }} />}
          {executions && executions.length === 0 && (
            <div className={styles.empty}>No runs match these filters.</div>
          )}

          {executions && executions.length > 0 && groupExecutions(executions, groupBy).map(group => (
            <div key={group.key}
                 id={focusTest && group.name.endsWith(focusTest) ? 'focus-group' : undefined}
                 className={`${styles.group} ${focusTest && group.name.endsWith(focusTest) ? styles.groupFocus : ''}`}>
              <div className={styles.groupHead}>
                <span className={styles.groupName}>{group.name}</span>
                <span className={styles.groupContext}>{group.context}</span>
                <span className={styles.groupStats}>
                  {group.total} run{group.total === 1 ? '' : 's'} ·
                  {' '}{group.passed} passed
                  {group.failed > 0 && <span className={styles.groupFailed}> · {group.failed} failed</span>}
                </span>
              </div>
              <div className="card" style={{ padding: 0 }}>
                {group.items.map(e => (
                <button key={e.id} className={styles.row} onClick={() => setSelected(e.id)}>
                  <ResultIcon result={e.result} />
                  <div className={styles.rowMain}>
                    {/* The test name is already the group header when grouping
                        by test case — showing it again on every run is noise. */}
                    {groupBy !== 'test' && (
                      <div className={styles.rowTitle}>{e.test_title}</div>
                    )}
                    <div className={styles.rowMeta}>
                      {groupBy !== 'test' && e.scenario && <>{e.scenario} · </>}
                      {/* Label it: "default · P1" read as two mysteries. */}
                      <span className={styles.envTag}
                            data-env={envTone(e.environment)}
                            title={`Environment: ${e.environment || 'default'}${e.environment_url ? ` · ${e.environment_url}` : ''}`}>
                        {e.environment || 'default'}
                      </span>
                      {e.priority && <> · {e.priority}</>}
                    </div>
                  </div>
                  <span className={`${styles.trigger} ${e.triggered_by === 'scheduler' ? styles.triggerSched : ''}`}>
                    {e.triggered_by === 'scheduler'
                      ? <><CalendarClock size={12} strokeWidth={2} /> scheduled</>
                      : <><User size={12} strokeWidth={2} /> manual</>}
                  </span>
                  <span className={styles.when}>{formatWhen(e.executed_at)}</span>
                  <span className={styles.dur}>
                    {e.duration_ms ? `${(e.duration_ms / 1000).toFixed(1)}s` : ''}
                  </span>
                  <ChevronRight size={15} strokeWidth={2} className={styles.chev} />
                </button>
                ))}
              </div>
            </div>
          ))}
        </>
      )}

      {tab === 'suites' && (
        <>
          {suiteRuns === null && <div className="skeleton" style={{ height: 200 }} />}
          {suiteRuns && suiteRuns.length === 0 && (
            <div className={styles.empty}>
              No suite runs yet. Run a suite from an application, or create a schedule.
            </div>
          )}
          {suiteRuns && suiteRuns.map(r => (
            <div key={r.id} className={`card ${styles.suiteRun}`}>
              <div className={styles.suiteRunTop}>
                <Layers size={15} strokeWidth={1.9} className={styles.suiteIcon} />
                <div className={styles.rowMain}>
                  <div className={styles.rowTitle}>
                    {r.suite_name || 'Suite'}
                    <span className={styles.schedName}> · {r.schedule_name}</span>
                  </div>
                  <div className={styles.rowMeta}>
                    {r.application_name} · {r.triggered_by === 'scheduler' ? 'scheduled' : 'run by hand'}
                    {' · '}{formatWhen(r.started_at)}
                  </div>
                </div>
                <span className={styles.counts}>
                  <CheckCircle2 size={13} strokeWidth={2} className={styles.pass} />{r.passed ?? 0}
                  <XCircle size={13} strokeWidth={2} className={styles.fail} />{r.failed ?? 0}
                  {(r.skipped ?? 0) > 0 && (
                    <><MinusCircle size={13} strokeWidth={2} className={styles.skip} />{r.skipped}</>
                  )}
                </span>
                {r.webhook_status && <span className={styles.webhook}>{r.webhook_status}</span>}
              </div>

              {r.summary?.results?.length > 0 && (
                <div className={styles.suiteMembers}>
                  {r.summary.results.map((t, i) => (
                    <div key={i} className={styles.memberRow}>
                      <ResultIcon result={t.result} small />
                      <span className={styles.memberTitle}>{t.test}</span>
                      {t.result === 'skipped' && <span className={styles.skipNote}>{t.reason}</span>}
                      {t.duration_ms && (
                        <span className={styles.dur}>{(t.duration_ms / 1000).toFixed(1)}s</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </>
      )}

      {selected && <ExecutionDetail id={selected} onClose={() => setSelected(null)} />}
    </div>
  )
}

/** One execution in full — why it failed, and the screenshots it produced. */
function ExecutionDetail({ id, onClose }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    getExecution(id)
      .then(d => d.error ? setError(d.error) : setData(d.execution))
      .catch(e => setError(e.message))
  }, [id])

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()}>
        <div className={styles.modalHead}>
          <span className={styles.modalTitle}>
            {data ? data.test_title : 'Run detail'}
          </span>
          <button className={styles.ghost} onClick={onClose}>Close</button>
        </div>

        <div className={styles.modalBody}>
          {error && <div className={styles.error}>{error}</div>}
          {!data && !error && <div className="skeleton" style={{ height: 160 }} />}

          {data && (
            <>
              <div className={styles.detailMeta}>
                <ResultIcon result={data.result} />
                <span className={styles.detailResult}>{data.result}</span>
                <span className={styles.metaItem}>
                  <Clock size={12} strokeWidth={2} />
                  {data.duration_ms ? `${(data.duration_ms / 1000).toFixed(1)}s` : '—'}
                </span>
                <span className={styles.metaItem}>{formatWhen(data.executed_at)}</span>
                {data.environment && <span className={styles.envChip}>{data.environment}</span>}
                <span className={styles.metaItem}>
                  {data.triggered_by === 'scheduler' ? 'scheduled' : 'started by hand'}
                </span>
                <Link href={`/applications/${data.application_id}`} className={styles.appLink}>
                  {data.application_name} →
                </Link>
              </div>

              {data.description && (
                <p className={styles.detailDesc}>{data.description}</p>
              )}

              {data.output && data.result !== 'pass' && (
                <div className={styles.section}>
                  <div className={styles.sectionTitle}>Why it failed</div>
                  <pre className={styles.output}>{data.output}</pre>
                </div>
              )}

              {data.screenshots?.length > 0 && (
                <div className={styles.section}>
                  <div className={styles.sectionTitle}>
                    Evidence · {data.screenshots.length} screenshot{data.screenshots.length === 1 ? '' : 's'}
                  </div>
                  <div className={styles.shots}>
                    {data.screenshots.map((s, i) => (
                      <a key={i} href={screenshotUrl(s)} target="_blank" rel="noreferrer">
                        <img src={screenshotUrl(s)} alt={`step ${i + 1}`} className={styles.shot} />
                      </a>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}


/**
 * Group executions so the list answers a question.
 *
 * A flat chronological log is technically accurate and tells you nothing.
 * Grouping decides what the page is FOR:
 *
 *   day   "what ran today, and what broke" — the operational question
 *   test  "how has this test behaved"      — the flaky-hunting question
 */
function groupExecutions(items, mode) {
  const map = new Map()
  for (const e of items) {
    const scenario = e.scenario || 'General'
    const key =
      mode === 'test'     ? `${e.application_name}::${scenario}::${e.test_title}`
      : mode === 'scenario' ? `${e.application_name}::${scenario}`
      : `${e.application_name}::${dayLabel(e.executed_at)}`
    // The name is shown ONCE, in the group header — repeating it on every
    // run is noise, and it is the same test each time by definition.
    const name =
      mode === 'test'     ? `${scenario} › ${e.test_title}`
      : mode === 'scenario' ? scenario
      : dayLabel(e.executed_at)
    const context = e.application_name
    if (!map.has(key)) map.set(key, { key, name, context, mode, items: [], total: 0, passed: 0, failed: 0 })
    const g = map.get(key)
    g.items.push(e)
    g.total += 1
    if (e.result === 'pass') g.passed += 1
    else g.failed += 1
  }
  return Array.from(map.values())
}

function dayLabel(iso) {
  const d = new Date(iso)
  const today = new Date()
  const yest = new Date(Date.now() - 86400000)
  if (d.toDateString() === today.toDateString()) return 'Today'
  if (d.toDateString() === yest.toDateString()) return 'Yesterday'
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' })
}


/**
 * How prominent an environment name should be.
 *
 * Production is called out deliberately: "this ran against prod" is
 * something you must not scroll past, which is why Datadog and BrowserStack
 * both colour it. Everything else stays quiet.
 */
function envTone(name) {
  const n = (name || '').toLowerCase()
  if (n.includes('prod')) return 'prod'
  if (n.includes('stage') || n.includes('uat') || n.includes('pre')) return 'stage'
  return 'normal'
}

function ResultIcon({ result, small }) {
  const size = small ? 13 : 16
  if (result === 'pass') return <CheckCircle2 size={size} strokeWidth={2} className={styles.pass} />
  if (result === 'skipped') return <MinusCircle size={size} strokeWidth={2} className={styles.skip} />
  return <XCircle size={size} strokeWidth={2} className={styles.fail} />
}

function formatWhen(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const today = new Date().toDateString() === d.toDateString()
  return today
    ? `today ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
    : d.toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}
