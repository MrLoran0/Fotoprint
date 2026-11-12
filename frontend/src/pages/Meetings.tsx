import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Feedback } from '../components/Feedback'
import { api, ApiError, id, type S } from '../api/client'
import { PeoplePicker, PersonSelect } from '../components/PeoplePicker'
import { AudioProgress, AudioRoute } from '../components/AudioRecognition'
import { ProtocolPrintButton } from '../components/ProtocolPrintButton'
import { SourceList } from '../components/Sources'
import { Badge, Button, Card, Empty, ErrorNote, Loading } from '../components/ui'
import { DECISION_STATUS_LABEL, KIND_LABEL, ORIGIN_LABEL, dateOnly, dateTime, fromLocalInput, plural, timeOnly, toLocalInput, weekday } from '../lib/format'
import { employeeName, useDraft, useLoad, useWorkspace } from '../lib/hooks'

const tabs = ['prepare', 'protocol'] as const
type MeetingTab = typeof tabs[number]
const PREP_STEPS = ['Укажите тему и участников', 'Проверьте повестку и источники', 'Выберите время и подтвердите приглашение']
const PROTOCOL_STEPS = ['Выберите встречу и вставьте расшифровку', 'Проверьте пункты и цитаты', 'Подтвердите публикацию и поручения']

export function MeetingsPage({ params }: { params: Record<string, string> }) {
  const ws = useWorkspace()
  const tab = params.tab === 'protocol' ? 'protocol' : 'prepare'
  const setTab = (next: MeetingTab) => ws.go('meetings', { tab: next })
  const tabList = useRef<HTMLDivElement>(null)
  function moveTab(event: KeyboardEvent<HTMLButtonElement>, current: MeetingTab) {
    const index = tabs.indexOf(current)
    let next: MeetingTab
    switch (event.key) {
      case 'ArrowLeft': next = tabs[(index + tabs.length - 1) % tabs.length]; break
      case 'ArrowRight': next = tabs[(index + 1) % tabs.length]; break
      case 'Home': next = tabs[0]; break
      case 'End': next = tabs[tabs.length - 1]; break
      default: return
    }
    event.preventDefault()
    setTab(next)
    tabList.current?.querySelector<HTMLButtonElement>(`#meeting-tab-${next}`)?.focus()
  }
  return <div className="page">
    <div className="page-heading"><div><span className="eyebrow">Встречи</span><h1>От повестки до поручений</h1>
      <p className="muted">Ассистент собирает повестку из документов, переписки и незакрытых поручений, находит время, а после встречи превращает расшифровку в протокол с цитатами.</p></div></div>
    <div className="tabs" role="tablist" aria-label="Этап встречи" ref={tabList}>
      {tabs.map(value => <button key={value} id={`meeting-tab-${value}`} role="tab" aria-selected={tab === value}
        aria-controls="meeting-tab-panel" tabIndex={tab === value ? 0 : -1} className={tab === value ? 'on' : ''}
        onKeyDown={event => moveTab(event, value)} onClick={() => setTab(value)}>
        {value === 'prepare' ? 'Подготовка и приглашение' : 'Протокол и поручения'}
      </button>)}
    </div>
    <div id="meeting-tab-panel" role="tabpanel" aria-labelledby={`meeting-tab-${tab}`}>
      {tab === 'prepare' ? <Prepare /> : <Protocol />}
    </div>
  </div>
}

// ---------------------------------------------------------------- подготовка

function agendaBadge(point: S['AgendaPoint']) {
  // В текущем контракте тип этих пунктов передаётся префиксом заголовка.
  if (!point.sources?.length) return null
  if (point.title.startsWith('Проверка решения:')) return <Badge tone="info">Проверка решения</Badge>
  if (point.title.startsWith('Обещание в переписке (')) return <Badge tone="info">Обещание</Badge>
  if (point.title.startsWith('Противоречие с реестром:')) return <Badge tone="warning">Противоречие</Badge>
  return null
}

