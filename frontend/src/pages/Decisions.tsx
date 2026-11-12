import { useEffect, useRef, useState } from 'react'
import { api, id, type S } from '../api/client'
import { AnalysisDrawer } from '../components/Analysis'
import { PrintButton, printSource } from '../components/PrintButton'
import { SourceList } from '../components/Sources'
import { Badge, Button, Card, Empty, ErrorNote, Loading, Stat } from '../components/ui'
import { DECISION_LOOP_LABEL, dateOnly, dateTime, num } from '../lib/format'
import { employeeName, useLoad, useWorkspace } from '../lib/hooks'

const STEPS = ['Сигнал', 'Причина', 'Действие', 'Владелец и срок', 'Повторная проверка']
const FIRST_DECISION = ['Разберите отклонение', 'Назначьте действие и ответственного', 'Сравните показатель до и после']

/** Реестр решений — контур из трека 1: решение назначено, сохранено и проверено повторно. */
export function DecisionsPage({ focus }: { focus?: string }) {
  const ws = useWorkspace()
  const list = useLoad(() => api.get<S['DecisionOut'][]>('/decisions'), [ws.refreshTick])
  const day = useLoad(() => api.get<S['MyDayOut']>('/my-day'), [ws.refreshTick])
  const tasks = useLoad(() => api.get<S['TaskOut'][]>('/tasks'), [ws.refreshTick])
  const taskTitle = Object.fromEntries((tasks.data ?? []).map(t => [t.id, t.title]))
  const [metric, setMetric] = useState<string>()
  if (list.loading && !list.data) return <Loading />
  const rows = list.data ?? []
  // сигнал «без решения» — красный показатель, по которому нет решения в работе (неудачное решение снова открывает сигнал)
  const covered = new Set(rows.filter(d => d.status === 'open' || d.status === 'in_progress').map(d => d.metric_id))
  const red = (day.data?.metrics ?? []).filter(m => m.id !== 'overdue_tasks' && (m.status === 'warning' || m.status === 'critical') && !covered.has(m.id))
  return <div className="page">
    <div className="page-heading"><div>
      <span className="eyebrow">Контур управления</span><h1>Решения</h1>
      <p className="muted">Каждое отклонение превращается в решение с причиной, ответственным и сроком. После срока AI-ассистент сам пересчитывает показатель и показывает, сработало ли решение.</p>
    </div></div>
    <ol className="loop">{STEPS.map((s, i) => <li key={s}><span>{i + 1}</span>{s}</li>)}</ol>
    <ErrorNote error={list.error} onRetry={list.reload} />
    <ErrorNote error={day.error} onRetry={day.reload} />
    {red.length > 0 && <Card title="Сигналы без решения" icon="▼" subtitle="Показатели вне плана — начните с разбора">
      <div className="signal-row">{red.map(m => <button key={m.id} className="signal" onClick={() => setMetric(m.id)}>
        <strong>{m.name}</strong><span className="tone-danger">{num(m.value, m.unit)}</span><small>план {num(m.plan, m.unit)}</small><em>Разобрать →</em>
      </button>)}</div>
    </Card>}
    {list.data?.length === 0 ? <Empty icon="◆" steps={FIRST_DECISION}>
      Зафиксируйте решение, чтобы проверить его эффект после срока.
    </Empty> :
      <div className="decisions">{rows.map(d => <DecisionCard key={d.id} d={d} open={focus === d.id} onChange={list.reload} taskTitle={taskTitle} />)}</div>}
    {metric && <AnalysisDrawer metricId={metric} onClose={() => setMetric(undefined)} />}
  </div>
}

