'use client'

/**
 * app/help/page.js
 *
 * Help and support.
 *
 * Four things sit behind one screen, in the order someone in trouble
 * actually needs them:
 *
 *   Browse    the articles, by category
 *   Search    articles and uploaded training documents together
 *   Training  the documents, and the record of who has read what
 *   Support   raise a ticket, and see the ones you have raised
 *
 * Search is first in the layout rather than buried, because a person who
 * opens Help already has a question — making them navigate a taxonomy to
 * reach a search box is the most common way an in-product help centre fails.
 *
 * Two behaviours worth knowing while reading this file:
 *
 *   Opening an article records a view. That record is the training evidence,
 *   so it is written by reading rather than by a separate action. An explicit
 *   acknowledgement is offered on top, because "opened the page" and "read
 *   and understood" are not the same claim.
 *
 *   A search that finds nothing is not a dead end. The backend records it as
 *   a documentation gap and the UI offers to raise a ticket, carrying the
 *   question across so it does not have to be typed twice.
 */

import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import {
  Search, BookOpen, GraduationCap, LifeBuoy, Upload, Download, Check,
  FileText, AlertTriangle, ExternalLink, ChevronLeft, Loader2, ShieldCheck,
  Inbox, CircleDot, CircleCheck, Users,
} from 'lucide-react'
import {
  getHelpCategories, getHelpArticles, getHelpArticle, acknowledgeHelpView,
  searchHelp, getTrainingDocuments, uploadTrainingDocument,
  trainingDocumentUrl, getTrainingRecords, getSupportConfig,
  getSupportCategories, getSupportTickets, raiseSupportTicket,
  collectSupportContext, getCurrentUser,
} from '../../lib/api'
import Markdown from '../../components/Markdown/Markdown'
import styles from './Help.module.css'

const TABS = [
  { key: 'browse',   label: 'Articles', icon: BookOpen },
  { key: 'training', label: 'Training', icon: GraduationCap },
  { key: 'support',  label: 'Support',  icon: LifeBuoy },
]

export default function HelpPage() {
  const [tab, setTab] = useState('browse')
  const [query, setQuery] = useState('')
  const [results, setResults] = useState(null)   // null = not searching
  const [searching, setSearching] = useState(false)
  const [deepSlug, setDeepSlug] = useState(null)

  // NextGen2 AI cites an article and links here, so a citation has to open the
  // article rather than dropping the reader on a category list to find it
  // themselves. Read from window.location rather than useSearchParams to
  // avoid forcing this page into a Suspense boundary for one query string.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const slug = params.get('article')
    const wanted = params.get('tab')
    if (slug) setDeepSlug(slug)
    if (wanted && TABS.some(t => t.key === wanted)) setTab(wanted)
  }, [])

  // Search is owned by the page rather than the Browse tab, so a result can
  // be opened without losing the query, and so the empty-result path can
  // hand the question to the support form.
  const runSearch = useCallback(async (q) => {
    const text = (q ?? query).trim()
    if (!text) { setResults(null); return }
    setSearching(true)
    try {
      const data = await searchHelp(text)
      setResults(data)
    } catch (e) {
      setResults({ query: text, results: [], error: e.message })
    } finally {
      setSearching(false)
    }
  }, [query])

  function clearSearch() {
    setQuery('')
    setResults(null)
  }

  if (deepSlug) {
    return (
      <div className={styles.page}>
        <ArticleReader slug={deepSlug} onBack={() => {
          setDeepSlug(null)
          window.history.replaceState({}, '', '/help')
        }} />
      </div>
    )
  }

  return (
    <div className={styles.page}>
      <div className={styles.head}>
        <div>
          <h1 className={styles.title}>Help and support</h1>
          <p className={styles.sub}>
            Search the guides, read the training material, or raise a ticket
            when neither covers it.
          </p>
        </div>
      </div>

      <SearchBar
        query={query} setQuery={setQuery} onSearch={runSearch}
        onClear={clearSearch} searching={searching}
      />

      {results ? (
        <SearchResults
          data={results} searching={searching}
          onClear={clearSearch}
          onRaiseTicket={() => { setTab('support') }}
        />
      ) : (
        <>
          <div className={styles.tabs} role="tablist">
            {TABS.map(t => {
              const Icon = t.icon
              return (
                <button key={t.key} role="tab" aria-selected={tab === t.key}
                  className={`${styles.tab} ${tab === t.key ? styles.tabOn : ''}`}
                  onClick={() => setTab(t.key)}>
                  <Icon size={14} strokeWidth={2} /> {t.label}
                </button>
              )
            })}
          </div>

          {tab === 'browse' && <BrowseTab />}
          {tab === 'training' && <TrainingTab />}
          {tab === 'support' && <SupportTab seedSummary={results?.query} />}
        </>
      )}
    </div>
  )
}


