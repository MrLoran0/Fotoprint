import type { S } from '../api/client'
import { Badge } from './ui'

export function AudioProgress({ percent }: { percent: number }) {
  return <div className="row" role="status">
    <span>{percent < 100 ? `Загрузка записи · ${percent}%` : 'Распознаём речь…'}</span>
    <progress aria-label="Загрузка и распознавание записи" max={100} value={percent < 100 ? percent : undefined} />
  </div>
}

export function AudioRoute({ route }: { route: S['AudioTranscriptOut']['route'] }) {
  return <Badge tone={route === 'local' ? 'success' : 'info'}>
    {route === 'local' ? '🔒 распознано в контуре' : '☁️ внешний сервис'}
  </Badge>
}
