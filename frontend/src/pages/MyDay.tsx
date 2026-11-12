import { useState } from 'react'
import { api, id, type S } from '../api/client'
import { AnalysisDrawer } from '../components/Analysis'
import { BriefPrintButton } from '../components/BriefPrintButton'
import { SourceChip } from '../components/Sources'
import { Badge, Button, Card, Empty, ErrorNote, Loading } from '../components/ui'
import { METRIC_TONE, ROLE_LABEL, dateTime, num, plural, timeOnly, weekday } from '../lib/format'
import { employeeName, useLoad, useWorkspace } from '../lib/hooks'

const NEED_ICON: Record<string, string> = {
  overdue_task: '⏰', clarification: '?', critical_metric: '▼', stale_source: '⌛', pending_action: '✦', decision_check: '◆',
  contradiction: '⚡', commitment: '✋',
}
const FIRST_BRIEF = ['Откройте источники данных', 'Разберите отклонение в показателе', 'Проверьте результат решения после срока']

export function MyDayPage() {
  const ws = useWorkspace()
  const day = useLoad(() => api.get<S['MyDayOut']>('/my-day'), [ws.refreshTick])
  const [metric, setMetric] = useState<string>()
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  async function card(path: string, key: string) {
    setBusy(key); setError('')
    try { ws.openAction(await api.post<S['ActionOut']>(path)) } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  async function openNeed(n: S['DecisionNeeded']) {
    if (n.object_type === 'metric' && n.object_id === 'overdue_tasks') ws.go('tasks')
    else if (n.object_type === 'metric') setMetric(n.object_id)
    else if (n.object_type === 'decision') ws.go('decisions', { id: n.object_id })
    else if (n.object_type === 'action') { try { ws.openAction(await api.get<S['ActionOut']>(`/actions/${id(n.object_id)}`)) } catch (e) { setError((e as Error).message) } }
    else if (n.object_type === 'task') ws.go('tasks', { id: n.object_id })
    else if (n.object_type === 'data_source') ws.go('sources')
  }

  if (day.loading && !day.data) return <Loading label="Собираем ваш день…" />
  if (day.error) return <ErrorNote error={day.error} onRetry={day.reload} />
  const d = day.data!
  const crit = d.needs_decision.filter(n => n.severity === 'critical').length
  return <div className="page my-day">
    <div className="hero">
      <div>
        <span className="eyebrow">{ROLE_LABEL[ws.user.role]} · {weekday(d.now)}</span>
        <h1>Добрый день, {ws.user.full_name.split(' ')[0]}</h1>
        <p className="muted">{d.needs_decision.length
          ? <>Ассистент разобрал данные: <strong>{plural(d.needs_decision.length, 'вопрос', 'вопроса', 'вопросов')}</strong> ждут вашего решения{crit ? <>, из них критичных — <strong className="tone-danger">{crit}</strong></> : null}.</>
          : 'Срочных вопросов нет — всё под контролем.'}</p>
      </div>
      <div className="hero-actions">
        <Button kind="primary" onClick={() => ws.openVoice?.()} title="Раздать поручения голосом">
          <span aria-hidden="true">🎙️</span> Раздать голосом
        </Button>
        <Button onClick={() => ws.go('meetings')}>Подготовить встречу</Button>
        <Button busy={busy === 'rem'} disabled={!!busy || !d.overdue.some(t => t.owner_employee_id)} onClick={() => void card('/reminders/propose', 'rem')}>Напомнить о просрочках</Button>
        <Button busy={busy === 'brief'} disabled={!!busy} onClick={() => void card('/my-day/brief', 'brief')}>Брифинг на почту</Button>
        <BriefPrintButton day={d} />
      </div>
    </div>
    <ErrorNote error={error} />

    {d.metrics.length === 0 ? <Card><Empty icon="◉" steps={FIRST_BRIEF}>
      Следите за отклонениями, чтобы вовремя принимать и проверять решения.
    </Empty></Card> : <div className="kpi-grid">{d.metrics.map(m => <button key={m.id} className={`kpi kpi-${METRIC_TONE[m.status]}`} onClick={() => m.id !== 'overdue_tasks' ? setMetric(m.id) : ws.go('tasks')}
      title={m.id !== 'overdue_tasks' ? 'Разобрать отклонение' : 'Открыть поручения'}>
      <span className="kpi-name">{m.name}{m.stale && <Badge tone="warning">устарело</Badge>}</span>
      <strong className="kpi-value">{num(m.value, m.unit)}</strong>
      <span className="kpi-plan">план {num(m.plan, m.unit)} · {m.period}</span>
      <span className="kpi-foot">{m.source_name} · {dateTime(m.updated_at)}</span>
      {m.id !== 'overdue_tasks' && (m.status === 'warning' || m.status === 'critical') && <span className="kpi-cta">Разобрать отклонение →</span>}
    </button>)}</div>}

    <div className="grid-main">
      <Card title="Требует вашего решения" icon="!" subtitle="Всё, что ассистент нашёл в данных, поручениях и источниках">
        {d.needs_decision.length === 0 ? <Empty icon="✓">Ничего не требует решения.</Empty> :
          <ul className="needs">{d.needs_decision.map((n, i) => <li key={`${n.object_id}-${i}`} className={`need need-${n.severity}`}>
            <span className="need-icon" aria-hidden="true">{NEED_ICON[n.kind] ?? '•'}</span>
            <div><strong>{n.title}</strong>{n.kind === 'commitment' && <Badge tone="info">Обещание</Badge>}
              {n.kind === 'contradiction' && <Badge tone="warning">Противоречие</Badge>}{n.detail && <small>{n.detail}</small>}</div>
            {n.object_type !== 'protocol_item' && <button className="text-button" onClick={() => void openNeed(n)}>
              {n.kind === 'commitment' && n.object_type === 'action' ? 'Поставить на контроль →' : 'Открыть →'}
            </button>}
          </li>)}</ul>}
      </Card>

      <div className="stack">
        <Card title="Предлагается делегировать" icon="✋" subtitle="Кому поручить задачи без владельца — с основанием">
          {d.delegation_suggestions.length === 0 ? <Empty>Задач без владельца нет.</Empty> :
            d.delegation_suggestions.map(a => {
              const p = a.payload as unknown as { task_id: string; owner_employee_id: string; reason: string }
              const ev = (a.preview as { evidence?: S['SourceRef'] }).evidence
              return <div key={a.id} className="suggestion-card">
                <div className="suggestion-head"><span className="avatar small">{employeeName(ws, p.owner_employee_id).slice(0, 1)}</span>
                  <div><strong>{employeeName(ws, p.owner_employee_id)}</strong><small className="muted">{p.reason.length > 140 ? p.reason.slice(0, 140) + '…' : p.reason}</small></div></div>
                {ev && <SourceChip source={ev} />}
                <Button kind="primary" onClick={() => ws.openAction(a)}>Проверить и поручить</Button>
              </div>
            })}
        </Card>

        <Card title="Ближайшие встречи" icon="◷">
          {d.upcoming_meetings.length === 0 ? <Empty>На ближайшие дни встреч нет.</Empty> :
            <ul className="agenda-mini">{d.upcoming_meetings.map(b => <li key={b.id}>
              <span className="time">{b.starts_at ? `${weekday(b.starts_at)} ${timeOnly(b.starts_at)}` : '—'}</span>
              <span>{b.title}{b.returned_task_ids.length > 0 && <Badge tone="warning">вернётся {b.returned_task_ids.length} незакрытых</Badge>}</span>
            </li>)}</ul>}
        </Card>
      </div>
    </div>

    <div className="grid-2">
      <Card title="Просрочено" icon="⏰" actions={<Button kind="ghost" onClick={() => ws.go('tasks')}>Все поручения</Button>}>
        {d.overdue.length === 0 ? <Empty icon="✓">Просрочек нет.</Empty> :
          <ul className="task-mini">{d.overdue.map(t => <li key={t.id}><strong>{t.title}</strong><small>{employeeName(ws, t.owner_employee_id)} · срок {dateTime(t.due_at)}</small></li>)}</ul>}
      </Card>
      <Card title="Результаты поручений за неделю" icon="✓">
        {d.task_results.length === 0 ? <Empty>Пока без завершённых поручений.</Empty> :
          <ul className="task-mini">{d.task_results.map(t => <li key={t.id}><strong>{t.title}</strong><small>{employeeName(ws, t.owner_employee_id)}: {t.result}</small></li>)}</ul>}
      </Card>
    </div>
    {metric && <AnalysisDrawer metricId={metric} onClose={() => setMetric(undefined)} />}
  </div>
}