/* ── search ─────────────────────────────────────────────────────────── */

function SearchBar({ query, setQuery, onSearch, onClear, searching }) {
  return (
    <div className={styles.searchRow}>
      <div className={styles.searchBox}>
        <Search size={15} strokeWidth={2} />
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') onSearch() }}
          placeholder="How do I schedule a suite? What does a sync flag mean?"
          aria-label="Search help"
        />
        {query && (
          <button className={styles.clearBtn} onClick={onClear} aria-label="Clear search">
            Clear
          </button>
        )}
      </div>
      <button className="btn btn-primary" onClick={() => onSearch()} disabled={searching || !query.trim()}>
        {searching ? <Loader2 size={15} className={styles.spin} /> : <Search size={15} />}
        Search
      </button>
    </div>
  )
}

function SearchResults({ data, searching, onClear, onRaiseTicket }) {
  const [openSlug, setOpenSlug] = useState(null)

  if (searching) return <div className={styles.empty}>Searching…</div>
  if (openSlug) {
    return <ArticleReader slug={openSlug} onBack={() => setOpenSlug(null)} />
  }

  const hits = data.results || []

  return (
    <div className={styles.panel}>
      <div className={styles.resultHead}>
        <span>
          {hits.length
            ? `${hits.length} passage${hits.length === 1 ? '' : 's'} for “${data.query}”`
            : `Nothing found for “${data.query}”`}
        </span>
        <button className={styles.linkBtn} onClick={onClear}>Back to articles</button>
      </div>

      {data.error && <div className={styles.errorBox}>{data.error}</div>}

      {hits.length === 0 && !data.error && (
        // A dead end is where an in-product help centre loses people, so the
        // no-result path is a route onward rather than an apology.
        <div className={styles.noResult}>
          <AlertTriangle size={18} strokeWidth={1.8} />
          <div>
            <strong>The help content does not cover this yet.</strong>
            <p>
              Your question has been recorded so the missing article can be
              written. If you need an answer now, raise a ticket — the question
              is carried across.
            </p>
            <button className="btn btn-primary" onClick={onRaiseTicket}>
              <LifeBuoy size={15} /> Raise a ticket
            </button>
          </div>
        </div>
      )}

      {hits.map(hit => (
        <button key={hit.chunk_id} className={styles.hit}
          onClick={() => hit.source_type === 'article' && setOpenSlug(hit.label)}
          disabled={hit.source_type !== 'article'}>
          <div className={styles.hitTop}>
            <span className={styles.hitTitle}>{hit.heading || hit.title}</span>
            <span className={styles.hitSource}>
              {hit.source_type === 'article'
                ? <><BookOpen size={11} /> {hit.citation}</>
                : <><FileText size={11} /> {hit.title}</>}
            </span>
          </div>
          <p className={styles.hitBody}>{hit.body}</p>
        </button>
      ))}
    </div>
  )
}


/* ── browse ─────────────────────────────────────────────────────────── */

