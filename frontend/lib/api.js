// lib/api.js
// The ONLY file that talks to the backend. Every component/page calls
// functions from here — never fetches directly.
//
// The backend base URL comes from NEXT_PUBLIC_API_URL (set in
// .env.local). Falls back to localhost:9090 to match the backend's
// default dev port.

import { readGenerationResult } from './generationStream.mjs'
import { msalInstance } from './msalInstance'

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:9090'

/**
 * Return the signed-in user's email (or 'anonymous' if MSAL isn't ready
 * or nobody is signed in). Used by apiFetch to stamp the identity header
 * on every backend call so the backend can attribute actions and enforce
 * ownership-based visibility (e.g. per-user test data).
 */
export function currentUserEmail() {
  try {
    const active = msalInstance?.getActiveAccount()
    if (active?.username) return active.username
    // After a plain page refresh MSAL restores the signed-in account into
    // its cache but does NOT re-fire a LOGIN event, so getActiveAccount()
    // can be null even though the user is signed in. Fall back to the first
    // cached account and promote it to active so every later call — and the
    // X-Triggered-By header that per-user credentials depend on — stays
    // consistent instead of silently degrading to 'anonymous'.
    const all = msalInstance?.getAllAccounts?.() || []
    if (all.length > 0) {
      msalInstance.setActiveAccount(all[0])
      return all[0].username || 'anonymous'
    }
    return 'anonymous'
  } catch {
    return 'anonymous'
  }
}

/**
 * Core fetch wrapper. Adds JSON + identity headers and converts any
 * non-2xx response into a thrown Error carrying the backend's message.
 *
 * @param {string} path - API path, e.g. '/v1/applications'
 * @param {object} opts - standard fetch options
 * @returns {Promise<any>} parsed JSON body
 */
async function apiFetch(path, opts = {}, fetcher = fetch) {
  const headers = {
    'Content-Type': 'application/json',
    'X-Triggered-By': currentUserEmail(),
    ...(opts.headers || {}),
  }
  const response = await fetcher(`${API_BASE}${path}`, { ...opts, headers })
  if (!response.ok) {
    let message = `Request failed: ${response.status}`
    try {
      const body = await response.json()
      message = body?.error?.message || body?.detail || message
    } catch {
      /* body wasn't JSON */
    }
    throw new Error(message)
  }
  // 204 No Content
  if (response.status === 204) return null
  return response.json()
}

// ─── Applications ────────────────────────────────────────────────────────

export function getApplications() {
  return apiFetch('/v1/applications')
}

export function getApplication(id) {
  return apiFetch(`/v1/applications/${id}`)
}

export function createApplication({ name, base_url, owner, requires_sso_gateway = false }, fetcher = fetch) {
  return apiFetch('/v1/applications', {
    method: 'POST',
    body: JSON.stringify({ name, base_url, owner, requires_sso_gateway }),
  }, fetcher)
}

export function setApplicationSsoGateway(id, requires_sso_gateway) {
  return apiFetch(`/v1/applications/${id}/sso-gateway`, {
    method: 'PATCH',
    body: JSON.stringify({ requires_sso_gateway: !!requires_sso_gateway }),
  })
}

/**
 * Start an App Map crawl for an application.
 * Returns a raw fetch Response — the caller reads it as an SSE stream.
 * Use the useSSE hook or read response.body directly.
 *
 * @param {number} applicationId
 * @param {{ username?: string, password?: string, maxPages?: number }} opts
 */
export function crawlApplication(applicationId, opts = {}) {
  const body = new FormData()
  body.append('max_pages', String(opts.maxPages || 30))
  if (opts.username) body.append('username', opts.username)
  if (opts.password) body.append('password', opts.password)
  return fetch(`${API_BASE}/v1/applications/${applicationId}/crawl`, {
    method: 'POST',
    headers: { 'X-Triggered-By': currentUserEmail() },
    body,
  })
}

/**
 * Return summary of the most recent App Map for an application.
 * Throws if no map has been crawled yet.
 */
export function getLatestAppMap(applicationId) {
  return apiFetch(`/v1/applications/${applicationId}/maps/latest`)
}

// ─── Proposals ───────────────────────────────────────────────────────────

export function getProposals(applicationId) {
  return apiFetch(`/v1/applications/${applicationId}/proposals`)
}

