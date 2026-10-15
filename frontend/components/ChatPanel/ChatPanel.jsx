'use client'

/**
 * components/ChatPanel/ChatPanel.jsx
 *
 * "Next Gen AI" — the right-side copilot panel (the industry-standard
 * pattern: available from any page, keeps your context, doesn't take
 * over the screen).
 *
 * Answers come from the LOCAL model, grounded in real database facts —
 * the backend gathers the numbers via SQL first, so nothing is invented.
 */

import { useState, useEffect, useRef } from 'react'
import { Sparkles, Send, X, Clock, List, Play, Check, AlertTriangle, Trash2,
         BookOpen, FileText, LifeBuoy, ExternalLink, Loader2 } from 'lucide-react'
import Markdown from '../Markdown/Markdown'
import { askKavach, getChatSuggestions, interpretInstruction, executeAction, resummarise,
         raiseSupportTicket, collectSupportContext } from '../../lib/api'
import styles from './ChatPanel.module.css'


/** ISO -> the value a datetime-local input expects, in local time. */
function toLocalInput(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const pad = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
         `T${pad(d.getHours())}:${pad(d.getMinutes())}`
}


/** One editable field on a proposal, chosen by name. */
function EditableField({ field, proposal, onChange }) {
  if (field === 'frequency') {
    return (
      <label className={styles.editField}>
        <span>Repeat</span>
        <select value={proposal.frequency || 'once'}
                onChange={e => onChange(e.target.value)}>
          <option value="once">Once</option>
          <option value="hourly">Every hour</option>
          <option value="daily">Every day</option>
          <option value="weekdays">Every weekday</option>
          <option value="weekly">Every week</option>
        </select>
      </label>
    )
  }
  if (field === 'run_at') {
    return (
      <label className={styles.editField}>
        <span>First run</span>
        <input type="datetime-local" value={toLocalInput(proposal.run_at)}
               onChange={e => onChange(new Date(e.target.value).toISOString())} />
      </label>
    )
  }
  return null
}


/**
 * Where the conversation is kept between visits.
 *
 * This is deliberately temporary. The right home is the database: once the
 * assistant can create a schedule or run a suite, "who instructed it and
 * who approved" belongs in the audit trail alongside
 * test_case_proposals.accepted_by and healing_proposals.decided_by. Browser
 * storage cannot answer that — it is one laptop, invisible to anyone else.
 *
 * What it does give: survives refresh, browser restart and server restart,
 * and is shared across tabs.
 */
const STORE_KEY = 'kavach-chat-history'
const MAX_STORED = 60          // enough to scroll back through, small enough to write

function loadHistory() {
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []                  // corrupt or unavailable — start clean rather than crash
  }
}

function saveHistory(messages) {
  try {
    // A proposal that was never decided is not carried over: the action it
    // describes may no longer be valid, and offering a stale "Yes, do it"
    // after a reload would be worse than losing it.
    const keep = messages
      .filter(m => !(m.proposal && !m.done && !m.failed && !m.cancelled))
      .slice(-MAX_STORED)
    localStorage.setItem(STORE_KEY, JSON.stringify(keep))
  } catch {
    // Quota exceeded or storage disabled. The conversation still works in
    // this tab; it just will not survive a reload.
  }
}

// Panel width. The default is comfortable for a question; a long procedural
// answer with numbered steps needs more, and the person reading it knows how
// much better than a default does.
const MIN_W = 340
const DEFAULT_W = 380

// The sidebar's width, and the least the page content may be squeezed to.
// Below roughly 560px the tables on Executions and Reports start wrapping
// into unreadability, which is worse than a narrower assistant.
const SIDEBAR_W = 244
const MIN_CONTENT_W = 560

/**
 * The widest the panel may be, given the window.
 *
 * Derived from what the PAGE needs rather than from a share of the viewport.
 * A percentage cap looks reasonable and still starves a laptop: 45% of
 * 1280px is 576px, which leaves 460px of content beside a 244px sidebar —
 * exactly the cramped layout this is meant to prevent.
 *
 * @returns {number} Maximum panel width in pixels.
 */