function BrowseTab() {
  const [categories, setCategories] = useState(null)
  const [articles, setArticles] = useState(null)
  const [slug, setSlug] = useState(null)

  useEffect(() => {
    getHelpCategories().then(setCategories).catch(() => setCategories([]))
    getHelpArticles().then(setArticles).catch(() => setArticles([]))
  }, [])

  const grouped = useMemo(() => {
    const map = new Map()
    for (const a of articles || []) {
      if (!map.has(a.category)) map.set(a.category, [])
      map.get(a.category).push(a)
    }
    // Follow the category order the backend returned, which is a teaching
    // order rather than alphabetical.
    return (categories || []).map(c => [c.category, map.get(c.category) || []])
  }, [articles, categories])

  if (slug) return <ArticleReader slug={slug} onBack={() => setSlug(null)} />

  if (!articles) return <div className={styles.empty}>Loading…</div>
  if (articles.length === 0) {
    return (
      <div className={styles.empty}>
        No help articles have been published yet.
      </div>
    )
  }

  return (
    <div className={styles.catGrid}>
      {grouped.map(([category, items]) => (
        <section key={category} className={styles.catCard}>
          <h2 className={styles.catTitle}>{category}</h2>
          <ul className={styles.artList}>
            {items.map(a => (
              <li key={a.id}>
                <button className={styles.artItem} onClick={() => setSlug(a.slug)}>
                  <span className={styles.artName}>
                    {a.title}
                    {a.status !== 'approved' && (
                      <span className={styles.draftTag}>Draft</span>
                    )}
                  </span>
                  {a.summary && <span className={styles.artSub}>{a.summary}</span>}
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}


/* ── article reader ─────────────────────────────────────────────────── */

function ArticleReader({ slug, onBack }) {
  const [article, setArticle] = useState(null)
  const [error, setError] = useState(null)
  const [ack, setAck] = useState(false)
  const [acking, setAcking] = useState(false)

  useEffect(() => {
    setArticle(null); setError(null); setAck(false)
    getHelpArticle(slug)
      .then(a => (a.error ? setError(a.error) : setArticle(a)))
      .catch(e => setError(e.message))
  }, [slug])

  async function acknowledge() {
    if (!article?.view_id) return
    setAcking(true)
    try {
      const r = await acknowledgeHelpView(article.view_id)
      if (!r.error) setAck(true)
    } catch { /* the article is still readable; the record simply is not written */ }
    finally { setAcking(false) }
  }

  if (error) return <div className={styles.panel}><div className={styles.errorBox}>{error}</div></div>
  if (!article) return <div className={styles.empty}>Loading…</div>

  return (
    <article className={styles.reader}>
      <button className={styles.backBtn} onClick={onBack}>
        <ChevronLeft size={15} /> Back
      </button>

      <header className={styles.readerHead}>
        <h1>{article.title}</h1>
        <div className={styles.readerMeta}>
          <span>{article.category}</span>
          <span>·</span>
          <span>Version {article.version}</span>
          {article.status === 'approved' ? (
            <span className={styles.approvedTag}>
              <ShieldCheck size={12} /> Approved
            </span>
          ) : (
            <span className={styles.draftTag}>Draft — not yet approved</span>
          )}
        </div>
      </header>

      <Markdown text={article.body_md} className={styles.prose} />

      {/* Reading records a view. This is the separate, deliberate claim that
          the reader understood it — which is what an audit asks for. */}
      <footer className={styles.ackBox}>
        {ack ? (
          <div className={styles.ackDone}>
            <Check size={15} strokeWidth={2.4} />
            Recorded — you have acknowledged version {article.version} of this article.
          </div>
        ) : (
          <>
            <div>
              <strong>Confirm you have read this</strong>
              <p>
                Opening an article records that you viewed it. Acknowledging
                records that you read and understood version {article.version},
                which is what appears in the training record.
              </p>
            </div>
            <button className="btn btn-primary" onClick={acknowledge}
              disabled={acking || !article.view_id}>
              {acking ? <Loader2 size={15} className={styles.spin} /> : <Check size={15} />}
              I have read and understood
            </button>
          </>
        )}
      </footer>
    </article>
  )
}


/* ── training ───────────────────────────────────────────────────────── */

function TrainingTab() {
  const [docs, setDocs] = useState(null)
  const [records, setRecords] = useState(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)
  const [mine, setMine] = useState(true)

  const load = useCallback(() => {
    getTrainingDocuments().then(setDocs).catch(() => setDocs([]))
  }, [])

  useEffect(load, [load])
  useEffect(() => {
    getTrainingRecords(mine ? { viewedBy: getCurrentUser() } : {})
      .then(setRecords).catch(() => setRecords([]))
  }, [mine])

  async function onUpload(e) {
    const file = e.target.files?.[0]
    if (!file) return
    setBusy(true); setNotice(null)
    try {
      const res = await uploadTrainingDocument(file)
      setNotice(res.warning
        ? { kind: 'warn', text: res.warning }
        : { kind: 'ok', text: `${file.name} indexed as ${res.chunks_indexed} searchable passage${res.chunks_indexed === 1 ? '' : 's'}.` })
      load()
    } catch (err) {
      setNotice({ kind: 'warn', text: err.message })
    } finally {
      setBusy(false)
      e.target.value = ''      // allow re-uploading the same file
    }
  }

  return (
    <>
      <div className={styles.panel}>
        <div className={styles.panelHead}>
          <div>
            <h2 className={styles.panelTitle}>Training material</h2>
            <p className={styles.panelSub}>
              Uploaded documents are searched alongside the articles and can be
              cited by NextGen2 AI.
            </p>
          </div>
          <label className={`btn btn-secondary ${styles.uploadBtn}`}>
            {busy ? <Loader2 size={15} className={styles.spin} /> : <Upload size={15} />}
            Upload
            <input type="file" accept=".pdf,.docx,.doc,.txt,.md"
              onChange={onUpload} disabled={busy} hidden />
          </label>
        </div>

        {notice && (
          <div className={notice.kind === 'ok' ? styles.okBox : styles.errorBox}>
            {notice.text}
          </div>
        )}

        {!docs ? <div className={styles.empty}>Loading…</div>
          : docs.length === 0 ? <div className={styles.empty}>No training documents uploaded yet.</div>
          : (
            <table className="table">
              <thead>
                <tr><th>Document</th><th>Indexed</th><th>Uploaded</th><th /></tr>
              </thead>
              <tbody>
                {docs.map(d => (
                  <tr key={d.id}>
                    <td><FileText size={13} className={styles.rowIcon} /> {d.original_filename}</td>
                    <td>
                      {d.chunk_count > 0
                        ? <span className="badge badge-green">{d.chunk_count} passages</span>
                        /* Zero passages means no text could be extracted —
                           usually a scanned PDF. Saying so beats letting the
                           upload look successful while answering nothing. */
                        : <span className="badge badge-amber">Not searchable</span>}
                    </td>
                    <td>{fmtDate(d.uploaded_at)}</td>
                    <td className={styles.rowRight}>
                      <a className={styles.linkBtn} href={trainingDocumentUrl(d.id)}
                         target="_blank" rel="noreferrer">
                        <Download size={13} /> Download
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div>

      <div className={styles.panel}>
        <div className={styles.panelHead}>
          <div>
            <h2 className={styles.panelTitle}>Training record</h2>
            <p className={styles.panelSub}>
              What was read, at which version, and what was acknowledged.
            </p>
          </div>
          <label className={styles.toggle}>
            <input type="checkbox" checked={mine} onChange={e => setMine(e.target.checked)} />
            Only mine
          </label>
        </div>

        {!records ? <div className={styles.empty}>Loading…</div>
          : records.length === 0 ? <div className={styles.empty}>Nothing recorded yet.</div>
          : (
            <table className="table">
              <thead>
                <tr><th>Read</th><th>Who</th><th>Version</th><th>Acknowledged</th><th>When</th></tr>
              </thead>
              <tbody>
                {records.slice(0, 100).map(r => (
                  <tr key={r.id}>
                    <td>{r.source_title}</td>
                    <td>{r.viewed_by}</td>
                    <td>{r.article_version ? `v${r.article_version}` : '—'}</td>
                    <td>
                      {r.acknowledged
                        ? <span className="badge badge-green">Yes</span>
                        : <span className="badge badge-gray">Viewed only</span>}
                    </td>
                    <td>{fmtDate(r.acknowledged_at || r.viewed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div>
    </>
  )
}


/* ── support ────────────────────────────────────────────────────────── */

function SupportTab({ seedSummary }) {
  const [config, setConfig] = useState(null)
  const [categories, setCategories] = useState([])
  const [tickets, setTickets] = useState(null)
  // Defaults to everyone. NextGen2 is a shared platform with a handful of
  // users, so the useful first view is what has been reported at all — a
  // personal list that reads "you have not raised any tickets" tells a new
  // user nothing and hides tickets someone else already raised about the
  // problem they are about to report.
  const [scope, setScope] = useState('all')          // 'mine' | 'all'
  const [form, setForm] = useState({ category: 'how-to', summary: seedSummary || '', description: '' })
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [highlightId, setHighlightId] = useState(null)

  const resultRef = useRef(null)
  const me = getCurrentUser()

  const loadTickets = useCallback(() => {
    const opts = scope === 'mine' ? { raisedBy: me } : {}
    getSupportTickets(opts).then(setTickets).catch(() => setTickets([]))
  }, [scope, me])

  useEffect(() => {
    getSupportConfig().then(setConfig).catch(() => setConfig(null))
    getSupportCategories().then(setCategories).catch(() => setCategories([]))
  }, [])

  useEffect(loadTickets, [loadTickets])

  // Counts come from the rows already loaded rather than a second request:
  // one source, so a figure can never disagree with the table beneath it.
  const counts = useMemo(() => {
    const list = tickets || []
    return {
      total: list.length,
      open: list.filter(t => ['submitted', 'open'].includes(t.status)).length,
      failed: list.filter(t => t.status === 'failed').length,
      closed: list.filter(t => ['resolved', 'closed'].includes(t.status)).length,
    }
  }, [tickets])

  async function submit() {
    if (!form.summary.trim() || busy) return
    setBusy(true); setResult(null)
    try {
      const res = await raiseSupportTicket({
        category: form.category,
        summary: form.summary.trim(),
        description: form.description.trim(),
        context: collectSupportContext(),
      })
      setResult(res)
      if (!res.error) {
        setForm(f => ({ ...f, summary: '', description: '' }))
        setHighlightId(res.id)
        // A confirmation the user has to go looking for is not a
        // confirmation. Bring it into view and mark the new row, so pressing
        // the button visibly does something even when the table is below the
        // fold.
        setTimeout(() => resultRef.current?.scrollIntoView({
          behavior: 'smooth', block: 'center',
        }), 60)
        setTimeout(() => setHighlightId(null), 6000)
      }
      loadTickets()
    } catch (e) {
      setResult({ error: e.message })
    } finally {
      setBusy(false)
    }
  }

  const chosen = categories.find(c => c.key === form.category)

  return (
    <>
      {/* Counters sit above the form so the state of your tickets is the
          first thing on the tab, not something found by scrolling past it. */}
      <div className={styles.statRow}>
        <Stat icon={Inbox}       label={scope === 'mine' ? 'Your tickets' : 'All tickets'} value={counts.total} />
        <Stat icon={CircleDot}   label="Open"      value={counts.open} tone="accent" />
        <Stat icon={CircleCheck} label="Closed"    value={counts.closed} tone="green" />
        <Stat icon={AlertTriangle} label="Not filed" value={counts.failed}
              tone={counts.failed ? 'red' : undefined}
            hint={counts.failed ? 'Saved in NextGen2 but not created in the ticketing system' : undefined} />
      </div>

      <div className={styles.panel}>
        <div className={styles.panelHead}>
          <div>
            <h2 className={styles.panelTitle}>Raise a ticket</h2>
            <p className={styles.panelSub}>
              The screen you were on, your browser and the last error are
              attached automatically — you do not need to gather them.
            </p>
          </div>
        </div>

        {/* A misconfigured ticketing system is worth saying before someone
            writes out a problem, not after they press Send. */}
        {config && !config.configured && (
          <div className={styles.errorBox}>
            <AlertTriangle size={14} /> Ticketing is not configured
            {config.reason ? `: ${config.reason}` : '.'} Your ticket will still
            be saved in NextGen2.
          </div>
        )}

        <div className={styles.formGrid}>
          <label className={styles.field}>
            <span>What kind of problem?</span>
            <select className="form-select" value={form.category}
              onChange={e => setForm(f => ({ ...f, category: e.target.value }))}>
              {categories.map(c => <option key={c.key} value={c.key}>{c.label}</option>)}
            </select>
            {chosen?.hint && <small className="form-hint">{chosen.hint}</small>}
          </label>

          <label className={styles.field}>
            <span>Summary</span>
            <input className="form-input" value={form.summary}
              onChange={e => setForm(f => ({ ...f, summary: e.target.value }))}
              placeholder="One line — what went wrong, or what you are trying to do" />
          </label>

          <label className={`${styles.field} ${styles.fieldWide}`}>
            <span>What happened?</span>
            <textarea className="form-textarea" rows={5} value={form.description}
              onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
              placeholder="What you did, what you expected, and what happened instead." />
          </label>
        </div>

        <div className={styles.formFoot}>
          <button className="btn btn-primary" onClick={submit}
            disabled={busy || !form.summary.trim()}>
            {busy ? <Loader2 size={15} className={styles.spin} /> : <LifeBuoy size={15} />}
            {busy ? 'Raising…' : 'Raise ticket'}
          </button>
          {config?.provider && (
            <span className={styles.footNote}>Filed in {config.provider}</span>
          )}
        </div>

        <div ref={resultRef}>
          {result && (
            <div className={result.error ? styles.errorBox : styles.okBox}>
              {result.error
                ? <>{result.error}</>
                : <>Ticket {result.external_ref || `#${result.id}`} raised.{' '}
                    {result.external_url && (
                      <a href={result.external_url} target="_blank" rel="noreferrer">
                        Open it <ExternalLink size={11} />
                      </a>
                    )}</>}
            </div>
          )}
        </div>
      </div>

      <div className={styles.panel}>
        <div className={styles.panelHead}>
          <div>
            <h2 className={styles.panelTitle}>
              {scope === 'mine' ? 'Your tickets' : 'All tickets'}
            </h2>
            <p className={styles.panelSub}>
              {scope === 'mine'
                ? <>Raised from NextGen2 as <strong>{me}</strong>.</>
                : <>Everything raised from NextGen2 by anyone.</>}
            </p>
          </div>

          {/* A filter, not a permission. NextGen2 has no authentication, so
              this changes what is shown and nothing else — saying otherwise
              would imply a control that does not exist. */}
          <div className={styles.scopeSwitch} role="group" aria-label="Ticket scope">
            <button className={scope === 'mine' ? styles.scopeOn : ''}
              onClick={() => setScope('mine')}>Mine</button>
            <button className={scope === 'all' ? styles.scopeOn : ''}
              onClick={() => setScope('all')}>
              <Users size={12} /> Everyone
            </button>
          </div>
        </div>

        {scope === 'all' && (
          <p className={styles.scopeNote}>
            Showing everyone&rsquo;s tickets. This is a view filter — NextGen2 has
            no sign-in yet, so it is not access control.
          </p>
        )}

        {!tickets ? <div className={styles.empty}>Loading…</div>
          : tickets.length === 0 ? (
            <div className={styles.empty}>
              {scope === 'mine'
                ? 'You have not raised any tickets.'
                : 'No tickets have been raised yet.'}
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Reference</th><th>Summary</th>
                  {scope === 'all' && <th>Raised by</th>}
                  <th>Kind</th><th>Status</th><th>Raised</th>
                </tr>
              </thead>
              <tbody>
                {tickets.map(t => (
                  <tr key={t.id} className={t.id === highlightId ? styles.newRow : undefined}>
                    <td>
                      {t.external_url
                        ? <a href={t.external_url} target="_blank" rel="noreferrer">
                            {t.external_ref} <ExternalLink size={11} />
                          </a>
                        : (t.external_ref || `#${t.id}`)}
                    </td>
                    <td>{t.summary}</td>
                    {scope === 'all' && <td>{t.raised_by}</td>}
                    <td>{t.category}</td>
                    <td>
                      {t.status === 'submitted' ? <span className="badge badge-green">Submitted</span>
                        : t.status === 'failed' ? <span className="badge badge-red" title={t.submit_error}>Not filed</span>
                        : ['resolved', 'closed'].includes(t.status) ? <span className="badge badge-gray">{t.status}</span>
                        : <span className="badge badge-amber">{t.status}</span>}
                    </td>
                    <td>{fmtDate(t.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div>
    </>
  )
}


/**
 * One counter above the support form.
 *
 * @param {object} props
 * @param {Function} props.icon - A lucide icon component.
 * @param {string} props.label - What is being counted.
 * @param {number} props.value - The count.
 * @param {string} [props.tone] - 'accent' | 'green' | 'red' for emphasis.
 * @param {string} [props.hint] - Tooltip explaining a figure that needs it.
 */
function Stat({ icon: Icon, label, value, tone, hint }) {
  return (
    <div className={`${styles.stat} ${tone ? styles['stat_' + tone] : ''}`} title={hint}>
      <div className={styles.statTop}>
        <Icon size={13} strokeWidth={2} />
        <span>{label}</span>
      </div>
      <div className={styles.statValue}>{value}</div>
    </div>
  )
}


/* ── helpers ────────────────────────────────────────────────────────── */

function fmtDate(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(undefined, {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}
