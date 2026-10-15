'use client'

/**
 * app/reports/page.js
 *
 * The artefacts that leave the tool.
 *
 * The dashboard answers "what is the state right now" and is consumed on
 * screen. A report is parameterised — you choose the scope and the period —
 * and is produced to be sent to someone or filed. That is the line every
 * test management tool draws, and it is why this is not a second dashboard.
 *
 * Three of them:
 *   Traceability   requirement -> test -> result -> evidence. The artefact a
 *                  regulated environment asks for.
 *   Executions     what ran in a period, where, and what started it.
 *   Coverage       per application, with the automation gap stated beside it.
 *
 * All three export to CSV, because a report nobody can send is a dashboard.
 */

import { useState, useEffect, useCallback } from 'react'
import Link from 'next/link'
import {
  FileText, Download, CheckCircle2, XCircle, MinusCircle, AlertTriangle,
  RefreshCw, CalendarClock, User,
} from 'lucide-react'
import {
  getTraceabilityReport, getExecutionReport, getCoverageReport, getApplications,
} from '../../lib/api'
import styles from './Reports.module.css'

const TABS = [
  { id: 'traceability', label: 'Traceability',
    blurb: 'Requirement to test to result to evidence' },
  { id: 'executions',   label: 'Execution report',
    blurb: 'What ran in a period, and where' },
  { id: 'coverage',     label: 'Coverage',
    blurb: 'By application, with the automation gap' },
]

export default function ReportsPage() {
  const [tab, setTab] = useState('traceability')
  const [apps, setApps] = useState([])
  const [applicationId, setApplicationId] = useState('')
  const [days, setDays] = useState(30)
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => { getApplications().then(setApps).catch(() => setApps([])) }, [])

  const load = useCallback(async () => {
    setBusy(true); setData(null)
    try {
      if (tab === 'traceability') setData(await getTraceabilityReport(applicationId || undefined))
      else if (tab === 'executions') setData(await getExecutionReport({
        application_id: applicationId || undefined, days,
      }))
      else setData(await getCoverageReport())
    } catch {
      setData({ error: true })
    } finally { setBusy(false) }
  }, [tab, applicationId, days])

  useEffect(() => { load() }, [load])

  return (
    <div>
      <div className={styles.head}>
        <div>
          <h2 className={styles.title}>Reports</h2>
          <p className={styles.sub}>
            Evidence you can send. Choose the scope, generate, export.
          </p>
        </div>
        <button className={styles.refresh} onClick={load} disabled={busy}>
          <RefreshCw size={14} strokeWidth={2} className={busy ? styles.spin : ''} />
          Regenerate
        </button>
      </div>

      <div className={styles.tabs}>
        {TABS.map(t => (
          <button key={t.id}
                  className={`${styles.tab} ${tab === t.id ? styles.tabOn : ''}`}
                  onClick={() => setTab(t.id)}>
            <span className={styles.tabLabel}>{t.label}</span>
            <span className={styles.tabBlurb}>{t.blurb}</span>
          </button>
        ))}
      </div>

      <div className={styles.controls}>
        {tab !== 'coverage' && (
          <select className={styles.select} value={applicationId}
                  onChange={e => setApplicationId(e.target.value)}>
            <option value="">All applications</option>
            {apps.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        )}
        {tab === 'executions' && (
          <select className={styles.select} value={days}
                  onChange={e => setDays(Number(e.target.value))}>
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
        )}
        {data && !data.error && (
          <button className={styles.export} onClick={() => exportCsv(tab, data)}>
            <Download size={14} strokeWidth={2} /> Export CSV
          </button>
        )}
      </div>

      {busy && <div className="skeleton" style={{ height: 260 }} />}
      {data?.error && <div className={styles.empty}>Could not generate that report.</div>}

      {!busy && data && !data.error && tab === 'traceability' && <Traceability data={data} />}
      {!busy && data && !data.error && tab === 'executions' && <Executions data={data} />}
      {!busy && data && !data.error && tab === 'coverage' && <Coverage data={data} />}

      {data?.generated_at && (
        <p className={styles.footNote}>
          Generated {new Date(data.generated_at).toLocaleString()}
        </p>
      )}
    </div>
  )
}