function maxWidth() {
  if (typeof window === 'undefined') return 560
  const available = window.innerWidth - SIDEBAR_W - MIN_CONTENT_W
  return Math.max(MIN_W, Math.min(900, available))
}


export default function ChatPanelV2() {
  // Visible by default; closing it leaves a small button to bring it back.
  const [open, setOpen] = useState(true)

  // Remembered per browser: someone who widens the panel to read a procedure
  // wants it wide next time too.
  const [width, setWidth] = useState(DEFAULT_W)
  const draggingRef = useRef(false)

  useEffect(() => {
    try {
      const saved = parseInt(localStorage.getItem('kavach-chat-width') || '', 10)
      // Clamped on read as well as on write: a width saved on a wide monitor
      // must not cripple the layout when the same person opens a laptop.
      if (saved >= MIN_W) setWidth(Math.min(saved, maxWidth()))
    } catch { /* storage disabled — the default is fine */ }
  }, [])

  // Dragging is bound to the document, not the handle, so the pointer can
  // leave the 6px strip mid-drag without the panel sticking.
  useEffect(() => {
    function move(e) {
      if (!draggingRef.current) return
      const next = Math.min(maxWidth(), Math.max(MIN_W, window.innerWidth - e.clientX))
      setWidth(next)
    }
    function up() {
      if (!draggingRef.current) return
      draggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      try { localStorage.setItem('kavach-chat-width', String(widthRef.current)) } catch {}
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
  }, [])

  const widthRef = useRef(DEFAULT_W)
  useEffect(() => { widthRef.current = width }, [width])

  useEffect(() => {
    const clamp = () => setWidth(w => Math.min(w, maxWidth()))
    window.addEventListener('resize', clamp)
    return () => window.removeEventListener('resize', clamp)
  }, [])

  function startDrag() {
    draggingRef.current = true
    // Set on the body so the cursor holds while dragging over other elements.
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }
  const [messages, setMessages] = useState([])
  const [restored, setRestored] = useState(false)
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [suggestions, setSuggestions] = useState([])

  // Restore once, on mount. Guarded so the empty initial state is never
  // written back over a real history.
  useEffect(() => {
    setMessages(loadHistory())
    setRestored(true)
  }, [])

  useEffect(() => {
    if (restored) saveHistory(messages)
  }, [messages, restored])

  // Another tab wrote to storage. Without this the tabs share the data but
  // not the view, and they drift apart until one is reloaded.
  useEffect(() => {
    function onStorage(e) {
      if (e.key !== STORE_KEY) return
      setMessages(loadHistory())
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])
  const endRef = useRef(null)

  useEffect(() => {
    getChatSuggestions().then(d => setSuggestions(d.suggestions || [])).catch(() => {})
  }, [])

  /**
   * Push the page content aside instead of covering it. On narrow screens
   * there isn't room to push, so the panel overlays instead.
   *
   * The padding must track the panel's ACTUAL width. It used to be a
   * constant 380px, which was correct while the panel could not be resized
   * and silently wrong the moment it could: widening the panel then covered
   * the page rather than making room, and the content underneath was simply
   * hidden. Any fixed-position panel that pushes content has to keep these
   * two numbers in step.
   *
   * No transition while dragging — animating padding against a mousemove
   * makes the content lag behind the edge being dragged.
   */
  useEffect(() => {
    const apply = () => {
      const roomToPush = window.innerWidth >= 1200
      document.body.style.transition = draggingRef.current
        ? 'none' : 'padding-right .22s ease'
      document.body.style.paddingRight = open && roomToPush ? `${width}px` : ''
    }
    apply()
    window.addEventListener('resize', apply)
    return () => {
      window.removeEventListener('resize', apply)
      document.body.style.paddingRight = ''
    }
  }, [open, width])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, busy])

  /**
   * Send a message.
   *
   * An instruction ("run the smoke suite") is INTERPRETED first, which
   * executes nothing — it returns a proposal for the user to confirm. A
   * question goes to the existing read-only endpoint, unchanged.
   */
  async function send(text) {
    const q = (text ?? input).trim()
    if (!q || busy) return
    setInput('')
    setMessages(m => [...m, { role: 'user', text: q }])
    setBusy(true)
    try {
      const intent = await interpretInstruction(q)

      if (intent && !intent.not_an_action) {
        if (intent.proposal) {
          // Show what will happen and wait. Nothing has run.
          setMessages(m => [...m, {
            role: 'ai', proposal: intent.proposal, summary: intent.summary,
            confirmFields: intent.confirm_fields || [],
          }])
          return
        }
        if (intent.needs_clarification) {
          setMessages(m => [...m, {
            role: 'ai', text: intent.question, options: intent.options,
          }])
          return
        }
        if (intent.error) {
          setMessages(m => [...m, {
            role: 'ai', text: intent.error + (intent.detail ? ` ${intent.detail}` : ''),
            error: true,
          }])
          return
        }
      }

      const res = await askKavach(q)
      setMessages(m => [...m, {
        role: 'ai', text: res.answer, facts: res.facts_used, stats: res.stats,
        // Carried so the answer can show what it was built from, and so the
        // panel can offer a ticket at the moment the help content fell short
        // rather than telling the user to go and find the Help screen.
        sources: res.sources || [], helpGap: !!res.help_gap, question: q,
      }])
    } catch (e) {
      setMessages(m => [...m, { role: 'ai', text: `Couldn't reach the assistant: ${e.message}`, error: true }])
    } finally {
      setBusy(false)
    }
  }

  /** Carry out a proposal the user has confirmed. */
  async function confirmAction(idx, proposal) {
    setMessages(m => m.map((msg, i) => i === idx ? { ...msg, running: true } : msg))
    try {
      const res = await executeAction(proposal)
      setMessages(m => m.map((msg, i) => i === idx
        ? { ...msg, running: false, done: !res.error, result: res.message || res.error,
            failed: !!res.error }
        : msg))
    } catch (e) {
      setMessages(m => m.map((msg, i) => i === idx
        ? { ...msg, running: false, failed: true, result: e.message } : msg))
    }
  }

  /**
   * Change a value on a proposal before it is confirmed.
   *
   * The schedule's NAME is derived from its frequency, so it has to be
   * recomputed here too — otherwise changing "weekdays" to "once" left a
   * schedule called "Smoke Suite 1 — weekdays" that runs once.
   */
  async function editProposal(idx, field, value) {
    let next
    setMessages(m => m.map((msg, i) => {
      if (i !== idx) return msg
      next = { ...msg.proposal, [field]: value }
      return { ...msg, proposal: next }
    }))
    // The sentence is rebuilt by the backend so the wording — and any value
    // derived from these fields, like the schedule's name — comes from one
    // place rather than being duplicated here.
    try {
      const r = await resummarise(next)
      if (r?.summary) {
        setMessages(m => m.map((msg, i) => i === idx ? { ...msg, summary: r.summary } : msg))
      }
    } catch { /* the card still shows the previous sentence */ }
  }

  function cancelAction(idx) {
    setMessages(m => m.map((msg, i) => i === idx
      ? { ...msg, cancelled: true } : msg))
  }

  function onKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  if (!open) {
    return (
      <button className={styles.reopen} onClick={() => setOpen(true)} title="Ask NextGen2 AI">
        <Sparkles size={16} strokeWidth={2} />
        Ask NextGen2
      </button>
    )
  }

  return (
    <aside className={styles.panel} style={{ width }} aria-label="NextGen2 AI">
      {/* Drag to resize. Keyboard users get the same range through the arrow
          keys, since a mouse-only control is not a control for everyone. */}
      <div
        className={styles.resizer}
        onMouseDown={startDrag}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the assistant panel"
        tabIndex={0}
        onKeyDown={e => {
          if (e.key === 'ArrowLeft') setWidth(w => Math.min(maxWidth(), w + 24))
          if (e.key === 'ArrowRight') setWidth(w => Math.max(MIN_W, w - 24))
        }}
      />
      <div className={styles.head}>
        <div className={styles.orb}>
          <Sparkles size={16} strokeWidth={2} />
        </div>
        <div className={styles.headText}>
          <div className={styles.title}>NextGen2 AI</div>
          <div className={styles.subtitle}>Powered by Netra (Coming Soon)</div>
        </div>
        <button
            className={styles.close}
            onClick={() => { setMessages([]); try { localStorage.removeItem(STORE_KEY) } catch {} }}
            title="Clear this conversation"
            aria-label="Clear conversation"
          >
            <Trash2 size={14} />
          </button>
          <button className={styles.close} onClick={() => setOpen(false)} aria-label="Close"><X size={15} /></button>
      </div>

      <div className={styles.body}>
        {messages.length === 0 ? (
          <div className={styles.hero}>
            <div className={styles.heroOrb}>
              <Sparkles size={27} strokeWidth={1.8} />
            </div>
            <h2>Ask about your tests</h2>
            <p>Coverage, failures, flaky tests — answered from your real data.</p>
            <div className={styles.suggestions}>
              {suggestions.map((s, i) => (
                <button key={i} className={styles.suggestion} onClick={() => send(s.title)}>
                  <strong>{s.title}</strong>
                  <span>{s.sub}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className={styles.thread}>
            {messages.map((m, i) => (
              <div key={i} className={m.role === 'user' ? styles.userMsg : styles.aiMsg}>
                {m.role === 'ai' && <div className={styles.aiTag}>NextGen2 AI</div>}
                {m.text && (
                  m.role === 'ai' && !m.error
                    // Model replies arrive as markdown — numbered steps, bold
                    // field names, bare URLs. Rendered as raw text they read
                    // as a wall of asterisks with no line breaks, which is
                    // indistinguishable from a wrong answer.
                    ? <Markdown text={m.text} className={styles.prose} />
                    : <div className={m.error ? styles.errText : undefined}>{m.text}</div>
                )}

                {/* A proposal is shown, never performed. Nothing has run at
                    this point — the user decides. */}
                {m.proposal && (
                  <div className={styles.actionCard}>
                    <div className={styles.actionHead}>
                      <Play size={13} strokeWidth={2} />
                      About to do this
                    </div>
                    <div className={styles.actionSummary}>{m.summary}</div>

                    {/* A schedule is a commitment, so the time and frequency
                        are editable here rather than take-it-or-leave-it.
                        The model's reading is a starting point. */}
                    {m.confirmFields?.length > 0 &&
                     !m.done && !m.failed && !m.cancelled && (
                      <div className={styles.actionEdit}>
                        {m.confirmFields.map(f => (
                          <EditableField key={f} field={f} proposal={m.proposal}
                                         onChange={v => editProposal(i, f, v)} />
                        ))}
                      </div>
                    )}

                    {m.done && (
                      <div className={styles.actionDone}>
                        <Check size={13} strokeWidth={2} />
                        <Markdown text={m.result} className={styles.resultProse} />
                      </div>
                    )}
                    {m.failed && (
                      <div className={styles.actionFailed}>
                        <AlertTriangle size={13} strokeWidth={2} />
                        <Markdown text={m.result} className={styles.resultProse} />
                      </div>
                    )}
                    {m.cancelled && (
                      <div className={styles.actionCancelled}>Cancelled — nothing was changed.</div>
                    )}

                    {!m.done && !m.failed && !m.cancelled && (
                      <div className={styles.actionBtns}>
                        <button className={styles.actionGo} disabled={m.running}
                                onClick={() => confirmAction(i, m.proposal)}>
                          {m.running ? 'Working…' : 'Yes, do it'}
                        </button>
                        <button className={styles.actionNo} disabled={m.running}
                                onClick={() => cancelAction(i)}>
                          Cancel
                        </button>
                      </div>
                    )}
                  </div>
                )}

                {/* Ambiguous instruction — ask rather than guess. */}
                {m.options && (
                  <div className={styles.optionList}>
                    {m.options.map((o, k) => (
                      <button key={k} className={styles.optionChip} onClick={() => send(o)}>
                        {o}
                      </button>
                    ))}
                  </div>
                )}

                {m.sources?.length > 0 && <Sources sources={m.sources} />}

                {m.role === 'ai' && m.question && !m.proposal && (
                  <TicketFromChat
                    question={m.question}
                    answer={m.text}
                    prominent={m.helpGap || (m.sources || []).length === 0}
                  />
                )}

                {(m.facts || m.stats) && <MessageMeta facts={m.facts} stats={m.stats} />}
              </div>
            ))}
            {busy && (
              <div className={styles.aiMsg}>
                <div className={styles.aiTag}>NextGen2 AI</div>
                <div className={styles.thinking}><span /><span /><span /></div>
              </div>
            )}
            <div ref={endRef} />
          </div>
        )}
      </div>

      <div className={styles.inputWrap}>
        <div className={styles.inputBox}>
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Ask about coverage, failures, health…"
            rows={1}
          />
          <button className={styles.send} onClick={() => send()} disabled={busy || !input.trim()} aria-label="Send">
            <Send size={15} strokeWidth={2} />
          </button>
        </div>
        <div className={styles.disclaimer}>Answers come from your database. Verify before acting.</div>
      </div>
    </aside>
  )
}

/**
 * Compact footer under each answer:
 *  - a stats chip (latency + tokens + model) — click to expand
 *  - a link to the exact figures the answer was based on, so any number
 *    can be traced back to the database (useful for audit)
 */
function MessageMeta({ facts, stats }) {
  const [showFacts, setShowFacts] = useState(false)
  const [showStats, setShowStats] = useState(false)

  const secs = stats?.latency_ms ? (stats.latency_ms / 1000).toFixed(1) : null

  return (
    <div className={styles.meta}>
      <div className={styles.metaRow}>
        {stats && (
          <button className={styles.metaChip} onClick={() => setShowStats(!showStats)} title="Model, latency and tokens">
            <Clock size={11} strokeWidth={2} />
            {secs ? `${secs}s` : 'stats'}
          </button>
        )}
        {facts && Object.keys(facts).length > 0 && (
          <button className={styles.metaChip} onClick={() => setShowFacts(!showFacts)} title="The data this answer used">
            <List size={11} strokeWidth={2} />
            Source data
          </button>
        )}
      </div>

      {showStats && stats && (
        <div className={styles.statsBox}>
          <div><span>Model</span><code>{stats.model || '—'}</code></div>
          <div><span>Latency</span><code>{secs ? `${secs}s` : '—'}</code></div>
          <div><span>Input tokens</span><code>{stats.input_tokens ?? '—'}</code></div>
          <div><span>Output tokens</span><code>{stats.output_tokens ?? '—'}</code></div>
          <div className={styles.statsNote}>Runs locally — no tokens billed, no data sent externally.</div>
        </div>
      )}

      {showFacts && facts && Object.keys(facts).length > 0 && (
        <pre className={styles.factsPre}>{JSON.stringify(facts, null, 2)}</pre>
      )}
    </div>
  )
}

/**
 * The help passages an answer was built from.
 *
 * Shown because a cited answer and an uncited one look identical otherwise,
 * and the difference matters: an answer with sources can be checked against
 * an approved article at a known version, which is what makes it usable in a
 * regulated setting. Each chip carries the version for the same reason — "we
 * told you that in v2" is a different claim from "we told you that".
 *
 * Clicking opens the article on the Help screen rather than expanding text
 * here: the article is the authority, so it should be read where it lives.
 */
function Sources({ sources }) {
  // The same article often supplies several passages. Collapse them, so the
  // row reads as two sources rather than five chips repeating one title.
  const unique = []
  const seen = new Set()
  for (const s of sources) {
    const key = `${s.source_type}:${s.label}:${s.version ?? ''}`
    if (seen.has(key)) continue
    seen.add(key)
    unique.push(s)
  }

  return (
    <div className={styles.sources}>
      <div className={styles.sourcesLabel}>Based on</div>
      <div className={styles.sourceChips}>
        {unique.map((s, i) => (
          s.source_type === 'article' ? (
            <a key={i} className={styles.sourceChip}
               href={`/help?article=${encodeURIComponent(s.label)}`}>
              <BookOpen size={11} strokeWidth={2} />
              {s.title}{s.version ? ` · v${s.version}` : ''}
            </a>
          ) : (
            <span key={i} className={styles.sourceChip}>
              <FileText size={11} strokeWidth={2} />
              {s.title}
            </span>
          )
        ))}
      </div>
    </div>
  )
}


/**
 * Raise a support ticket without leaving the conversation.
 *
 * The point of a copilot is that you do not have to go somewhere else. Being
 * told "raise a ticket from the Help screen" by the thing you are already
 * talking to is the moment the pattern breaks, so the form is here.
 *
 * The question and the answer are carried into the ticket automatically. What
 * the user asked, and what Kavach told them, is most of what whoever picks
 * the ticket up needs — and re-typing it is exactly the friction that stops
 * people reporting gaps at all.
 *
 * `prominent` distinguishes the two situations: when the help content fell
 * short this is the obvious next step and is shown as a button; otherwise it
 * is a quiet line, so a good answer is not followed by a nag.
 */
function TicketFromChat({ question, answer, prominent }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)

  async function raise() {
    if (busy) return
    setBusy(true)
    try {
      const res = await raiseSupportTicket({
        // Asked in the assistant and unanswered: a how-to, not a defect.
        category: 'how-to',
        summary: question.slice(0, 180),
        description:
          `Asked NextGen2 AI:\n${question}\n\n` +
          `NextGen2 AI replied:\n${answer || '(no answer)'}\n\n` +
          (detail.trim() ? `The person added:\n${detail.trim()}\n` : ''),
        context: collectSupportContext({ source: 'kavach-ai', question }),
      })
      setResult(res)
    } catch (e) {
      setResult({ error: e.message })
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className={result.error ? styles.ticketFailed : styles.ticketDone}>
        {result.error ? (
          <><AlertTriangle size={12} strokeWidth={2} /> {result.error}</>
        ) : (
          <>
            <Check size={12} strokeWidth={2.4} />
            Ticket {result.external_ref || `#${result.id}`} raised.
            {result.external_url && (
              <a href={result.external_url} target="_blank" rel="noreferrer">
                Open <ExternalLink size={10} />
              </a>
            )}
          </>
        )}
      </div>
    )
  }

  if (!open) {
    return (
      <button
        className={prominent ? styles.ticketCta : styles.ticketLink}
        onClick={() => setOpen(true)}>
        <LifeBuoy size={prominent ? 13 : 11} strokeWidth={2} />
        {prominent ? 'Raise a ticket about this' : 'Not what you needed?'}
      </button>
    )
  }

  return (
    <div className={styles.ticketBox}>
      <div className={styles.ticketHead}>
        <LifeBuoy size={12} strokeWidth={2} /> Raise a ticket
      </div>
      <p className={styles.ticketNote}>
        Your question and this answer are attached automatically.
      </p>
      <textarea
        className={styles.ticketInput}
        value={detail}
        onChange={e => setDetail(e.target.value)}
        placeholder="Anything to add? (optional)"
        rows={2}
      />
      <div className={styles.ticketBtns}>
        <button className={styles.ticketGo} onClick={raise} disabled={busy}>
          {busy ? <Loader2 size={12} className={styles.spin} /> : null}
          {busy ? 'Raising…' : 'Raise ticket'}
        </button>
        <button className={styles.ticketNo} onClick={() => setOpen(false)} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  )
}