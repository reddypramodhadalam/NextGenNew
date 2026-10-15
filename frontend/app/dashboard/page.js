'use client'

/**
 * app/dashboard/page.js
 * Two role views (toggle), all metrics from real DB data, professional
 * charts via Recharts. Defensible metrics — every number has visible
 * scope, nothing rests on made-up assumptions.
 */

import { useState, useEffect } from 'react'
import {
  AreaChart, Area, BarChart, Bar, LineChart, Line, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer, Cell, PieChart, Pie,
} from 'recharts'
import { getEngineerDashboard, getLeadershipDashboard, getApplications, getAiUsage } from '../../lib/api'
import styles from './Dashboard.module.css'

const C = {
  green: '#4ade80', red: '#f87171', amber: '#fbbf24', skip: '#cbd5e1',
  accent: '#6366f1', shield: '#818cf8', grid: 'var(--border)',
}

/** Range choices for the Execution Trend / Results Distribution filter bar. */
const RANGE_OPTIONS = [
  { value: '7', label: 'Last week' },
  { value: '14', label: 'Last 2 weeks' },
  { value: '30', label: 'Last 30 days' },
  { value: '56', label: 'Last 8 weeks' },
  { value: '90', label: 'Last 3 months' },
]
const DEFAULT_RANGE = '7' // default: "Last week"

/** Test-type choices, shared with the existing by_type breakdown vocabulary. */
const TEST_TYPE_OPTIONS = [
  { value: 'all', label: 'All Types' },
  { value: 'functional', label: 'Functional' },
  { value: 'regression', label: 'Regression' },
  { value: 'smoke', label: 'Smoke' },
]
const ALL_APPS = 'all' // sentinel for the Applications filter's "no restriction" option

/** Row cap requested when "More" is clicked on Failing Tests / Recent Runs. */
const FULL_LIST_LIMIT = 500
/** Character cap for test names on the main dashboard cards (not in the "More" modal). */
const TITLE_TRUNCATE_LEN = 40

export default function DashboardPage() {
  // Leadership is the default view on login — the Subscriber/Engineer
  // view is still one click away via the toggle below, this only
  // changes which tab is selected first.
  const [role, setRole] = useState('leadership')
  return (
    <div>
      <div className={styles.head}>
        <div>
          <h1>Dashboard</h1>
          <p className={styles.sub}>
            {role === 'engineer' ? '' : ''}
          </p>
        </div>
        <div className={styles.roleToggle}>
          <button className={role === 'engineer' ? styles.roleOn : ''} onClick={() => setRole('engineer')}>Subscriber</button>
          <button className={role === 'leadership' ? styles.roleOn : ''} onClick={() => setRole('leadership')}>Leadership</button>
        </div>
      </div>
      {role === 'engineer' ? <EngineerView /> : <LeadershipView />}
      <TokensCostCard />
    </div>
  )
}

/**
 * Top-of-dashboard summary — three at-a-glance counters shown to both
 * roles: how many applications are under test, how much of the scenario
 * estate is automated, and how many executions those automated tests
 * have racked up. Reads off the same `scope`/`kpis` payload each view
 * already fetches from the dashboard API — no new endpoint, one source
 * of truth for these numbers.
 */
function SummaryCards({ apps, scenarios, automated, executions }) {
  return (
    <div className={styles.summaryGrid}>
      <Kpi label="Applications" value={apps} sub="under test" tone="accent" />
      <Kpi label="Total Scenarios" value={scenarios} sub={`${automated} automated`} tone="accent" />
      <Kpi label="Executions" value={executions} sub="across automated tests" tone="accent" />
    </div>
  )
}

/**
 * Range + test-type + application filters. Used by both roles, applied
 * to EVERY panel positioned below this bar in that role's view —
 * Engineer's Execution Trend, Results Distribution, Failing Tests AND
 * Recent Runs; Leadership's Quality Trend, Automated Scripts by
 * Application AND Coverage by Application (including its per-module
 * drill-down) — not just the nearest chart. Only the KPI cards ABOVE
 * this bar (SummaryCards, Critical Pass Rate/Flaky/Avg Run Time for
 * Engineer; Automation Coverage/Incomplete/Critical Pass Rate/Overall
 * Pass Rate for Leadership) stay on the full, unfiltered dataset, so
 * picking a filter never makes one of THOSE numbers jump around.
 *
 * `applications` is the live list from GET /v1/applications (already used
 * elsewhere in the app) — the dropdown always reflects real app names,
 * never a hardcoded list.
 *
 * @param {string} [idPrefix] - Distinguishes this instance's <select> ids
 *   from the other role's FilterBar (only one is ever mounted at a time,
 *   but unique ids keep labels/inputs correctly associated regardless).
 */
