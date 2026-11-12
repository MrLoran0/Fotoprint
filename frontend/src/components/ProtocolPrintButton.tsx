import type { S } from '../api/client'
import { DECISION_STATUS_LABEL, KIND_LABEL, dateOnly, dateTime } from '../lib/format'
import { employeeName, useWorkspace } from '../lib/hooks'
import { PrintButton, type PrintSource } from './PrintButton'

export function ProtocolPrintButton({ protocol, meeting }: {
  protocol: S['ProtocolDraftOut']
  meeting?: S['MeetingOut']
}) {
  const ws = useWorkspace()
  const items = protocol.items.filter(item => !item.removed).sort((a, b) => a.position - b.position)
  const segments = new Map(protocol.segments.map(segment => [segment.n, segment]))
  const sources: PrintSource[] = []
  const sourceNumbers = new Map<string, number>()
  for (const item of items) {
    if (!item.quote_verified || !item.evidence_quote.trim() || item.segment_id == null) continue
    const segment = segments.get(item.segment_id)
    if (!segment) continue
    const title = [`Реплика ${segment.n}`, segment.ts, segment.speaker].filter(Boolean).join(' · ')
    sources.push({ title, quote: item.evidence_quote })
    sourceNumbers.set(item.id, sources.length)
  }

  return <PrintButton title={meeting ? `Протокол · ${meeting.title}` : 'Протокол встречи'} sources={sources}>
    {meeting?.starts_at && <p>Дата встречи · {dateOnly(meeting.starts_at)}</p>}
    {meeting?.participant_ids.length ? <p>Участники · {meeting.participant_ids.map(person => employeeName(ws, person)).join(', ')}</p> : null}
    {protocol.summary && <p>{protocol.summary}</p>}
    <h2>Пункты протокола</h2>
    {items.length ? <ol>{items.map(item => <li key={item.id}>
      <strong>{KIND_LABEL[item.kind]}</strong>{item.decision_status && ` · ${DECISION_STATUS_LABEL[item.decision_status]}`}
      <div>{item.statement}{sourceNumbers.has(item.id) && <sup> [{sourceNumbers.get(item.id)}]</sup>}</div>
      {item.kind === 'task' && <div>{employeeName(ws, item.owner_employee_id)} · {item.due_at ? `срок ${dateTime(item.due_at)}` : 'без срока'}</div>}
    </li>)}</ol> : <p>В опубликованный протокол не включено ни одного пункта.</p>}
  </PrintButton>
}
