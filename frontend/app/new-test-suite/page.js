'use client'

/**
 * app/new-test-suite/page.js
 *
 * Complete new test suite flow — 6 steps:
 *
 *  1. configure  — test type selection + URL + document inputs
 *  2. survey     — Author Agent running, live SSE progress
 *  3. review     — human reviews and prioritises proposed scenarios
 *  4. generate   — Explorer Agent generating scripts, live SSE progress
 *  5. scripts    — view generated scripts, edit (CodeMirror), download, execute individually
 *  6. report     — final execution report
 *
 * Generation and execution are fully separated — step 5 is always shown
 * before execution begins, so the user can inspect, edit, or download
 * scripts before running them.
 */

import { useState, useCallback, useRef, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import dynamic from 'next/dynamic'
import styles from './NewTestSuite.module.css'
import ProgressStream from '../../components/ProgressStream/ProgressStream'
import ScenarioReview from '../../components/ScenarioReview/ScenarioReview'
import { API_BASE, createApplication, currentUserEmail } from '../../lib/api'
import useWorkflowCancellation from '../../hooks/useWorkflowCancellation.mjs'

// CodeMirror loaded client-side only (no SSR)
const CodeMirror = dynamic(() => import('@uiw/react-codemirror'), { ssr: false })

const STEPS = ['configure', 'survey', 'review', 'generate', 'scripts', 'report']

// Bump when this file changes. Shown in the UI so a stale cached bundle is
// obvious instead of looking like a backend bug.
const UI_BUILD = 'ui-2026-07-18-approval'

// Test type definitions with labels and descriptions
const TEST_TYPES = [
  {
    key: 'functional',
    label: 'Functional',
    desc: 'Verify features work correctly with valid and invalid inputs',
  },
  {
    key: 'regression',
    label: 'Regression',
    desc: 'Re-run existing tests after a change to catch breakage',
  },
  {
    key: 'smoke',
    label: 'Smoke',
    desc: 'Quick check of the most critical paths before a full run',
  },
]

const STEP_LABELS = [
  { key: 'configure', label: 'Configure' },
  { key: 'survey',    label: 'Surveying' },
  { key: 'review',    label: 'Review' },
  { key: 'generate',  label: 'Generating' },
  { key: 'scripts',   label: 'Scripts' },
  { key: 'report',    label: 'Report' },
]


/**
 * A spreadsheet is a set of test cases — the file says so.
 *
 * Routing on the radio button alone meant that uploading an .xlsx with the
 * wrong option selected silently crawled the page instead of reading the
 * file, and produced scenarios that looked plausible but came from nowhere
 * near the spreadsheet.
 */
function isTestCaseFile(file, documentType) {
  if (documentType === 'existing_test_cases') return true
  const n = (file?.name || '').toLowerCase()
  return n.endsWith('.xlsx') || n.endsWith('.xlsm') || n.endsWith('.csv')
}

function classifySsoPrecheck(message) {
  const text = String(message || '').toLowerCase()
  if (!text.includes('sso precheck')) return null
  if (text.includes('running')) return 'running'
  if (text.includes('passed')) return 'passed'
  if (text.includes('failed')) return 'failed'
  return null
}

export default function NewTestSuitePage() {
  const router = useRouter()
  const {
    request: workflowFetch, cancel: cancelWorkflow, retry: retryCancellation,
    state: cancellationState, error: cancellationError, isCancelled,
    releaseResponse, trackReader,
  } = useWorkflowCancellation(API_BASE)
  const [cancelConfirmation, setCancelConfirmation] = useState(false)
  const cancellationDialog = useRef(null)
  const cancelling = cancellationState !== 'active'
  const readWorkflowSSE = useCallback((response, handlers) =>
    consumeSSE(response, handlers, { isCancelled, releaseResponse, trackReader }),
  [isCancelled, releaseResponse, trackReader])

  useEffect(() => {
    const dialog = cancellationDialog.current
    if (!dialog) return
    if ((cancelConfirmation || cancelling) && !dialog.open) dialog.showModal()
    if (!cancelConfirmation && !cancelling && dialog.open) dialog.close()
    // The confirmation buttons disappear during progress; retain focus in the modal.
    if (cancelling) dialog.focus()
  }, [cancelConfirmation, cancelling])

  // ── Form state ──────────────────────────────────────────────────────
  const [selectedTypes, setSelectedTypes]   = useState(['functional'])
  const [applicationName, setApplicationName] = useState('')
  const [baseUrl, setBaseUrl]               = useState('')
  const [requiresSsoGateway, setRequiresSsoGateway] = useState(false)
  const [includeSsoScenarios, setIncludeSsoScenarios] = useState(true)
  const [documentFile, setDocumentFile]     = useState(null)
  const [documentType, setDocumentType]     = useState('requirements')
  const [targetFlow, setTargetFlow]         = useState('')
  // Where requirements come from: an uploaded BRD/test-case file, Jira, or ALM.
  const [source, setSource]                 = useState('brd')   // 'brd' | 'jira' | 'alm'
  const [jiraProjectKey, setJiraProjectKey] = useState('KAN')
  const [jiraIssueKey, setJiraIssueKey]     = useState('')      // scope survey to one Jira issue (deep-link)
  const [almUsername, setAlmUsername]       = useState('')
  const [almPassword, setAlmPassword]       = useState('')
  const [almDomain, setAlmDomain]           = useState('GLOBAL_IT')
  const [almProject, setAlmProject]         = useState('EnterpriseOne')
  const [almBaseUrl, setAlmBaseUrl]         = useState('https://baxter.alm.saas.microfocus.com/qcbin')
  const [almPageSize, setAlmPageSize]       = useState('50')
  const [autoSurvey, setAutoSurvey]         = useState(false)   // auto-run once deep-link params are applied
  const autoStartedRef = useRef(false)
  const [formErrors, setFormErrors]         = useState({})
  const [lockedAppContext, setLockedAppContext] = useState(null) // {id, name, base_url}

  // ── Flow state ──────────────────────────────────────────────────────
  const [step, setStep]                   = useState('configure')
  const [progressMessages, setProgressMessages] = useState([])
  const [ssoPrecheckState, setSsoPrecheckState] = useState({ survey: null, generate: null })
  const [surveyNotice, setSurveyNotice] = useState(null)
  const [pickedAppId, setPickedAppId] = useState('')
  const [appList, setAppList] = useState([])
  const [importMapping, setImportMapping] = useState(null)
  const [importInfo, setImportInfo] = useState(null)
  const [existingApp, setExistingApp] = useState(null)

  // The application list is needed when importing, where you pick rather
  // than type a URL.
  useEffect(() => {
    fetch(`${API_BASE}/v1/applications`).then(r => r.json())
      .then(d => setAppList(Array.isArray(d) ? d : (d.applications || [])))
      .catch(() => setAppList([]))
  }, [])

  // If opened from an application page, reuse that saved application
  // identity so the wizard does not ask for name/URL again.
  useEffect(() => {
    if (isCancelled() || !Array.isArray(appList) || appList.length === 0) return
    const params = new URLSearchParams(window.location.search)
    const appIdParam = params.get('app_id')
    if (!appIdParam) return

    const match = appList.find(a => String(a.id) === String(appIdParam))
    if (!match) return

    setLockedAppContext({
      id: match.id,
      name: match.name || '',
      base_url: match.base_url || '',
    })
    setApplicationId(match.id)
    setPickedAppId(String(match.id))
    setApplicationName(match.name || '')
    setBaseUrl(match.base_url || '')
    setRequiresSsoGateway(!!match.requires_sso_gateway)
  }, [appList, isCancelled])
  const [pendingApproval, setPendingApproval] = useState(null)
  const [savedWithoutScripts, setSavedWithoutScripts] = useState(false)
  const [watchBrowser, setWatchBrowser] = useState(true)   // headed by default: seeing it run is the point
  const [applicationId, setApplicationId] = useState(null)
  useEffect(() => {
    if (cancellationState !== 'cancelled') return
    const knownId = applicationId || lockedAppContext?.id ||
      (pickedAppId && pickedAppId !== '__new__' ? pickedAppId : null) ||
      new URLSearchParams(window.location.search).get('app_id')
    router.push(knownId ? `/applications/${encodeURIComponent(knownId)}` : '/applications')
  }, [cancellationState, applicationId, lockedAppContext, pickedAppId, router])
  const [proposals, setProposals]         = useState([])
  const [approvedProposals, setApprovedProposals] = useState([])
  // TOKENS & COST: id of the survey run that produced the proposals. Sent when they are accepted,
  // so each test case takes its cost share from the right run. Null for file and ALM imports.
  const [surveyRunId, setSurveyRunId]     = useState(null)
  const [generatedResults, setGeneratedResults]   = useState([])
  const [executionResults, setExecutionResults]   = useState(null)
  const [automationWarnings, setAutomationWarnings] = useState([])
  const [error, setError]                 = useState(null)

  // ── Code editor modal state ─────────────────────────────────────────
  const [modalOpen, setModalOpen]         = useState(false)
  const [modalResult, setModalResult]     = useState(null)  // the result row being viewed/edited
  const [modalReadOnly, setModalReadOnly] = useState(true)
  const [editedContent, setEditedContent] = useState('')
  const [savingEdit, setSavingEdit]       = useState(false)

  const applySsoPrecheckMessage = useCallback((phase, message) => {
    const next = classifySsoPrecheck(message)
    if (!next) return
    setSsoPrecheckState(prev => ({ ...prev, [phase]: next }))
  }, [])

  // ── Toggle test type selection ──────────────────────────────────────
  const toggleType = useCallback((key) => {
    setSelectedTypes(prev =>
      prev.includes(key)
        ? prev.length > 1 ? prev.filter(k => k !== key) : prev  // keep at least one
        : [...prev, key]
    )
  }, [])

  // ── Step 1 → 2: validate and start survey ──────────────────────────
  /** Actually start the survey (called after confirmation, if one was needed). */
  /**
   * Import test cases from the uploaded spreadsheet instead of crawling.
   *
   * "Existing test cases" previously fed the file to the AI as background
   * text, which is not importing — the rows were never parsed. It now goes
   * through the importer and lands in the SAME Review step, so both sources
   * share one screen and one set of decisions.
   */
  const runImport = useCallback(async () => {
    if (isCancelled()) return
    setExistingApp(null)
    setError(null)
    setProgressMessages(['Reading the spreadsheet…'])
    setStep('survey')

    try {
      // The application is chosen from the list — no URL, no crawl.
      // Resolve the application. Picking one from the list already answers
      // the identity question. Creating one needs the URL, because that IS
      // the identity — normalized_url is unique, the name is not — and it is
      // where any scripts will run.
      let appId = pickedAppId || applicationId
      if (appId === '__new__') {
        setProgressMessages(m => [...m, 'Checking the application…'])
        const look = await workflowFetch(
          `${API_BASE}/v1/application-lookup?url=${encodeURIComponent(baseUrl.trim())}`
        ).then(r => r.json()).catch(() => ({}))
        if (isCancelled()) return

        if (look.exists && look.application) {
          appId = look.application.id
          setSurveyNotice(
            `Added to existing application '${look.application.name}' — apps are matched by URL.`
          )
        } else {
          const created = await createApplication({
            name: applicationName.trim(),
            base_url: baseUrl.trim(),
            requires_sso_gateway: requiresSsoGateway,
          }, workflowFetch)
          if (isCancelled()) return
          if (created?.error || !created?.id) {
            setError(created?.error || 'Could not create the application.')
            setStep('configure')
            return
          }
          appId = created.id
          setSurveyNotice(`Created the application '${applicationName.trim()}'.`)
        }
      }
      if (!appId) {
        setError('Choose the application these test cases belong to.')
        setStep('configure')
        return
      }
      setApplicationId(appId)

      const body = new FormData()
      body.append('file', documentFile)
      const res = await workflowFetch(`${API_BASE}/v1/applications/${appId}/import-preview`, {
        method: 'POST', body,
      })
      const data = await res.json()
      if (isCancelled()) return
      if (data.error) { setError(data.error); setStep('configure'); return }

      setImportMapping(data.mapping)
      setImportInfo({ filename: data.filename, rowCount: data.row_count, headers: data.headers })
      setSurveyRunId(null)   // TOKENS & COST: a file import has no survey run
      setProposals(data.proposals || [])
      setSurveyNotice(
        `Read ${data.row_count} rows from ${data.filename} — ` +
        `${(data.proposals || []).length} test cases found. ` +
        `Columns ${data.mapping.source}.`
      )
      setStep('review')
    } catch (err) {
      if (isCancelled()) return
      setError(err.message)
      setStep('configure')
    }
  }, [applicationId, pickedAppId, documentFile, applicationName, baseUrl, requiresSsoGateway, workflowFetch, isCancelled])

  const runSurvey = useCallback(async () => {
    if (isCancelled()) return
    setExistingApp(null)
    setError(null)
    setProgressMessages([])
    setSsoPrecheckState(prev => ({ ...prev, survey: null }))
    setStep('survey')

    const jira = source === 'jira'
    const body = new FormData()
    body.append('application_name', applicationName.trim())
    body.append('base_url', baseUrl.trim())
    body.append('requires_sso_gateway', requiresSsoGateway ? 'true' : 'false')
    body.append('include_sso_scenarios', includeSsoScenarios ? 'true' : 'false')
    if (targetFlow.trim()) body.append('target_flow', targetFlow.trim())
    if (jira) {
      // Requirements are read from Jira; the URL is still surveyed against.
      if (jiraProjectKey.trim()) body.append('jira_project_key', jiraProjectKey.trim())
      // Scope to a single requirement when deep-linked from Regenerate/Generate.
      if (jiraIssueKey.trim()) body.append('jira_issue_key', jiraIssueKey.trim())
    } else if (documentFile) {
      body.append('document', documentFile)
      body.append('document_type', documentType)
    }

    const surveyUrl = jira
      ? `${API_BASE}/v1/test-suites/survey-from-jira`
      : `${API_BASE}/v1/test-suites/survey`

    try {
      const response = await workflowFetch(surveyUrl, {
        method: 'POST',
        headers: { 'X-Triggered-By': currentUserEmail() },
        body,
      })
      if (!response.ok) throw new Error(`Server error: ${response.status}`)

      await readWorkflowSSE(response, {
        onStatus:   (data) => {
          const msg = data.message || ''
          applySsoPrecheckMessage('survey', msg)
          setProgressMessages(prev => [...prev, msg])
        },
        onAgentStep:(data) => setProgressMessages(prev => [...prev, `Step ${data.step_num}: ${data.action || ''}`]),
        onComplete: (data) => {
          setApplicationId(data.application_id)
          setProposals(data.proposals || [])
          // TOKENS & COST: every proposal carries the same run id, so the first one is enough.
          setSurveyRunId((data.proposals || []).find(p => p.survey_run_id)?.survey_run_id || null)
          setSurveyNotice(data.notice || null)
          setStep('review')
        },
        onError:    (data) => {
          applySsoPrecheckMessage('survey', data.message)
          setError(data.message)
          setStep('configure')
        },
      })
    } catch (err) {
      if (isCancelled()) return
      setError(err.message)
      setStep('configure')
    }
  }, [applicationName, baseUrl, documentFile, documentType, source, jiraProjectKey, jiraIssueKey, targetFlow, requiresSsoGateway, includeSsoScenarios, applySsoPrecheckMessage, workflowFetch, readWorkflowSSE, isCancelled])

  // Read requirements directly from ALM and place them into the same review
  // step used by other inputs so downstream behavior stays consistent.
  const runAlmRequirements = useCallback(async () => {
    if (isCancelled()) return
    setError(null)
    setProgressMessages(['Connecting to ALM…'])
    setStep('survey')

    const parsedPageSize = Number.parseInt(almPageSize, 10)
    const safePageSize = Number.isFinite(parsedPageSize)
      ? Math.max(1, Math.min(parsedPageSize, 200))
      : 50

    try {
      const response = await workflowFetch(`${API_BASE}/v1/alm/requirements`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: almUsername.trim(),
          password: almPassword,
          domain: almDomain.trim(),
          project: almProject.trim(),
          base_url: almBaseUrl.trim() || undefined,
          page: 1,
          page_size: safePageSize,
        }),
      })

      const payload = await response.json().catch(() => ({}))
      if (isCancelled()) return
      if (!response.ok) {
        const message = payload?.detail?.message || payload?.error || `ALM request failed (${response.status})`
        throw new Error(message)
      }

      const rows = Array.isArray(payload.data) ? payload.data : []
      const mapped = rows.map((r) => {
        const rid = String(r.id || '').trim()
        const title = (r.title || '').trim() || (rid ? `ALM Requirement ${rid}` : 'ALM Requirement')
        const status = (r.status || '').trim()
        const product = (r.product || '').trim()
        const priority = (r.priority || '').trim().toLowerCase()

        const details = [
          rid ? `Requirement ID: ${rid}` : null,
          status ? `Status: ${status}` : null,
          product ? `Product: ${product}` : null,
          r.description ? String(r.description).trim() : null,
        ].filter(Boolean)

        return {
          title,
          description: details.join('\n\n'),
          test_type: 'functional',
          priority: ['low', 'medium', 'high', 'critical'].includes(priority) ? priority : 'medium',
          derivation: 'stated',
          requirement_id: rid || null,
          tags: {
            source: 'alm',
            domain: almDomain.trim(),
            project: almProject.trim(),
          },
        }
      })

      setSurveyRunId(null)   // TOKENS & COST: ALM requirements have no survey run
      setProposals(mapped)
      setSurveyNotice(
        `Read ${mapped.length} requirement(s) from ALM ${almDomain.trim()} / ${almProject.trim()} ` +
        `(page size ${safePageSize}, total ${payload.total ?? 'unknown'}).`
      )
      setStep('review')
    } catch (err) {
      if (isCancelled()) return
      setError(err.message || 'Could not read requirements from ALM.')
      setStep('configure')
    }
  }, [almUsername, almPassword, almDomain, almProject, almBaseUrl, almPageSize, workflowFetch, isCancelled])

  // Deep-link entry: Regenerate / Generate open THIS wizard pre-scoped to one
  // Jira requirement — ?source=jira&project=KAN&issue=KAN-5&app=…&url=… — so
  // the whole existing flow (survey → ScenarioReview → accept → generate →
  // report) is reused rather than duplicated. Pre-fill the form once from the
  // query string, then auto-run the scoped survey and land on Review.
  useEffect(() => {
    if (isCancelled() || autoStartedRef.current) return
    const params = new URLSearchParams(window.location.search)
    const qSource = params.get('source')
    const qIssue = params.get('issue')
    if (qSource === 'jira' && qIssue) {
      autoStartedRef.current = true
      setSource('jira')
      setJiraProjectKey(params.get('project') || 'KAN')
      setJiraIssueKey(qIssue)
      setApplicationName(params.get('app') || '')
      setBaseUrl(params.get('url') || '')
      setAutoSurvey(true)
    }
  }, [isCancelled])

  // Once the deep-link params have settled into state, start the survey.
  useEffect(() => {
    if (!isCancelled() && !cancelConfirmation && autoSurvey && source === 'jira' && jiraIssueKey && applicationName && baseUrl) {
      setAutoSurvey(false)
      runSurvey()
    }
  }, [autoSurvey, source, jiraIssueKey, applicationName, baseUrl, runSurvey, isCancelled, cancelConfirmation])

  /**
   * Step 1 -> 2. Before surveying, check whether an application already
   * exists for this URL. Applications are identified by URL, so a familiar
   * URL with a new name silently reuses the existing one — the user should
   * decide that, not discover it afterwards.
   */
  const handleStartSurvey = useCallback(async () => {
    if (isCancelled()) return
    const errors = {}
    const importing = source === 'brd' && documentFile && isTestCaseFile(documentFile, documentType)

    if (importing) {
      // No URL required: nothing is crawled when the test cases come from a
      // file. It only needs to know which application they belong to.
      if (!pickedAppId && !applicationName.trim()) {
        errors.applicationName = 'Choose the application these test cases belong to.'
      }
    } else {
      if (!applicationName.trim()) errors.applicationName = 'Application name is required.'
      if (!baseUrl.trim())         errors.baseUrl = 'URL is required.'
      if (source === 'alm') {
        if (!almUsername.trim()) errors.almUsername = 'ALM username is required.'
        if (!almPassword)        errors.almPassword = 'ALM password is required.'
        if (!almDomain.trim())   errors.almDomain = 'ALM domain is required.'
        if (!almProject.trim())  errors.almProject = 'ALM project is required.'
      }
    }
    if (Object.keys(errors).length > 0) { setFormErrors(errors); return }

    setFormErrors({})
    setError(null)

    try {
      const res  = await workflowFetch(`${API_BASE}/v1/application-lookup?url=${encodeURIComponent(baseUrl.trim())}`)
      const data = await res.json()
      if (isCancelled()) return
      if (data.exists && data.application) {
        setApplicationId(data.application.id)
        setRequiresSsoGateway(!!data.application.requires_sso_gateway)
        setExistingApp(data.application)   // ask before going any further
        return
      }
    } catch {
      // If the check fails, carry on rather than blocking the user.
    }
    if (isCancelled()) return

    // A spreadsheet is imported, not crawled — whichever radio is selected.
    // Jira never goes through the importer; it always surveys.
    if (source === 'brd' && documentFile && isTestCaseFile(documentFile, documentType)) {
      runImport()
      return
    }
    if (source === 'alm') {
      runAlmRequirements()
      return
    }
    runSurvey()
  }, [
    applicationName,
    baseUrl,
    documentType,
    documentFile,
    runSurvey,
    runImport,
    runAlmRequirements,
    source,
    pickedAppId,
    almUsername,
    almPassword,
    almDomain,
    almProject,
    workflowFetch,
    isCancelled,
  ])


  // ── Step 3 → 4: accept the reviewed proposals, then generate ───────
  //
  // The survey only PROPOSES — nothing is in the database yet. Accepting
  // here is the point at which AI output becomes a real test case, and it
  // records who accepted it. Abandoning the wizard leaves nothing behind.
  const handleReviewComplete = useCallback((approvedList) => {
    if (isCancelled()) return
    // Saving AI output into the test repository is an approval event, so it
    // gets an explicit confirmation rather than happening as a side effect
    // of clicking Generate. The record carries who approved it.
    setPendingApproval(approvedList)
  }, [isCancelled])

  /** Called after the reviewer confirms: accept, then generate. */
  const confirmAndGenerate = useCallback(async (approvedList) => {
    if (isCancelled()) return
    setPendingApproval(null)
    setError(null)
    setAutomationWarnings([])
    setProgressMessages([])
    setSsoPrecheckState(prev => ({ ...prev, generate: null }))
    setSavedWithoutScripts(false)
    setStep('generate')

    let accepted = approvedList
    try {
      const res = await workflowFetch(`${API_BASE}/v1/test-suites/accept-proposals`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          application_id: applicationId,
          accepted_by: 'anonymous',
          // When the requirements came from Jira, tell the backend so each
          // saved test case is recorded with origin='jira' and its source
          // issue's updated-timestamp — the baseline the sync poll compares.
          jira_source: source === 'jira',
          jira_project_key: source === 'jira' ? (jiraProjectKey.trim() || null) : null,
          alm_source: source === 'alm',
          alm_domain: source === 'alm' ? (almDomain.trim() || null) : null,
          alm_project: source === 'alm' ? (almProject.trim() || null) : null,
          proposals: approvedList.map(p => ({
            title: p.title,
            description: p.description || p.title,
            // Forward the structured steps the survey produced. Without this
            // line they are dropped here, and the backend stores steps = NULL
            // even though it received a numbered description.
            steps: p.steps || null,
            // Preserve setup context so generation can satisfy preconditions
            // (auth, starting screen, required data) before step 1.
            preconditions: Array.isArray(p.preconditions) ? p.preconditions : [],
            derivation: p.derivation || null,
            category: p.category || null,
            priority: p.priority || null,
            test_type: p.test_type || 'functional',
            scenario: (p.tags && p.tags.scenario) || p.scenario || null,
            requirement_id: (p.tags && p.tags.requirement_id) || p.requirement_id || null,
            should_automate: p.automate !== false,
            on_duplicate: p.on_duplicate || 'skip',
            // TOKENS & COST: lets the server attach this test case's share of the survey run's AI cost.
            survey_run_id: p.survey_run_id || surveyRunId || null,
          })),
        }),
      })
      const data = await res.json()
      if (isCancelled()) return
      if (!res.ok || !data.created) throw new Error(data.error || `Server error: ${res.status}`)
      accepted = data.created            // now they have real database ids
      const warnings = Array.isArray(data.automation_warnings)
        ? data.automation_warnings
        : []
      setAutomationWarnings(warnings)
      if (data.replaced && data.replaced.length) {
        setSurveyNotice(
          `${data.replaced.length} existing test case(s) were replaced and their old scripts retired: ` +
          data.replaced.slice(0, 3).join('; ') + (data.replaced.length > 3 ? '…' : '')
        )
      } else if (data.skipped_duplicates && data.skipped_duplicates.length) {
        setSurveyNotice(
          `${data.skipped_duplicates.length} scenario(s) already existed and were not added again: ` +
          data.skipped_duplicates.slice(0, 3).join('; ') +
          (data.skipped_duplicates.length > 3 ? '…' : '')
        )
      }
      if (warnings.length > 0) {
        setProgressMessages(prev => [
          ...prev,
          `${warnings.length} scenario(s) were saved but marked manual-only due to missing navigation/setup context.`,
        ])
      }
      if (accepted.length === 0) {
        setError('All of those scenarios already exist for this application. Nothing new was added.')
        setStep('review')
        return
      }
    } catch (err) {
      if (isCancelled()) return
      setError(`Could not save the accepted scenarios: ${err.message}`)
      setStep('review')
      return
    }

    if (isCancelled()) return
    setApprovedProposals(accepted)

    // Every kept scenario is now saved. Only generate scripts for the ones
    // marked "Automate" — the rest remain as test cases to automate later.
    const toAutomate = accepted.filter(p => p.should_automate !== false)
    if (toAutomate.length === 0) {
      // Saved with nothing to automate: show a confirmation screen instead of
      // dropping the user back into the selection list as if nothing happened.
      setSavedWithoutScripts(true)
      setStep('scripts')
      return
    }

    const body = new FormData()
    body.append('application_id', applicationId)
    body.append('proposal_ids', toAutomate.map(p => p.id).join(','))
    body.append('environment_key', 'default')

    try {
      const response = await workflowFetch(`${API_BASE}/v1/test-suites/generate`, {
        method: 'POST',
        headers: { 'X-Triggered-By': currentUserEmail() },
        body,
      })
      if (!response.ok) throw new Error(`Server error: ${response.status}`)

      await readWorkflowSSE(response, {
        onStep:     (data) => {
          const msg = data.message || ''
          applySsoPrecheckMessage('generate', msg)
          setProgressMessages(prev => [...prev, msg])
        },
        onAgentStep:(data) => setProgressMessages(prev => [...prev, `Step ${data.step_num}: ${data.action || ''}`]),
        onComplete: (data) => {
          setGeneratedResults(data.results || [])
          setStep('scripts')
        },
        onError:    (data) => {
          applySsoPrecheckMessage('generate', data.message)
          setError(data.message)
          setStep('review')
        },
      })
    } catch (err) {
      if (isCancelled()) return
      setError(err.message)
      setStep('review')
    }
  }, [applicationId, source, jiraProjectKey, almDomain, almProject, surveyRunId, applySsoPrecheckMessage, workflowFetch, readWorkflowSSE, isCancelled])

  // ── Execute one generated script ────────────────────────────────────
  const handleExecuteOne = useCallback(async (generationId) => {
    if (isCancelled()) return null
    const body = new FormData()
    body.append('application_id', applicationId)
    body.append('generation_ids', String(generationId))
    body.append('headed', watchBrowser ? 'true' : 'false')

    try {
      const response = await workflowFetch(`${API_BASE}/v1/test-suites/execute`, {
        method: 'POST',
        headers: { 'X-Triggered-By': currentUserEmail() },
        body,
      })
      if (!response.ok) throw new Error(`Server error: ${response.status}`)

      let finalData = null
      await readWorkflowSSE(response, {
        onComplete: (data) => { finalData = data },
        onError:    (data) => { setError(data.message) },
      })
      return isCancelled() ? null : finalData
    } catch (err) {
      if (isCancelled()) return null
      setError(err.message)
      return null
    }
  }, [applicationId, watchBrowser, workflowFetch, readWorkflowSSE, isCancelled])

  // ── Execute all generated scripts ───────────────────────────────────
  const handleExecuteAll = useCallback(async () => {
    if (isCancelled()) return
    const ids = generatedResults
      .filter(r => r.result === 'generated' && r.generation_id)
      .map(r => r.generation_id)

    if (!ids.length) return
    setError(null)

    const body = new FormData()
    body.append('application_id', applicationId)
    body.append('generation_ids', ids.join(','))

    try {
      const response = await workflowFetch(`${API_BASE}/v1/test-suites/execute`, {
        method: 'POST',
        headers: { 'X-Triggered-By': currentUserEmail() },
        body,
      })
      if (!response.ok) throw new Error(`Server error: ${response.status}`)

      await readWorkflowSSE(response, {
        onComplete: (data) => { setExecutionResults(data); setStep('report') },
        onError:    (data) => { setError(data.message) },
      })
    } catch (err) {
      if (isCancelled()) return
      setError(err.message)
    }
  }, [applicationId, generatedResults, workflowFetch, readWorkflowSSE, isCancelled])

  // ── Download a script as a .py file ────────────────────────────────
  const handleDownload = useCallback((result) => {
    if (isCancelled()) return
    if (!result.script_content && !result.notes) return
    const content = result.script_content || `# Script generation was incomplete\n# Notes: ${result.notes}`
    const blob = new Blob([content], { type: 'text/plain' })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href     = url
    a.download = `test_${result.proposal_id}.py`
    a.click()
    URL.revokeObjectURL(url)
  }, [isCancelled])

  // ── Open view/edit modal ────────────────────────────────────────────
  const handleOpenModal = useCallback((result, readOnly) => {
    if (isCancelled()) return
    setModalResult(result)
    setModalReadOnly(readOnly)
    setEditedContent(result.script_content || '')
    // If the stream didn't carry the source (older results), fetch it from the
    // API so the modal always shows what is actually on disk.
    if (!result.script_content && result.generation_id) {
      workflowFetch(`${API_BASE}/v1/generations/${result.generation_id}/script`)
        .then(r => r.json())
        .then(d => { if (!isCancelled()) setEditedContent(d.script_content || `# No script found for this scenario.\n# ${result.notes || ''}`) })
        .catch(() => { if (!isCancelled()) setEditedContent(`# Could not load the script.\n# ${result.notes || ''}`) })
    }
    setModalOpen(true)
  }, [workflowFetch, isCancelled])

  // ── Save edited script back to the backend ──────────────────────────
  const handleSaveEdit = useCallback(async () => {
    if (isCancelled() || !modalResult?.generation_id) return
    setSavingEdit(true)
    try {
      const res = await workflowFetch(`${API_BASE}/v1/generations/${modalResult.generation_id}/script`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'X-Triggered-By': currentUserEmail(),
        },
        body: JSON.stringify({ script_content: editedContent }),
      })
      if (isCancelled()) return
      if (res.ok) {
        // Update local results with the new content
        setGeneratedResults(prev =>
          prev.map(r =>
            r.generation_id === modalResult.generation_id
              ? { ...r, script_content: editedContent }
              : r
          )
        )
        setModalOpen(false)
      }
    } catch (err) {
      if (isCancelled()) return
      setError(err.message)
    } finally {
      if (!isCancelled()) setSavingEdit(false)
    }
  }, [modalResult, editedContent, workflowFetch, isCancelled])

  // ── Render ──────────────────────────────────────────────────────────
  return (
    <div className={`${styles.page} ${(step === 'review' || step === 'scripts') ? styles.pageWide : ''}`}>
      <header className={styles.workflowHeader}>
        <span>New test suite</span>
        <button type="button" className="btn btn-ghost" onClick={() => setCancelConfirmation(true)} disabled={cancelling}>
          {cancelling ? 'Cancelling…' : 'Cancel flow'}
        </button>
      </header>

      <dialog
        ref={cancellationDialog}
        className={styles.cancellationDialog}
        aria-labelledby="workflow-cancellation-title"
        aria-describedby="workflow-cancellation-description"
        onCancel={event => {
          event.preventDefault()
          if (!isCancelled()) setCancelConfirmation(false)
        }}
      >
        <h2 id="workflow-cancellation-title">{cancelling ? 'Cancelling…' : 'Cancel this flow?'}</h2>
        <p id="workflow-cancellation-description">
          Saved test cases, scripts, completed results and evidence will be retained.
          Actions already performed in the application under test are not undone.
          {step === 'report' && ' Exiting this report will not change completed results.'}
        </p>
        {cancelling ? (
          <>
            <p role="status" aria-live="polite">
              Waiting for the server to confirm that all flow work, running scripts and cleanup have stopped.
              Keep this screen open.
            </p>
            {cancellationError && <p className={styles.cancellationError} role="alert">{cancellationError}</p>}
            {cancellationError && (
              <div className={styles.cancellationActions}>
                <button type="button" className="btn btn-primary" onClick={() => { void retryCancellation() }}>
                  Retry confirmation
                </button>
              </div>
            )}
          </>
        ) : (
          <div className={styles.cancellationActions}>
            <button type="button" className="btn btn-ghost" onClick={() => setCancelConfirmation(false)} autoFocus>
              Keep working
            </button>
            <button type="button" className="btn btn-primary" onClick={() => {
              setAutoSurvey(false)
              void cancelWorkflow()
            }}>
              Cancel flow
            </button>
          </div>
        )}
      </dialog>

      {/* Step indicator */}
      <div className={styles.stepIndicator} role="navigation" aria-label="Progress">
        {STEP_LABELS.map((s, i) => {
          const currentIdx = STEPS.indexOf(step)
          const thisIdx    = STEPS.indexOf(s.key)
          const isDone     = thisIdx < currentIdx
          const isActive   = thisIdx === currentIdx
          return (
            <div key={s.key} className={styles.stepItem}>
              <div className={`${styles.stepCircle} ${isDone ? styles.stepDone : isActive ? styles.stepActive : ''}`}>
                {isDone ? <i className="ti ti-check" aria-hidden="true" /> : <span>{i + 1}</span>}
              </div>
              <span className={`${styles.stepLabel} ${isActive ? styles.stepLabelActive : ''}`}>
                {s.label}
              </span>
              {i < STEP_LABELS.length - 1 && (
                <div className={`${styles.stepLine} ${isDone ? styles.stepLineDone : ''}`} />
              )}
            </div>
          )
        })}
      </div>

      {/* ── Step 1: Configure ──────────────────────────────────────── */}
      {step === 'configure' && (
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2>Create a new test suite</h2>
            <p>Select what you want to test, provide the application URL and optionally a requirements document.</p>
          </div>

          {error && (
            <div className={styles.errorBanner} role="alert">
              <i className="ti ti-alert-circle" aria-hidden="true" />
              {error}
            </div>
          )}

          {/* Test type selection */}
          <div className="form-group">
            <label className="form-label">Test type (select one or more)</label>
            <div className={styles.testTypeGrid}>
              {TEST_TYPES.map(t => (
                <div
                  key={t.key}
                  className={`${styles.testTypeCard} ${selectedTypes.includes(t.key) ? styles.testTypeCardSelected : ''}`}
                  onClick={() => toggleType(t.key)}
                  role="checkbox"
                  aria-checked={selectedTypes.includes(t.key)}
                  tabIndex={0}
                  onKeyDown={e => e.key === ' ' && toggleType(t.key)}
                >
                  <input
                    type="checkbox"
                    className={styles.testTypeCheckbox}
                    checked={selectedTypes.includes(t.key)}
                    onChange={() => toggleType(t.key)}
                    aria-label={t.label}
                    onClick={e => e.stopPropagation()}
                  />
                  <div>
                    <div className={styles.testTypeLabel}>{t.label}</div>
                    <div className={styles.testTypeDesc}>{t.desc}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Application name */}
          {lockedAppContext ? (
            <div className="form-group">
              <label className="form-label">Application</label>
              <div className="form-input" style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                <span>{lockedAppContext.name}</span>
                <span className="text-muted">from Applications</span>
              </div>
              <span className="form-hint">
                This run is scoped to the selected application. Open New Test Suite directly if you want to choose another app.
              </span>
              <span className="form-hint">
                SSO gateway: {requiresSsoGateway ? 'Enabled' : 'Disabled'} for this application.
              </span>
            </div>
          ) : (
            <div className="form-group">
              <label className="form-label" htmlFor="appName">Application name *</label>
              <input
                id="appName"
                type="text"
                className="form-input"
                placeholder="e.g. Netra, BaxCentral, JD Edwards"
                value={applicationName}
                onChange={e => setApplicationName(e.target.value)}
              />
              {formErrors.applicationName && (
                <span className="form-error">{formErrors.applicationName}</span>
              )}
            </div>
          )}

          {!lockedAppContext && (
            <div className="form-group">
              <label className="form-label" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <input
                  type="checkbox"
                  checked={requiresSsoGateway}
                  onChange={e => setRequiresSsoGateway(e.target.checked)}
                />
                This application requires SSO login gateway
              </label>
              <span className="form-hint">
                When enabled, survey and generation first run the SSO gateway test and stop early if login is not successful.
              </span>
              {requiresSsoGateway && (
                <label className="form-label" style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, paddingLeft: 22 }}>
                  <input
                    type="checkbox"
                    checked={includeSsoScenarios}
                    onChange={e => setIncludeSsoScenarios(e.target.checked)}
                  />
                  Include SSO / authentication scenarios in proposals
                </label>
              )}
            </div>
          )}

          {/* Importing test cases needs an APPLICATION, not a URL — there
              is nothing to crawl when the cases come from a file. */}
          {documentFile && (
            <div className={isTestCaseFile(documentFile, documentType)
                   ? "form-hint" : "form-hint"}
                 style={{
                   padding: '9px 13px', marginBottom: 14, borderRadius: 8,
                   background: 'var(--accent-glow)', border: '1px solid var(--accent)',
                   color: 'var(--text-primary)', fontSize: 12,
                 }}>
              {isTestCaseFile(documentFile, documentType)
                ? <>Reading <b>{documentFile.name}</b> as test cases — the page will not be crawled.</>
                : <>Using <b>{documentFile.name}</b> as requirements — the page will be crawled and scenarios proposed.</>}
            </div>
          )}

          {documentFile && isTestCaseFile(documentFile, documentType) ? (
            <>
            <div className="form-group">
              <label className="form-label" htmlFor="pickedApp">Application *</label>
              <select
                id="pickedApp"
                className="form-input"
                value={pickedAppId}
                onChange={e => setPickedAppId(e.target.value)}
              >
                <option value="">Choose an application…</option>
                {appList.map(a => (
                  <option key={a.id} value={a.id}>{a.name} — {a.base_url}</option>
                ))}
                <option value="__new__">+ New application…</option>
              </select>
              <span className="form-hint">
                {pickedAppId === '__new__'
                  ? 'A new application needs a URL: applications are identified by URL, not by name, and any scripts you generate later run against it.'
                  : 'Test cases come from the file — the page is not crawled. Scripts generated later run against this application\u2019s environment.'}
              </span>
              {formErrors.applicationName && (
                <span className="form-error">{formErrors.applicationName}</span>
              )}
            </div>

            {/* Creating an application still needs a URL: normalized_url is
                its identity (the name is not unique), and it is where scripts
                will run. */}
            {pickedAppId === '__new__' && (
              <>
                <div className="form-group">
                  <label className="form-label" htmlFor="newAppName">Application name *</label>
                  <input id="newAppName" className="form-input" placeholder="e.g. Netra"
                         value={applicationName}
                         onChange={e => setApplicationName(e.target.value)} />
                </div>
                <div className="form-group">
                  <label className="form-label" htmlFor="newAppUrl">Application URL *</label>
                  <input id="newAppUrl" type="url" className="form-input"
                         placeholder="http://localhost:3000"
                         value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />
                  {formErrors.baseUrl && (
                    <span className="form-error">{formErrors.baseUrl}</span>
                  )}
                  <span className="form-hint">
                    Identifies the application and is where scripts run. The page
                    is still not crawled — your test cases come from the file.
                  </span>
                </div>
              </>
            )}
            </>
          ) : (
            <div className="form-group">
            <label className="form-label" htmlFor="baseUrl">Live URL *</label>
            <input
              id="baseUrl"
              type="url"
              className="form-input"
              placeholder="http://localhost:3000"
              value={baseUrl}
              onChange={e => setBaseUrl(e.target.value)}
              readOnly={!!lockedAppContext}
            />
            {formErrors.baseUrl && (
              <span className="form-error">{formErrors.baseUrl}</span>
            )}
            <span className="form-hint">
              {lockedAppContext
                ? 'Using the saved application URL from Applications.'
                : 'The Author Agent will open this URL to survey the screen.'}
            </span>
          </div>
          )}

          {/* Optional: point the crawl at one specific flow instead of a broad
              exploration — useful for deep menus (e.g. ERP-style apps) where the
              screen you care about is several menu levels down. */}
          <div className="form-group">
            <label className="form-label" htmlFor="targetFlow">User Defined Flow (optional)</label>
            <textarea
              id="targetFlow"
              className="form-textarea"
              rows={4}
              placeholder={"e.g. Sales > Sales Order Entry > Standard Order SO\n" +
                "or a numbered list of steps, one per line:\n" +
                "1. Click Sales\n2. Click Sales Order Entry\n3. Click Standard Order SO"}
              value={targetFlow}
              onChange={e => setTargetFlow(e.target.value)}
            />
            <span className="form-hint">
              Leave blank to explore broadly. Set this to point the crawl at one
              specific screen buried in the navigation — it will follow menus
              toward that destination and capture every screen on the way there.
              For a specific multi-step scenario, enter one step per line and the
              crawl will follow it in order.
            </span>
          </div>

          {/* Where requirements come from: an uploaded file, or Jira. */}
          <div className="form-group">
            <label className="form-label">Requirements source</label>
            <div className={styles.docTypeRow}>
              <label className={styles.radioLabel}>
                <input
                  type="radio"
                  name="reqSource"
                  value="brd"
                  checked={source === 'brd'}
                  onChange={() => setSource('brd')}
                />
                Upload a document
              </label>
              <label className={styles.radioLabel}>
                <input
                  type="radio"
                  name="reqSource"
                  value="jira"
                  checked={source === 'jira'}
                  onChange={() => setSource('jira')}
                />
                Pull from Jira
              </label>
              <label className={styles.radioLabel}>
                <input
                  type="radio"
                  name="reqSource"
                  value="alm"
                  checked={source === 'alm'}
                  onChange={() => setSource('alm')}
                />
                Pull from ALM
              </label>
            </div>
          </div>

          {source === 'jira' ? (
            <div className="form-group">
              <label className="form-label" htmlFor="jiraProject">Jira project key</label>
              <input
                id="jiraProject"
                type="text"
                className="form-input"
                placeholder="KAN"
                value={jiraProjectKey}
                onChange={e => setJiraProjectKey(e.target.value)}
              />
              <span className="form-hint">
                Requirements (Features) are read from this Jira project and surveyed against the URL above.
                Generated test cases are linked back to their Jira issue, so the sync check can flag them when a requirement changes.
              </span>
            </div>
          ) : source === 'alm' ? (
            <div className="form-group">
              <label className="form-label" htmlFor="almUsername">ALM username</label>
              <input
                id="almUsername"
                type="text"
                className="form-input"
                placeholder="adalamr"
                value={almUsername}
                onChange={e => setAlmUsername(e.target.value)}
              />
              {formErrors.almUsername && <span className="form-error">{formErrors.almUsername}</span>}

              <label className="form-label" htmlFor="almPassword" style={{ marginTop: 10 }}>ALM password</label>
              <input
                id="almPassword"
                type="password"
                className="form-input"
                placeholder="Enter ALM password"
                value={almPassword}
                onChange={e => setAlmPassword(e.target.value)}
              />
              {formErrors.almPassword && <span className="form-error">{formErrors.almPassword}</span>}

              <label className="form-label" htmlFor="almDomain" style={{ marginTop: 10 }}>ALM domain</label>
              <input
                id="almDomain"
                type="text"
                className="form-input"
                placeholder="GLOBAL_IT"
                value={almDomain}
                onChange={e => setAlmDomain(e.target.value)}
              />
              {formErrors.almDomain && <span className="form-error">{formErrors.almDomain}</span>}

              <label className="form-label" htmlFor="almProject" style={{ marginTop: 10 }}>ALM project</label>
              <input
                id="almProject"
                type="text"
                className="form-input"
                placeholder="EnterpriseOne"
                value={almProject}
                onChange={e => setAlmProject(e.target.value)}
              />
              {formErrors.almProject && <span className="form-error">{formErrors.almProject}</span>}

              <label className="form-label" htmlFor="almBaseUrl" style={{ marginTop: 10 }}>ALM base URL</label>
              <input
                id="almBaseUrl"
                type="url"
                className="form-input"
                placeholder="https://baxter.alm.saas.microfocus.com/qcbin"
                value={almBaseUrl}
                onChange={e => setAlmBaseUrl(e.target.value)}
              />

              <label className="form-label" htmlFor="almPageSize" style={{ marginTop: 10 }}>Requirements to load (page size)</label>
              <input
                id="almPageSize"
                type="number"
                min="1"
                max="200"
                className="form-input"
                value={almPageSize}
                onChange={e => setAlmPageSize(e.target.value)}
              />

              <span className="form-hint">
                Reads requirements directly from ALM and opens them in the Review step.
              </span>
            </div>
          ) : (
          /* Requirements document — optional but strongly recommended */
          <div className="form-group">
            <label className="form-label">Requirements document (optional — but strongly recommended)</label>
            <div className={styles.docTypeRow}>
              <label className={styles.radioLabel}>
                <input
                  type="radio"
                  name="documentType"
                  value="requirements"
                  checked={documentType === 'requirements'}
                  onChange={() => setDocumentType('requirements')}
                />
                BRD / FRD / PRD / Requirements
              </label>
              <label className={styles.radioLabel}>
                <input
                  type="radio"
                  name="documentType"
                  value="existing_test_cases"
                  checked={documentType === 'existing_test_cases'}
                  onChange={() => setDocumentType('existing_test_cases')}
                />
                Existing test cases
              </label>
            </div>
            <div
              className={styles.fileDropzone}
              onClick={() => document.getElementById('docUpload').click()}
            >
              {documentFile ? (
                <div className={styles.fileSelected}>
                  <i className="ti ti-file-check" aria-hidden="true" />
                  <span>{documentFile.name}</span>
                  <button
                    className={styles.removeFile}
                    onClick={e => { e.stopPropagation(); setDocumentFile(null) }}
                    aria-label="Remove file"
                  >
                    <i className="ti ti-x" aria-hidden="true" />
                  </button>
                </div>
              ) : (
                <div className={styles.fileEmpty}>
                  <i className="ti ti-upload" aria-hidden="true" />
                  <span>Click to upload or drag and drop</span>
                  <span className={styles.fileHint}>PDF, DOCX, TXT, CSV, XLSX, JSON</span>
                </div>
              )}
              <input
                id="docUpload"
                type="file"
                accept=".pdf,.docx,.doc,.txt,.csv,.xlsx,.json"
                style={{ display: 'none' }}
                onChange={e => setDocumentFile(e.target.files[0] || null)}
                aria-label="Upload document"
              />
            </div>
            <span className="form-hint">
              Without a requirements document, only URL-based smoke/structural tests are generated — these can never fail on a missing business rule.
              Uploading a BRD or FRD enables meaningful negative and boundary test cases.
            </span>
          </div>
          )}

          <div className={styles.actions}>
            <button className="btn btn-primary" onClick={handleStartSurvey}>
              <i className="ti ti-wand" aria-hidden="true" />
              Generate scenarios
            </button>
          </div>
        </div>
      )}

      {/* ── Step 2: Survey in progress ─────────────────────────────── */}
      {step === 'survey' && (
        <>
          {ssoPrecheckState.survey && (
            <div className={`${styles.ssoPrecheckBanner} ${styles[`ssoPrecheck${ssoPrecheckState.survey[0].toUpperCase()}${ssoPrecheckState.survey.slice(1)}`]}`}>
              <strong>SSO Gateway Precheck:</strong>
              <span>
                {ssoPrecheckState.survey === 'running' && ' Running'}
                {ssoPrecheckState.survey === 'passed' && ' Passed'}
                {ssoPrecheckState.survey === 'failed' && ' Failed'}
              </span>
            </div>
          )}
          <ProgressStream
            title="Surveying the application…"
            messages={progressMessages}
            description="The Author Agent is reading the screen and proposing test scenarios."
          />
        </>
      )}

      {/* ── Step 3: Review and prioritise ──────────────────────────── */}
      {step === 'review' && importMapping && (
        <div style={{
          padding: '13px 16px', marginBottom: 12, borderRadius: 10,
          background: 'var(--bg-card)', border: '1px solid var(--border-strong)',
          fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.6,
        }}>
          <b style={{ color: 'var(--text-primary)' }}>How the file was read</b>
          {' — '}{importInfo?.filename}
          <div style={{ marginTop: 7, fontFamily: 'var(--font-mono)', fontSize: 12 }}>
            title: {importMapping.title_column || '—'} ·
            {' '}step: {importMapping.step_column || '—'} ·
            {' '}expected: {importMapping.expected_column || '—'}
          </div>
          <div style={{ marginTop: 5, color: 'var(--text-muted)' }}>
            {importMapping.row_mode === 'one_row_per_step'
              ? 'One row per step; a blank title continues the test case above.'
              : 'One row per test case.'}
            {' '}If that is wrong, go back and check the file has a header row.
          </div>
        </div>
      )}

      {step === 'review' && surveyNotice && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '7px 12px', marginBottom: 10, borderRadius: 8,
          background: 'var(--accent-glow)', border: '1px solid var(--accent)',
          color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.4,
        }}>
          <i className="ti ti-info-circle" aria-hidden="true" style={{ flexShrink: 0 }} />
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{surveyNotice}</span>
          <button type="button" onClick={() => setSurveyNotice(null)} title="Dismiss"
            style={{ border: 'none', background: 'transparent', cursor: 'pointer',
              color: 'var(--text-muted)', fontSize: 16, lineHeight: 1, flexShrink: 0, padding: 0 }}>×</button>
        </div>
      )}

      {step === 'review' && error && (
        <div className={styles.errorBanner} role="alert">
          <i className="ti ti-alert-circle" aria-hidden="true" />
          {error}
        </div>
      )}

      {step === 'review' && (
        <ScenarioReview
          proposals={proposals}
          onConfirm={handleReviewComplete}
        />
      )}

      {/* ── Step 4: Generating scripts ─────────────────────────────── */}
      {step === 'generate' && (
        <>
          {ssoPrecheckState.generate && (
            <div className={`${styles.ssoPrecheckBanner} ${styles[`ssoPrecheck${ssoPrecheckState.generate[0].toUpperCase()}${ssoPrecheckState.generate.slice(1)}`]}`}>
              <strong>SSO Gateway Precheck:</strong>
              <span>
                {ssoPrecheckState.generate === 'running' && ' Running'}
                {ssoPrecheckState.generate === 'passed' && ' Passed'}
                {ssoPrecheckState.generate === 'failed' && ' Failed'}
              </span>
            </div>
          )}
          <ProgressStream
            title="Generating test scripts…"
            messages={progressMessages}
            description={`Writing and verifying ${approvedProposals.length} script${approvedProposals.length !== 1 ? 's' : ''}. Each is generated, then run once to confirm it works. Partial results are saved as they finish.`}
          />
        </>
      )}

      {/* ── Step 5: Generated scripts — view, edit, download, execute ─ */}
      {step === 'scripts' && savedWithoutScripts && (
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2>{approvedProposals.length} test case{approvedProposals.length === 1 ? '' : 's'} saved</h2>
            <p>
              Saved to the repository — nothing was automated. The steps are stored
              with each test case. You can generate a script for any of them anytime
              from the Applications page.
            </p>
          </div>
          {automationWarnings.length > 0 && (
            <div className={styles.warningBanner} role="status">
              <i className="ti ti-alert-triangle" aria-hidden="true" />
              <div>
                <strong>{automationWarnings.length} scenario(s) saved as manual-only</strong>
                <ul className={styles.warningList}>
                  {automationWarnings.slice(0, 5).map((w, idx) => (
                    <li key={`${w.title || 'scenario'}-${idx}`}>
                      {(w.title || 'Untitled scenario')}
                      {Array.isArray(w.blocked_reasons) && w.blocked_reasons.length > 0
                        ? ` — ${w.blocked_reasons[0]}`
                        : ''}
                    </li>
                  ))}
                  {automationWarnings.length > 5 && (
                    <li>+{automationWarnings.length - 5} more</li>
                  )}
                </ul>
              </div>
            </div>
          )}
          <div style={{ display: 'flex', gap: 10, marginTop: 4 }}>
            <a href={`/applications/${applicationId}`} className="btn btn-primary">
              View in Applications
            </a>
            <a href="/new-test-suite" className="btn btn-ghost">
              Start another
            </a>
          </div>
        </div>
      )}

      {step === 'scripts' && !savedWithoutScripts && (
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2>Generated test scripts</h2>
            <p>
              {(() => {
                const ok   = generatedResults.filter(r => r.result === 'generated').length
                const bad  = generatedResults.filter(r => r.result !== 'generated').length
                const total = generatedResults.length
                if (ok === total) return `All ${total} script${total === 1 ? '' : 's'} generated and verified.`
                if (ok === 0)     return `None of the ${total} script${total === 1 ? '' : 's'} could be generated. Open a row to see why.`
                return `${ok} of ${total} scripts generated. ${bad} could not be generated — open a row to see why.`
              })()}
            </p>
          </div>

          {automationWarnings.length > 0 && (
            <div className={styles.warningBanner} role="status">
              <i className="ti ti-alert-triangle" aria-hidden="true" />
              <div>
                <strong>{automationWarnings.length} scenario(s) were saved but not automated</strong>
                <ul className={styles.warningList}>
                  {automationWarnings.slice(0, 5).map((w, idx) => (
                    <li key={`${w.title || 'scenario'}-${idx}`}>
                      {(w.title || 'Untitled scenario')}
                      {Array.isArray(w.blocked_reasons) && w.blocked_reasons.length > 0
                        ? ` — ${w.blocked_reasons[0]}`
                        : ''}
                    </li>
                  ))}
                  {automationWarnings.length > 5 && (
                    <li>+{automationWarnings.length - 5} more</li>
                  )}
                </ul>
              </div>
            </div>
          )}

          {error && (
            <div className={styles.errorBanner} role="alert">
              <i className="ti ti-alert-circle" aria-hidden="true" />
              {error}
            </div>
          )}

          <table className={styles.scriptsTable}>
            <thead>
              <tr>
                <th>Scenario</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {generatedResults.map((result, i) => {
                const succeeded = result.result === 'generated'
                const proposal  = approvedProposals.find(p => p.id === result.proposal_id) || {}
                return (
                  <tr key={result.proposal_id || i}>
                    <td>
                      <div className={styles.scriptTitle}>
                        {proposal.title || `Scenario ${result.proposal_id}`}
                      </div>
                    </td>
                    <td>
                      {succeeded
                        ? <span className="badge badge-green">Generated</span>
                        : <span className="badge badge-amber">Incomplete</span>
                      }
                    </td>
                    <td>
                      <div className={styles.scriptRowActions}>
                        <button
                          className="btn btn-ghost"
                          style={{ fontSize: '12px', padding: '5px 10px' }}
                          onClick={() => handleOpenModal(result, true)}
                          title="View script"
                        >
                          <i className="ti ti-eye" aria-hidden="true" />
                          View
                        </button>
                        {succeeded && (
                          <>
                            <button
                              className="btn btn-ghost"
                              style={{ fontSize: '12px', padding: '5px 10px' }}
                              onClick={() => handleOpenModal(result, false)}
                              title="Edit script"
                            >
                              <i className="ti ti-edit" aria-hidden="true" />
                              Edit
                            </button>
                            <button
                              className="btn btn-ghost"
                              style={{ fontSize: '12px', padding: '5px 10px' }}
                              onClick={() => handleDownload(result)}
                              title="Download .py file"
                            >
                              <i className="ti ti-download" aria-hidden="true" />
                              Download
                            </button>
                            <button
                              className="btn btn-primary"
                              style={{ fontSize: '12px', padding: '5px 12px' }}
                              onClick={async () => {
                                const res = await handleExecuteOne(result.generation_id)
                                if (res && !isCancelled()) {
                                  setGeneratedResults(prev =>
                                    prev.map(r =>
                                      r.generation_id === result.generation_id
                                        ? { ...r, execResult: res.results?.[0]?.result }
                                        : r
                                    )
                                  )
                                }
                              }}
                              title="Execute this test"
                            >
                              <i className="ti ti-player-play" aria-hidden="true" />
                              Execute
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          <div className={styles.actions}>
            <button className="btn btn-secondary" onClick={() => router.push(`/applications/${applicationId}`)}>
              View application
            </button>
            <label
              title="Watch the browser as each test runs, instead of running invisibly"
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 8,
                marginRight: 'auto', fontSize: 14, color: 'var(--text-secondary)',
                cursor: 'pointer',
              }}
            >
              <input
                type="checkbox"
                checked={watchBrowser}
                onChange={e => setWatchBrowser(e.target.checked)}
                style={{ accentColor: 'var(--accent)', width: 15, height: 15 }}
              />
              Watch the browser
            </label>

            {generatedResults.some(r => r.result === 'generated') && (
              <button className="btn btn-primary" onClick={handleExecuteAll}>
                <i className="ti ti-player-play" aria-hidden="true" />
                Execute all generated
              </button>
            )}
          </div>
        </div>
      )}

      {/* ── Step 6: Execution report ────────────────────────────────── */}
      {step === 'report' && executionResults && (
        <div className={styles.section}>
          <div className={styles.reportHeader}>
            <div className={styles.reportIcon}>
              <i className="ti ti-check" aria-hidden="true" />
            </div>
            <h2>Execution complete</h2>
            <p>
              {executionResults.passed} of {executionResults.passed + executionResults.failed} test cases passed.
            </p>
          </div>

          <div className={styles.reportMetrics}>
            <div className={styles.reportMetric}>
              <div className={styles.reportMetricValue} style={{ color: 'var(--green)' }}>
                {executionResults.passed}
              </div>
              <div className={styles.reportMetricLabel}>Passed</div>
            </div>
            <div className={styles.reportMetric}>
              <div className={styles.reportMetricValue} style={{ color: 'var(--red)' }}>
                {executionResults.failed}
              </div>
              <div className={styles.reportMetricLabel}>Failed</div>
            </div>
            <div className={styles.reportMetric}>
              <div className={styles.reportMetricValue}>
                {executionResults.passed + executionResults.failed > 0
                  ? Math.round((executionResults.passed / (executionResults.passed + executionResults.failed)) * 100)
                  : 0}%
              </div>
              <div className={styles.reportMetricLabel}>Pass rate</div>
            </div>
          </div>

          <div className={styles.reportActions}>
            <button className="btn btn-primary" onClick={() => router.push(`/applications/${applicationId}`)}>
              <i className="ti ti-apps" aria-hidden="true" />
              View application
            </button>
            <button className="btn btn-secondary" onClick={() => router.push('/reports')}>
              <i className="ti ti-chart-bar" aria-hidden="true" />
              View reports
            </button>
          </div>
        </div>
      )}



      {pendingApproval && (
        <div
          onClick={() => setPendingApproval(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
                   display: 'grid', placeItems: 'center', zIndex: 400, padding: 40 }}
        >
          <div
            onClick={e => e.stopPropagation()}
            style={{ background: 'var(--bg-card)', border: '1px solid var(--border)',
                     borderRadius: 12, padding: 24, width: 'min(600px, 100%)',
                     maxHeight: '80vh', display: 'flex', flexDirection: 'column' }}
          >
            <h3 style={{ marginBottom: 6 }}>
              {pendingApproval.filter(p => p.automate !== false).length === 0
                ? `Save ${pendingApproval.length} test case${pendingApproval.length === 1 ? '' : 's'}?`
                : `Add ${pendingApproval.length} test case${pendingApproval.length === 1 ? '' : 's'} to the repository?`}
            </h3>
            <p className="text-muted" style={{ fontSize: 12, lineHeight: 1.6, marginBottom: 16 }}>
              {pendingApproval.filter(p => p.automate !== false).length === 0
                ? 'These are AI proposals. Nothing has been saved yet. Accepting records them against this application with your name and the time. No scripts will be generated — you can automate them anytime from Applications.'
                : 'These are AI proposals. Nothing has been saved yet. Accepting records them against this application with your name and the time, and then generates a script for each one marked to automate.'}
            </p>

            <div style={{ overflowY: 'auto', border: '1px solid var(--border)',
                          borderRadius: 10, marginBottom: 16 }}>
              {pendingApproval.map((p, i) => (
                <div key={p.id || i}
                     style={{ display: 'flex', alignItems: 'center', gap: 10,
                              padding: '9px 13px',
                              borderTop: i ? '1px solid var(--border)' : 'none' }}>
                  {p.priority && (
                    <span className={`badge badge-${(p.priority === 'P0' || p.priority === 'P1') ? 'red' : 'amber'}`}>
                      {p.priority}
                    </span>
                  )}
                  <span style={{ fontSize: 14, color: 'var(--text-primary)', flex: 1 }}>{p.title}</span>
                 
                  {p.duplicate_of && (
                    <span className="badge badge-amber" title={`Similar to an existing test case: ${p.duplicate_title || ''}`}>
                      similar exists
                    </span>
                  )}
                </div>
              ))}
            </div>

            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <button className="btn btn-ghost" onClick={() => setPendingApproval(null)}>
                Back to review
              </button>
              <button className="btn btn-primary" onClick={() => confirmAndGenerate(pendingApproval)}>
                {pendingApproval.filter(p => p.automate !== false).length === 0
                  ? `Save ${pendingApproval.length} test case${pendingApproval.length === 1 ? '' : 's'}`
                  : 'Accept and generate'}
              </button>
            </div>
          </div>
        </div>
      )}

      {existingApp && (
        <div
          onClick={() => setExistingApp(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
                   display: 'grid', placeItems: 'center', zIndex: 400, padding: 40 }}
        >
          <div
            onClick={e => e.stopPropagation()}
            style={{ background: 'var(--bg-card)', border: '1px solid var(--border)',
                     borderRadius: 12, padding: 24, width: 'min(560px, 100%)' }}
          >
            <h3 style={{ marginBottom: 8 }}>This URL already has an application</h3>
            <p className="text-secondary" style={{ fontSize: 14, lineHeight: 1.6, marginBottom: 6 }}>
              <strong style={{ color: 'var(--text-primary)' }}>{existingApp.name}</strong>
              {' — '}{existingApp.base_url}
              {typeof existingApp.scenario_count === 'number' &&
                ` · ${existingApp.scenario_count} scenario${existingApp.scenario_count === 1 ? '' : 's'}`}
            </p>
            <p className="text-muted" style={{ fontSize: 12, lineHeight: 1.6, marginBottom: 18 }}>
              Applications are identified by their URL, so the name you entered
              (&ldquo;{applicationName}&rdquo;) will not be used. Continuing adds these
              scenarios to the existing application. To create a separate
              application, go back and enter a different URL.
            </p>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <button className="btn btn-ghost" onClick={() => setExistingApp(null)}>
                Go back and change the URL
              </button>
              <button className="btn btn-primary" onClick={runSurvey}>
                Add scenarios to &ldquo;{existingApp.name}&rdquo;
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Code editor modal ───────────────────────────────────────── */}
      {modalOpen && modalResult && (
        <div className={styles.modalBackdrop} onClick={() => setModalOpen(false)}>
          <div className={styles.modal} onClick={e => e.stopPropagation()}>
            <div className={styles.modalHeader}>
              <span className={styles.modalTitle}>
                {modalReadOnly ? 'View script' : 'Edit script'} —{' '}
                {(approvedProposals.find(p => p.id === modalResult.proposal_id) || {}).title
                  || `Scenario ${modalResult.proposal_id}`}
              </span>
              <div className={styles.modalHeaderActions}>
                {!modalReadOnly && (
                  <button
                    className="btn btn-ghost"
                    style={{ fontSize: '12px' }}
                    onClick={() => handleDownload({ ...modalResult, script_content: editedContent })}
                  >
                    <i className="ti ti-download" aria-hidden="true" />
                    Download
                  </button>
                )}
                <button className="btn btn-ghost" onClick={() => setModalOpen(false)} aria-label="Close">
                  <i className="ti ti-x" aria-hidden="true" />
                </button>
              </div>
            </div>

            <div className={styles.modalBody}>
              <CodeMirror
                value={editedContent}
                onChange={val => !modalReadOnly && setEditedContent(val)}
                readOnly={modalReadOnly}
                height="500px"
                theme="dark"
                extensions={[]}
                basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: !modalReadOnly }}
                style={{ fontSize: '14px', fontFamily: 'var(--font-mono)' }}
              />
            </div>

            {!modalReadOnly && (
              <div className={styles.modalFooter}>
                <button className="btn btn-ghost" onClick={() => setModalOpen(false)}>Cancel</button>
                <button className="btn btn-primary" onClick={handleSaveEdit} disabled={savingEdit}>
                  {savingEdit ? 'Saving…' : 'Save changes'}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Consumes an SSE response stream and calls event-type-specific handlers.
 *
 * @param {Response} response - The fetch Response with a readable body stream.
 * @param {object} handlers   - Map of event type → handler function.
 *   Supported keys: onStatus, onStep, onAgentStep, onComplete, onError.
 *   Each handler receives the event's data payload as its only argument.
 */
async function consumeSSE(response, handlers, { isCancelled, releaseResponse, trackReader }) {
  const reader  = response.body.getReader()
  const untrack = trackReader(reader)
  const decoder = new TextDecoder()
  let buffer    = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const chunks = buffer.split('\n\n')
      buffer = chunks.pop()

      for (const chunk of chunks) {
        if (!chunk.startsWith('data: ')) continue
        try {
          const payload = JSON.parse(chunk.slice(6))
          const handler = {
            status:     handlers.onStatus,
            step:       handlers.onStep,
            agent_step: handlers.onAgentStep,
            complete:   handlers.onComplete,
            error:      handlers.onError,
          }[payload.type]
          if (!isCancelled()) handler?.(payload.data)
        } catch {
          // Malformed chunk — skip
        }
      }
    }
  } finally {
    untrack()
    releaseResponse(response)
    reader.releaseLock()
  }
}