/** Requirement to test to result to evidence. */
function Traceability({ data }) {
  if (!data?.requirements) return <NotAvailable data={data} what="traceability report" />
  const c = data.counts || {}
  return (
    <>
      <div className={styles.stats}>
        <Stat label="Requirements" value={c.requirements} />
        <Stat label="Verified" value={c.verified} tone="good" />
        <Stat label="Not verified" value={c.unverified}
              tone={c.unverified ? 'warn' : undefined} />
        <Stat label="Tests without a requirement" value={c.tests_untraced}
              tone={c.tests_untraced ? 'warn' : undefined} />
      </div>

      {(data.requirements || []).length === 0 && (
        <div className={styles.empty}>
          No requirements recorded yet. They come from the requirement id captured
          when a scenario is accepted — upload a BRD when creating a test suite and
          they will appear here.
        </div>
      )}

      {(data.requirements || []).map(r => (
        <div key={r.requirement_id} className={styles.reqBlock}>
          <div className={styles.reqHead}>
            {r.verified
              ? <CheckCircle2 size={15} strokeWidth={2} className={styles.pass} />
              : <AlertTriangle size={15} strokeWidth={2} className={styles.warn} />}
            <span className={styles.reqId}>{r.requirement_id}</span>
            <span className={styles.reqState}>
              {r.verified ? 'verified' : 'not verified'}
            </span>
            <span className={styles.reqCount}>
              {r.tests.length} test{r.tests.length === 1 ? '' : 's'}
            </span>
          </div>
          {r.tests.map(t => (
            <div key={t.id} className={styles.testRow}>
              <ResultIcon result={t.last_result} automated={t.automated} />
              <span className={styles.testTitle}>{t.title}</span>
              <span className={styles.testMeta}>
                <Link href={`/applications/${t.application_id}`} className={styles.appLink}>
                  {t.application_name}
                </Link>
                {t.environment && <> · {t.environment}</>}
                {t.last_run_at && <> · {new Date(t.last_run_at).toLocaleDateString()}</>}
              </span>
              {t.accepted_by && (
                <span className={styles.approved} title={`Accepted ${t.accepted_at}`}>
                  approved by {t.accepted_by}
                </span>
              )}
            </div>
          ))}
        </div>
      ))}

      {(data.untraced_tests || []).length > 0 && (
        <div className={styles.untraced}>
          <div className={styles.untracedHead}>
            {(data.untraced_tests || []).length} test case
            {(data.untraced_tests || []).length === 1 ? '' : 's'} with no requirement
          </div>
          <p className={styles.untracedNote}>
            Listed rather than omitted — a test that traces to nothing is a
            finding, not something to leave out of the report.
          </p>
          {(data.untraced_tests || []).slice(0, 20).map(t => (
            <div key={t.id} className={styles.testRow}>
              <ResultIcon result={t.last_result} automated={t.automated} />
              <span className={styles.testTitle}>{t.title}</span>
              <span className={styles.testMeta}>{t.application_name}</span>
            </div>
          ))}
        </div>
      )}
    </>
  )
}

/** What ran in a period. */
function Executions({ data }) {
  // A report page must not white-screen because an endpoint returned
  // something unexpected. Missing data is a message, not a crash.
  if (!data?.runs) return <NotAvailable data={data} what="execution report" />
  const c = data.counts || {}
  return (
    <>
      <div className={styles.stats}>
        <Stat label="Runs" value={c.total} />
        <Stat label="Passed" value={c.passed} tone="good" />
        <Stat label="Failed" value={c.failed} tone={c.failed ? 'warn' : undefined} />
        <Stat label="Pass rate" value={c.pass_rate_pct != null ? `${c.pass_rate_pct}%` : '—'} />
        <Stat label="Unattended" value={c.scheduled} />
      </div>

      {(data.runs || []).length === 0 && (
        <div className={styles.empty}>Nothing ran in this period.</div>
      )}

      {(data.runs || []).length > 0 && (
        <div className="card" style={{ padding: 0 }}>
          {(data.runs || []).map(r => (
            <div key={r.id} className={styles.runRow}>
              <ResultIcon result={r.result} automated />
              <span className={styles.testTitle}>{r.test_title}</span>
              <span className={styles.testMeta}>
                {r.application_name}
                {r.requirement_id && <> · <span className={styles.req}>{r.requirement_id}</span></>}
              </span>
              <span className={styles.env}>{r.environment || 'default'}</span>
              <span className={styles.trigger}>
                {r.triggered_by === 'scheduler'
                  ? <><CalendarClock size={11} strokeWidth={2} /> scheduled</>
                  : <><User size={11} strokeWidth={2} /> manual</>}
              </span>
              <span className={styles.when}>
                {new Date(r.executed_at).toLocaleString([], {
                  day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
                })}
              </span>
            </div>
          ))}
        </div>
      )}
    </>
  )
}

