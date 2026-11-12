import { useState } from 'react'
import { api, type S } from '../api/client'
import { Button } from './ui'

/** Оценка результата ИИ (AI-07): 👍/👎 и, при 👎, что именно не так — уходит в POST /feedback. */
export function Feedback({ targetType, targetId, label = 'Полезно?' }: {
  targetType: S['FeedbackIn']['target_type']; targetId: string; label?: string
}) {
  const [rating, setRating] = useState<1 | -1>()
  const [comment, setComment] = useState('')
  const [state, setState] = useState<'idle' | 'busy' | 'sent'>('idle')
  const [error, setError] = useState('')

  async function send(value: 1 | -1, text = '') {
    setState('busy'); setError('')
    try { await api.post('/feedback', { target_type: targetType, target_id: targetId, rating: value, comment: text }); setState('sent') }
    catch (e) { setError((e as Error).message); setState('idle') }
  }

  if (state === 'sent') return <p className="feedback muted small">Спасибо — оценка сохранена и поможет улучшить ассистента.</p>
  return <div className="feedback">
    <span className="muted small">{label}</span>
    <button type="button" className="icon-button" aria-label="Полезно" aria-pressed={rating === 1} disabled={state === 'busy'}
      onClick={() => { setRating(1); void send(1) }}>👍</button>
    <button type="button" className="icon-button" aria-label="Есть ошибка" aria-pressed={rating === -1} disabled={state === 'busy'}
      onClick={() => setRating(-1)}>👎</button>
    {rating === -1 && <div className="feedback-form">
      <textarea rows={2} maxLength={1000} value={comment} placeholder="Что не так: неверный факт, пропущенный пункт, не тот исполнитель…"
        onChange={e => setComment(e.target.value)} aria-label="Что не так" />
      <Button kind="primary" busy={state === 'busy'} onClick={() => void send(-1, comment)}>Отправить</Button>
    </div>}
    {error && <span className="tone-danger small">{error}</span>}
  </div>
}
