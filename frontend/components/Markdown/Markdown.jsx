'use client'

/**
 * components/Markdown/Markdown.jsx
 *
 * A deliberately small markdown renderer, shared by the help article reader
 * and the chat panel.
 *
 * It exists because both places need the same thing and neither should carry
 * a markdown library. The articles are authored in this repository and
 * reviewed in a diff; the chat text comes from a model that was told to write
 * plain prose and lists. Between them they use headings, bullets, numbered
 * steps, bold, and inline code — that is the whole grammar, and it is about
 * sixty lines.
 *
 * The chat panel matters more than it looks. Rendering a model's reply as raw
 * text turns "1. **Create the suite** — Open Applications" into a wall with
 * literal asterisks and no line breaks, because HTML collapses newlines. The
 * answer can be correct and still unreadable, which is indistinguishable from
 * being wrong.
 *
 * Nothing here uses dangerouslySetInnerHTML. Text is rendered as React nodes,
 * so neither an article nor a model reply can inject markup.
 */

import { useMemo } from 'react'

export default function Markdown({ text, className }) {
  const blocks = useMemo(() => parseMarkdown(text || ''), [text])
  return (
    <div className={className}>
      {blocks.map((b, i) => {
        if (b.type === 'h2') return <h2 key={i}>{inline(b.text)}</h2>
        if (b.type === 'h3') return <h3 key={i}>{inline(b.text)}</h3>
        if (b.type === 'ul') return <ul key={i}>{b.items.map((t, j) => <li key={j}>{inline(t)}</li>)}</ul>
        if (b.type === 'ol') return <ol key={i}>{b.items.map((t, j) => <li key={j}>{inline(t)}</li>)}</ol>
        return <p key={i}>{inline(b.text)}</p>
      })}
    </div>
  )
}

/**
 * Split markdown into blocks.
 *
 * @param {string} src - Markdown text.
 * @returns {Array<object>} Blocks in document order.
 */
export function parseMarkdown(src) {
  // A local model often emits a whole list on one line: "1. Do this. 2. Then
  // that." Splitting only on newlines would render that as one paragraph, so
  // an obvious run of numbered or bulleted items mid-line is broken out first.
  const normalised = String(src)
    .replace(/\s+(\d+\.\s+)/g, '\n$1')
    .replace(/\s+([-*]\s+)(?=\S)/g, '\n$1')

  const lines = normalised.split('\n')
  const blocks = []
  let para = []
  let list = null

  const flushPara = () => { if (para.length) { blocks.push({ type: 'p', text: para.join(' ') }); para = [] } }
  const flushList = () => { if (list) { blocks.push(list); list = null } }

  for (const raw of lines) {
    const line = raw.trimEnd()
    if (!line.trim()) { flushPara(); flushList(); continue }

    const h = line.match(/^(#{1,6})\s+(.*)$/)
    if (h) {
      flushPara(); flushList()
      blocks.push({ type: h[1].length <= 2 ? 'h2' : 'h3', text: h[2] })
      continue
    }
    const ul = line.match(/^\s*[-*]\s+(.*)$/)
    if (ul) {
      flushPara()
      if (!list || list.type !== 'ul') { flushList(); list = { type: 'ul', items: [] } }
      list.items.push(ul[1])
      continue
    }
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/)
    if (ol) {
      flushPara()
      if (!list || list.type !== 'ol') { flushList(); list = { type: 'ol', items: [] } }
      list.items.push(ol[1])
      continue
    }
    flushList()
    para.push(line.trim())
  }
  flushPara(); flushList()
  return blocks
}

/**
 * Render bold, inline code and bare URLs as React nodes — never as raw HTML.
 *
 * URLs are linked because a model that reports "Ticket KAV-5 raised.
 * https://…/browse/KAV-5" has given the user the one thing they want to click
 * on, and printing it as text makes them select and copy it by hand.
 *
 * @param {string} text - A line of markdown.
 * @returns {Array} React nodes.
 */
export function inline(text) {
  const parts = String(text).split(/(\*\*[^*]+\*\*|`[^`]+`|https?:\/\/[^\s<>()]+)/g)
  return parts.map((p, i) => {
    if (!p) return null
    if (p.startsWith('**') && p.endsWith('**')) return <strong key={i}>{p.slice(2, -2)}</strong>
    if (p.startsWith('`') && p.endsWith('`')) return <code key={i}>{p.slice(1, -1)}</code>
    if (/^https?:\/\//.test(p)) {
      // Trailing punctuation belongs to the sentence, not the link.
      const m = p.match(/^(.*?)([.,;:)]*)$/)
      return (
        <span key={i}>
          <a href={m[1]} target="_blank" rel="noreferrer">{m[1]}</a>{m[2]}
        </span>
      )
    }
    return p
  })
}
