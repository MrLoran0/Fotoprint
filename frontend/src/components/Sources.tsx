import { useEffect, useRef, useState, type ReactNode } from 'react'
import { api, id, type S } from '../api/client'
import { useLoad } from '../lib/hooks'
import { dateOnly, dateTime, SOURCE_LABEL } from '../lib/format'
import { Badge, ErrorNote, Loading, Modal } from './ui'

/** Сноска на первоисточник: тип, автор, время, цитата. Клик — открыть источник с подсветкой. */
export function SourceChip({ source, n }: { source: S['SourceRef']; n?: number }) {
  const [open, setOpen] = useState(false)
  const kind = SOURCE_LABEL[source.source_type ?? 'document'] ?? SOURCE_LABEL.document
  return <>
    <button className="source-chip" onClick={() => setOpen(true)} title={source.quote || source.title}>
      {n !== undefined && <span className="source-n">{n}</span>}
      <span className="source-icon" aria-hidden="true">{kind.icon}</span>
      <span className="source-title">{source.author ? `${source.author}, ` : ''}{source.title}</span>
      <span className="source-meta">{source.sent_at ? dateTime(source.sent_at) : dateOnly(source.source_date)}{source.version ? ` · v${source.version}` : ''}</span>
    </button>
    {open && <SourceViewer key={source.source_id} source={source} onClose={() => setOpen(false)} />}
  </>
}

export function SourceList({ sources }: { sources: S['SourceRef'][] }) {
  if (!sources.length) return null
  return <div className="source-list">{sources.map((s, i) => <SourceChip key={`${s.source_id}-${s.chunk_id}-${i}`} source={s} n={i + 1} />)}</div>
}

const fold = (text: string) => text.toLowerCase().replaceAll('ё', 'е')

function quoteRanges(parts: string[], quote: string) {
  const ranges = new Map<number, { start: number; end: number }>()
  const needle = fold(quote).replace(/\s+/g, ' ').trim()
  if (!needle) return ranges
  const words: { part: number; offset: number; from: number; to: number; length: number }[] = []
  let searchable = ''
  for (const [part, text] of parts.entries()) {
    for (const word of text.matchAll(/\S+/g)) {
      if (searchable) searchable += ' '
      const from = searchable.length
      searchable += fold(word[0])
      words.push({ part, offset: word.index, from, to: searchable.length, length: word[0].length })
    }
  }
  const start = searchable.indexOf(needle)
  if (start < 0) return ranges
  const end = start + needle.length
  for (const word of words) {
    if (word.to <= start) continue
    if (word.from >= end) break
    const lo = word.offset + Math.max(start - word.from, 0)
    const hi = word.offset + Math.min(end - word.from, word.length)
    ranges.set(word.part, { start: ranges.get(word.part)?.start ?? lo, end: hi })
  }
  return ranges
}

/** Простой рендер Markdown: заголовки, списки, таблицы, абзацы — без сторонних библиотек. */
function MarkdownText({ text, quote }: { text: string; quote: string }) {
  const viewer = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const container = viewer.current
    const mark = container?.querySelector('mark')
    if (!container || !mark) return
    const offset = mark.getBoundingClientRect().top - container.getBoundingClientRect().top
    if (offset >= container.clientHeight) container.scrollTop += offset - 16
  }, [text, quote])
  const parts: string[] = []
  const addPart = (value: string) => parts.push(value) - 1
  type RenderText = (part: number) => ReactNode
  const blocks = text.replace(/\r\n/g, '\n').split(/\n{2,}/).map((b, i) => {
    const lines = b.split('\n')
    if (lines.length === 1 && /^#{1,6} /.test(b)) {
      const heading = addPart(b.replace(/^#{1,6} /, ''))
      return (render: RenderText) => <h3 key={i}>{render(heading)}</h3>
    }
    if (lines.every(l => l.trim().startsWith('|'))) {
      const rows = lines.map(l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim()))
      const header = rows[1]?.every(c => /^:?-{3,}:?$/.test(c)) ? rows.splice(0, 2)[0].map(addPart) : undefined
      const body = rows.map(row => row.map(addPart))
      return (render: RenderText) => <table key={i} className="doc-table">
        {header && <thead><tr>{header.map(c => <th key={c} scope="col">{render(c)}</th>)}</tr></thead>}
        <tbody>{body.map((row, j) => <tr key={j}>{row.map(c => <td key={c}>{render(c)}</td>)}</tr>)}</tbody>
      </table>
    }
    if (lines.every(l => /^\s*[-*] /.test(l))) {
      const list = lines.map(l => addPart(l.replace(/^\s*[-*] /, '')))
      return (render: RenderText) => <ul key={i}>{list.map(part => <li key={part}>{render(part)}</li>)}</ul>
    }
    const paragraph = lines.map(addPart)
    return (render: RenderText) => <p key={i}>{paragraph.map((part, j) => <span key={part}>{render(part)}{j < lines.length - 1 && <br />}</span>)}</p>
  })
  const ranges = quoteRanges(parts, quote)
  function render(part: number) {
    const value = parts[part]
    const range = ranges.get(part)
    return range ? <>{value.slice(0, range.start)}<mark>{value.slice(range.start, range.end)}</mark>{value.slice(range.end)}</> : value
  }
  return <div ref={viewer} className="doc-text" role="region" aria-label="Текст источника" tabIndex={0}>{blocks.map(block => block(render))}</div>
}

export function SourceViewer({ source, onClose }: { source: S['SourceRef']; onClose: () => void }) {
  const isDoc = ['document', 'email', 'telegram', 'chat'].includes(source.source_type ?? 'document')
  const doc = useLoad(() => isDoc ? api.get<S['DocumentFull']>(`/documents/${id(source.source_id)}`) : Promise.resolve(undefined), [source.source_id, isDoc])
  const kind = SOURCE_LABEL[source.source_type ?? 'document'] ?? SOURCE_LABEL.document
  return <Modal onClose={onClose} label="Первоисточник" wide>
    <div className="modal-head"><div><span className="eyebrow">{kind.icon} {kind.label} · первоисточник</span><h2>{source.title}</h2>
      <p className="muted">{source.author && <>{source.author} · </>}{source.sent_at ? dateTime(source.sent_at) : dateOnly(source.source_date)}{source.version ? ` · версия ${source.version}` : ''}</p></div>
      <button className="icon-button" aria-label="Закрыть" onClick={onClose}>×</button></div>
    {source.quote && <blockquote className="quote">«{source.quote}»</blockquote>}
    {isDoc && (doc.loading ? <Loading /> : doc.error ? <ErrorNote error={doc.error} onRetry={doc.reload} /> : doc.data &&
      <><div className="doc-meta"><Badge>{doc.data.access_level === 'participants' ? 'только участники' : doc.data.access_level === 'department' ? 'подразделение' : doc.data.access_level === 'confidential' ? 'только директор' : 'все сотрудники'}</Badge>
        {!doc.data.ai_allowed && <Badge tone="danger">запрещено для AI</Badge>}</div>
        <MarkdownText text={doc.data.content} quote={source.quote ?? ''} /></>)}
  </Modal>
}