function FilterBar({ range, onRangeChange, testType, onTestTypeChange, applicationId, onApplicationChange, applications, idPrefix = 'exec' }) {
  return (
    <div className={styles.filterBar}>
      <div className={styles.filterGroup}>
        <label className={styles.filterLabel} htmlFor={`${idPrefix}-range-filter`}>Range</label>
        <select
          id={`${idPrefix}-range-filter`}
          className={styles.filterSelect}
          value={range}
          onChange={e => onRangeChange(e.target.value)}
        >
          {RANGE_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>
      <div className={styles.filterGroup}>
        <label className={styles.filterLabel} htmlFor={`${idPrefix}-type-filter`}>Test Type</label>
        <select
          id={`${idPrefix}-type-filter`}
          className={styles.filterSelect}
          value={testType}
          onChange={e => onTestTypeChange(e.target.value)}
        >
          {TEST_TYPE_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>
      <div className={styles.filterGroup}>
        <label className={styles.filterLabel} htmlFor={`${idPrefix}-app-filter`}>Application</label>
        <select
          id={`${idPrefix}-app-filter`}
          className={styles.filterSelect}
          value={applicationId}
          onChange={e => onApplicationChange(e.target.value)}
        >
          <option value={ALL_APPS}>All Applications</option>
          {applications.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
      </div>
    </div>
  )
}

/** Number format for token counts: full numbers with thousands separators. */
const tokenFormat = new Intl.NumberFormat('en-US')

/** The two AI activities the Tokens & Cost table reports on, in display order. */
const AI_ACTIVITIES = [
  { key: 'test_case_generation', label: 'Test case generation', countKey: 'test_cases_generated' },
  { key: 'script_generation', label: 'Tests automated', countKey: 'tests_automated' },
]

/**
 * Formats a USD amount. Amounts of $1 or more show 2 decimals; smaller
 * amounts show 4 so low-cost models don't collapse into "$0.00".
 */
function formatCost(value) {
  const n = Number(value) || 0
  if (n === 0) return '$0.00'
  if (n < 0.0001) return '< $0.0001'
  const digits = n >= 1 ? 2 : 4
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
}

/**
 * Tokens & Cost section, shown in the same rounded card and table style as
 * "Coverage by Application". One row per AI activity (test case generation,
 * script generation) plus a Total row, with input tokens, output tokens and
 * cost. Has its own range and application filters, so it works the same in
 * both dashboard views.
 */
function TokensCostCard() {
  const [range, setRange] = useState('30')
  const [applicationId, setApplicationId] = useState(ALL_APPS)
  const [applications, setApplications] = useState([])
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  // Loads the application list once, for the filter dropdown.
  useEffect(() => { getApplications().then(setApplications).catch(() => setApplications([])) }, [])

  // Reloads usage whenever a filter changes. `cancelled` stops an older,
  // slower response from overwriting a newer one.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getAiUsage({ rangeDays: Number(range), applicationId })
      .then(d => { if (!cancelled) setData(d) })
      .catch(e => { if (!cancelled) setError(e.message) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [range, applicationId])


  // Description on the left, filters on the right. Always shown so the filters stay usable.
  // The filter bar's own border and background are removed because the card already provides them.
  const controls = (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, marginBottom: 14 }}>

      <div className={styles.filterBar} style={{ margin: 0, padding: 0, border: 'none', background: 'transparent', boxShadow: 'none' }}>
        <div className={styles.filterGroup}>
          <label className={styles.filterLabel} htmlFor="ai-range-filter">Range</label>
          <select id="ai-range-filter" className={styles.filterSelect} value={range} onChange={e => setRange(e.target.value)}>
            {RANGE_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        <div className={styles.filterGroup}>
          <label className={styles.filterLabel} htmlFor="ai-app-filter">Application</label>
          <select id="ai-app-filter" className={styles.filterSelect} value={applicationId} onChange={e => setApplicationId(e.target.value)}>
            <option value={ALL_APPS}>All Applications</option>
            {applications.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </div>
      </div>
    </div>
  )

  // Chooses what goes under the controls: placeholder while loading, an error message, or the table.
  // Later reloads keep the old numbers on screen until the new ones arrive.
  let content
  if (loading && !data) {
    content = <div className="skeleton" style={{ height: 140 }} />
  } else if (error) {
    content = <div className={styles.errBox}>Could not load token usage: {error}</div>
  } else {
    // One row per activity. An activity with no usage in the period shows zeros.
    const byPurpose = data?.by_purpose || []
    const rows = AI_ACTIVITIES.map(({ key, label, countKey }) => {
      const p = byPurpose.find(x => x.purpose === key)
      return {
        key,
        label,
        count: data?.counts?.[countKey] || 0,
        input: p?.input_tokens || 0,
        output: p?.output_tokens || 0,
        cost: Number(p?.cost_usd || 0),
        unpriced: p?.unpriced_calls || 0,
      }
    })

    // Total row adds up the two activities.
    const totalInput = rows.reduce((n, r) => n + r.input, 0)
    const totalOutput = rows.reduce((n, r) => n + r.output, 0)
    const totalCost = rows.reduce((n, r) => n + r.cost, 0)
    const unpricedCalls = rows.reduce((n, r) => n + r.unpriced, 0)

    content = (
      <>
        <table className="table">
          <thead>
            <tr><th>Activity</th><th>Count</th><th>Input Tokens</th><th>Output Tokens</th><th>Cost</th></tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.key}>
                <td style={{ color: 'var(--text-primary)', fontWeight: 400 }}>{r.label}</td>
                <td className={styles.aiNum}>{tokenFormat.format(r.count)}</td>
                <td className={styles.aiNum}>{tokenFormat.format(r.input)}</td>
                <td className={styles.aiNum}>{tokenFormat.format(r.output)}</td>
                <td className={styles.aiNum}>{formatCost(r.cost)}</td>
              </tr>
            ))}
            <tr className={styles.aiTotalRow}>
              <td style={{ color: 'var(--text-primary)', fontWeight: 600 }}>Total</td>
              {/* Test cases and scripts are different things, so adding them together would mean nothing. */}
              <td className={styles.aiNum}>—</td>
              <td className={styles.aiNum}>{tokenFormat.format(totalInput)}</td>
              <td className={styles.aiNum}>{tokenFormat.format(totalOutput)}</td>
              <td className={styles.aiNum}>{formatCost(totalCost)}</td>
            </tr>
          </tbody>
        </table>

        {/* Explains an incomplete cost, so a missing price doesn't look like a real $0. */}
        {unpricedCalls > 0 && (
          <div className="text-muted" style={{ fontSize: 12, marginTop: 10 }}>
            {tokenFormat.format(unpricedCalls)} call(s) have no cost because pricing is not configured for their model.
          </div>
        )}
      </>
    )
  }

  // Panel is the same rounded card used by "Coverage by Application" and the other sections.
  return (
    <Panel title="Tokens & Cost">
      {controls}
      {content}
    </Panel>
  )
}

