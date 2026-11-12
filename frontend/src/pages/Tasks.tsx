import { useEffect, useMemo, useState } from 'react'
import { api, id, type S } from '../api/client'
import { PersonSelect } from '../components/PeoplePicker'
import { Badge, Button, Card, Empty, ErrorNote, Loading, Modal } from '../components/ui'
import { ORIGIN_LABEL, TASK_STATUS_LABEL, dateTime, fromLocalInput, toLocalInput } from '../lib/format'
import { employeeName, useLoad, useWorkspace } from '../lib/hooks'

const FILTERS = [
  { id: 'open', label: 'Открытые' },
  { id: 'overdue', label: 'Просроченные' },
  { id: 'no_owner', label: 'Без владельца' },
  { id: 'no_due', label: 'Без срока' },
  { id: 'done', label: 'Выполненные' },
  { id: 'all', label: 'Все' },
]
const FIRST_TASK = ['Составьте протокол встречи', 'Подтвердите поручения и ответственных', 'Отметьте результат исполнения']
const STATUS_NEXT: Record<S['TaskOut']['status'], S['TaskOut']['status'][]> = {
  assigned: ['accepted', 'cancelled'],
  accepted: ['in_progress', 'done', 'cancelled'],
  in_progress: ['done', 'cancelled'],
  done: [],
  cancelled: [],
}