function DecisionCard({ d, open, onChange, taskTitle }: { d: S['DecisionOut']; open: boolean; onChange: () => void; taskTitle: Record<string, string> }) {
  const ws = useWorkspace()
  // раскрыта при переходе к решению или когда пора проверить; после проверки не схлопывается под рукой
  const [expanded, setExpanded] = useState(open || d.check_due)
  const card = useRef<HTMLDetailsElement>(null)
  useEffect(() => {
    if (open) {
      setExpanded(true)
      card.current?.scrollIntoView({ block: 'nearest' })
    }
  }, [open])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const st = DECISION_LOOP_LABEL[d.status]
  const b = d.baseline as { value?: number; period?: string; unit?: string; plan?: number; numerator?: number; denominator?: number } | null
  const r = d.recheck as { value?: number | null; period?: string; delta?: number | null; verdict?: string; low_sample?: boolean; numerator?: number; denominator?: number; baseline_denominator?: number } | null
  const unit = b?.unit ?? ''
  const delta = r?.delta == null ? undefined
    : `${r.delta > 0 ? '+' : r.delta < 0 ? '−' : ''}${num(Math.abs(r.delta))}${unit === '%' ? ' п.п.' : unit && ` ${unit}`}`
  const owner = employeeName(ws, d.owner_employee_id)
  const causeSources = d.cause_sources ?? []
  async function recheck() {
    setBusy(true); setError('')
    try { await api.post(`/decisions/${id(d.id)}/recheck`); ws.toast('Показатель пересчитан — результат решения обновлён.'); onChange() }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  return <details ref={card} className={`decision decision-${st.tone}`} open={expanded} onToggle={e => setExpanded(e.currentTarget.open)}>
    <summary>
      <Badge tone={st.tone}>{st.label}</Badge>
      <strong>{d.title}</strong>
      {d.check_due && <Badge tone="warning">пора проверить</Badge>}
      {r?.low_sample && <Badge tone="neutral">предварительно</Badge>}
      <span className="muted small">{employeeName(ws, d.owner_employee_id)} · до {dateOnly(d.due_at)}</span>
    </summary>
    <div className="decision-body">
      <div className="before-after">
        <Stat label={`До решения · ${b?.period ?? ''}`} value={num(b?.value ?? null, unit)} hint={b?.denominator ? `${b.numerator}/${b.denominator}` : undefined} />
        <span className="arrow" aria-hidden="true">→</span>
        <Stat label={r ? `После · ${r.period}` : 'После'} value={r ? num(r.value ?? null, unit) : '…'}
          hint={r ? (r.denominator ? `${r.numerator}/${r.denominator}` : 'нет данных') : 'проверка после срока'}
          tone={r ? DECISION_LOOP_LABEL[d.status].tone : undefined} />
        <Stat label="План" value={num(b?.plan ?? null, unit)} />
        {delta && <Stat label="Изменение" value={delta} />}
      </div>
      {r?.low_sample && <div className="notice warning">Вердикт предварительный: после решения данных мало ({r.denominator} против {r.baseline_denominator} в базовом периоде). Проверьте ещё раз позже.</div>}
      <dl className="decision-fields">
        {d.impact && <><dt>Влияние</dt><dd>{d.impact}</dd></>}
        {d.cause_hypothesis && <><dt>Причина <Badge tone="warning">гипотеза</Badge></dt><dd>{d.cause_hypothesis}<SourceList sources={causeSources} /></dd></>}
        <dt>Действие</dt><dd>{d.action_text}</dd>
        <dt>Ответственный</dt><dd>{employeeName(ws, d.owner_employee_id)} · срок {dateTime(d.due_at)} · приоритет {d.priority === 'high' ? 'высокий' : d.priority === 'low' ? 'низкий' : 'средний'}</dd>
        {d.task_ids.length > 0 && <><dt>Поручения</dt><dd>{d.task_ids.map(t => <button key={t} className="text-button block" onClick={() => ws.go('tasks', { id: t })}>{taskTitle[t] ?? t} ↗</button>)}</dd></>}
      </dl>
      <ErrorNote error={error} />
      <div className="row">
        <PrintButton title={d.title} sources={causeSources.map(printSource)}>
          <p>Карточка решения · {st.label}</p>
          <h2>Сравнение до и после</h2>
          <table>
            <thead><tr><th scope="col">Период</th><th scope="col">Значение</th><th scope="col">Выборка</th></tr></thead>
            <tbody>
              <tr><th scope="row">До{b?.period && ` · ${b.period}`}</th><td>{num(b?.value, unit)}</td><td>{b?.denominator ? `${b.numerator}/${b.denominator}` : '—'}</td></tr>
              <tr><th scope="row">После{r?.period && ` · ${r.period}`}</th><td>{r ? num(r.value, unit) : 'проверка ещё не выполнена'}</td><td>{r?.denominator ? `${r.numerator}/${r.denominator}` : '—'}</td></tr>
              {b?.plan != null && <tr><th scope="row">План</th><td>{num(b.plan, unit)}</td><td>—</td></tr>}
              {delta && <tr><th scope="row">Изменение</th><td>{delta}</td><td>—</td></tr>}
            </tbody>
          </table>
          {r?.low_sample && <p>Вердикт предварительный. После решения данных мало ({r.denominator} против {r.baseline_denominator} в базовом периоде).</p>}
          <h2>Решение</h2>
          <dl>
            {d.impact && <><dt>Влияние</dt><dd>{d.impact}</dd></>}
            {d.cause_hypothesis && <><dt>Причина (гипотеза)</dt><dd>
              {d.cause_hypothesis}{causeSources.length > 0 && <sup> [{causeSources.map((_, index) => index + 1).join(', ')}]</sup>}
            </dd></>}
            <dt>Действие</dt><dd>{d.action_text}</dd>
            <dt>Ответственный</dt><dd>{owner}</dd>
            <dt>Срок</dt><dd>{d.due_at ? dateOnly(d.due_at) : 'без срока'}</dd>
          </dl>
          {d.task_ids.length > 0 && <><h2>Поручения</h2><ul>{d.task_ids.map(task => <li key={task}>{taskTitle[task] ?? task}</li>)}</ul></>}
        </PrintButton>
        {d.metric_id && <Button kind={d.check_due ? 'primary' : 'secondary'} busy={busy} disabled={!r && !d.check_due} onClick={() => void recheck()}>{r ? 'Пересчитать ещё раз' : 'Проверить результат'}</Button>}
        {(d.status === 'partial' || d.status === 'ineffective') && <span className="muted small">Решение не довело показатель до плана — откройте сигнал выше и оформите следующее решение.</span>}
      </div>
      <details className="history"><summary>История</summary><ul>{(d.history ?? []).map((h, i) => {
        const e = h as unknown as { at: string; event: string; detail?: string }
        return <li key={i}><span className="muted">{dateTime(e.at)}</span> {e.event}{e.detail ? ` — ${e.detail}` : ''}</li>
      })}</ul></details>
    </div>
  </details>
}
