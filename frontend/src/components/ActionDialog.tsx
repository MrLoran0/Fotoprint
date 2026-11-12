import { useState } from 'react'
import { api, ApiError, id, type S } from '../api/client'
import { ACTION_TITLE, DECISION_STATUS_LABEL, KIND_LABEL, TASK_STATUS_LABEL, dateTime, fromLocalInput, toLocalInput, weekday, timeOnly } from '../lib/format'
import { employeeName, useLoad, useWorkspace } from '../lib/hooks'
import { PersonSelect } from './PeoplePicker'
import { SourceChip, SourceList } from './Sources'
import { Badge, Button, Modal } from './ui'

type Payload = Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any

/**
 * Карточка действия: ассистент только предлагает — исполняет сервер после подтверждения конкретной версии.
 * Правки сохраняются как новая ревизия; старое подтверждение после правки недействительно (409).
 */
export function ActionDialog({ card, onClose, onDone }: { card: S['ActionOut']; onClose: () => void; onDone: (message: string) => void }) {
  const [current, setCurrent] = useState(card)
  const [draft, setDraft] = useState<Payload>(structuredClone(card.payload))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const dirty = JSON.stringify(draft) !== JSON.stringify(current.payload)
  const editable = current.status === 'pending'
  const url = `/actions/${id(current.id)}`

  async function run(op: 'save' | 'confirm' | 'cancel' | 'retry' | 'reload') {
    setBusy(true); setError('')
    try {
      if (op === 'reload') {
        const fresh = await api.get<S['ActionOut']>(url)
        setCurrent(fresh); setDraft(structuredClone(fresh.payload)); setConflict(false)
      } else if (op === 'save') {
        const updated = await api.patch<S['ActionOut']>(url, { revision: current.revision, payload: draft })
        setCurrent(updated); setDraft(structuredClone(updated.payload))
      } else {
        const body = op === 'confirm' ? { revision: current.revision, payload_hash: current.payload_hash } : {}
        const result = await api.post<S['ActionOut']>(`${url}/${op}`, body)
        setCurrent(result)
        if (result.status === 'executed') onDone(doneMessage(result))
        else if (result.status === 'cancelled') onDone('Действие отменено — в системах ничего не изменилось.')
        else setError(result.error || `Статус: ${result.status}`)
      }
    } catch (e) {
      setError((e as Error).message)
      if (e instanceof ApiError && e.status === 409) setConflict(true)
    } finally { setBusy(false) }
  }

  const failed = current.status === 'failed' || current.status === 'partially_failed'
  return <Modal onClose={() => !busy && onClose()} label="Подтверждение действия" wide>
    <div className="modal-head">
      <div>
        <span className="eyebrow">{current.origin === 'ai' ? '✦ Подготовил ассистент' : 'Действие'} · требуется ваше решение</span>
        <h2>{(current.preview as Payload)?.title || ACTION_TITLE[current.type] || current.type}</h2>
        <p className="muted">Ничего не будет выполнено без вашего подтверждения. Версия карточки {current.revision}.</p>
      </div>
      <button className="icon-button" aria-label="Закрыть" disabled={busy} onClick={onClose}>×</button>
    </div>
    <fieldset className="action-body" disabled={busy || conflict}>
      <ActionBody type={current.type} draft={draft} setDraft={setDraft} editable={editable} preview={current.preview as Payload} />
    </fieldset>
    {dirty && editable && <div className="notice warning">Есть несохранённые правки. Сохраните их — после этого предпросмотр обновится, и можно будет подтвердить.</div>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {conflict && <div className="notice warning">Карточка или исходные данные изменились. <button className="text-button" disabled={busy} onClick={() => void run('reload')}>Загрузить актуальную версию</button></div>}
    <div className="modal-actions">
      {editable && <Button kind="ghost" disabled={busy} onClick={() => void run('cancel')}>Отменить действие</Button>}
      {editable && dirty && <Button busy={busy} disabled={conflict} onClick={() => void run('save')}>Сохранить правки</Button>}
      {editable && <Button kind="primary" busy={busy} disabled={dirty || conflict} onClick={() => void run('confirm')}>Подтвердить</Button>}
      {failed && <Button kind="primary" busy={busy} disabled={conflict} onClick={() => void run('retry')}>Повторить отправку</Button>}
      {!editable && !failed && <Button onClick={onClose}>Закрыть</Button>}
    </div>
  </Modal>
}

function doneMessage(a: S['ActionOut']) {
  const r = (a.result ?? {}) as Payload
  switch (a.type) {
    case 'publish_protocol': return `Протокол опубликован, поручений создано: ${(r.task_ids ?? []).length}. Ассистент подготовил рассылку итогов — она ждёт подтверждения.`
    case 'create_event': return r.delivery_status === 'sent' ? 'Встреча создана, приглашения отправлены.' : 'Встреча создана, но приглашения не ушли — повторите отправку.'
    case 'send_summary': case 'send_reminders': case 'send_brief': return `Отправлено писем: ${r.sent_count ?? 0}.`
    case 'delegate_task': return 'Исполнитель назначен.'
    case 'update_task': return 'Поручение изменено.'
    case 'create_decision': return r.task_ids?.length
      ? 'Решение зафиксировано, поручение создано. Повторная проверка — после срока.'
      : 'Решение зафиксировано.'
    default: return 'Готово.'
  }
}

function TaskTitle({ taskId }: { taskId: string }) {
  const t = useLoad(() => api.get<S['TaskOut']>(`/tasks/${id(taskId)}`), [taskId])
  return <>{t.data?.title ?? taskId}</>
}

function Field({ label, value, onChange, editable, multiline, rows = 4 }: { label: string; value: string; onChange: (v: string) => void; editable: boolean; multiline?: boolean; rows?: number }) {
  return <label>{label}{multiline
    ? <textarea rows={rows} value={value} readOnly={!editable} onChange={e => onChange(e.target.value)} />
    : <input value={value} readOnly={!editable} onChange={e => onChange(e.target.value)} />}</label>
}

function ActionBody({ type, draft, setDraft, editable, preview }: { type: string; draft: Payload; setDraft: (p: Payload) => void; editable: boolean; preview: Payload }) {
  const ws = useWorkspace()
  const set = (patch: Payload) => setDraft({ ...draft, ...patch })
  switch (type) {
    case 'publish_protocol': {
      const items = preview.items as S['ProtocolItemOut'][]
      return <>
        <p className="muted">В протокол войдут {items.length} пунктов, поручений будет создано: {preview.task_count}. Править пункты можно в редакторе протокола до подтверждения.</p>
        <ul className="action-list">{items.map(i => <li key={i.id}>
          <Badge tone={i.kind === 'decision' ? 'success' : i.kind === 'task' ? 'info' : i.kind === 'proposal' ? 'warning' : 'neutral'}>{KIND_LABEL[i.kind]}</Badge>
          <span>{i.statement}{i.kind === 'task' && <small> · {employeeName(ws, i.owner_employee_id)} · {i.due_at ? dateTime(i.due_at) : 'без срока'}</small>}</span>
          {i.decision_status && i.kind !== 'task' && <small className="muted">{DECISION_STATUS_LABEL[i.decision_status]}</small>}
        </li>)}</ul></>
    }
    case 'create_event':
      return <div className="form-grid">
        <Field label="Название встречи" value={draft.title ?? ''} onChange={v => set({ title: v })} editable={editable} />
        <div className="kv"><span>Когда</span><strong>{weekday(draft.starts_at)} {timeOnly(draft.starts_at)}–{timeOnly(draft.ends_at)}</strong></div>
        <div className="kv"><span>Участники</span><strong>{(draft.attendee_ids ?? []).map((e: string) => employeeName(ws, e)).join(', ')}</strong></div>
        {(draft.agenda ?? []).length > 0 && <div className="kv"><span>Повестка</span><ol>{draft.agenda.map((a: Payload, i: number) => <li key={i}>{a.title}{a.minutes ? ` (${a.minutes} мин)` : ''}</li>)}</ol></div>}
        <Field label="Текст приглашения" value={draft.message ?? ''} onChange={v => set({ message: v })} editable={editable} multiline />
        <p className="muted small">К письму будет приложен файл приглашения .ics. Свободное время участников перепроверяется в момент подтверждения.</p>
      </div>
    case 'send_summary': case 'send_reminders':
      return <div className="letters">{(draft.letters ?? []).map((l: Payload, i: number) => <details key={l.recipient_id} open={i === 0} className="letter">
        <summary><strong>{employeeName(ws, l.recipient_id)}</strong> <small className="muted">· {l.subject}</small></summary>
        <Field label="Тема" value={l.subject} editable={editable} onChange={v => { const letters = [...draft.letters]; letters[i] = { ...l, subject: v }; set({ letters }) }} />
        <Field label="Текст письма" value={l.body} multiline rows={8} editable={editable} onChange={v => { const letters = [...draft.letters]; letters[i] = { ...l, body: v }; set({ letters }) }} />
      </details>)}</div>
    case 'send_brief':
      return <div className="form-grid">
        <div className="kv"><span>Кому</span><strong>{employeeName(ws, draft.recipient_id)} (себе)</strong></div>
        <Field label="Тема" value={draft.subject ?? ''} onChange={v => set({ subject: v })} editable={editable} />
        <Field label="Брифинг" value={draft.body ?? ''} onChange={v => set({ body: v })} editable={editable} multiline rows={14} />
      </div>
    case 'delegate_task':
      return <div className="form-grid">
        <div className="kv"><span>Поручение</span><strong>{draft.expected_hash === 'new'
          ? preview.expected_result || 'Новое поручение'
          : <TaskTitle taskId={draft.task_id} />}</strong></div>
        {editable ? <PersonSelect label="Исполнитель" value={draft.owner_employee_id} onChange={v => set({ owner_employee_id: v })} sameDepartmentAs={ws.user.department_id} />
          : <div className="kv"><span>Исполнитель</span><strong>{employeeName(ws, draft.owner_employee_id)}</strong></div>}
        <label>Срок<input type="datetime-local" value={toLocalInput(draft.due_at)} readOnly={!editable} onChange={e => set({ due_at: fromLocalInput(e.target.value) })} /></label>
        <Field label="Ожидаемый результат" value={draft.expected_result ?? ''} onChange={v => set({ expected_result: v })} editable={editable} />
        <Field label="Почему этот исполнитель" value={draft.reason ?? ''} onChange={v => set({ reason: v })} editable={editable} multiline rows={3} />
        {preview?.evidence && <div className="kv"><span>Основание</span><SourceChip source={preview.evidence} /></div>}
      </div>
    case 'create_decision':
      return <div className="form-grid">
        <Field label="Проблема" value={draft.title ?? ''} onChange={v => set({ title: v })} editable={editable} />
        <Field label="Влияние" value={draft.impact ?? ''} onChange={v => set({ impact: v })} editable={editable} multiline rows={2} />
        <Field label="Гипотеза причины (не доказанный факт)" value={draft.cause_hypothesis ?? ''} onChange={v => set({ cause_hypothesis: v })} editable={editable} multiline rows={3} />
        {(draft.cause_sources ?? []).length > 0 && <div className="kv"><span>На чём основано</span><SourceList sources={draft.cause_sources} /></div>}
        <Field label="Действие" value={draft.action_text ?? ''} onChange={v => set({ action_text: v })} editable={editable} multiline rows={2} />
        <div className="grid-2">
          {editable ? <PersonSelect label="Ответственный" value={draft.owner_employee_id} onChange={v => set({ owner_employee_id: v })} sameDepartmentAs={ws.user.department_id} />
            : <div className="kv"><span>Ответственный</span><strong>{employeeName(ws, draft.owner_employee_id)}</strong></div>}
          <label>Срок проверки<input type="datetime-local" value={toLocalInput(draft.due_at)} readOnly={!editable} onChange={e => set({ due_at: fromLocalInput(e.target.value) })} /></label>
        </div>
        <label>Приоритет<select value={draft.priority} disabled={!editable} onChange={e => set({ priority: e.target.value })}>
          <option value="high">Высокий</option><option value="medium">Средний</option><option value="low">Низкий</option></select></label>
        <p className="muted small">Базовое значение показателя сервер зафиксирует в момент подтверждения. После срока AI-ассистент пересчитает показатель и покажет, сработало ли решение.</p>
      </div>
    case 'update_task': {
      const ch: Payload = draft.changes ?? {}
      const setCh = (patch: Payload) => set({ changes: { ...ch, ...patch } })
      return <div className="form-grid">
        <div className="kv"><span>Поручение</span><strong><TaskTitle taskId={draft.task_id} /></strong></div>
        <p className="muted small">Меняются только перечисленные поля. Если поручение успеют изменить до подтверждения — карточку нужно будет создать заново.</p>
        {'title' in ch && <Field label="Формулировка" value={ch.title ?? ''} onChange={v => setCh({ title: v })} editable={editable} />}
        {'owner_employee_id' in ch && (editable
          ? <PersonSelect label="Ответственный" value={ch.owner_employee_id} onChange={v => setCh({ owner_employee_id: v || null })} sameDepartmentAs={ws.user.department_id} allowEmpty />
          : <div className="kv"><span>Ответственный</span><strong>{employeeName(ws, ch.owner_employee_id)}</strong></div>)}
        {'due_at' in ch && <label>Срок<input type="datetime-local" value={toLocalInput(ch.due_at)} readOnly={!editable} onChange={e => setCh({ due_at: fromLocalInput(e.target.value) })} /></label>}
        {'status' in ch && <label>Статус<select value={ch.status} disabled={!editable} onChange={e => setCh({ status: e.target.value })}>
          {Object.entries(TASK_STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>}
        {'expected_result' in ch && <Field label="Ожидаемый результат" value={ch.expected_result ?? ''} onChange={v => setCh({ expected_result: v })} editable={editable} multiline rows={2} />}
        {'result' in ch && <Field label="Результат" value={ch.result ?? ''} onChange={v => setCh({ result: v })} editable={editable} multiline rows={2} />}
        {preview?.evidence && <div className="kv"><span>Основание</span><SourceChip source={preview.evidence} /></div>}
      </div>
    }
    default:
      return <pre className="raw">{JSON.stringify(draft, null, 2)}</pre>
  }
}
