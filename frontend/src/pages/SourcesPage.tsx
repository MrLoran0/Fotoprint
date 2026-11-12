import { useState } from 'react'
import { api, id, type S } from '../api/client'
import { Badge, Button, Card, Empty, ErrorNote, Loading } from '../components/ui'
import { SOURCE_LABEL, dateTime, plural } from '../lib/format'
import { useLoad, useWorkspace } from '../lib/hooks'

const ACCESS: Record<string, string> = {
  participants: 'только участники переписки', department: 'всё подразделение', public: 'все сотрудники', confidential: 'только директор',
}
const PILOT: Record<string, string> = {
  email_eml: 'IMAP / Exchange / Microsoft Graph', telegram_export: 'Telegram Bot API (бот в группе)', chat_json: 'Битрикс24 / корпоративный мессенджер',
}

export function SourcesPage() {
  const ws = useWorkspace()
  const sources = useLoad(() => api.get<S['SourceOut'][]>('/sources'), [ws.refreshTick])
  const docs = useLoad(() => api.get<{ id: string; kind?: string }[]>('/documents'), [ws.refreshTick])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  async function sync(sid: string) {
    setBusy(sid); setError('')
    try { const s = await api.post<S['SourceOut']>(`/sources/${id(sid)}/sync`); ws.toast(s.status === 'ok' ? `Синхронизировано: ${plural(s.items_count, 'сообщение', 'сообщения', 'сообщений')}` : `Ошибка источника: ${s.last_error}`); sources.reload() }
    catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  if (sources.loading && !sources.data) return <Loading />
  const plainDocs = docs.data?.filter(d => !d.id.includes(':')).length
  return <div className="page">
    <div className="page-heading"><div><span className="eyebrow">Откуда AI-ассистент знает</span><h1>Источники</h1>
      <p className="muted">Работа живёт не только в документах: почта, Telegram-группы и рабочие чаты. Коннекторы забирают сообщения, сохраняют автора и время и связывают всё в граф компании. Права — как в источнике: переписку видят только её участники, даже директор не читает чужие чаты.</p></div></div>
    <ErrorNote error={error} />
    <ErrorNote error={sources.error} onRetry={sources.reload} />
    <div className="source-grid">
      <div className="source-card"><div className="source-card-head"><span className="source-kind">▤</span><div><strong>Хранилище документов</strong><small className="muted">регламенты, отчёты, спецификации</small></div></div>
        {plainDocs !== undefined && <div className="source-stats"><span>Доступно вам: <strong>{plural(plainDocs, 'документ', 'документа', 'документов')}</strong></span></div>}
        {docs.loading && plainDocs === undefined && <Loading label="Загружаем документы…" />}
        <ErrorNote error={docs.error} onRetry={docs.reload} />
      </div>
      {(sources.data ?? []).map(s => {
        const k = SOURCE_LABEL[s.kind] ?? SOURCE_LABEL.document
        return <div key={s.id} className={`source-card ${s.status === 'error' ? 'bad' : ''}`}>
          <div className="source-card-head"><span className="source-kind">{k.icon}</span><div><strong>{s.name}</strong><small className="muted">{k.label} · {ACCESS[s.access_level] ?? s.access_level}{s.member_count ? ` · ${s.member_count} участн.` : ''}</small></div></div>
          <div className="source-stats"><span><strong>{s.items_count}</strong> {plural(s.items_count, 'сообщение', 'сообщения', 'сообщений').split(' ')[1]}</span>
            <Badge tone={s.status === 'ok' ? 'success' : s.status === 'error' ? 'danger' : 'neutral'}>{s.status === 'ok' ? 'синхронизирован' : s.status === 'error' ? 'ошибка' : 'новый'}</Badge></div>
          <small className="muted">Обновлён {dateTime(s.last_sync_at)} · в пилоте: {PILOT[s.connector] ?? s.connector}</small>
          {s.last_error && <div className="notice error small">{s.last_error}</div>}
          {ws.user.role === 'director' && <Button kind="ghost" disabled={!!busy} busy={busy === s.id} onClick={() => void sync(s.id)}>Синхронизировать</Button>}
        </div>
      })}
    </div>
    {sources.data?.length === 0 && <Card><Empty>Вам недоступен ни один канал переписки.</Empty></Card>}
  </div>
}
