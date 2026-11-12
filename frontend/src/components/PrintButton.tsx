import { useEffect, useState, type ReactNode } from 'react'
import { createPortal, flushSync } from 'react-dom'
import type { S } from '../api/client'
import { dateOnly, dateTime, SOURCE_LABEL } from '../lib/format'
import { Button } from './ui'

export type PrintSource = {
  title: string
  detail?: string
  quote?: string
}

export function printSource(source: S['SourceRef']): PrintSource {
  return {
    title: source.title,
    detail: [
      SOURCE_LABEL[source.source_type ?? 'document']?.label,
      source.author,
      source.sent_at ? dateTime(source.sent_at) : source.source_date && dateOnly(source.source_date),
      source.version != null && `версия ${source.version}`,
    ].filter(Boolean).join(' · '),
    quote: source.quote,
  }
}

export function PrintButton({ title, date, sources, children }: {
  title: string
  date?: string
  sources: PrintSource[]
  children: ReactNode
}) {
  const [printDate, setPrintDate] = useState<string>()
  useEffect(() => {
    const clear = () => setPrintDate(undefined)
    window.addEventListener('afterprint', clear)
    return () => window.removeEventListener('afterprint', clear)
  }, [])

  function print() {
    // Печатная версия должна быть в DOM до открытия системного диалога.
    flushSync(() => setPrintDate(date ?? new Date().toISOString()))
    window.print()
  }

  return <>
    <Button onClick={print}>Печать / PDF</Button>
    {printDate && createPortal(<article className="print-document">
      <header className="print-header">AI-ассистент · ООО «Фотопринт» · {dateOnly(printDate)}</header>
      <h1>{title}</h1>
      {children}
      <section className="print-sources">
        <h2>Источники</h2>
        {sources.length ? <ol>{sources.map((source, index) => <li key={index}>
          <strong>{source.title}</strong>
          {source.detail && <div>{source.detail}</div>}
          {source.quote && <blockquote>«{source.quote}»</blockquote>}
        </li>)}</ol> : <p>В данных этой карточки нет ссылок на источники.</p>}
      </section>
    </article>, document.body)}
  </>
}