function EngineerView() {
  const [d, setD] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => { getEngineerDashboard().then(setD).catch(e => setErr(e.message)) }, [])

  // Range/Test Type/Application filters — a second, independent fetch
  // (see FilterBar's doc comment for why this is kept separate from `d`
  // instead of refetching the whole dashboard payload). Applied to
  // EVERY panel below the filter bar: Execution Trend, Results
  // Distribution, Failing Tests, AND Recent Runs all read from `scoped`
  // (this filtered result), not from the initial unfiltered `d` — only
  // the KPI cards above the filter bar still read `d` directly.
  const [range, setRange] = useState(DEFAULT_RANGE)
  const [testType, setTestType] = useState('all')
  const [applicationId, setApplicationId] = useState(ALL_APPS)
  const [applications, setApplications] = useState([])
  const [trendD, setTrendD] = useState(null)

  useEffect(() => { getApplications().then(setApplications).catch(() => setApplications([])) }, [])
  useEffect(() => {
    getEngineerDashboard({ rangeDays: Number(range), testType, applicationId })
      .then(setTrendD)
      .catch(() => setTrendD(null))
  }, [range, testType, applicationId])

  // "More" modal for Failing Tests / Recent Runs — fetched on demand
  // (not on page load) since it asks the backend for up to
  // FULL_LIST_LIMIT rows instead of the dashboard card's default
  // ~12/~10. Carries the SAME range/testType/applicationId currently
  // selected in the filter bar, so "More" shows the rest of what's
  // already displayed, not a different, unfiltered universe.
  const [modalKind, setModalKind] = useState(null) // 'failing' | 'recent' | null
  const [modalRows, setModalRows] = useState([])
  const [modalLoading, setModalLoading] = useState(false)

  function openMore(kind) {
    setModalKind(kind)
    setModalLoading(true)
    const base = { rangeDays: Number(range), testType, applicationId }
    const params = kind === 'failing'
      ? { ...base, failingLimit: FULL_LIST_LIMIT }
      : { ...base, recentLimit: FULL_LIST_LIMIT }
    getEngineerDashboard(params)
      .then(full => setModalRows(kind === 'failing' ? full.failing : full.recent_runs))
      .catch(() => setModalRows([]))
      .finally(() => setModalLoading(false))
  }
  function closeModal() { setModalKind(null); setModalRows([]) }

  if (err) return <ErrBox msg={err} />
  if (!d) return <LoadingGrid />

  // Fall back to the initial unfiltered load so every panel below the
  // filter bar renders immediately on first paint, before the filtered
  // fetch resolves.
  const scoped = trendD || d
  const rangeLabel = (RANGE_OPTIONS.find(o => o.value === range)?.label || 'Last week').toLowerCase()

  const dist = scoped.distribution
  const distData = [
    { name: 'Passed', value: dist.pass, color: C.green },
    { name: 'Failed', value: dist.fail, color: C.red },
    { name: 'Error', value: dist.error, color: C.amber },
    { name: 'Skipped', value: dist.skip, color: C.skip },
  ].filter(x => x.value > 0)
  const distTotal = dist.pass + dist.fail + dist.skip + dist.error || 0
  const trendData = (scoped.trend || []).map(t => ({ wk: formatDDMMM(t.date || t.wk), Passed: t.passed, Failed: t.failed, Other: t.other }))
  const autoSub = d.kpis.incomplete_tests > 0
    ? `of ${d.kpis.total_scenarios} scenarios · ${d.kpis.incomplete_tests} incomplete`
    : `of ${d.kpis.total_scenarios} scenarios`

  return (
    <>
      <SummaryCards
        apps={d.scope.apps}
        scenarios={d.kpis.total_scenarios}
        automated={d.kpis.total_tests}
        executions={d.scope.runs}
      />
      <div className={styles.summaryGrid}>
        <Kpi label="Critical Pass Rate" value={`${d.kpis.p0_pass_rate}%`} sub="P0 + P1 tests" tone={d.kpis.p0_pass_rate >= 90 ? 'ok' : d.kpis.p0_pass_rate >= 70 ? 'warn' : 'bad'} />
        <Kpi label="Flaky Tests" value={d.kpis.flaky} sub="mixed pass/fail" tone={d.kpis.flaky === 0 ? 'ok' : 'warn'} />
        <Kpi label="Avg Run Time" value={`${(d.kpis.avg_ms / 1000).toFixed(1)}s`} sub="per test" tone="neutral" />
      </div>

      <FilterBar
        range={range} onRangeChange={setRange}
        testType={testType} onTestTypeChange={setTestType}
        applicationId={applicationId} onApplicationChange={setApplicationId}
        applications={applications}
      />
      <div className={styles.row2}>
        <Panel title="Execution Trend" chip={rangeLabel}>
          <div className={styles.chartBox}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={trendData} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
                <XAxis dataKey="wk" tick={{ fontSize: 11, fill: 'var(--text-muted)' }} tickLine={false} axisLine={{ stroke: C.grid }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11, fill: 'var(--text-muted)' }} tickLine={false} axisLine={false} />
                <Tooltip content={<ChartTip />} cursor={{ fill: 'var(--bg-card-alt)' }} />
                <Bar dataKey="Passed" stackId="a" fill={C.green} maxBarSize={34} />
                <Bar dataKey="Failed" stackId="a" fill={C.red} maxBarSize={34} />
                <Bar dataKey="Other" stackId="a" fill={C.skip} radius={[4, 4, 0, 0]} maxBarSize={34} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Panel>
        <Panel title="Results Distribution" chip={`${distTotal} runs`}>
          {distTotal === 0 ? <div className={styles.chartBox}><Empty msg="No runs yet." /></div> : (
            <div className={styles.donutLayout}>
              <div className={styles.donutChart}>
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie data={distData} dataKey="value" nameKey="name" innerRadius={52} outerRadius={74} paddingAngle={2.5} stroke="none" cornerRadius={3}>
                      {distData.map((e, i) => <Cell key={i} fill={e.color} />)}
                    </Pie>
                    <Tooltip content={<ChartTip />} />
                  </PieChart>
                </ResponsiveContainer>
                <div className={styles.donutCenterAbs}>
                  <div className={styles.donutNum}>{distTotal}</div>
                  <div className={styles.donutCap}>runs</div>
                </div>
              </div>
              <div className={styles.donutLegend}>
                {distData.map(s => (
                  <div key={s.name} className={styles.donutLegendRow}>
                    <span className={styles.dot} style={{ background: s.color }} />
                    <span className={styles.donutLegendName}>{s.name}</span>
                    <span className={styles.donutLegendVal}>{s.value}</span>
                    <span className={styles.donutLegendPct}>{Math.round(s.value / distTotal * 100)}%</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Failing Tests" onMore={() => openMore('failing')}>
        <FailingTable rows={scoped.failing} />
      </Panel>

      <Panel title="Recent Runs" onMore={() => openMore('recent')}>
        <RecentRunsList rows={scoped.recent_runs} />
      </Panel>

      {modalKind && (
        <Modal
          title={modalKind === 'failing' ? 'Failing Tests — full list' : 'Recent Runs — full list'}
          onClose={closeModal}
        >
          {modalLoading ? (
            <div className={styles.emptyChart}>Loading…</div>
          ) : modalKind === 'failing' ? (
            <FailingTable rows={modalRows} truncateNames={false} />
          ) : (
            <RecentRunsList rows={modalRows} truncateNames={false} />
          )}
        </Modal>
      )}
    </>
  )
}

function LeadershipView() {
  const [d, setD] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => { getLeadershipDashboard().then(setD).catch(e => setErr(e.message)) }, [])

  // Which application rows are expanded in "Coverage by Application", to
  // reveal their per-module breakdown. Keyed by app name (unique in this
  // table); collapsed by default so the table's default look is unchanged.
  const [expandedApps, setExpandedApps] = useState(() => new Set())
  function toggleApp(appName) {
    setExpandedApps(prev => {
      const next = new Set(prev)
      if (next.has(appName)) next.delete(appName)
      else next.add(appName)
      return next
    })
  }

  // Range/Test Type/Application filters — mirrors EngineerView's pattern
  // exactly (see FilterBar's doc comment): a second, independent fetch
  // applied to EVERY panel below the filter bar — Quality Trend,
  // Automated Scripts by Application, AND Coverage by Application (incl.
  // its per-module drill-down) all read from `scoped`, not from the
  // initial unfiltered `d`. Only the KPI row above the filter bar still
  // reads `d` directly, so it never shifts just because a filter changed.
  const [range, setRange] = useState(DEFAULT_RANGE)
  const [testType, setTestType] = useState('all')
  const [applicationId, setApplicationId] = useState(ALL_APPS)
  const [applications, setApplications] = useState([])
  const [trendD, setTrendD] = useState(null)

  useEffect(() => { getApplications().then(setApplications).catch(() => setApplications([])) }, [])
  useEffect(() => {
    getLeadershipDashboard({ rangeDays: Number(range), testType, applicationId })
      .then(setTrendD)
      .catch(() => setTrendD(null))
  }, [range, testType, applicationId])

  if (err) return <ErrBox msg={err} />
  if (!d) return <LoadingGrid />

  // Fall back to the initial unfiltered load so every panel below the
  // filter bar renders immediately on first paint, before the filtered
  // fetch resolves.
  const scoped = trendD || d
  const rangeLabel = (RANGE_OPTIONS.find(o => o.value === range)?.label || 'Last week').toLowerCase()

  const cf = d.kpis.coverage_fraction
  const qtrend = (scoped.quality_trend || []).map(t => ({ wk: formatDDMMM(t.date || t.wk), pass: Number(t.pass_rate) || 0 }))
  // 3-way stacked breakdown per application — see _scripts_by_app's
  // docstring (dashboard.py) for exactly what each bucket means.
  const sbApp = (scoped.scripts_by_app || []).map(t => ({
    name: t.name,
    Automated: Number(t.automated) || 0,
    'Not Automated': Number(t.not_automated) || 0,
    'Manual Only': Number(t.manual_only) || 0,
  }))

  // Data for the 4 top-of-page donut KPI cards. All unfiltered (read
  // from `d`, never `scoped`) — see SummaryKpiRow's doc comment.
  const appsAuto = d.kpis.apps_automation || { with_automation: 0, without_automation: 0, total: d.scope.apps }
  const execDist = d.kpis.execution_distribution || { pass: 0, fail: 0, skip: 0, error: 0 }
  const passCounts = d.kpis.pass_counts || { overall: { pass: 0, total: 0 }, critical: { pass: 0, total: 0 } }
  const notAutomatedScenarios = Math.max(0, cf.total - cf.automated - cf.incomplete)

  return (
    <>
      <div className={styles.pieCardGrid}>
        <PieKpiCard
          title="Applications"
          centerValue={d.scope.apps}
          centerLabel="under test"
          data={[
            { name: 'Automated', value: appsAuto.with_automation, color: C.green },
            { name: 'Not Automated', value: appsAuto.without_automation, color: C.skip },
          ]}
        />
        <PieKpiCard
          title="Total Scenarios"
          centerValue={cf.total}
          centerLabel="scenarios"
          data={[
            { name: 'Automated', value: cf.automated, color: C.green },
            { name: 'Incomplete', value: cf.incomplete, color: C.amber },
            { name: 'Not Automated', value: notAutomatedScenarios, color: C.skip },
          ]}
        />
        <PieKpiCard
          title="Executions"
          centerValue={d.scope.runs}
          centerLabel="total runs"
          data={[
            { name: 'Passed', value: execDist.pass || 0, color: C.green },
            { name: 'Failed', value: execDist.fail || 0, color: C.red },
            { name: 'Error', value: execDist.error || 0, color: C.amber },
            { name: 'Skipped', value: execDist.skip || 0, color: C.skip },
          ]}
        />
        <PassRateDonutCard overall={passCounts.overall} critical={passCounts.critical} />
      </div>

      <FilterBar
        idPrefix="lead"
        range={range} onRangeChange={setRange}
        testType={testType} onTestTypeChange={setTestType}
        applicationId={applicationId} onApplicationChange={setApplicationId}
        applications={applications}
      />
      <div className={styles.row2}>
        <Panel title="Quality Trend" chip={`pass rate · ${rangeLabel}`}>
          <div className={styles.chartBox}>
            {qtrend.length === 0 ? <Empty msg="No runs yet." /> : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={qtrend} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                  <defs>
                    <linearGradient id="qg" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={C.green} stopOpacity={0.4} />
                      <stop offset="100%" stopColor={C.green} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
                  <XAxis dataKey="wk" tick={{ fontSize: 11, fill: 'var(--text-muted)' }} tickLine={false} axisLine={{ stroke: C.grid }} interval="preserveStartEnd" />
                  <YAxis domain={[0, 100]} tick={{ fontSize: 11, fill: 'var(--text-muted)' }} tickLine={false} axisLine={false} unit="%" />
                  <Tooltip content={<ChartTip suffix="%" />} />
                  <Area type="monotone" dataKey="pass" stroke={C.green} strokeWidth={2.5} fill="url(#qg)" name="Pass rate" />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </Panel>
        <Panel title="Automated Scripts by Application" chip={testType !== 'all' || applicationId !== ALL_APPS ? '' : ''}>
          <div className={styles.chartBox}>
            {sbApp.length === 0 ? <Empty msg="No scenarios yet." /> : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={sbApp} margin={{ top: 8, right: 12, left: -10, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
                  <XAxis dataKey="name" tick={{ fontSize: 11, fill: 'var(--text-muted)' }} tickLine={false} axisLine={{ stroke: C.grid }} interval={0} angle={0} />
                  <YAxis tick={{ fontSize: 11, fill: 'var(--text-muted)' }} tickLine={false} axisLine={false} allowDecimals={false} />
                  <Tooltip content={<ChartTip />} cursor={{ fill: 'var(--bg-card-alt)' }} />
                  <Bar dataKey="Automated" stackId="scn" fill={C.green} maxBarSize={38} />
                  <Bar dataKey="Manual Only" stackId="scn" fill={C.skip} maxBarSize={38} />
                  <Bar dataKey="Not Automated" stackId="scn" fill={C.amber} radius={[5, 5, 0, 0]} maxBarSize={38} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
          {sbApp.length > 0 && (
            <div className={styles.legendRow2}>
              <span className={styles.legendItem} title="Scenario has at least one successful, working automated script.">
                <span className={styles.dot} style={{ background: C.green }} /> <b>Automated</b>
              </span>
              <span className={styles.legendItem} title="A reviewer explicitly marked this scenario as not to be automated (e.g. needs manual/visual judgement).">
                <span className={styles.dot} style={{ background: C.skip }} /> <b>Manual Only</b>
              </span>
              <span className={styles.legendItem} title="No working script yet, and not flagged manual-only — could still be automated.">
                <span className={styles.dot} style={{ background: C.amber }} /> <b>Not Automated</b>
              </span>
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Coverage by Application">
        <table className="table">
          <thead><tr><th className={styles.expandCol}></th><th>Application</th><th>NextGen Scenarios</th><th>Automated</th><th>Incomplete</th><th>NextGen Coverage</th></tr></thead>
          <tbody>
            {(scoped.coverage_by_app || []).flatMap((a, i) => {
              const hasModules = (a.modules || []).length > 0
              const isOpen = hasModules && expandedApps.has(a.app)
              const rows = [
                <tr key={`app-${i}`}>
                  <td className={styles.expandCol}>
                    {hasModules && (
                      <button
                        type="button"
                        className={styles.expandBtn}
                        onClick={() => toggleApp(a.app)}
                        aria-label={isOpen ? `Collapse ${a.app} modules` : `Expand ${a.app} modules`}
                        aria-expanded={isOpen}
                      >
                        {isOpen ? '▾' : '▸'}
                      </button>
                    )}
                  </td>
                  <td style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{a.app}</td>
                  <td>{a.scenarios}</td>
                  <td>{a.automated}</td>
                  <td>{a.incomplete || 0}</td>
                  <td>
                    <div className={styles.covBar}>
                      <div className={styles.covTrack}><div className={styles.covFill} style={{ width: `${a.coverage}%`, background: covColor(a.coverage) }} /></div>
                      <span className={styles.covPct}>{a.coverage}%</span>
                    </div>
                  </td>
                </tr>,
              ]
              if (isOpen) {
                a.modules.forEach((m, j) => {
                  rows.push(
                    <tr key={`app-${i}-mod-${j}`} className={styles.moduleRow}>
                      <td className={styles.expandCol}></td>
                      <td className={styles.moduleName}>{m.module}</td>
                      <td>{m.scenarios}</td>
                      <td>{m.automated}</td>
                      <td>{m.incomplete || 0}</td>
                      <td>
                        <div className={styles.covBar}>
                          <div className={styles.covTrack}><div className={styles.covFill} style={{ width: `${m.coverage}%`, background: covColor(m.coverage) }} /></div>
                          <span className={styles.covPct}>{m.coverage}%</span>
                        </div>
                      </td>
                    </tr>
                  )
                })
              }
              return rows
            })}
          </tbody>
        </table>
      </Panel>
    </>
  )
}

/**
 * One KPI tile: label, big number, optional caption.
 * The caption line is only drawn when `sub` is given.
 */
function Kpi({ label, value, sub, tone }) {
  const color = { accent: 'var(--accent)', ok: 'var(--green)', warn: 'var(--amber)', bad: 'var(--red)', neutral: 'var(--text-secondary)' }[tone] || 'var(--accent)'
  return (
    <div className={styles.kpi}>
      <div className={styles.kpiLabel}>{label}</div>
      <div className={styles.kpiVal} style={{ color }}>{value}</div>
      {sub ? <div className={styles.kpiSub}>{sub}</div> : null}
    </div>
  )
}

/**
 * One donut-chart KPI card for the Leadership view's top summary row.
 * Consolidates what used to be 7 flat number tiles (Applications, Total
 * Scenarios, Executions, Automation Coverage, Incomplete Scenarios,
 * Critical Pass Rate, Overall Pass Rate) into 4 cards — this one
 * reused 3 times (Applications / Total Scenarios / Executions) plus
 * PassRateDonutCard below for the 4th. No number from those 7 tiles is
 * dropped: every one now appears as either the center value or a
 * legend row's value on one of these 4 cards.
 *
 * Always fed from `d` (the unfiltered load), never `scoped` — same
 * "KPI row above the filter bar stays unfiltered" convention already
 * used by the rest of this page (see FilterBar's doc comment).
 *
 * @param {string} title - Card heading (e.g. "Applications").
 * @param {number|string} centerValue - Big number shown in the donut's
 *   center hole.
 * @param {string} centerLabel - Small caption under centerValue.
 * @param {Array<{name:string,value:number,color:string}>} data - Pie
 *   slices. Every entry always appears as a legend row (with its raw
 *   value + share of the total) even when value is 0, so the legend
 *   fully explains what the donut could show, not just what it
 *   currently does — the pie itself only renders non-zero slices.
 */
function PieKpiCard({ title, centerValue, centerLabel, data }) {
  const slices = data.filter(s => s.value > 0)
  const total = data.reduce((n, s) => n + (s.value || 0), 0)
  return (
    <div className={styles.pieKpiCard}>
      <div className={styles.pieKpiTitle}>{title}</div>
      {slices.length === 0 ? (
        <div className={styles.emptyChart}>No data yet.</div>
      ) : (
        <div className={styles.pieKpiBody}>
          <div className={styles.pieKpiChartCol}>
            <div className={styles.pieKpiChart}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={slices} dataKey="value" nameKey="name" innerRadius={46} outerRadius={62} paddingAngle={2.5} stroke="none" cornerRadius={2}>
                    {slices.map((s, i) => <Cell key={i} fill={s.color} />)}
                  </Pie>
                  <Tooltip content={<ChartTip />} />
                </PieChart>
              </ResponsiveContainer>
              {/* Only the number lives inside the ring's hole — a single
                  short line always fits cleanly. The caption is rendered
                  as normal-flow text BELOW the chart box instead (see
                  .pieKpiCap below), so it can never compete with the ring
                  for the same pixels no matter how long the label is. */}
              <div className={styles.pieKpiCenterAbs}>
                <div className={styles.pieKpiNum}>{centerValue}</div>
              </div>
            </div>
            <div className={styles.pieKpiCap}>{centerLabel}</div>
          </div>
          <div className={styles.pieKpiLegend}>
            {data.map(s => (
              <div key={s.name} className={styles.pieKpiLegendRow}>
                <span className={styles.dot} style={{ background: s.color }} />
                <span className={styles.pieKpiLegendName}>{s.name}</span>
                <span className={styles.pieKpiLegendVal}>
                  {s.value}{total > 0 ? ` · ${Math.round((s.value || 0) / total * 100)}%` : ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Critical vs Overall pass rate as ONE nested donut instead of two
 * disconnected percentage tiles — the outer ring is the Overall
 * (all-executions) pass/fail split, the inner ring is the Critical
 * (P0+P1 priority only) pass/fail split, so a viewer can see at a
 * glance whether the highest-impact tests are doing better or worse
 * than the estate as a whole. Green/red is shared by both rings
 * (Passed/Failed) — what distinguishes the rings is their radius
 * (outer vs inner) and the two legend rows explaining each.
 *
 * @param {{pass:number,total:number}} overall
 * @param {{pass:number,total:number}} critical
 */
function PassRateDonutCard({ overall, critical }) {
  const overallData = [
    { name: 'Overall Passed', value: overall.pass || 0, color: C.green },
    { name: 'Overall Failed', value: Math.max(0, (overall.total || 0) - (overall.pass || 0)), color: C.red },
  ].filter(s => s.value > 0)
  const criticalData = [
    { name: 'Critical Passed', value: critical.pass || 0, color: C.green },
    { name: 'Critical Failed', value: Math.max(0, (critical.total || 0) - (critical.pass || 0)), color: C.red },
  ].filter(s => s.value > 0)
  const overallPct = overall.total ? Math.round(100 * (overall.pass || 0) / overall.total) : 0
  const criticalPct = critical.total ? Math.round(100 * (critical.pass || 0) / critical.total) : 0
  const hasData = overallData.length > 0 || criticalData.length > 0

  return (
    <div className={styles.pieKpiCard}>
      <div className={styles.pieKpiTitle}>Pass Rate</div>
      {!hasData ? (
        <div className={styles.emptyChart}>No executions yet.</div>
      ) : (
        <div className={styles.pieKpiBody}>
          <div className={styles.pieKpiChartCol}>
            <div className={styles.pieKpiChart}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  {overallData.length > 0 && (
                    <Pie data={overallData} dataKey="value" nameKey="name" innerRadius={50} outerRadius={62} paddingAngle={2} stroke="none">
                      {overallData.map((s, i) => <Cell key={i} fill={s.color} />)}
                    </Pie>
                  )}
                  {criticalData.length > 0 && (
                    <Pie data={criticalData} dataKey="value" nameKey="name" innerRadius={32} outerRadius={42} paddingAngle={2} stroke="none">
                      {criticalData.map((s, i) => <Cell key={i} fill={s.color} />)}
                    </Pie>
                  )}
                  <Tooltip content={<ChartTip />} />
                </PieChart>
              </ResponsiveContainer>
              {/* Same "number inside, caption outside" split as PieKpiCard
                  above — see that component's comment. */}
              <div className={styles.pieKpiCenterAbs}>
                <div className={styles.pieKpiNum}>{overallPct}%</div>
              </div>
            </div>
            <div className={styles.pieKpiCap}>overall</div>
          </div>
          <div className={styles.pieKpiLegend}>
            <div className={styles.pieKpiLegendRow}>
              <span className={styles.dot} style={{ background: C.green }} />
              <span className={styles.pieKpiLegendName}>Passed</span>
            </div>
            <div className={styles.pieKpiLegendRow}>
              <span className={styles.dot} style={{ background: C.red }} />
              <span className={styles.pieKpiLegendName}>Failed</span>
            </div>
            <div className={styles.pieKpiLegendRow} title="All executions across every test case.">
              <span className={styles.pieKpiLegendName}><b>Overall</b> (outer ring)</span>
              <span className={styles.pieKpiLegendVal}>{overallPct}%</span>
            </div>
            <div className={styles.pieKpiLegendRow} title="P0 and P1 priority tests only — the highest-impact subset.">
              <span className={styles.pieKpiLegendName}><b>Critical</b> (inner ring)</span>
              <span className={styles.pieKpiLegendVal}>{criticalPct}%</span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * @param {function} [onMore] - When provided, renders a "More" action next
 *   to the chip. Omitted entirely (as in every other Panel on this page)
 *   leaves the header exactly as it was before.
 */
function Panel({ title, chip, onMore, moreLabel = 'More', children }) {
  return (
    <div className="card" style={{ marginBottom: 15 }}>
      <div className={styles.panelHead}>
        <span className={styles.panelTitle}>{title}</span>
        <div className={styles.panelHeadRight}>
          {chip && <span className={styles.chip}>{chip}</span>}
          {onMore && (
            <button type="button" className={styles.moreBtn} onClick={onMore}>
              {moreLabel} →
            </button>
          )}
        </div>
      </div>
      <div className={styles.panelBody}>{children}</div>
    </div>
  )
}

/**
 * Failing Tests table body — shared by the dashboard card (truncated
 * names, capped at ~12 rows server-side) and the "More" modal (full
 * names, up to FULL_LIST_LIMIT rows).
 */
function FailingTable({ rows, truncateNames = true }) {
  if (rows.length === 0) {
    return <div className={styles.allGreen}>✓ No failing tests. Everything&apos;s green.</div>
  }
  return (
    <table className="table">
      <thead><tr><th>Test</th><th>Priority</th><th>Type</th><th>Category</th><th>Last run</th></tr></thead>
      <tbody>
        {rows.map((f, i) => (
          <tr key={i}>
            <td style={{ color: 'var(--text-primary)' }} title={f.title}>
              {truncateNames ? truncate(f.title) : f.title}
            </td>
            <td><span className={`badge badge-${prioBadge(f.priority)}`}>{f.priority || '—'}</span></td>
            <td className="text-muted">{f.test_type}</td>
            <td>{f.category && <span className={`badge badge-${catBadge(f.category)}`}>{f.category}</span>}</td>
            <td className="text-muted" style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>{shortDate(f.last_run)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/**
 * Recent Runs list body — shared by the dashboard card (truncated names,
 * capped at ~10 rows server-side) and the "More" modal (full names, up
 * to FULL_LIST_LIMIT rows).
 */
function RecentRunsList({ rows, truncateNames = true }) {
  if (rows.length === 0) {
    return <div className={styles.emptyChart}>No runs yet.</div>
  }
  return (
    <div className={styles.recentList}>
      {rows.map((r, i) => (
        <div key={i} className={styles.recentRow}>
          <span className={styles.dot} style={{ background: r.result === 'pass' ? C.green : r.result === 'fail' ? C.red : r.result === 'error' ? C.amber : C.skip }} />
          <span className={styles.recentTitle} title={r.title}>{truncateNames ? truncate(r.title) : r.title}</span>
          <span className={styles.recentApp}>{r.app}</span>
          <span className={styles.recentTime}>{shortDate(r.executed_at)}</span>
        </div>
      ))}
    </div>
  )
}

/**
 * Generic centered overlay modal, used by the "More" action on Failing
 * Tests / Recent Runs. Click on the backdrop or the close button, or
 * press Escape, to dismiss.
 */
function Modal({ title, onClose, children }) {
  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className={styles.modalOverlay} onClick={onClose}>
      <div className={styles.modalPanel} onClick={e => e.stopPropagation()}>
        <div className={styles.modalHeader}>
          <span className={styles.modalTitle}>{title}</span>
          <button type="button" className={styles.modalClose} onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className={styles.modalBody}>{children}</div>
      </div>
    </div>
  )
}

function TypeChart({ byType }) {
  const entries = Object.entries(byType || {})
  if (entries.length === 0) return <Empty msg="No test types yet." />
  const colors = { functional: C.accent, regression: C.shield, smoke: C.amber }
  const data = entries.map(([type, count]) => ({ type: type.charAt(0).toUpperCase() + type.slice(1), count, fill: colors[type] || C.skip }))
  return (
    <div className={styles.chartBox}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 20, left: 20, bottom: 4 }}>
          <XAxis type="number" tick={{ fontSize: 11, fill: 'var(--text-muted)' }} tickLine={false} axisLine={false} allowDecimals={false} />
          <YAxis type="category" dataKey="type" tick={{ fontSize: 12, fill: 'var(--text-secondary)' }} tickLine={false} axisLine={false} width={80} />
          <Tooltip content={<ChartTip />} cursor={{ fill: 'var(--bg-card-alt)' }} />
          <Bar dataKey="count" radius={[0, 4, 4, 0]} barSize={18}>
            {data.map((e, i) => <Cell key={i} fill={e.fill} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function ChartTip({ active, payload, label, suffix }) {
  if (!active || !payload || !payload.length) return null
  return (
    <div className={styles.tip}>
      {label && <div className={styles.tipLabel}>{label}</div>}
      {payload.map((p, i) => (
        <div key={i} className={styles.tipRow}>
          <span className={styles.dot} style={{ background: p.color || (p.payload && p.payload.fill) }} />
          {p.name}: <b>{p.value}{suffix || ''}</b>
        </div>
      ))}
    </div>
  )
}

function LoadingGrid() {
  return (<>
    <div className={styles.kpiGrid}>{[1, 2, 3, 4].map(i => <div key={i} className="skeleton" style={{ height: 90 }} />)}</div>
    <div className="skeleton" style={{ height: 220, marginBottom: 15 }} />
  </>)
}
function ErrBox({ msg }) { return <div className={styles.errBox}>Could not load dashboard: {msg}. Is the backend running and seeded?</div> }
function Empty({ msg }) { return <div className={styles.emptyChart}>{msg}</div> }

function prioBadge(p) { const x = (p || '').toUpperCase(); if (x === 'P0' || x === 'P1') return 'red'; if (x === 'P2') return 'amber'; return 'gray' }
function catBadge(c) { c = (c || '').toLowerCase(); if (c === 'positive') return 'green'; if (c === 'negative') return 'red'; if (c === 'edge' || c === 'boundary') return 'amber'; if (c === 'security') return 'purple'; return 'gray' }
function covColor(c) { return c >= 70 ? '#34d399' : c >= 40 ? '#fbbf24' : '#fb7185' }
function shortDate(s) { if (!s) return '—'; const d = new Date(s); return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }

/**
 * Caps a test/scenario title at `max` characters, appending "..." when
 * cut short. Used on the main dashboard's Failing Tests / Recent Runs
 * cards so a long title never wraps to multiple lines or forces the
 * layout wide — the full title is still available via the element's
 * `title` attribute (native browser tooltip) and in the "More" modal.
 */
function truncate(text, max = TITLE_TRUNCATE_LEN) {
  if (!text) return ''
  return text.length > max ? `${text.slice(0, max).trimEnd()}...` : text
}

/**
 * Formats a date as dd-MMM (e.g. "25-Sep") for the Execution Trend x-axis.
 * Falls back to returning the original value untouched if it isn't a
 * parseable date, so this stays safe even against an older backend
 * payload that hasn't yet switched to real per-bucket dates.
 */
function formatDDMMM(value) {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const day = String(d.getDate()).padStart(2, '0')
  const month = d.toLocaleDateString('en-GB', { month: 'short' })
  return `${day}-${month}`
}