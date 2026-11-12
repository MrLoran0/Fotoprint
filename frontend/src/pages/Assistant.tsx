import { useEffect, useRef, useState } from 'react'
import { api, type S } from '../api/client'
import { SourceList } from '../components/Sources'
import { Badge, Button } from '../components/ui'
import { useDraft, useWorkspace } from '../lib/hooks'

type Turn = { q: string; a?: S['AskOut']; error?: string }

const SUGGESTIONS = [
  'Какая доля брака фотокниг была в октябре?',
  'Кто взялся запросить сертификаты на Б-1102?',
  'Что решили по поставщику «БелЛист»?',
  'Какие оклады у технологов?',
  'Сколько стоит аренда склада в Гомеле?',
]

const STATUS: Record<string, { label: string; tone: string; icon: string }> = {
  answered: { label: 'Ответ подтверждён источниками', tone: 'success', icon: '✓' },
  partial: { label: 'Частичный ответ — не всё подтверждено', tone: 'warning', icon: '◐' },
  not_found: { label: 'Недостаточно данных', tone: 'neutral', icon: '∅' },
  refused: { label: 'Запрещено политикой', tone: 'danger', icon: '⛔' },
}

/** Ассистент отвечает только тем, что может подтвердить. Нет основания — так и говорит, и подсказывает, кто знает. */
export function AssistantPage() {
  const ws = useWorkspace()
  const [turns, setTurns] = useState<Turn[]>([])
  const [q, setQ] = useDraft('ask', '')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { end.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns])

  async function ask(question = q) {
    if (!question.trim() || inFlight.current) return
    inFlight.current = true
    setBusy(true); setQ('')
    setTurns(t => [...t, { q: question }])
    try {
      const a = await api.post<S['AskOut']>('/assistant/ask', { question })
      setTurns(t => t.map((x, i) => i === t.length - 1 ? { ...x, a } : x))
    } catch (e) {
      setTurns(t => t.map((x, i) => i === t.length - 1 ? { ...x, error: (e as Error).message } : x))
      setQ(value => value || question)
    } finally { inFlight.current = false; setBusy(false) }
  }

  return <div className="page assistant">
    <div className="page-heading"><div><span className="eyebrow">Ассистент</span><h1>Спросите AI-ассистента</h1>
      <p className="muted">Ответы — только по документам и переписке, доступным именно вам. Каждое утверждение — с первоисточником; нет основания — «недостаточно данных» и кто может знать.</p></div></div>
    <div className="chat">
      {turns.length === 0 && <div className="suggestions">{SUGGESTIONS.map(s => <button key={s} className="suggestion" disabled={busy} onClick={() => void ask(s)}>{s}</button>)}</div>}
      {turns.map((t, i) => <div key={i} className="turn">
        <div className="bubble me">{t.q}</div>
        {!t.a && !t.error && <div className="bubble bot thinking"><span className="spinner small" /> Ищу в разрешённых источниках…</div>}
        {t.error && <div className="bubble bot error">{t.error}</div>}
        {t.a && <Answer a={t.a} onAsk={ask} busy={busy} />}
      </div>)}
      <div ref={end} />
    </div>
    <form className="ask-form" onSubmit={e => { e.preventDefault(); void ask() }}>
      <textarea rows={2} value={q} onChange={e => setQ(e.target.value)} placeholder="Например: что известно о партии Б-1102?" aria-label="Вопрос"
        onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void ask() } }} />
      <Button kind="primary" busy={busy} disabled={!q.trim()}>Спросить</Button>
    </form>
    <p className="muted small">{ws.user.full_name}: ассистент видит только то, что доступно вам. Переписку видят лишь её участники.</p>
  </div>
}

function Answer({ a, onAsk, busy }: { a: S['AskOut']; onAsk: (q: string) => void; busy: boolean }) {
  const st = STATUS[a.status]
  return <div className={`bubble bot answer-${a.status}`}>
    <div className="answer-head"><Badge tone={st.tone}>{st.icon} {st.label}</Badge>
      <span className="muted small">{a.route === 'local' ? '🔒 модель в контуре' : '☁ модель по API'} · {a.model}</span></div>
    {a.claims.length > 0 ? <ol className="claims">{a.claims.map((c, i) => <li key={i}><p>{c.text}</p><SourceList sources={c.sources ?? []} /></li>)}</ol>
      : <p>{a.answer}</p>}
    {a.gaps.length > 0 && <div className="gaps"><strong>Не подтверждено источниками</strong><ul>{a.gaps.map((g, i) => <li key={i}>{g}</li>)}</ul></div>}
    {a.experts.length > 0 && <div className="experts"><strong>Кто может знать</strong>
      {a.experts.map(e => <div key={e.employee_id} className="expert"><span className="avatar small">{e.full_name.slice(0, 1)}</span>
        <div><strong>{e.full_name}</strong> <small className="muted">{e.position}</small><small className="block">{e.reason}</small></div></div>)}</div>}
    {a.warnings.map((w, i) => <div key={i} className="notice warning small">{w}</div>)}
    {a.status === 'refused' && <p className="muted small">Оклады и персональные данные не обрабатываются AI ни для одной роли — это зафиксировано в аудите.</p>}
    {a.status === 'not_found' && a.experts.length === 0 && <button className="text-button" disabled={busy} onClick={() => onAsk('Кто отвечает за эту тему?')}>Спросить, кто отвечает за тему →</button>}
  </div>
}
