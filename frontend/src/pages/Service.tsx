import { api, type S } from '../api/client'
import { Badge, Card, Empty, ErrorNote, Loading } from '../components/ui'
import { dateTime } from '../lib/format'
import { useLoad, useWorkspace } from '../lib/hooks'

const ACTION_RU: Record<string, string> = {
  'auth.login': 'Вход', 'document.open': 'Открытие документа', 'assistant.ask': 'Вопрос ассистенту', 'protocol.extract': 'Черновик протокола',
  'protocol.edit': 'Правка протокола', 'action.propose': 'Карточка действия', 'action.confirm': 'Подтверждение', 'action.cancel': 'Отмена',
  'action.edit': 'Правка карточки', 'meeting.prepare': 'Подготовка встречи', 'my_day.view': 'Мой день', 'graph.view': 'Граф',
  'metric.analysis': 'Разбор отклонения', 'decision.recheck': 'Повторная проверка', 'source.sync': 'Синхронизация источника', 'transcript.upload': 'Загрузка расшифровки',
}

export function AuditPage() {
  const ws = useWorkspace()
  const rows = useLoad(() => api.get<S['AuditOut'][]>('/audit?limit=200'), [ws.refreshTick])
  return <div className="page">
    <div className="page-heading"><div><span className="eyebrow">Аудит</span><h1>Журнал действий</h1>
      <p className="muted">{ws.user.role === 'director' ? 'Все события компании' : 'Ваши события'}: кто, когда, что сделал и с каким результатом. Тексты ответов и чувствительные данные в журнал не попадают, контакты и суммы маскируются.</p></div></div>
    <ErrorNote error={rows.error} onRetry={rows.reload} />
    <Card>{rows.loading && !rows.data ? <Loading /> : rows.data?.length === 0 ? <Empty>Событий нет.</Empty> : rows.data &&
      <div className="table-wrap" role="region" aria-label="Таблица аудита" tabIndex={0}><table><thead><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Объект</th><th>Результат</th><th>Детали</th></tr></thead>
        <tbody>{rows.data!.map(r => <tr key={r.id}>
          <td>{dateTime(r.at)}</td><td>{r.user_name ?? r.user_id ?? '—'}</td><td>{ACTION_RU[r.action] ?? r.action}</td>
          <td><small>{r.object_type} {r.object_id}</small></td>
          <td><Badge tone={r.result === 'denied' || r.result === 'error' ? 'danger' : r.result === 'refused' ? 'warning' : 'success'}>{r.result}</Badge></td>
          <td><small className="muted">{Object.entries(r.details ?? {}).slice(0, 4).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ')}</small></td>
        </tr>)}</tbody></table></div>}</Card>
  </div>
}

export function DataPage({ health, loading, error, onRetry }: {
  health?: S['HealthOut']
  loading: boolean
  error: string
  onRetry: () => void
}) {
  const llm = health?.llm
  return <div className="page">
    <div className="page-heading"><div><span className="eyebrow">Данные и модель</span><h1>Как AI-ассистент обращается с данными</h1></div></div>
    <div className="grid-2">
      <Card title="Модель" icon="✦">
        <ErrorNote error={error} onRetry={onRetry} />
        {loading && !llm && <Loading label="Проверяем состояние модели…" />}
        {llm && <dl className="decision-fields">
          <dt>Сейчас</dt><dd><Badge tone={llm.reachable ? 'success' : 'danger'}>{llm.reachable ? 'доступна' : 'недоступна'}</Badge> {llm.model}</dd>
          <dt>Маршрут</dt><dd>{llm.route === 'local' ? '🔒 в контуре компании — данные не уходят наружу' : '☁ внешний API — передаются фрагменты синтетических данных'}</dd>
          <dt>Почта</dt><dd><Badge tone={health?.smtp ? 'success' : 'danger'}>{health?.smtp ? 'шлюз доступен' : 'шлюз недоступен'}</Badge></dd>
        </dl>}
        <p className="muted">В демо используется модель по API: на ноутбуках команды умная локальная модель не помещается. Подключение локальной модели сделано штатно: любой OpenAI‑совместимый сервер (например, vLLM) на мощностях компании — одной настройкой, без изменения кода. Маршрут каждого ответа виден в интерфейсе, скрытого переключения нет.</p>
      </Card>
      <Card title="Правила" icon="◇">
        <ul className="rules">
          <li>Все данные стенда синтетические; реальных персональных данных нет.</li>
          <li>Права проверяются на сервере до поиска: закрытое не попадает ни в ответ, ни в цитату, ни в запрос к модели.</li>
          <li>Переписку (почта, Telegram, чаты) видят только её участники — даже директор не читает чужие чаты.</li>
          <li>Оклады и персональные данные не обрабатываются AI ни для одной роли.</li>
          <li>Инструкции внутри документов и сообщений — это данные, не команды; подозрительное помечается.</li>
          <li>Любое изменение — письмо, встреча, поручение, решение — только после вашего подтверждения конкретной версии карточки.</li>
          <li>Запросы, расшифровки и результаты хранятся в базе стенда до сброса; аудит маскирует контакты и суммы.</li>
        </ul>
      </Card>
    </div>
  </div>
}