export function setProposalPriority(proposalId, priority) {
  return apiFetch(`/v1/proposals/${proposalId}/priority`, {
    method: 'PATCH',
    body: JSON.stringify({ priority }),
  })
}

export function setProposalStatus(proposalId, status) {
  return apiFetch(`/v1/proposals/${proposalId}/status`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  })
}

export function archiveProposal(proposalId) {
  return apiFetch(`/v1/proposals/${proposalId}/archive`, { method: 'POST' })
}

// ─── Test suites (generation + execution) ────────────────────────────────

export function surveyTestSuite(payload) {
  return apiFetch('/v1/test-suites/survey', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function generateTestSuite(payload) {
  return apiFetch('/v1/test-suites/generate', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function executeTestSuite(payload) {
  return apiFetch('/v1/test-suites/execute', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

// ─── Scoped Jira generation (Regenerate / Generate one requirement) ──────

export function surveyFromJira({ application_name, base_url, jira_project_key, jira_issue_key }) {
  const body = new FormData()
  body.append('application_name', application_name)
  body.append('base_url', base_url)
  if (jira_project_key) body.append('jira_project_key', jira_project_key)
  if (jira_issue_key) body.append('jira_issue_key', jira_issue_key)
  return fetch(`${API_BASE}/v1/test-suites/survey-from-jira`, {
    method: 'POST', headers: { 'X-Triggered-By': currentUserEmail() }, body,
  })
}

export function acceptJiraProposals(applicationId, proposals, { acceptedBy = 'anonymous', jiraProjectKey } = {}) {
  return apiFetch('/v1/test-suites/accept-proposals', {
    method: 'POST',
    body: JSON.stringify({
      application_id: applicationId, accepted_by: acceptedBy, proposals,
      jira_source: true, jira_project_key: jiraProjectKey || null,
    }),
  })
}

// ─── Jira change-detection review ────────────────────────────────────────

export function getJiraPending(applicationId, projectKey) {
  const q = new URLSearchParams({ application_id: applicationId })
  if (projectKey) q.set('jira_project_key', projectKey)
  return apiFetch(`/v1/jira/pending?${q.toString()}`)
}

export function markTestCaseReviewed(proposalId) {
  return apiFetch(`/v1/test-cases/${proposalId}/review`, { method: 'POST' })
}

// ─── Executions ──────────────────────────────────────────────────────────

export function getExecutions(generationId) {
  return apiFetch(`/v1/generations/${generationId}/executions`)
}

// ─── Reports ─────────────────────────────────────────────────────────────

export function getApplicationReport(applicationId) {
  return apiFetch(`/v1/applications/${applicationId}/report`)
}

export function getLeadershipReport() {
  return apiFetch('/v1/reports/leadership')
}

// ─── Health ──────────────────────────────────────────────────────────────

export function getHealth() {
  return apiFetch('/health')
}

// ─── Execution (real Selenium runs) ──────────────────────────────────────

export function runScript(generationId, headed, target = {}) {
  return apiFetch('/v1/run/script', {
    method: 'POST',
    body: JSON.stringify({ generation_id: generationId, headed, ...target }),
  })
}

export function runScenario(proposalId, headed, target = {}) {
  return apiFetch('/v1/run/scenario', {
    method: 'POST',
    body: JSON.stringify({ proposal_id: proposalId, headed, ...target }),
  })
}

export function getScenarioScripts(proposalId) {
  return apiFetch(`/v1/proposals/${proposalId}/scripts`)
}

export function getExecutionHistory(generationId) {
  return apiFetch(`/v1/generations/${generationId}/history`)
}

export function screenshotUrl(path) {
  // path already like /v1/screenshots/<run>/<name>; prefix with API base
  return `${API_BASE}${path}`
}

// ─── Test data (externalised) ────────────────────────────────────────────

export function getAppTestData({ applicationId, environment } = {}) {
  const q = new URLSearchParams()
  if (applicationId !== undefined && applicationId !== null) {
    q.set('application_id', String(applicationId))
  }
  if (environment) {
    q.set('environment', environment)
  }
  const suffix = q.toString() ? `?${q.toString()}` : ''
  return apiFetch(`/v1/test-data/app${suffix}`)
}

export function saveAppTestData(content, { applicationId, environment } = {}) {
  const q = new URLSearchParams()
  if (applicationId !== undefined && applicationId !== null) {
    q.set('application_id', String(applicationId))
  }
  if (environment) {
    q.set('environment', environment)
  }
  const suffix = q.toString() ? `?${q.toString()}` : ''
  return apiFetch(`/v1/test-data/app${suffix}`, { method: 'PUT', body: JSON.stringify({ content }) })
}

/**
 * Reveal one masked field's real value.
 *
 * @param {object} opts
 * @param {number} opts.applicationId
 * @param {string} [opts.environment]
 * @param {string} opts.key - Field name, e.g. "password".
 * @param {string} [opts.persona] - When set, reveal
 *   environments.<env>.personas.<persona>.<key> instead of a flat
 *   top-level field (e.g. persona="approver", key="password") — see
 *   test_data/applications/app_<id>.json's "personas" block, used for
 *   multi-actor procedures (requester/approver/admin/... all within one
 *   continuous script run). Omitted (the default) is the existing
 *   flat-field behaviour, unchanged.
 */
export function revealAppTestDataField({ applicationId, environment, key, persona }) {
  return apiFetch('/v1/test-data/app/reveal', {
    method: 'POST',
    body: JSON.stringify({
      application_id: applicationId,
      environment,
      key,
      ...(persona ? { persona } : {}),
    }),
  })
}

// ─── My application login (per-user credentials) ─────────────────────────
//
// Each signed-in user stores THEIR OWN login for an application. The backend
// scopes every one of these calls to the caller (via the identity header
// apiFetch already sends), so a user only ever sees or changes their own
// credential — never anyone else's.

/**
 * Get the caller's saved login for an application. The password comes back
 * masked ("********") if one is stored; use revealMyAppCredential to see it.
 */
export function getMyAppCredential(applicationId) {
  return apiFetch(`/v1/app-credentials/${applicationId}`)
}

/**
 * Save (or replace) the caller's login for an application. Pass the masked
 * placeholder (or leave the password empty) to keep the stored password and
 * only update the username.
 */
export function saveMyAppCredential(applicationId, { username, password }) {
  return apiFetch(`/v1/app-credentials/${applicationId}`, {
    method: 'PUT',
    body: JSON.stringify({ username, password }),
  })
}

/** Reveal the caller's OWN password in plaintext for this application. */
export function revealMyAppCredential(applicationId) {
  return apiFetch(`/v1/app-credentials/${applicationId}/reveal`, { method: 'POST' })
}

/** Delete the caller's login for this application. */
export function deleteMyAppCredential(applicationId) {
  return apiFetch(`/v1/app-credentials/${applicationId}`, { method: 'DELETE' })
}

export function getScenarioTestData(filename) {
  return apiFetch(`/v1/test-data/scenario/${filename}`)
}

export function saveScenarioTestData(filename, content) {
  return apiFetch(`/v1/test-data/scenario/${filename}`, { method: 'PUT', body: JSON.stringify({ content }) })
}

/**
 * Link (or unlink) a scenario-level test data file to one test case.
 *
 * Backs PATCH /v1/proposals/{id}/test-data-file (api/routes/run.py). This
 * only records WHICH file a scenario should read at generate/execute/run
 * time (test_case_proposals.tags.test_data_file) — it does not create or
 * modify the file's contents. Use saveScenarioTestData() for that, and
 * save the file BEFORE calling this, so a scenario is never linked to a
 * file that does not yet contain what the reviewer intended.
 *
 * @param {number} proposalId - The scenario (test case) to link.
 * @param {string|null} testDataFile - Filename under test_data/scenarios/
 *   (e.g. "item_2.json"), or null/empty to detach — the scenario then
 *   falls back to application-level test data only.
 */
export function setProposalTestDataFile(proposalId, testDataFile) {
  return apiFetch(`/v1/proposals/${proposalId}/test-data-file`, {
    method: 'PATCH',
    body: JSON.stringify({ test_data_file: testDataFile || null }),
  })
}

// ─── Dashboards (real data) ──────────────────────────────────────────────

/**
 * Engineer/Subscriber dashboard.
 *
 * Called with no arguments for the page's main load (unchanged from
 * before). The Execution Trend / Results Distribution panels additionally
 * pass rangeDays/testType/applicationId to re-scope just those two
 * charts — everything else in the payload (KPIs, failing tests, recent
 * runs) is unaffected by these params on the backend, so passing them
 * never changes any other card on the page.
 *
 * @param {object} [params]
 * @param {number} [params.rangeDays] - How many days back to look (e.g.
 *   7, 14, 30, 56, 90). Omitted from the request entirely when not passed.
 * @param {string} [params.testType] - Restrict to one test type
 *   ('functional' | 'regression' | 'smoke'). 'all' means "no filter" and
 *   is not sent, matching how the rest of this file treats empty filters.
 * @param {string|number} [params.applicationId] - Restrict to one
 *   application's id. 'all' (or omitted) means "no filter" and is not
 *   sent — same convention as testType above.
 * @param {number} [params.failingLimit] - How many Failing Tests rows to
 *   return. Omitted = the backend's default (~12, matching the dashboard
 *   card). Pass a higher number (e.g. for the "More" modal) to get the
 *   full list.
 * @param {number} [params.recentLimit] - Same idea as failingLimit, for
 *   Recent Runs (backend default ~10).
 */
export function getEngineerDashboard({ rangeDays, testType, applicationId, failingLimit, recentLimit } = {}) {
  const q = new URLSearchParams()
  if (rangeDays !== undefined && rangeDays !== null && rangeDays !== '') {
    q.set('range_days', String(rangeDays))
  }
  if (testType && testType !== 'all') {
    q.set('test_type', testType)
  }
  if (applicationId !== undefined && applicationId !== null && applicationId !== 'all' && applicationId !== '') {
    q.set('application_id', String(applicationId))
  }
  if (failingLimit !== undefined && failingLimit !== null && failingLimit !== '') {
    q.set('failing_limit', String(failingLimit))
  }
  if (recentLimit !== undefined && recentLimit !== null && recentLimit !== '') {
    q.set('recent_limit', String(recentLimit))
  }
  const suffix = q.toString() ? `?${q.toString()}` : ''
  return apiFetch(`/v1/dashboard/engineer${suffix}`)
}

/**
 * Leadership dashboard.
 *
 * Called with no arguments for the page's main load (unchanged from
 * before). The Quality Trend / Automated Scripts by Application panels
 * additionally pass rangeDays/testType/applicationId to re-scope just
 * those two charts — same convention as getEngineerDashboard above, and
 * everything else in the payload (KPIs, coverage_by_app, risks) is
 * unaffected by these params on the backend.
 *
 * Note: rangeDays only affects Quality Trend. Automated Scripts by
 * Application is a snapshot of the current automation estate per
 * application (not an execution trend), so it only responds to
 * testType/applicationId — see dashboard.py for why.
 *
 * @param {object} [params]
 * @param {number} [params.rangeDays] - How many days back to look for
 *   Quality Trend. Omitted from the request entirely when not passed.
 * @param {string} [params.testType] - Restrict to one test type
 *   ('functional' | 'regression' | 'smoke'). 'all' means "no filter".
 * @param {string|number} [params.applicationId] - Restrict to one
 *   application's id. 'all' (or omitted) means "no filter".
 */
export function getLeadershipDashboard({ rangeDays, testType, applicationId } = {}) {
  const q = new URLSearchParams()
  if (rangeDays !== undefined && rangeDays !== null && rangeDays !== '') {
    q.set('range_days', String(rangeDays))
  }
  if (testType && testType !== 'all') {
    q.set('test_type', testType)
  }
  if (applicationId !== undefined && applicationId !== null && applicationId !== 'all' && applicationId !== '') {
    q.set('application_id', String(applicationId))
  }
  const suffix = q.toString() ? `?${q.toString()}` : ''
  return apiFetch(`/v1/dashboard/leadership${suffix}`)
}

/**
 * Token usage and cost per AI activity, for the dashboard's Tokens & Cost card.
 * applicationId 'all' (or empty) means no application filter.
 */
/**
 * Token usage and cost per AI activity, for the dashboard's Tokens & Cost card.
 * applicationId 'all' (or empty) means no application filter.
 */
export function getAiUsage({ rangeDays = 30, applicationId = 'all' } = {}) {
  // Builds the query string, adding application_id only when a specific app is chosen.
  const q = new URLSearchParams({ days: String(rangeDays) })
  if (applicationId !== undefined && applicationId !== null && applicationId !== 'all' && applicationId !== '') {
    q.set('application_id', String(applicationId))
  }
  return apiFetch(`/v1/reports/ai-usage?${q.toString()}`)
}

/**
 * AI cost per test case (estimated) and per script (exact) for one application.
 * Returns { test_cases: { [proposalId]: { }, scripts: { [proposalId]: { } }.
 */
export function getAiCostItems(applicationId) {
  return apiFetch(`/v1/reports/ai-cost/items?application_id=${encodeURIComponent(applicationId)}`)
}

// ─── Kavach AI chat ──────────────────────────────────────────────────────

export function askKavach(question) {
  return apiFetch('/v1/chat', { method: 'POST', body: JSON.stringify({ question }) })
}

export function getChatSuggestions() {
  return apiFetch('/v1/chat/suggestions')
}

// ─── Script view / edit / download ───────────────────────────────────────

export function getScript(generationId) {
  return apiFetch(`/v1/generations/${generationId}/script`)
}

export function saveScript(generationId, scriptContent) {
  return apiFetch(`/v1/generations/${generationId}/script`, {
    method: 'PATCH',
    body: JSON.stringify({ script_content: scriptContent }),
  })
}

// ─── Archive / delete ────────────────────────────────────────────────────

export function archiveScript(generationId) {
  return apiFetch(`/v1/generations/${generationId}/archive`, { method: 'POST' })
}

export function restoreScript(generationId) {
  return apiFetch(`/v1/generations/${generationId}/restore`, { method: 'POST' })
}

export function deleteScript(generationId) {
  return apiFetch(`/v1/generations/${generationId}`, { method: 'DELETE' })
}

export function archiveScenario(proposalId) {
  return apiFetch(`/v1/proposals/${proposalId}/archive`, { method: 'POST' })
}

export function deleteScenario(proposalId) {
  return apiFetch(`/v1/proposals/${proposalId}`, { method: 'DELETE' })
}

// ─── Applications: archive / delete ──────────────────────────────────────

export function archiveApplication(appId) {
  return apiFetch(`/v1/applications/${appId}/archive`, { method: 'POST' })
}

export function restoreApplication(appId) {
  return apiFetch(`/v1/applications/${appId}/restore`, { method: 'POST' })
}

export function deleteApplication(appId) {
  return apiFetch(`/v1/applications/${appId}`, { method: 'DELETE' })
}

// ─── Automate a test case that has no script yet ─────────────────────────

export async function generateScriptsFor(applicationId, proposalIds, options = {}) {
  const body = new FormData()
  body.append('application_id', applicationId)
  body.append('proposal_ids', proposalIds.join(','))
  body.append('environment_key', 'default')
  if (options.replaceExisting === true) body.append('replace_existing', 'true')
  const response = await fetch(`${API_BASE}/v1/test-suites/generate`, {
    method: 'POST',
    headers: { 'X-Triggered-By': currentUserEmail() },
    body,
  })
  return readGenerationResult(response)
}

// ─── Self-healing ────────────────────────────────────────────────────────

export function diagnoseFailure(generationId, executionId) {
  return apiFetch(`/v1/generations/${generationId}/diagnose`, {
    method: 'POST',
    body: JSON.stringify({ execution_id: executionId || null }),
  })
}

export function getHealingHistory(generationId) {
  return apiFetch(`/v1/generations/${generationId}/healing`)
}

export function approveHealing(healingId) {
  return apiFetch(`/v1/healing/${healingId}/approve`, { method: 'POST' })
}

export function rejectHealing(healingId) {
  return apiFetch(`/v1/healing/${healingId}/reject`, { method: 'POST' })
}

// ─── Environments ────────────────────────────────────────────────────────

export function getEnvironments(appId) {
  return apiFetch(`/v1/applications/${appId}/environments`)
}

export function createEnvironment(appId, env) {
  return apiFetch(`/v1/applications/${appId}/environments`, {
    method: 'POST', body: JSON.stringify(env),
  })
}

export function updateEnvironment(envId, patch) {
  return apiFetch(`/v1/environments/${envId}`, {
    method: 'PATCH', body: JSON.stringify(patch),
  })
}

export function deleteEnvironment(envId) {
  return apiFetch(`/v1/environments/${envId}`, { method: 'DELETE' })
}

// ─── Regenerate a script for an existing test case ───────────────────────

export function retireScripts(proposalId) {
  return apiFetch(`/v1/proposals/${proposalId}/retire-scripts`, { method: 'POST' })
}

// ─── Test suites ─────────────────────────────────────────────────────────

export function getSuites(appId) {
  return apiFetch(`/v1/applications/${appId}/suites`)
}

export function getSuiteCandidates(appId, kind) {
  return apiFetch(`/v1/applications/${appId}/suite-candidates?kind=${encodeURIComponent(kind)}`)
}

export function createSuite(appId, suite) {
  return apiFetch(`/v1/applications/${appId}/suites`, {
    method: 'POST', body: JSON.stringify(suite),
  })
}

export function getSuite(suiteId) {
  return apiFetch(`/v1/suites/${suiteId}`)
}

export function setSuiteMembers(suiteId, memberIds) {
  return apiFetch(`/v1/suites/${suiteId}/members`, {
    method: 'PUT', body: JSON.stringify({ member_ids: memberIds }),
  })
}

export function deleteSuite(suiteId) {
  return apiFetch(`/v1/suites/${suiteId}`, { method: 'DELETE' })
}

export function runSuite(suiteId, opts = {}) {
  return apiFetch(`/v1/suites/${suiteId}/run`, {
    method: 'POST', body: JSON.stringify({ headed: false, ...opts }),
  })
}

// ─── Schedules ───────────────────────────────────────────────────────────

export function getSchedules(applicationId) {
  const q = applicationId ? `?application_id=${applicationId}` : ''
  return apiFetch(`/v1/schedules${q}`)
}

export function createSchedule(schedule) {
  return apiFetch('/v1/schedules', { method: 'POST', body: JSON.stringify(schedule) })
}

export function updateSchedule(id, patch) {
  return apiFetch(`/v1/schedules/${id}`, { method: 'PATCH', body: JSON.stringify(patch) })
}

export function deleteSchedule(id) {
  return apiFetch(`/v1/schedules/${id}`, { method: 'DELETE' })
}

export function runScheduleNow(id) {
  return apiFetch(`/v1/schedules/${id}/run`, { method: 'POST' })
}

export function getScheduleRuns(id) {
  return apiFetch(`/v1/schedules/${id}/runs`)
}

// ─── Executions (the single place run history lives) ─────────────────────

export function listExecutions(params = {}) {
  const q = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== '')
  ).toString()
  return apiFetch(`/v1/executions${q ? `?${q}` : ''}`)
}

export function getExecution(id) {
  return apiFetch(`/v1/executions/${id}`)
}

export function listScheduleRuns() {
  return apiFetch('/v1/schedule-runs')
}

export function getSchedulesSummary() {
  return apiFetch('/v1/schedules/summary')
}

// ─── Import manual test cases ────────────────────────────────────────────

export function importPreview(applicationId, file, overrides = {}) {
  const body = new FormData()
  body.append('file', file)
  Object.entries(overrides).forEach(([k, v]) => { if (v) body.append(k, v) })
  return fetch(`${API_BASE}/v1/applications/${applicationId}/import-preview`, {
    method: 'POST', body,
  }).then(r => r.json())
}

export function acceptProposals(applicationId, proposals, acceptedBy = 'anonymous') {
  return apiFetch('/v1/test-suites/accept-proposals', {
    method: 'POST',
    body: JSON.stringify({ application_id: applicationId, accepted_by: acceptedBy, proposals }),
  })
}

// ─── Chat actions (interpret, then execute only on confirmation) ─────────

export function interpretInstruction(instruction) {
  return apiFetch('/v1/chat/interpret', {
    method: 'POST', body: JSON.stringify({ instruction }),
  })
}

export function executeAction(proposal) {
  return apiFetch('/v1/chat/execute', {
    method: 'POST', body: JSON.stringify({ proposal }),
  })
}

export function resummarise(proposal) {
  return apiFetch('/v1/chat/resummarise', {
    method: 'POST', body: JSON.stringify({ proposal }),
  })
}

// ─── Test repository (across applications) ───────────────────────────────

export function listRepositoryTestCases(filters = {}) {
  const q = new URLSearchParams(
    Object.entries(filters).filter(([, v]) => v !== '' && v !== null && v !== undefined)
  ).toString()
  return apiFetch(`/v1/repository/test-cases${q ? `?${q}` : ''}`)
}

export function getRepositorySummary() {
  return apiFetch('/v1/repository/summary')
}

// ─── Reports (the artefacts that leave the tool) ─────────────────────────

export function getTraceabilityReport(applicationId) {
  const q = applicationId ? `?application_id=${applicationId}` : ''
  return apiFetch(`/v1/reports/traceability${q}`)
}

export function getExecutionReport(params = {}) {
  const q = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== '' && v != null)
  ).toString()
  return apiFetch(`/v1/reports/executions${q ? `?${q}` : ''}`)
}

export function getCoverageReport() {
  return apiFetch('/v1/reports/coverage')
}

/**
 * Who the backend should attribute this request to.
 *
 * Kavach has no authentication yet, so this reads a name the user has set
 * locally and falls back to 'anonymous' — exactly what the backend already
 * defaults to. It is not a security control and does not pretend to be one:
 * anyone can set it. What it does give is a training record and an audit
 * trail that say something more useful than "anonymous" for every entry,
 * and one place to change when SSO arrives.
 *
 * @returns {string} The current user's name.
 */
export function getCurrentUser() {
  if (typeof window === 'undefined') return 'anonymous'
  try {
    return localStorage.getItem('kavach-user') || 'anonymous'
  } catch {
    return 'anonymous'
  }
}

/**
 * Set the name requests are attributed to.
 *
 * @param {string} name - The user's name, or empty to clear it.
 */
export function setCurrentUser(name) {
  try {
    if (name) localStorage.setItem('kavach-user', name)
    else localStorage.removeItem('kavach-user')
  } catch {
    /* storage disabled — requests fall back to 'anonymous' */
  }
}

// ─── Help: articles ──────────────────────────────────────────────────────

/**
 * The article categories, for the Help navigation.
 *
 * @param {boolean} includeDrafts - Count unapproved articles too.
 */
export function getHelpCategories(includeDrafts = false) {
  return apiFetch(`/v1/help/categories?include_drafts=${includeDrafts}`)
}

/**
 * List help articles.
 *
 * @param {object} opts
 * @param {string} [opts.category] - Restrict to one category.
 * @param {boolean} [opts.includeDrafts] - Include unapproved articles.
 */
export function getHelpArticles({ category, includeDrafts = false } = {}) {
  const q = new URLSearchParams({ include_drafts: String(includeDrafts) })
  if (category) q.set('category', category)
  return apiFetch(`/v1/help/articles?${q.toString()}`)
}

/**
 * Fetch one article.
 *
 * Reading an article records a view against the current user — that record
 * is the training evidence, so it is written by opening the article rather
 * than by a separate action. Pass recordView=false for an authoring preview,
 * where creating evidence that someone was trained would be false.
 *
 * @param {string} slug - The article's stable identifier.
 * @param {boolean} recordView - Whether to write a training record.
 */
export function getHelpArticle(slug, recordView = true) {
  return apiFetch(`/v1/help/articles/${encodeURIComponent(slug)}?record_view=${recordView}`)
}

/**
 * Approve a draft article, making it citable by Kavach AI.
 *
 * @param {number} articleId - help_articles.id.
 */
export function approveHelpArticle(articleId) {
  return apiFetch(`/v1/help/articles/${articleId}/approve`, { method: 'POST' })
}

/**
 * Record "I have read and understood" against a view.
 *
 * @param {number} viewId - The view id returned by getHelpArticle.
 */
export function acknowledgeHelpView(viewId) {
  return apiFetch(`/v1/help/views/${viewId}/acknowledge`, { method: 'POST' })
}

// ─── Help: search ────────────────────────────────────────────────────────

/**
 * Search articles and training documents together.
 *
 * A query that finds nothing is recorded as a documentation gap by the
 * backend; the response flags it so the UI can offer to raise a ticket.
 *
 * @param {string} query - The question, in the user's own words.
 * @param {object} opts
 * @param {boolean} [opts.includeDrafts] - Search unapproved articles too.
 */
export function searchHelp(query, { includeDrafts = false } = {}) {
  const q = new URLSearchParams({ q: query, include_drafts: String(includeDrafts) })
  return apiFetch(`/v1/help/search?${q.toString()}`)
}

/**
 * The questions that found nothing — the documentation backlog.
 *
 * @param {number} limit - Maximum rows.
 */
export function getHelpGaps(limit = 100) {
  return apiFetch(`/v1/help/gaps?limit=${limit}`)
}

// ─── Help: training documents and records ────────────────────────────────

/** Uploaded training documents, with their indexed passage counts. */
export function getTrainingDocuments() {
  return apiFetch('/v1/help/training')
}

/**
 * Upload a training document.
 *
 * Bypasses apiFetch because that wrapper sets a JSON content type, which
 * would break the multipart boundary. Same approach as importPreview.
 *
 * @param {File} file - The PDF, DOCX or text file.
 * @param {string} [title] - Display name; defaults to the filename.
 */
export function uploadTrainingDocument(file, title) {
  const body = new FormData()
  body.append('file', file)
  if (title) body.append('title', title)
  return fetch(`${API_BASE}/v1/help/training`, {
    method: 'POST',
    headers: { 'X-Triggered-By': getCurrentUser() },
    body,
  }).then(async r => {
    const data = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(data?.detail || `Upload failed: ${r.status}`)
    return data
  })
}

/**
 * A link to download a training document.
 *
 * @param {number} documentId - documents.id.
 */
export function trainingDocumentUrl(documentId) {
  return `${API_BASE}/v1/help/training/${documentId}/file`
}

/**
 * The training record.
 *
 * @param {object} opts
 * @param {string} [opts.viewedBy] - Restrict to one person.
 * @param {boolean} [opts.acknowledgedOnly] - Only acknowledged views, which
 *   is the subset that constitutes training evidence.
 */
export function getTrainingRecords({ viewedBy, acknowledgedOnly = false } = {}) {
  const q = new URLSearchParams({ acknowledged_only: String(acknowledgedOnly) })
  if (viewedBy) q.set('viewed_by', viewedBy)
  return apiFetch(`/v1/help/records?${q.toString()}`)
}

// ─── Support tickets ─────────────────────────────────────────────────────

/** Which ticketing provider is active, and whether it is usable. */
export function getSupportConfig() {
  return apiFetch('/v1/support/config')
}

/** The categories the support form offers. */
export function getSupportCategories() {
  return apiFetch('/v1/support/categories')
}

/**
 * Tickets raised from Kavach.
 *
 * @param {object} opts
 * @param {string} [opts.raisedBy] - Restrict to one person.
 * @param {number} [opts.limit] - Maximum rows.
 */
export function getSupportTickets({ raisedBy, limit = 50 } = {}) {
  const q = new URLSearchParams({ limit: String(limit) })
  if (raisedBy) q.set('raised_by', raisedBy)
  return apiFetch(`/v1/support/tickets?${q.toString()}`)
}

/**
 * Raise a support ticket.
 *
 * The response always carries the ticket, even when the external system
 * could not be reached — in that case it also carries an `error` and its
 * status is 'failed'. Callers should show both rather than treating the
 * error as a lost submission: the ticket exists in Kavach either way.
 *
 * @param {object} ticket
 * @param {string} ticket.category - One of getSupportCategories().
 * @param {string} ticket.summary - One-line title.
 * @param {string} [ticket.description] - The user's account of the problem.
 * @param {object} [ticket.context] - Technical detail captured automatically.
 */
export function raiseSupportTicket({ category, summary, description, context }) {
  return apiFetch('/v1/support/tickets', {
    method: 'POST',
    body: JSON.stringify({
      category,
      summary,
      description: description || '',
      context: context || {},
    }),
  })
}

/**
 * Collect the technical context worth attaching to a ticket.
 *
 * Gathered here rather than asked for, because a user reporting a problem
 * should not have to know what a generation id is, and a ticket without this
 * detail costs a round trip before anyone can start on it.
 *
 * @param {object} extra - Anything the calling screen knows, e.g.
 *   application_id or execution_id.
 * @returns {object} Context to send with the ticket.
 */
export function collectSupportContext(extra = {}) {
  const ctx = { ...extra }
  if (typeof window !== 'undefined') {
    ctx.route = window.location.pathname
    ctx.browser = navigator.userAgent
    ctx.viewport = `${window.innerWidth}x${window.innerHeight}`
  }
  ctx.raised_at = new Date().toISOString()
  return ctx
}