export function TasksPage({ focus }: { focus?: string }) {
  const ws = useWorkspace()
  const tasks = useLoad(() => api.get<S['TaskOut'][]>('/tasks'), [ws.refreshTick])
  const [filter, setFilter] = useState(focus ? 'all' : 'open')
  const [editing, setEditing] = useState<S['TaskOut']>()
  const [query, setQuery] = useState('')
  useEffect(() => {
    if (focus) { setFilter('all'); setQuery('') }
  }, [focus])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const open = (t: S['TaskOut']) => !['done', 'cancelled'].includes(t.status)
  const rows = useMemo(() => (tasks.data ?? []).filter(t => {
    if (query && !t.title.toLowerCase().includes(query.toLowerCase())) return false
    switch (filter) {
      case 'open': return open(t)
      case 'overdue': return t.overdue
      case 'no_owner': return open(t) && !t.owner_employee_id
      case 'no_due': return open(t) && !t.due_at
      case 'done': return t.status === 'done'
      default: return true
    }
  }).sort((a, b) => Number(b.overdue) - Number(a.overdue) || (a.due_at ?? '9').localeCompare(b.due_at ?? '9')), [tasks.data, filter, query])
  useEffect(() => {
    if (focus && rows.some(task => task.id === focus)) document.getElementById(`task-${focus}`)?.scrollIntoView({ block: 'center' })
  }, [focus, rows])
  const count = (f: string) => (tasks.data ?? []).filter(t => f === 'open' ? open(t) : f === 'overdue' ? t.overdue : f === 'no_owner' ? open(t) && !t.owner_employee_id : f === 'no_due' ? open(t) && !t.due_at : f === 'done' ? t.status === 'done' : true).length

  async function delegate(t: S['TaskOut']) {
    setBusy(t.id); setError('')
    try { ws.openAction(await api.post<S['ActionOut']>('/delegation/propose', { task_id: t.id })) } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  async function remind() {
    setBusy('reminders'); setError('')
    try { ws.openAction(await api.post<S['ActionOut']>('/reminders/propose')) }
    catch (e) { setError((e as Error).message) }
    finally { setBusy('') }
  }

  if (tasks.loading && !tasks.data) return <Loading />
  return <div className="page">
    <div className="page-heading"><div><span className="eyebrow">Реестр</span><h1>Поручения</h1>
      <p className="muted">Каждое поручение связано со встречей, протоколом и цитатой-основанием. Менять владельца и сроки — через карточку с подтверждением.</p></div>
      <div className="row">
        <Button kind="primary" onClick={() => ws.openVoice?.()} title="Раздать поручения голосом">
          <span aria-hidden="true">🎙️</span> Раздать голосом
        </Button>
        <Button busy={busy === 'reminders'} disabled={!!busy || !tasks.data?.some(t => t.overdue && t.owner_employee_id)} onClick={() => void remind()}>Напомнить о просрочках</Button>
      </div>
    </div>
    <div className="filters">
      {FILTERS.map(f => <button key={f.id} className={`filter ${filter === f.id ? 'on' : ''}`}
        aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>{f.label}<span>{count(f.id)}</span></button>)}
      <input className="search" placeholder="Поиск по названию" value={query} onChange={e => setQuery(e.target.value)} aria-label="Поиск поручений" />
    </div>
    <ErrorNote error={error} />
    <ErrorNote error={tasks.error} onRetry={tasks.reload} />
    {tasks.data && <Card>
      {tasks.data.length === 0 ? <Empty icon="☑" steps={FIRST_TASK}>
        Сохраняйте поручения с ответственными и сроками, чтобы видеть результат встречи.
      </Empty> : rows.length === 0 ? <Empty>{query.trim() ? 'Поручений с таким названием не найдено.' : 'По этому фильтру поручений нет.'}</Empty> :
        <div className="table-wrap" role="region" aria-label="Таблица поручений" tabIndex={0}><table className="tasks-table"><thead><tr><th>Поручение</th><th>Ответственный</th><th>Срок</th><th>Статус</th><th>Связи</th><th></th></tr></thead>
          <tbody>{rows.map(t => <tr key={t.id} id={`task-${t.id}`} className={`${t.overdue ? 'overdue' : ''} ${focus === t.id ? 'focused' : ''}`}>
            <td><strong>{t.title}</strong>{t.expected_result && <small className="muted block">{t.expected_result}</small>}
              {t.delegation_reason && <small className="muted block">✋ {t.delegation_reason.length > 90 ? t.delegation_reason.slice(0, 90) + '…' : t.delegation_reason}</small>}</td>
            <td>{t.owner_employee_id ? <>{employeeName(ws, t.owner_employee_id)}<small className="muted block">{ORIGIN_LABEL[t.owner_origin ?? 'unknown']}</small></> : <Badge tone="danger">без владельца</Badge>}</td>
            <td>{t.due_at ? <>{dateTime(t.due_at)}{t.overdue && <Badge tone="danger">просрочено</Badge>}</> : <Badge tone="warning">без срока</Badge>}</td>
            <td><Badge tone={t.status === 'done' ? 'success' : t.status === 'in_progress' ? 'info' : 'neutral'}>{TASK_STATUS_LABEL[t.status]}</Badge>{t.result && <small className="muted block">{t.result}</small>}</td>
            <td className="links">
              {t.meeting_id && <button className="text-button" onClick={() => ws.go('graph', { focus: `meeting:${t.meeting_id}` })}>встреча ↗</button>}
              <button className="text-button" onClick={() => ws.go('graph', { focus: `task:${t.id}` })}>граф ↗</button>
            </td>
            <td className="row-actions">{!t.owner_employee_id && open(t) && <Button kind="primary" disabled={!!busy} busy={busy === t.id} onClick={() => void delegate(t)}>Поручить</Button>}
              {open(t) && <Button kind="ghost" disabled={!!busy} onClick={() => setEditing(t)}>Изменить</Button>}</td>
          </tr>)}</tbody></table></div>}
    </Card>}
    {editing && <TaskEdit task={editing} onClose={() => setEditing(undefined)} />}
  </div>
}

/** Правка поручения: форма собирает только изменённые поля → карточка update_task → подтверждение. Напрямую ничего не пишем. */
function TaskEdit({ task, onClose }: { task: S['TaskOut']; onClose: () => void }) {
  const ws = useWorkspace()
  const [owner, setOwner] = useState(task.owner_employee_id ?? '')
  const [due, setDue] = useState(toLocalInput(task.due_at))
  const [status, setStatus] = useState<string>(task.status)
  const [result, setResult] = useState(task.result ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const changes: Record<string, string | null> = {}
  if (owner !== (task.owner_employee_id ?? '')) changes.owner_employee_id = owner || null
  if (due !== toLocalInput(task.due_at)) changes.due_at = fromLocalInput(due)
  if (status !== task.status) changes.status = status
  if (result.trim() !== (task.result ?? '')) changes.result = result.trim()
  const empty = Object.keys(changes).length === 0
  const needsResult = status === 'done' && !result.trim()
  async function submit() {
    setBusy(true); setError('')
    try { const card = await api.post<S['ActionOut']>(`/tasks/${id(task.id)}/change-proposal`, changes); onClose(); ws.openAction(card) }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  return <Modal label="Изменить поручение" onClose={() => { if (!busy) onClose() }}>
    <div className="modal-head"><div><span className="eyebrow">Изменение поручения</span><h2>{task.title}</h2></div>
      <button className="icon-button" aria-label="Закрыть" onClick={onClose} disabled={busy}>×</button></div>
    <fieldset className="action-body" disabled={busy}>
      <PersonSelect label="Ответственный" value={owner} onChange={setOwner} sameDepartmentAs={ws.user.department_id} allowEmpty />
      <div className="grid-2">
        <div>
          <label>Срок<input type="datetime-local" value={due} onChange={e => setDue(e.target.value)} /></label>
          {due && <button className="text-button" onClick={() => setDue('')}>Убрать срок</button>}
        </div>
        <label>Статус<select value={status} onChange={e => setStatus(e.target.value)}>
          {[task.status, ...STATUS_NEXT[task.status]].map(value => <option key={value} value={value}>{TASK_STATUS_LABEL[value]}</option>)}</select></label>
      </div>
      <label>Результат<textarea rows={2} value={result} onChange={e => setResult(e.target.value)} placeholder="Что сделано — попадёт в «Результаты поручений»" /></label>
      {needsResult && <p className="muted small">Укажите результат выполнения поручения.</p>}
      <p className="muted small">Изменение не применится сразу: появится карточка с подтверждением.</p>
      <ErrorNote error={error} />
      <div className="row end"><Button kind="ghost" onClick={onClose} disabled={busy}>Отмена</Button>
        <Button kind="primary" busy={busy} disabled={empty || needsResult} onClick={() => void submit()}>Подготовить карточку</Button></div>
    </fieldset>
  </Modal>
}
