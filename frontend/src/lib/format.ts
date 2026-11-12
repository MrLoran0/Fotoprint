const TZ = 'Europe/Minsk'

export function dateTime(value?: string | null) {
  if (!value) return '—'
  return new Date(value).toLocaleString('ru-RU', { timeZone: TZ, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function dateOnly(value?: string | null) {
  if (!value) return '—'
  const d = value.length === 10 ? new Date(`${value}T12:00:00+03:00`) : new Date(value)
  return d.toLocaleDateString('ru-RU', { timeZone: TZ, day: '2-digit', month: '2-digit', year: 'numeric' })
}

export function timeOnly(value?: string | null) {
  if (!value) return ''
  return new Date(value).toLocaleTimeString('ru-RU', { timeZone: TZ, hour: '2-digit', minute: '2-digit' })
}

export function weekday(value: string) {
  return new Date(value).toLocaleDateString('ru-RU', { timeZone: TZ, weekday: 'short', day: '2-digit', month: '2-digit' })
}

export function num(value?: number | null, unit = '') {
  if (value === null || value === undefined) return 'нет данных'
  const s = Number.isInteger(value) ? String(value) : value.toLocaleString('ru-RU', { maximumFractionDigits: 1 })
  return unit === 'шт' ? `${s} шт` : `${s}${unit}`
}

/** Значение datetime-local (в поясе компании) ↔ ISO со смещением */
const inputDate = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

export function toLocalInput(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return ''
  const parts = Object.fromEntries(inputDate.formatToParts(date).map(part => [part.type, part.value]))
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`
}

export function fromLocalInput(value: string) {
  return value ? `${value}:00+03:00` : null
}

export const ROLE_LABEL: Record<string, string> = { director: 'Директор', production_head: 'Руководитель производства' }

export const SOURCE_LABEL: Record<string, { icon: string; label: string }> = {
  document: { icon: '▤', label: 'Документ' },
  email: { icon: '✉', label: 'Почта' },
  telegram: { icon: '➤', label: 'Telegram' },
  chat: { icon: '◍', label: 'Чат' },
  transcript: { icon: '◷', label: 'Расшифровка' },
  task: { icon: '☑', label: 'Поручение' },
  metric: { icon: '▥', label: 'Показатель' },
}

export const KIND_LABEL: Record<string, string> = {
  decision: 'Решение', proposal: 'Предложение', discussion: 'Обсуждение', question: 'Вопрос', risk: 'Риск', task: 'Задача',
}

export const DECISION_STATUS_LABEL: Record<string, string> = {
  decided: 'решено', proposed: 'предложено', discussed: 'обсуждалось', needs_clarification: 'требует уточнения',
}

export const TASK_STATUS_LABEL: Record<string, string> = {
  assigned: 'назначено', accepted: 'принято', in_progress: 'в работе', done: 'выполнено', cancelled: 'отменено',
}

export const ORIGIN_LABEL: Record<string, string> = {
  explicit: 'сказано явно', suggested: 'предложено ассистентом', user_entered: 'введено вручную', unknown: 'неизвестно',
}

export const DECISION_LOOP_LABEL: Record<string, { label: string; tone: string }> = {
  open: { label: 'Открыто', tone: 'neutral' },
  in_progress: { label: 'В работе', tone: 'info' },
  effective: { label: 'Сработало', tone: 'success' },
  partial: { label: 'Частично', tone: 'warning' },
  ineffective: { label: 'Не сработало', tone: 'danger' },
  no_data: { label: 'Нет данных', tone: 'neutral' },
  cancelled: { label: 'Отменено', tone: 'neutral' },
}

export const METRIC_TONE: Record<string, string> = { ok: 'success', warning: 'warning', critical: 'danger', no_data: 'neutral' }

export const ACTION_TITLE: Record<string, string> = {
  publish_protocol: 'Публикация протокола',
  create_event: 'Приглашение на встречу',
  send_summary: 'Рассылка итогов встречи',
  delegate_task: 'Поручить исполнителю',
  update_task: 'Изменение поручения',
  send_reminders: 'Напоминания о просрочках',
  send_brief: 'Утренний брифинг',
  create_decision: 'Управленческое решение',
}

export function plural(n: number, one: string, few: string, many: string) {
  const m10 = n % 10, m100 = n % 100
  return `${n} ${m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many}`
}
