'use client'

/**
 * app/schedules/page.js
 *
 * Scheduled runs — test suites and Jira change-detection.
 *
 * A scheduler is invisible by nature — you cannot watch a clock tick over in
 * a meeting. So this page makes the waiting visible: every schedule shows a
 * live countdown to its next run, the list refreshes itself, and a run that
 * fires appears without anyone touching the page.
 *
 * A schedule is one of two types:
 *   - Suite run: runs a test suite (smoke/regression/custom).
 *   - Jira sync: checks a Jira project for changed / new requirements and
 *     flags the affected test cases. It DETECTS only — generating tests from a
 *     flagged requirement stays a separate, human action.
 *
 * "Run now" uses the same code path as the scheduler, so demonstrating it by
 * hand is a genuine rehearsal rather than a lookalike.
 */

import { useState, useEffect, useCallback, useMemo } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import {
  Calendar, Clock, Play, Pause, Trash2, Plus, CheckCircle2, XCircle,
  MinusCircle, Mail, RefreshCw, ChevronRight,
} from 'lucide-react'
import {
  getSchedules, createSchedule, updateSchedule, deleteSchedule,
  runScheduleNow, getApplications, getApplication, getSuites, getSuite,
  getJiraPending, markTestCaseReviewed, archiveScenario,
} from '../../lib/api'
import styles from './Schedules.module.css'

const FREQUENCIES = [
  { value: 'once',     label: 'Once' },
  { value: 'hourly',   label: 'Hourly' },
  { value: 'daily',    label: 'Daily' },
  { value: 'weekdays', label: 'Weekdays' },
  { value: 'weekly',   label: 'Weekly' },
]

// A Jira run returns a change-detection summary rather than a suite result.
// This is the reliable way to tell them apart in the "Run now" response and in
// the stored last_summary, without depending on job_type being present.
function isJiraResult(x) {
  return !!x && (x.project_key !== undefined || 'changed_requirements' in x)
}