/** Coverage per application. */
function Coverage({ data }) {
  if (!data?.applications) return <NotAvailable data={data} what="coverage report" />
  const t = data.totals || {}
  return (
    <>
      <div className={styles.stats}>
        <Stat label="Test cases" value={t.test_cases} />
        <Stat label="Automated" value={t.automated} tone="good" />
        <Stat label="P0/P1 without a script" value={t.critical_gap}
              tone={t.critical_gap ? 'warn' : undefined} />
        <Stat label="Imported" value={t.imported} />
      </div>

      <div className="card" style={{ padding: 0 }}>
        {(data.applications || []).map(a => (
          <div key={a.id} className={styles.covRow}>
            <Link href={`/applications/${a.id}`} className={styles.covName}>{a.name}</Link>
            <span className={styles.covBar}>
              <span className={styles.covFill} style={{ width: `${a.coverage_pct}%` }} />
            </span>
            <span className={styles.covPct}>{a.coverage_pct}%</span>
            <span className={styles.covMeta}>
              {a.automated} of {a.test_cases} automated
              {a.requirements > 0 && <> · {a.requirements} requirements</>}
            </span>
            {a.critical_gap > 0 && (
              <span className={styles.covGap}>{a.critical_gap} critical without a script</span>
            )}
          </div>
        ))}
      </div>
    </>
  )
}


/**
 * Shown when an endpoint returned something the report cannot render.
 *
 * Says what came back rather than a bare failure: a missing endpoint and an
 * empty result look identical otherwise, and that ambiguity sent me looking
 * in the wrong place more than once already.
 */
function NotAvailable({ data, what }) {
  const detail = data?.detail || data?.error || data?.message
  return (
    <div className={styles.empty}>
      <p style={{ margin: '0 0 8px', color: 'var(--text-primary)' }}>
        The {what} is not available.
      </p>
      <p style={{ margin: 0, fontSize: 12 }}>
        {detail
          ? String(detail)
          : 'The endpoint did not return the expected data. If the backend was ' +
            'not restarted after copying api/routes/reports.py, that is the ' +
            'likely cause.'}
      </p>
    </div>
  )
}

function Stat({ label, value, tone }) {
  return (
    <div className={`${styles.stat} ${tone === 'warn' ? styles.statWarn : ''} ${tone === 'good' ? styles.statGood : ''}`}>
      <span className={styles.statValue}>{value}</span>
      <span className={styles.statLabel}>{label}</span>
    </div>
  )
}

function ResultIcon({ result, automated }) {
  if (result === 'pass') return <CheckCircle2 size={14} strokeWidth={2} className={styles.pass} />
  if (result) return <XCircle size={14} strokeWidth={2} className={styles.fail} />
  return <MinusCircle size={14} strokeWidth={2} className={styles.none}
                      // No result at all is different from a failure, and the
                      // report should not let them look alike.
                      title={automated ? 'Never run' : 'No script'} />
}

/**
 * Write the report to a CSV the browser downloads.
 *
 * Done client-side from data already fetched: a report that cannot leave the
 * screen is a dashboard, and this is the smallest honest way to make it
 * leave. A PDF with letterhead is the next step, not the first.
 */
function exportCsv(tab, data) {
  let rows = []
  if (tab === 'traceability') {
    rows.push(['Requirement', 'Verified', 'Test case', 'Application',
               'Last result', 'Last run', 'Environment', 'Approved by'])
    ;(data.requirements || []).forEach(r => (r.tests || []).forEach(t => rows.push([
      r.requirement_id, r.verified ? 'yes' : 'no', t.title, t.application_name,
      t.last_result || 'never run', t.last_run_at || '', t.environment || '',
      t.accepted_by || '',
    ])))
    ;(data.untraced_tests || []).forEach(t => rows.push([
      '(none)', '', t.title, t.application_name,
      t.last_result || 'never run', t.last_run_at || '', t.environment || '',
      t.accepted_by || '',
    ]))
  } else if (tab === 'executions') {
    rows.push(['Test case', 'Application', 'Requirement', 'Result',
               'Environment', 'Started by', 'When', 'Duration (s)'])
    ;(data.runs || []).forEach(r => rows.push([
      r.test_title, r.application_name, r.requirement_id || '', r.result,
      r.environment || '', r.triggered_by, r.executed_at,
      r.duration_ms ? (r.duration_ms / 1000).toFixed(1) : '',
    ]))
  } else {
    rows.push(['Application', 'Test cases', 'Automated', 'Coverage %',
               'P0/P1 without a script', 'Imported', 'Requirements'])
    ;(data.applications || []).forEach(a => rows.push([
      a.name, a.test_cases, a.automated, a.coverage_pct,
      a.critical_gap, a.imported, a.requirements,
    ]))
  }

  const csv = rows.map(r => r.map(cell => {
    const s = String(cell ?? '')
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }).join(',')).join('\n')

  const stamp = new Date().toISOString().slice(0, 10)
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `nextgen2-${tab}-${stamp}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}