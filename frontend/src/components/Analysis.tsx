import { useState } from 'react'
import { api, id, type S } from '../api/client'
import { METRIC_TONE, dateOnly, dateTime, num } from '../lib/format'
import { useLoad, useWorkspace } from '../lib/hooks'
import { SourceList } from './Sources'
import { Badge, Bars, Button, Drawer, ErrorNote, Loading } from './ui'

/** Разбор отклонения KPI: тренд и разрезы считает код, гипотеза — с источниками, дальше — карточка решения. */
export function AnalysisDrawer({ metricId, onClose }: { metricId: string; onClose: () => void }) {
  const ws = useWorkspace()
  const a = useLoad(() => api.get<S['MetricAnalysisOut']>(`/metrics/${id(metricId)}/analysis`), [metricId])
  const decisions = useLoad(() => api.get<S['DecisionOut'][]>('/decisions'), [metricId])
  const active = (decisions.data ?? []).filter(d => d.metric_id === metricId && (d.status === 'open' || d.status === 'in_progress'))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function propose() {
    setBusy(true); setError('')
    try { ws.openAction(await api.post<S['ActionOut']>(`/metrics/${id(metricId)}/decision-proposal`)); onClose() }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const m = a.data?.metric
  const cited = new Set((a.data?.hypothesis?.sources ?? []).map(s => s.chunk_id))
  const more = (a.data?.evidence ?? []).filter(s => !cited.has(s.chunk_id))
  return <Drawer onClose={onClose} label="Разбор отклонения" busy={busy}>
    {a.loading ? <Loading label="Считаем разрезы и ищем причины…" /> : a.error ? <ErrorNote error={a.error} onRetry={a.reload} /> : a.data && m && <>
      <span className="eyebrow">Разбор отклонения · сигнал → причина → действие</span>
      <h2>{m.name}: <span className={`tone-${METRIC_TONE[m.status]}`}>{num(m.value, m.unit)}</span> <small className="muted">при плане {num(m.plan, m.unit)}</small></h2>
      <p className="muted">{m.period} · {m.formula} · источник: {m.source_name}, обновлён {dateTime(m.updated_at)}{m.stale && <Badge tone="warning">данные устарели</Badge>}</p>

      <h3>Динамика</h3>
      <Bars unit={m.unit} plan={m.plan} higherIsBetter={m.id !== 'defect_rate'}
        rows={a.data.trend.map(t => ({ label: t.label, value: t.value, hint: t.denominator ? ` ${t.numerator}/${t.denominator}` : undefined }))} />

      {a.data.breakdowns.length > 0 && <h3>Разрезы</h3>}
      <div className="breakdowns">{a.data.breakdowns.map(b => <div key={`${b.dimension}-${b.period}`} className="breakdown">
        <strong>{b.label}</strong> <small className="muted">{b.period}</small>
        <Bars unit={m.unit} plan={m.plan} higherIsBetter={m.id !== 'defect_rate'}
          rows={b.rows.map(r => ({ label: r.key, value: r.value, hint: ` ${r.numerator}/${r.denominator}` }))} />
      </div>)}</div>

      {a.data.top_contributor && <div className="callout"><strong>Главный вклад (посчитано по данным):</strong> {a.data.top_contributor}</div>}
      {a.data.hypothesis && <div className="hypothesis">
        <Badge tone="warning">гипотеза, не доказанный факт</Badge>
        <p>{a.data.hypothesis.text}</p>
        <SourceList sources={a.data.hypothesis.sources ?? []} />
      </div>}
      {more.length > 0 && <><h3>Что ещё об этом пишут в компании</h3><SourceList sources={more} /></>}
      {a.data.suggested_action && <div className="callout soft"><strong>Предлагаемое действие:</strong> {a.data.suggested_action}</div>}
      {active.length > 0 && <div className="notice info">По этому показателю уже есть решение в работе: «{active[0].title}», проверка {dateOnly(active[0].due_at)}.{' '}
        <button className="text-button" onClick={() => { onClose(); ws.go('decisions', { id: active[0].id }) }}>Открыть решение →</button></div>}
      <ErrorNote error={error} />
      <div className="drawer-actions">
        {(m.status === 'warning' || m.status === 'critical') && <Button kind={active.length ? 'secondary' : 'primary'} busy={busy} onClick={() => void propose()}>{active.length ? 'Оформить ещё одно решение' : 'Оформить решение →'}</Button>}
        <span className="muted small">Маршрут модели: {a.data.route === 'local' ? 'в контуре' : 'внешний API'} · {a.data.model}</span>
      </div>
    </>}
  </Drawer>
}