export default function SchedulesPage() {
  const [schedules, setSchedules] = useState(null)
  const [creating, setCreating] = useState(false)
  const [runningId, setRunningId] = useState(null)
  const [toast, setToast] = useState(null)
  const [tick, setTick] = useState(0)
  const [filter, setFilter] = useState('all')

  // Filter chips are built from what is actually present, so no empty chip is
  // ever shown: always "All", one per suite kind in use, and "Jira sync" if
  // any Jira schedule exists.
  const filters = useMemo(() => {
    const out = [{ key: 'all', label: 'All' }]
    if (!schedules) return out
    const kinds = new Set()
    let hasJira = false
    for (const s of schedules) {
      if (s.job_type === 'jira_sync') hasJira = true
      else if (s.suite_kind) kinds.add(s.suite_kind)
    }
    for (const k of ['smoke', 'regression', 'custom']) {
      if (kinds.has(k)) { out.push({ key: `kind:${k}`, label: cap(k) }); kinds.delete(k) }
    }
    for (const k of kinds) out.push({ key: `kind:${k}`, label: cap(k) })  // any others
    if (hasJira) out.push({ key: 'jira', label: 'Jira sync' })
    return out
  }, [schedules])

  const visible = (schedules || []).filter(s => {
    if (filter === 'all') return true
    if (filter === 'jira') return s.job_type === 'jira_sync'
    if (filter.startsWith('kind:')) return s.job_type !== 'jira_sync' && s.suite_kind === filter.slice(5)
    return true
  })

  const load = useCallback(async () => {
    try {
      const d = await getSchedules()
      const list = d.schedules || []
      setSchedules(list)
      return list
    } catch { setSchedules([]); return [] }
  }, [])

  // Jira review backlog per application. Fetched only on load and after
  // actions — never on the 1s countdown or the 15s list timer — so the
  // on-screen queue reflects the current standing state without polling Jira
  // on every tick.
  const [pendingByApp, setPendingByApp] = useState({})
  const loadPending = useCallback(async (list) => {
    const jiraApps = [...new Set(
      (list || []).filter(s => s.job_type === 'jira_sync').map(s => s.application_id)
    )]
    if (!jiraApps.length) return
    const entries = await Promise.all(jiraApps.map(async id => {
      try { return [id, await getJiraPending(id)] } catch { return [id, null] }
    }))
    setPendingByApp(prev => ({ ...prev, ...Object.fromEntries(entries) }))
  }, [])

  const refresh = useCallback(async () => {
    const list = await load()
    await loadPending(list)
  }, [load, loadPending])

  useEffect(() => { refresh() }, [refresh])

  // Two timers with different jobs: one drives the countdown text every
  // second, the other re-reads the list so a run that fires on the server
  // appears here on its own.
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [])
  useEffect(() => {
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load])

  async function runNow(s) {
    setRunningId(s.id)
    try {
      const r = await runScheduleNow(s.id)
      if (r.error) {
        setToast({ message: r.error, tone: 'error' })
      } else if (isJiraResult(r)) {
        // Jira sync: name what changed / is new, not pass/fail.
        const changed = r.changed_requirements || []
        const flagged = r.flagged_cases || []
        const created = r.new_requirements || []
        let msg
        if (!changed.length && !flagged.length && !created.length) {
          msg = `${r.project_key}: no changes since last check`
        } else {
          const parts = []
          if (changed.length) parts.push(`changed: ${changed.join(', ')}`)
          if (flagged.length) parts.push(`${flagged.length} test case${flagged.length > 1 ? 's' : ''} flagged`)
          if (created.length) parts.push(`new: ${created.join(', ')}`)
          msg = `${r.project_key} — ${parts.join(' · ')}`
        }
        setToast({ message: msg + webhookNote(r.webhook_status) })
      } else {
        // Suite run: the original pass/fail summary.
        setToast({
          message: `${r.suite}: ${r.passed} passed, ${r.failed} failed` +
                   (r.skipped ? `, ${r.skipped} skipped` : '') +
                   webhookNote(r.webhook_status),
          tone: r.failed ? 'error' : undefined,
        })
      }
      refresh()
    } catch (e) {
      setToast({ message: e.message, tone: 'error' })
    } finally { setRunningId(null) }
  }

  async function toggleEnabled(s) {
    await updateSchedule(s.id, { enabled: !s.enabled })
    load()
  }

  return (
    <div>
      <div className={styles.pageHead}>
        <div>
          <h2 className={styles.pageTitle}>Schedules</h2>
          <p className={styles.pageSub}>
            Suites that run on their own, and Jira checks that flag changed
            requirements. Nobody has to remember to press anything.
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          <Plus size={15} strokeWidth={2} style={{ verticalAlign: -3, marginRight: 6 }} />
          New schedule
        </button>
      </div>

      {schedules === null && <div className="skeleton" style={{ height: 120 }} />}

      {schedules && schedules.length > 0 && filters.length > 1 && (
        <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
          {filters.map(f => (
            <button key={f.key} className={styles.actBtn}
                    onClick={() => setFilter(f.key)}
                    style={filter === f.key
                      ? { borderColor: 'var(--accent)', color: 'var(--accent)' }
                      : undefined}>
              {f.label}
            </button>
          ))}
        </div>
      )}

      {schedules && schedules.length === 0 && (
        <div className="card" style={{ padding: 40, textAlign: 'center' }}>
          <Calendar size={30} strokeWidth={1.5} className={styles.emptyIcon} />
          <p className={styles.emptyTitle}>No schedules yet</p>
          <p className={styles.emptySub}>
            Create one to run a suite automatically, or a Jira sync to watch a
            project for requirement changes.
          </p>
        </div>
      )}

      {schedules && visible.map(s => (
        <ScheduleCard
          key={s.id}
          schedule={s}
          tick={tick}
          pending={pendingByApp[s.application_id]}
          running={runningId === s.id}
          onRun={() => runNow(s)}
          onToggle={() => toggleEnabled(s)}
          onReviewed={refresh}
          onDelete={async () => { await deleteSchedule(s.id); load() }}
        />
      ))}

      {creating && (
        <ScheduleForm
          onClose={() => setCreating(false)}
          onSaved={() => { setCreating(false); refresh() }}
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

/** One schedule, with its countdown, recent results and controls. */
function ScheduleCard({ schedule: s, tick, pending, running, onRun, onToggle, onReviewed, onDelete }) {
  const countdown = useCountdown(s.next_run_at, tick)
  const last = s.last_summary || {}
  const isJira = s.job_type === 'jira_sync'
  const [busy, setBusy] = useState(null)
  const [opening, setOpening] = useState(false)
  const router = useRouter()

  // Regenerate / Generate reuse the existing New Test Suite wizard (survey →
  // ScenarioReview → accept → generate → report), deep-linked and scoped to one
  // Jira requirement. No modal, no duplicated review screen — just the wizard
  // you already have. The requirement's flag is cleared server-side on accept.
  async function openWizard(issueKey) {
    setOpening(true)
    try {
      const app = await getApplication(s.application_id)
      const base_url = app?.application?.base_url || app?.base_url || ''
      const params = new URLSearchParams({
        source: 'jira',
        project: s.jira_project_key || '',
        issue: issueKey,
        app: s.application_name || '',
        url: base_url,
      })
      router.push(`/new-test-suite?${params.toString()}`)
    } catch {
      setOpening(false)
    }
  }

  // The requirement changed but this test is still valid: clear the flag,
  // regenerate nothing. This is the explicit "reviewed, no change" decision.
  async function noChange(id) {
    setBusy(id)
    try { await markTestCaseReviewed(id); onReviewed && onReviewed() }
    catch { /* parent refresh reconciles */ }
    finally { setBusy(null) }
  }

  // This test no longer applies: archive it (row + history kept for audit).
  async function reject(id) {
    setBusy(id)
    try { await archiveScenario(id); onReviewed && onReviewed() }
    catch { /* parent refresh reconciles */ }
    finally { setBusy(null) }
  }

  // Which test cases this schedule will actually run. Suite-only: a Jira
  // schedule has no suite to expand.
  const [members, setMembers] = useState(null)
  const [showMembers, setShowMembers] = useState(false)

  async function toggleMembers() {
    const next = !showMembers
    setShowMembers(next)
    if (next && members === null && s.suite_id) {
      try {
        const d = await getSuite(s.suite_id)
        setMembers(d.suite?.members || [])
      } catch { setMembers([]) }
    }
  }

  return (
    <div className={`card ${styles.schedCard}`}>
      <div className={styles.schedTop}>
        <span className={`${styles.dot} ${s.enabled ? styles.dotOn : ''}`} />
        <div className={styles.schedMain}>
          <div className={styles.schedName}>{s.name}</div>
          <div className={styles.schedMeta}>
            {/* Type comes first so the list is unambiguous at a glance. */}
            {isJira ? 'Jira sync' : 'Suite'} · {s.application_name}
            {isJira && s.jira_project_key && <> · project {s.jira_project_key}</>}
            {!isJira && s.suite_name && (
              <> · <button className={styles.suiteChip} data-kind={s.suite_kind}
                           onClick={toggleMembers}
                           title="Show the test cases this suite runs">
                {s.suite_name}
                <ChevronRight size={10} strokeWidth={2.5}
                  style={{ verticalAlign: -1, marginLeft: 3,
                           transform: showMembers ? 'rotate(90deg)' : 'none',
                           transition: 'transform .15s' }} />
              </button></>
            )}
            {!isJira && s.environment && <> · {s.environment}</>}
            {!isJira && typeof s.member_count === 'number' && <> · {s.member_count} test cases</>}
          </div>
        </div>

        <div className={styles.schedWhen}>
          <div className={styles.freq}>
            <Clock size={12} strokeWidth={2} style={{ verticalAlign: -1, marginRight: 5 }} />
            {FREQUENCIES.find(f => f.value === s.frequency)?.label || s.frequency}
            {s.next_run_at && ` · ${formatTime(s.next_run_at)}`}
          </div>
          {s.enabled && countdown && (
            <div className={`${styles.countdown} ${countdown.soon ? styles.countdownSoon : ''}`}>
              {countdown.text}
            </div>
          )}
          {!s.enabled && <div className={styles.paused}>paused</div>}
        </div>

        <div className={styles.schedActions}>
          <button className={styles.actBtn} onClick={onRun} disabled={running}
                  title={isJira
                    ? 'Check Jira for changes now — the same path the scheduler uses'
                    : 'Run this schedule now — the same path the scheduler uses'}>
            {running
              ? <RefreshCw size={14} strokeWidth={2} className={styles.spin} />
              : <Play size={14} strokeWidth={2} />}
            {running ? (isJira ? 'Checking…' : 'Running…') : (isJira ? 'Check now' : 'Run now')}
          </button>
          <button className={styles.actBtn} onClick={onToggle}
                  title={s.enabled ? 'Pause' : 'Resume'}>
            {s.enabled ? <Pause size={14} strokeWidth={2} /> : <Play size={14} strokeWidth={2} />}
          </button>
          <button className={`${styles.actBtn} ${styles.actDanger}`} onClick={onDelete} title="Delete">
            <Trash2 size={14} strokeWidth={2} />
          </button>
        </div>
      </div>

      {showMembers && (
        <div className={styles.memberBox}>
          {members === null && <div className="skeleton" style={{ height: 40 }} />}
          {members && members.length === 0 && (
            <div className={styles.memberEmpty}>This suite has no test cases.</div>
          )}
          {members && members.map(m => (
            <div key={m.id} className={styles.memberRow}>
              {m.priority && <span className={styles.memberPrio}>{m.priority}</span>}
              <span className={styles.memberName}>{m.title}</span>
              {m.automated
                ? <span className={styles.memberAuto}>automated</span>
                : <span className={styles.memberManual}>no script · will be skipped</span>}
            </div>
          ))}
        </div>
      )}

      {s.last_run_at && (
        <div className={styles.schedLast}>
          <span className={styles.lastLabel}>{isJira ? 'review queue' : 'last run'}</span>
          <span className={styles.lastTime}>{formatWhen(s.last_run_at)}</span>

          {isJira ? (
            <span className={styles.counts}>
              {pending
                ? (pending.changed_count > 0 || pending.new_count > 0
                    ? `${pending.changed_count} awaiting review · ${pending.new_count} new`
                    : 'nothing pending')
                : <JiraLastSummary last={last} />}
            </span>
          ) : (
            <span className={styles.counts}>
              <CheckCircle2 size={13} strokeWidth={2} className={styles.pass} />
              {last.passed ?? 0}
              <XCircle size={13} strokeWidth={2} className={styles.fail} />
              {last.failed ?? 0}
              {(last.skipped ?? 0) > 0 && (
                <>
                  <MinusCircle size={13} strokeWidth={2} className={styles.skip} />
                  {last.skipped}
                </>
              )}
            </span>
          )}

          {s.last_summary?.webhook_status && (
            <span className={styles.webhook}>
              <Mail size={12} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
              {s.last_summary.webhook_status}
            </span>
          )}

          {!isJira && (
            <Link href="/executions" className={styles.detailLink}>
              see details →
            </Link>
          )}

          {!isJira && s.recent_runs?.length > 0 && (
            <span className={styles.spark}>
              {s.recent_runs.slice().reverse().map((r, i) => (
                <span key={i}
                      className={`${styles.sparkBar} ${r.failed ? styles.sparkFail : styles.sparkPass}`}
                      title={`${r.passed} passed, ${r.failed} failed`} />
              ))}
            </span>
          )}
        </div>
      )}

      {/* Jira: the standing review queue for this app — the CURRENT backlog,
          not one check's delta. Each item persists until a human clears it, so
          a changed requirement no longer vanishes on the next check. */}
      {isJira && pending && (pending.changed?.length > 0 || pending.new?.length > 0) && (
        <div className={styles.memberBox}>
          {pending.changed?.length > 0 && (
            <div className={styles.memberRow}
                 style={{ background: 'var(--bg-card-alt)', fontWeight: 600, fontSize: 11,
                          textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--amber)' }}>
              Changed — needs review
            </div>
          )}
          {pending.changed.map(g => (
            <div key={`chg-${g.requirement_id}`}>
              <div className={styles.memberRow}>
                <span className={styles.memberPrio} style={{ color: 'var(--amber)' }}>changed</span>
                <span className={styles.memberName} style={{ fontWeight: 500, color: 'var(--text-primary)' }}>
                  {g.requirement_id}{g.summary ? ` — ${g.summary}` : ''}
                </span>
                <button className={styles.actBtn} disabled={opening}
                        onClick={() => openWizard(g.requirement_id)}
                        title="Re-derive this requirement's test(s) in the Test Suite wizard, review the steps, then generate. Clears the flag on accept.">
                  {opening ? 'Opening…' : 'Regenerate'}
                </button>
              </div>
              {g.cases.map(c => (
                <div key={`case-${c.id}`} className={styles.memberRow} style={{ paddingLeft: 40 }}>
                  <span className={styles.memberPrio}>↳ test</span>
                  <span className={styles.memberName}>{c.title}</span>
                  <span style={{ display: 'inline-flex', gap: 6, flexShrink: 0 }}>
                    <button className={styles.actBtn} disabled={busy === c.id}
                            onClick={() => noChange(c.id)}
                            title="The requirement changed but this test is still valid — clear the flag. Nothing is regenerated.">
                      {busy === c.id ? '…' : 'No change needed'}
                    </button>
                    <button className={`${styles.actBtn} ${styles.actDanger}`} disabled={busy === c.id}
                            onClick={() => reject(c.id)}
                            title="This test no longer applies — archive it. The row and its run history are kept for audit.">
                      Reject
                    </button>
                  </span>
                </div>
              ))}
            </div>
          ))}

          {pending.new?.length > 0 && (
            <div className={styles.memberRow}
                 style={{ background: 'var(--bg-card-alt)', fontWeight: 600, fontSize: 11,
                          textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--accent)',
                          borderTop: pending.changed?.length ? '1px solid var(--border)' : 'none' }}>
              New — no test coverage
            </div>
          )}
          {pending.new.map(n => (
            <div key={`new-${n.requirement_id}`} className={styles.memberRow}>
              <span className={styles.memberPrio} style={{ color: 'var(--accent)' }}>new</span>
              <span className={styles.memberName} style={{ fontWeight: 500, color: 'var(--text-primary)' }}>
                {n.requirement_id}{n.summary ? ` — ${n.summary}` : ''}
              </span>
              <button className={styles.actBtn} disabled={opening}
                      onClick={() => openWizard(n.requirement_id)}
                      title="Survey this requirement in the Test Suite wizard, review the proposed test, then generate it.">
                {opening ? 'Opening…' : 'Generate tests'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** Change-detection counts for a Jira schedule's last run (changed / flagged /
 *  new), reusing the same understated row styling as suite counts. */
function JiraLastSummary({ last }) {
  const changed = (last.changed_requirements || []).length
  const flagged = (last.flagged_cases || []).length
  const created = (last.new_requirements || []).length
  if (!changed && !flagged && !created) {
    return <span className={styles.counts}>no changes</span>
  }
  return (
    <span className={styles.counts}>
      {changed} changed · {flagged} flagged · {created} new
    </span>
  )
}

/** Create a schedule — a suite run or a Jira sync. Defaults to five minutes
 *  from now, the common case being "set it just ahead and watch it fire". */
function ScheduleForm({ onClose, onSaved }) {
  const [apps, setApps] = useState([])
  const [suites, setSuites] = useState([])
  const [form, setForm] = useState({
    job_type: 'suite_run',
    application_id: '', suite_id: '', jira_project_key: '', name: '',
    frequency: 'once', run_at: defaultRunAt(),
    environment: '', webhook_url: '', recipients: '',
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const isJira = form.job_type === 'jira_sync'

  useEffect(() => { getApplications().then(setApps).catch(() => setApps([])) }, [])

  // Suites are only relevant to a suite-run schedule.
  useEffect(() => {
    if (isJira || !form.application_id) { setSuites([]); return }
    getSuites(form.application_id)
      .then(d => setSuites(d.suites || []))
      .catch(() => setSuites([]))
  }, [form.application_id, isJira])

  function set(k, v) { setForm(f => ({ ...f, [k]: v })) }

  function onPickApp(v) {
    set('application_id', v)
    // Helpful default name for a Jira schedule so the list reads clearly.
    if (isJira && !form.name) {
      const a = apps.find(x => String(x.id) === v)
      if (a) set('name', `Jira Sync – ${a.name}`)
    }
  }

  async function save() {
    // Type-specific required fields.
    if (isJira) {
      if (!form.application_id || !form.jira_project_key.trim() || !form.name.trim()) {
        setError('Application, Jira project and a name are required.'); return
      }
    } else {
      if (!form.application_id || !form.suite_id || !form.name.trim()) {
        setError('Application, suite and a name are required.'); return
      }
    }

    setSaving(true); setError(null)
    try {
      const payload = {
        job_type: form.job_type,
        application_id: Number(form.application_id),
        name: form.name.trim(),
        frequency: form.frequency,
        run_at: new Date(form.run_at).toISOString(),
        webhook_url: form.webhook_url,
        recipients: form.recipients,
      }
      if (isJira) {
        payload.jira_project_key = form.jira_project_key.trim()
        payload.suite_id = null
        payload.enabled = false          // Jira schedules start paused
      } else {
        payload.suite_id = Number(form.suite_id)
        payload.environment = form.environment
        payload.enabled = true           // unchanged suite behaviour
      }

      const res = await createSchedule(payload)
      if (res.error) setError(res.error)
      else onSaved()
    } catch (e) { setError(e.message) } finally { setSaving(false) }
  }

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()}>
        <div className={styles.modalHead}>
          <span className={styles.modalTitle}>New schedule</span>
          <button className={styles.ghost} onClick={onClose}>Close</button>
        </div>

        <div className={styles.modalBody}>
          <Field label="Schedule type">
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="button"
                      className={form.job_type === 'suite_run' ? 'btn btn-primary' : styles.ghost}
                      onClick={() => set('job_type', 'suite_run')}>
                Suite run
              </button>
              <button type="button"
                      className={form.job_type === 'jira_sync' ? 'btn btn-primary' : styles.ghost}
                      onClick={() => set('job_type', 'jira_sync')}>
                Jira sync
              </button>
            </div>
          </Field>

          <Field label="Application">
            <select className={styles.input} value={form.application_id}
                    onChange={e => onPickApp(e.target.value)}>
              <option value="">Select…</option>
              {apps.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </Field>

          {!isJira && (
            <Field label="Suite">
              <select className={styles.input} value={form.suite_id}
                      onChange={e => {
                        set('suite_id', e.target.value)
                        const s = suites.find(x => String(x.id) === e.target.value)
                        if (s && !form.name) set('name', `${s.name} run`)
                      }}
                      disabled={!form.application_id}>
                <option value="">{form.application_id ? 'Select…' : 'Choose an application first'}</option>
                {suites.map(s => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.member_count} test cases)
                  </option>
                ))}
              </select>
              {form.application_id && suites.length === 0 && (
                <span className={styles.hint}>
                  This application has no suites yet — create one on its page first.
                </span>
              )}
            </Field>
          )}

          {isJira && (
            <Field label="Jira project"
                   hint="The project key whose stories this schedule checks, e.g. KAN.">
              <input className={styles.input} value={form.jira_project_key}
                     onChange={e => set('jira_project_key', e.target.value)}
                     placeholder="e.g. KAN" />
            </Field>
          )}

          <Field label="Name">
            <input className={styles.input} value={form.name}
                   onChange={e => set('name', e.target.value)}
                   placeholder={isJira ? 'e.g. Jira Sync – Payroll' : 'e.g. Netra morning smoke'} />
          </Field>

          <div className={styles.row}>
            <Field label="Frequency">
              <select className={styles.input} value={form.frequency}
                      onChange={e => set('frequency', e.target.value)}>
                {FREQUENCIES.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
              </select>
            </Field>
            <Field label="First run">
              <input className={styles.input} type="datetime-local" value={form.run_at}
                     onChange={e => set('run_at', e.target.value)} />
            </Field>
          </div>

          {isJira && (
            <div className={styles.hint} style={{ marginTop: -6, marginBottom: 14 }}>
              Jira schedules start paused. Use “Check now” to detect changes on
              demand; enable it to run on the cadence above.
            </div>
          )}

          {!isJira && (
            <Field label="Environment"
                   hint="Leave blank to use the application's default environment.">
              <input className={styles.input} value={form.environment}
                     onChange={e => set('environment', e.target.value)}
                     placeholder="dev / test / stage" />
            </Field>
          )}

          <Field label="Webhook URL"
               hint="Posted to when the run finishes. Point this at your n8n webhook and n8n sends the email — NextGen2 never holds mail credentials.">
            <input className={styles.input} value={form.webhook_url}
                   onChange={e => set('webhook_url', e.target.value)}
                 placeholder="https://n8n.example.com/webhook/nextgen2" />
          </Field>

          <Field label="Recipients" hint="Recorded for reference; n8n decides delivery.">
            <input className={styles.input} value={form.recipients}
                   onChange={e => set('recipients', e.target.value)}
                   placeholder="qa-team@baxter.com" />
          </Field>

          {error && <div className={styles.error}>{error}</div>}
        </div>

        <div className={styles.modalFoot}>
          <button className={styles.ghost} onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? 'Creating…' : 'Create schedule'}
          </button>
        </div>
      </div>
    </div>
  )
}

function Field({ label, hint, children }) {
  return (
    <div className={styles.field}>
      <label className={styles.label}>{label}</label>
      {children}
      {hint && <span className={styles.hint}>{hint}</span>}
    </div>
  )
}

/**
 * Time remaining until the next run.
 *
 * Recomputed every second from the `tick` prop so the page has a single
 * timer rather than one per row.
 */
function useCountdown(nextRunAt, tick) {  // eslint-disable-line
  if (!nextRunAt) return null
  const ms = new Date(nextRunAt).getTime() - Date.now()
  if (ms <= 0) return { text: 'due now', soon: true }
  const s = Math.floor(ms / 1000)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const text =
    d > 0 ? `in ${d}d ${h}h`
    : h > 0 ? `in ${h}h ${m}m`
    : m > 0 ? `in ${m}m ${String(sec).padStart(2, '0')}s`
    : `in ${sec}s`
  return { text, soon: ms < 10 * 60 * 1000 }
}

function cap(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s
}

// Only mention the webhook when one actually fired — avoids the confusing
// "webhook no webhook configured" line when none is set.
function webhookNote(status) {
  if (!status || status === 'no webhook configured') return ''
  return ` · notified (${status})`
}

function defaultRunAt() {
  const d = new Date(Date.now() + 5 * 60 * 1000)   // five minutes ahead
  d.setSeconds(0, 0)
  const pad = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function formatTime(iso) {
  const d = new Date(iso)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatWhen(iso) {
  const d = new Date(iso)
  const today = new Date().toDateString() === d.toDateString()
  return today
    ? `today ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
    : d.toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}