function Prepare() {
  const ws = useWorkspace()
  const meetings = useLoad(() => api.get<S['MeetingOut'][]>('/meetings'), [])
  const [topic, setTopic] = useDraft('prep-topic', 'Рост брака фотокниг после смены бумаги')
  const [people, setPeople] = useState<string[]>(() => ws.user.employee_id ? [ws.user.employee_id] : [])
  const [previous, setPrevious] = useState('')
  const [prep, setPrep] = useState<S['MeetingPrepOut']>()
  const [slots, setSlots] = useState<S['SlotOut'][]>()
  const [slot, setSlot] = useState<S['SlotOut']>()
  const [duration, setDuration] = useState(45)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const locked = busy !== ''

  function clearSlots() {
    setSlots(undefined); setSlot(undefined)
  }

  function clearAgenda() {
    setPrep(undefined); setError(''); clearSlots()
  }

  async function run<T>(key: string, fn: () => Promise<T>) {
    setBusy(key); setError('')
    try { return await fn() } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  const prepare = () => run('prep', async () => {
    clearAgenda()
    setPrep(await api.post<S['MeetingPrepOut']>('/meetings/prepare', { topic, participant_ids: people, previous_meeting_id: previous || null }))
  })
  const findSlots = () => run('slots', async () => { clearSlots(); setSlots(await api.post<S['SlotOut'][]>('/calendar/free-slots', { participant_ids: people, duration_minutes: duration, days_ahead: 5 })) })
  const invite = () => run('invite', async () => {
    if (!slot || !prep) return
    const returned = prep.agenda.filter(a => a.returned_task_id).length
    const lines = prep.agenda.map((a, i) => `${i + 1}. ${a.title}${a.minutes ? ` — ${a.minutes} мин` : ''}`).join('\n')
    const message = `Коллеги, приглашаю на встречу «${topic}».\n\nПовестка (${plural(prep.agenda.length, 'пункт', 'пункта', 'пунктов')}`
      + `${returned ? `, из них незакрытых поручений прошлой встречи — ${returned}` : ''}):\n${lines}\n\nМатериалы и источники — в AI-ассистенте.`
    ws.openAction(await api.post<S['ActionOut']>('/calendar/events/proposal', {
      title: topic, topic, starts_at: slot.starts_at, ends_at: slot.ends_at, timezone: 'Europe/Minsk', attendee_ids: people,
      agenda: prep.agenda.map(a => ({ title: a.title, minutes: a.minutes ?? null })), message,
    }))
  })

  const byDay = useMemo(() => {
    const out: Record<string, S['SlotOut'][]> = {}
    for (const s of slots ?? []) (out[weekday(s.starts_at)] ??= []).push(s)
    return out
  }, [slots])

  return <div className="grid-main">
    <Card title="Что за встреча" icon="◷">
      <div className="form-grid">
        <label>Тема<input value={topic} disabled={locked} onChange={e => { setTopic(e.target.value); clearAgenda() }} /></label>
        <label>Продолжение встречи<select value={previous} disabled={locked} onChange={e => { setPrevious(e.target.value); clearAgenda() }}>
          <option value="">Найти по теме автоматически</option>
          {(meetings.data ?? []).map(m => <option key={m.id} value={m.id}>{dateOnly(m.starts_at)} · {m.title}</option>)}
        </select></label>
        <ErrorNote error={meetings.error} onRetry={meetings.reload} />
        <PeoplePicker value={people} disabled={locked} onChange={value => { setPeople(value); clearAgenda() }} />
        <ErrorNote error={error} />
        <Button kind="primary" busy={busy === 'prep'} disabled={locked || !topic.trim() || people.length === 0} onClick={() => void prepare()}>Собрать повестку</Button>
      </div>
    </Card>
    <div className="stack">
      {!prep ? <Card><Empty icon="✦" steps={PREP_STEPS}>Соберите повестку с источниками и незакрытыми поручениями.</Empty></Card> : <>
        <Card title="Повестка" icon="☰" subtitle={prep.meeting_id ? `Продолжение встречи ${prep.meeting_id}` : 'Новая тема'}>
          {prep.agenda.length ? <ol className="agenda">{prep.agenda.map((a, i) => <li key={i} className={a.returned_task_id ? 'returned' : ''}>
            <div><strong>{a.title}</strong>{a.minutes && <small className="muted"> · {a.minutes} мин</small>}
              {a.returned_task_id && <Badge tone="warning">вернулось незакрытым</Badge>}{agendaBadge(a)}</div>
            <SourceList sources={a.sources ?? []} />
          </li>)}</ol> : <Empty>В повестке пока нет пунктов.</Empty>}
          {prep.questions.length > 0 && <><h3>Вопросы к решению</h3><ul className="questions">{prep.questions.map((q, i) => <li key={i}>{q.text}<SourceList sources={q.sources ?? []} /></li>)}</ul></>}
          {prep.open_tasks.length > 0 && <><h3>Открытые поручения участников</h3><ul className="task-mini">{prep.open_tasks.map(t => <li key={t.id}>
            <strong>{t.title}</strong><small>{employeeName(ws, t.owner_employee_id)} · {t.due_at ? `срок ${dateTime(t.due_at)}` : 'без срока'}{t.overdue && ' · просрочено'}</small></li>)}</ul></>}
        </Card>
        <Card title="Время и приглашение" icon="▦">
          <div className="row">
            <label className="inline">Длительность<select value={duration} disabled={locked} onChange={e => { setDuration(Number(e.target.value)); clearSlots(); setError('') }}>{[30, 45, 60, 90].map(n => <option key={n} value={n}>{n} мин</option>)}</select></label>
            <Button busy={busy === 'slots'} disabled={locked} onClick={() => void findSlots()}>Найти свободное время</Button>
          </div>
          {slots && (slots.length === 0 ? <Empty>Нет общего свободного времени на 5 рабочих дней.</Empty> :
            <div className="slots">{Object.entries(byDay).map(([day, list]) => <div key={day} className="slot-day"><strong>{day}</strong>
              <div>{list.slice(0, 8).map(s => <button key={s.starts_at} disabled={locked} aria-pressed={slot?.starts_at === s.starts_at} className={`slot ${slot?.starts_at === s.starts_at ? 'on' : ''}`} onClick={() => setSlot(s)}>{timeOnly(s.starts_at)}</button>)}</div>
            </div>)}</div>)}
          {slot && <Button kind="primary" disabled={locked} busy={busy === 'invite'} onClick={() => void invite()}>Подготовить приглашение на {weekday(slot.starts_at)} {timeOnly(slot.starts_at)}</Button>}
        </Card>
      </>}
    </div>
  </div>
}

// ---------------------------------------------------------------- протокол

type Item = S['ProtocolItemOut']
const itemKinds: Item['kind'][] = ['decision', 'task', 'proposal', 'risk', 'question', 'discussion']
const itemStatuses: NonNullable<Item['decision_status']>[] = ['decided', 'proposed', 'discussed', 'needs_clarification']

function Protocol() {
  const meetings = useLoad(() => api.get<S['MeetingOut'][]>('/meetings'), [])
  const [meetingId, setMeetingId] = useDraft('selected-meeting')
  const dirtyRef = useRef(false)
  const meeting = meetings.data?.find(m => m.id === meetingId)
  const choose = (next: string) => {
    if (dirtyRef.current && !window.confirm('В протоколе есть несохранённые правки. Перейти к другой встрече и потерять их?')) return
    dirtyRef.current = false
    setMeetingId(next)
  }
  const picker = <label>Встреча<select value={meeting?.id ?? ''} onChange={e => choose(e.target.value)}>
    <option value="">{meetings.loading ? 'Загружаем встречи…' : 'Выберите встречу'}</option>
    {meetings.data?.map(m => <option key={m.id} value={m.id}>{dateOnly(m.starts_at)} · {m.title}</option>)}
  </select></label>
  const meetingError = <ErrorNote error={meetings.error} onRetry={meetings.reload} />
  if (meeting) return <ProtocolEditor key={meeting.id} meeting={meeting} picker={picker} onDirty={d => { dirtyRef.current = d }} />
  return <div className="protocol-layout">
    <Card title="Расшифровка" icon="✎">
      <div className="form-grid">{picker}{meetingError}{meetings.data?.length === 0 && <Empty>Нет доступных встреч.</Empty>}</div>
    </Card>
    <Card><Empty icon="▤" steps={PROTOCOL_STEPS}>Превратите расшифровку в протокол и поручения с цитатами.</Empty></Card>
  </div>
}

type NewItem = { key: number; segment_id: number; kind: Item['kind']; statement: string }

function ProtocolEditor({ meeting, picker, onDirty }: { meeting: S['MeetingOut']; picker: React.ReactNode; onDirty: (dirty: boolean) => void }) {
  const ws = useWorkspace()
  const sttStatus = useLoad(() => api.get<S['STTStatusOut']>('/stt/status'), [])
  const meetingId = meeting.id
  const [text, setText] = useDraft(`transcript-${meetingId}`, '')
  const [savedDraftId, setSavedDraftId] = useDraft(`protocol-${meetingId}`)
  const [draft, setDraft] = useState<S['ProtocolDraftOut']>()
  const [items, setItems] = useState<Item[]>([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const [focusSeg, setFocusSeg] = useState<number>()
  const [newItems, setNewItems] = useState<NewItem[]>([])
  const [summary, setSummary] = useState('')
  const [audio, setAudio] = useState<S['AudioTranscriptOut']>()
  const [uploadPercent, setUploadPercent] = useState(0)
  const [speakers, setSpeakers] = useState<Record<string, string>>({})
  const filePicker = useRef<HTMLInputElement>(null)
  const audioUpload = useRef<AbortController>(null)
  const locked = busy !== ''
  const alive = useRef(false)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; audioUpload.current?.abort() }
  }, [])

  async function run(key: string, fn: () => Promise<void>) {
    setBusy(key); setError('')
    try { await fn() }
    catch (e) {
      setError((e as Error).message)
      if (key === 'save' && e instanceof ApiError && e.status === 409) setConflict(true)
    } finally { setBusy('') }
  }
  function loadDraft(d: S['ProtocolDraftOut']) {
    setDraft(d); setItems(d.items); setConflict(false); setNewItems([]); setSummary(d.summary)
  }
  const loadSample = () => run('sample', async () => {
    const rows = await api.get<{ text: string }[]>(`/meetings/${id(meetingId)}/transcripts`)
    if (rows[0]) {
      setText(rows[0].text); setAudio(undefined); setSpeakers({})
    } else setError('У этой встречи нет сохранённой расшифровки.')
  })
  const uploadRecording = (file: File) => run('audio', async () => {
    if (!/\.(mp3|m4a|wav|webm|ogg)$/i.test(file.name)) throw new Error('Формат записи не поддерживается. Используйте MP3, M4A, WAV, WebM или OGG.')
    const limit = sttStatus.data?.max_mb
    if (limit && file.size > limit * 1024 * 1024) throw new Error(`Запись больше допустимого размера (${limit} МБ).`)
    const upload = new AbortController()
    audioUpload.current = upload
    setUploadPercent(0)
    try {
      const result = await api.uploadAudio(`/meetings/${id(meetingId)}/audio`, file, upload.signal, setUploadPercent)
      if (!upload.signal.aborted) { setAudio(result); setText(result.text); setSpeakers({}) }
    } finally {
      audioUpload.current = null
    }
  })
  function matchSpeaker(speaker: string, employeeId: string) {
    const label = (value: string) => value ? employeeName(ws, value) : speaker
    const previous = label(speakers[speaker])
    const next = label(employeeId)
    // Меняем только подпись говорящего в текущем тексте, сохраняя внесённые правки.
    setText(text.replace(/^([ \t]*(?:\[\d{1,2}:\d{2}(?::\d{2})?\][ \t]*)?)([^:\[\]\r\n]{2,60}?)(:[ \t]*)/gm,
      (line, prefix: string, name: string, delimiter: string) =>
        name.trim() === previous ? `${prefix}${next}${delimiter}` : line))
    setSpeakers({ ...speakers, [speaker]: employeeId })
  }
  const extract = () => run('extract', async () => {
    const d = await api.post<S['ProtocolDraftOut']>(`/meetings/${id(meetingId)}/transcript`, { text })
    loadDraft(d); setSavedDraftId(d.id); setFocusSeg(undefined)
  })
  const originals = new Map(draft?.items.map(item => [item.id, item]))
  const changed = items.flatMap(item => {
    const before = originals.get(item.id)
    if (!before) return []
    const patch: S['ProtocolItemPatch'] = { id: item.id }
    if (item.statement !== before.statement && (!item.removed || item.statement.trim())) patch.statement = item.statement
    if (item.kind !== before.kind) patch.kind = item.kind
    if (item.decision_status !== before.decision_status) patch.decision_status = item.decision_status
    if (item.owner_employee_id !== before.owner_employee_id) patch.owner_employee_id = item.owner_employee_id
    if (item.due_at !== before.due_at) patch.due_at = item.due_at
    if (item.removed !== before.removed) patch.removed = item.removed
    return Object.keys(patch).length > 1 ? [patch] : []
  })
  const summaryChanged = draft != null && summary.trim() !== draft.summary.trim()
  const editCount = changed.length + newItems.length + (summaryChanged ? 1 : 0)
  const dirty = editCount > 0
  useEffect(() => { onDirty(dirty) }, [dirty, onDirty])
  useEffect(() => {
    if (!dirty) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  const save = () => run('save', async () => {
    if (!draft) return
    const d = await api.patch<S['ProtocolDraftOut']>(`/drafts/${id(draft.id)}`, {
      revision: draft.revision, items: changed,
      new_items: newItems.map(n => ({ kind: n.kind, statement: n.statement.trim(), segment_id: n.segment_id })),
      ...(summaryChanged ? { summary: summary.trim() } : {}),
    })
    loadDraft(d)
  })
  const addFromSegment = (segment: S['TranscriptSegment']) =>
    setNewItems(list => [...list, { key: Date.now() + segment.n, segment_id: segment.n, kind: 'task', statement: segment.text.slice(0, 500) }])
  const updateNew = (key: number, patch: Partial<NewItem>) => setNewItems(list => list.map(n => n.key === key ? { ...n, ...patch } : n))
  const reloadDraft = () => run('reload', async () => {
    loadDraft(await api.get<S['ProtocolDraftOut']>(`/drafts/${id(savedDraftId)}`))
  })
  const publish = () => run('publish', async () => {
    if (!draft) return
    const path = `/drafts/${id(draft.id)}`
    const action = draft.status === 'published'
      ? await api.post<S['ActionOut']>(`${path}/summary-proposal`)
      : (await api.post<S['ActionOut'][]>(`${path}/proposals`)).find(card => card.type === 'publish_protocol')
    if (action && alive.current) ws.openAction(action)
  })
  useEffect(() => {
    if (!savedDraftId || dirty || locked) return
    let active = true
    api.get<S['ProtocolDraftOut']>(`/drafts/${id(savedDraftId)}`).then(d => {
      if (active) loadDraft(d)
    }).catch(e => {
      if (!active) return
      if (e instanceof ApiError && e.status === 404) setSavedDraftId('')
      else setError((e as Error).message)
    })
    return () => { active = false }
  }, [ws.refreshTick, savedDraftId, dirty, locked, setSavedDraftId])
  const published = draft?.status === 'published'
  const update = (itemId: string, patch: Partial<Item>) => setItems(list => list.map(i => i.id === itemId ? { ...i, ...patch } : i))

  return <div className="protocol-layout">
    <div className="stack">
      <Card title="Расшифровка" icon="✎">
        <div className="form-grid">
          {picker}
          <p className="muted small">Участники: {meeting.participant_ids.map(p => employeeName(ws, p)).join(', ')}</p>
          <label>Текст расшифровки<textarea rows={10} maxLength={60000} value={text} disabled={locked} onChange={e => setText(e.target.value)} placeholder="[00:14] Анна Смирнова: Решили вернуть входной контроль…" /></label>
          {audio && <>
            <div className="row"><AudioRoute route={audio.route} /></div>
            {audio.warnings.map(warning => <p key={warning} className="muted small">{warning}</p>)}
            {[...new Set(audio.segments.map(segment => segment.speaker))].map(speaker =>
              <label key={speaker}>{speaker}<select value={speakers[speaker] ?? ''} disabled={locked} onChange={e => matchSpeaker(speaker, e.target.value)}>
                <option value="">Оставить подпись «{speaker}»</option>
                {meeting.participant_ids.map(person => <option value={person} key={person}>{employeeName(ws, person)}</option>)}
              </select></label>)}
          </>}
          {text.length > 60000 && <ErrorNote error="Для протокола оставьте до 60 000 символов расшифровки." />}
          <ErrorNote error={error} />
          <input ref={filePicker} type="file" accept=".mp3,.m4a,.wav,.webm,.ogg" hidden onChange={e => {
            const file = e.target.files?.[0]
            e.target.value = ''
            if (file) void uploadRecording(file)
          }} />
          {sttStatus.data?.available && sttStatus.data.route === 'external' &&
            <p className="muted small">Запись будет передана внешнему сервису распознавания.</p>}
          <div className="row">
            <Button disabled={locked} busy={busy === 'sample'} onClick={() => void loadSample()}>Подставить расшифровку встречи</Button>
            <Button disabled={locked || sttStatus.loading} busy={busy === 'audio'} onClick={() => filePicker.current?.click()}>Загрузить запись встречи</Button>
            <Button kind="primary" disabled={locked || dirty || !text.trim() || text.length > 60000} busy={busy === 'extract'} onClick={() => void extract()}>Составить протокол</Button>
          </div>
          {busy === 'audio' && <AudioProgress percent={uploadPercent} />}
          {busy === 'extract' && <Loading label="Ассистент раскладывает расшифровку на решения, предложения и задачи…" />}
        </div>
      </Card>
      {draft && <Card title="Исходные реплики" icon="◷" subtitle="Каждый пункт протокола опирается на цитату">
        <ol className="segments">{draft.segments.map(s => <li key={s.n} className={focusSeg === s.n ? 'focus' : ''} id={`seg-${s.n}`} tabIndex={-1}>
          <span className="seg-n">{s.n}</span><span>{s.ts && <small className="muted">[{s.ts}] </small>}<strong>{s.speaker}:</strong> {s.text}
            {s.injection_flag && <Badge tone="warning">Подозрительная инструкция</Badge>}</span>
          {draft.status !== 'published' && <button type="button" className="text-button seg-add" disabled={locked} title="Модель пропустила пункт — добавить его к этой реплике"
            onClick={() => addFromSegment(s)}>+ пункт</button>}</li>)}</ol>
      </Card>}
    </div>
    <div>
      {!draft ? <Card><Empty icon="▤" steps={PROTOCOL_STEPS}>Превратите расшифровку в протокол и поручения с цитатами.</Empty></Card> :
        <Card title={published ? 'Протокол опубликован' : 'Черновик протокола'} icon={published ? '✓' : '▤'} subtitle={draft.summary}
          actions={<Badge tone={draft.route === 'local' ? 'success' : 'info'}>{draft.route === 'local' ? '🔒 в контуре' : '☁ API'} · {draft.model}</Badge>}>
          {published && <div className="notice success">
            <span>Протокол опубликован, поручения созданы и видны в реестре. Осталось разослать участникам итоги — каждому его поручения и общие решения.</span>
            <span className="row"><ProtocolPrintButton protocol={draft} meeting={meeting} />
              <Button kind="ghost" onClick={() => ws.go('tasks')}>Поручения</Button>
              <Button kind="primary" disabled={locked} busy={busy === 'publish'} onClick={() => void publish()}>Разослать итоги →</Button></span>
          </div>}
          {!published && <label className="summary-edit">Краткое содержание
            <textarea rows={2} maxLength={2000} value={summary} disabled={locked} onChange={e => setSummary(e.target.value)} /></label>}
          {newItems.length > 0 && <section className="item-group new-items"><h3>Добавлено вами <small className="muted">{newItems.length} · сохраните правки</small></h3>
            {newItems.map(n => <article key={n.key} className="pitem">
              <div className="pitem-top"><Badge tone="info">новый пункт</Badge><Badge tone="success" title="Основание — реплика расшифровки">✓ цитата</Badge>
                <button className="text-button push" onClick={() => setNewItems(list => list.filter(x => x.key !== n.key))}>Убрать</button></div>
              <input className="pitem-text" maxLength={500} value={n.statement} onChange={e => updateNew(n.key, { statement: e.target.value })} aria-label="Формулировка нового пункта" />
              <label>Вид пункта<select value={n.kind} onChange={e => updateNew(n.key, { kind: e.target.value as Item['kind'] })}>
                {itemKinds.map(value => <option key={value} value={value}>{KIND_LABEL[value]}</option>)}</select></label>
              <small className="muted">↳ реплика {n.segment_id}. Владельца и срок задачи можно указать после сохранения.</small>
            </article>)}
          </section>}
          <fieldset className="protocol-items" disabled={locked}>{itemKinds.map(kind => {
            const list = items.filter(i => i.kind === kind && (!published || !i.removed))
            if (!list.length) return null
            return <section key={kind} className="item-group"><h3>{KIND_LABEL[kind]} <small className="muted">{list.filter(i => !i.removed).length}</small></h3>
              {list.map(i => <article key={i.id} className={`pitem ${i.removed ? 'removed' : ''} ${i.needs_clarification.length ? 'flag' : ''}`}>
                <div className="pitem-top">
                  {i.decision_status && <Badge tone={i.decision_status === 'decided' ? 'success' : i.decision_status === 'needs_clarification' ? 'danger' : i.decision_status === 'proposed' ? 'warning' : 'neutral'}>{DECISION_STATUS_LABEL[i.decision_status]}</Badge>}
                  {i.quote_verified ? <Badge tone="success" title="Цитата найдена в реплике">✓ цитата</Badge> : <Badge tone="danger" title="Цитата не найдена — проверьте пункт">⚠ без подтверждения</Badge>}
                  {i.needs_clarification.map(f => <Badge key={f} tone="warning">уточнить: {f === 'owner_employee_id' ? 'владельца' : f === 'due_at' ? 'срок' : f === 'evidence_quote' ? 'цитату' : f === 'classification' ? 'тип пункта' : f}</Badge>)}
                  {!published && <button className="text-button push" onClick={() => update(i.id, { removed: !i.removed })}>{i.removed ? 'Вернуть' : 'Убрать'}</button>}
                </div>
                {!i.removed && <>
                  {published ? <p className="pitem-text static">{i.statement}{kind === 'task' && <small className="muted block">
                    {employeeName(ws, i.owner_employee_id)} · {i.due_at ? `срок ${dateTime(i.due_at)}` : 'без срока'}</small>}</p>
                  : <>
                    <input className="pitem-text" maxLength={500} value={i.statement} onChange={e => update(i.id, { statement: e.target.value })} aria-label="Формулировка" />
                    <div className="grid-2">
                      <label>Вид пункта<select value={i.kind} onChange={e => {
                        const kind = e.target.value as Item['kind']
                        const decision_status = kind === 'proposal' ? 'proposed' : kind === 'decision' ? 'needs_clarification' : 'discussed'
                        update(i.id, { kind, decision_status })
                      }}>{itemKinds.map(value => <option key={value} value={value}>{KIND_LABEL[value]}</option>)}</select></label>
                      <label>Статус пункта<select disabled={kind === 'proposal'} value={i.decision_status ?? ''} onChange={e => update(i.id, { decision_status: e.target.value as Item['decision_status'] })}>
                        {!i.decision_status && <option value="" disabled>Выберите статус</option>}
                        {itemStatuses.map(value => <option key={value} value={value}>{DECISION_STATUS_LABEL[value]}</option>)}
                      </select></label>
                    </div>
                  </>}
                  {kind === 'task' && !published && <div className="grid-2">
                    <PersonSelect label={`Владелец · ${ORIGIN_LABEL[i.owner_origin ?? 'unknown']}`} value={i.owner_employee_id} onChange={v => update(i.id, { owner_employee_id: v || null })} sameDepartmentAs={ws.user.department_id} allowEmpty />
                    <label>Срок · {ORIGIN_LABEL[i.due_origin ?? 'unknown']}<input type="datetime-local" value={toLocalInput(i.due_at)} onChange={e => update(i.id, { due_at: fromLocalInput(e.target.value) })} /></label>
                  </div>}
                  {i.segment_id != null && <button className="evidence" onClick={() => {
                    setFocusSeg(i.segment_id ?? undefined)
                    const segment = document.getElementById(`seg-${i.segment_id}`)
                    segment?.focus({ preventScroll: true })
                    segment?.scrollIntoView({ block: 'center' })
                  }}>↳ реплика {i.segment_id}: «{i.evidence_quote}»</button>}
                </>}
              </article>)}
            </section>
          })}</fieldset>
          {conflict && <div className="notice warning">
            Черновик изменился. Скопируйте нужные правки, прежде чем загрузить версию с сервера.
            <Button disabled={locked} busy={busy === 'reload'} onClick={() => void reloadDraft()}>Загрузить актуальную версию</Button>
          </div>}
          {!published && <div className="sticky-actions">
            <span className="muted small">{dirty ? `Несохранённых правок: ${editCount}` : `Ревизия ${draft.revision}`}</span>
            <Button disabled={locked || !dirty || conflict || items.some(i => !i.removed && !i.statement.trim()) || newItems.some(n => !n.statement.trim())} busy={busy === 'save'} onClick={() => void save()}>Сохранить правки</Button>
            <Button kind="primary" disabled={locked || dirty || conflict} busy={busy === 'publish'} onClick={() => void publish()}>Опубликовать и создать поручения →</Button>
          </div>}
          <Feedback targetType="protocol" targetId={draft.id} label="Протокол составлен верно?" />
        </Card>}
    </div>
  </div>
}
