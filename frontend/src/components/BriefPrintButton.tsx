import type { S } from '../api/client'
import { dateOnly, dateTime, num, timeOnly } from '../lib/format'
import { employeeName, useWorkspace } from '../lib/hooks'
import { PrintButton, type PrintSource } from './PrintButton'

export function BriefPrintButton({ day }: { day: S['MyDayOut'] }) {
  const ws = useWorkspace()
  const sources: PrintSource[] = []
  const metrics = day.metrics.map(metric => {
    if (!metric.source_name) return { metric }
    sources.push({
      title: metric.source_name,
      detail: [metric.period, metric.updated_at && `обновлён ${dateTime(metric.updated_at)}`].filter(Boolean).join(' · '),
    })
    return { metric, sourceNumber: sources.length }
  })

  return <PrintButton title="Брифинг «Мой день»" date={day.now} sources={sources}>
    <p>{ws.user.full_name}</p>
    <h2>Показатели</h2>
    {metrics.length ? <table>
      <thead><tr><th scope="col">Показатель</th><th scope="col">Факт</th><th scope="col">План</th><th scope="col">Период</th></tr></thead>
      <tbody>{metrics.map(({ metric, sourceNumber }) => <tr key={metric.id}>
        <td>{metric.name}{sourceNumber && <sup> [{sourceNumber}]</sup>}{metric.stale && ' · данные устарели'}</td>
        <td>{num(metric.value, metric.unit)}</td><td>{num(metric.plan, metric.unit)}</td><td>{metric.period}</td>
      </tr>)}</tbody>
    </table> : <p>Нет доступных показателей.</p>}
    <h2>Требует решения</h2>
    {day.needs_decision.length ? <ul>{day.needs_decision.map((need, index) => <li key={index}>
      <strong>{need.title}</strong>{need.detail && <div>{need.detail}</div>}
    </li>)}</ul> : <p>Нет вопросов, требующих решения.</p>}
    <h2>Ближайшие встречи</h2>
    {day.upcoming_meetings.length ? <ul>{day.upcoming_meetings.map(meeting => <li key={meeting.id}>
      {meeting.title}{meeting.starts_at && ` · ${dateOnly(meeting.starts_at)}, ${timeOnly(meeting.starts_at)}`}
    </li>)}</ul> : <p>Ближайших встреч нет.</p>}
    <h2>Просроченные поручения</h2>
    {day.overdue.length ? <ul>{day.overdue.map(task => <li key={task.id}>
      {task.title} · {employeeName(ws, task.owner_employee_id)}{task.due_at && ` · срок ${dateTime(task.due_at)}`}
    </li>)}</ul> : <p>Просрочек нет.</p>}
    {day.task_results.length > 0 && <>
      <h2>Результаты поручений</h2>
      <ul>{day.task_results.map(task => <li key={task.id}>{task.title}{task.result && ` · ${task.result}`}</li>)}</ul>
    </>}
  </PrintButton>
}
