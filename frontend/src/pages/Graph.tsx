import { useMemo, useRef, useState } from 'react'
import { api, type S } from '../api/client'
import { SourceChip, SourceViewer } from '../components/Sources'
import { Badge, Card, Empty, ErrorNote, Loading } from '../components/ui'
import { useLoad, useWorkspace } from '../lib/hooks'

const TYPE: Record<string, { label: string; color: string; icon: string }> = {
  employee: { label: 'Сотрудник', color: '#7cc4ff', icon: '👤' },
  entity: { label: 'Сущность', color: '#b5f06c', icon: '◆' },
  meeting: { label: 'Встреча', color: '#c9a7ff', icon: '◷' },
  task: { label: 'Поручение', color: '#ffd585', icon: '☑' },
  protocol_item: { label: 'Пункт протокола', color: '#ffb3c7', icon: '▤' },
  document: { label: 'Документ', color: '#9fb0c4', icon: '▤' },
  email: { label: 'Письмо', color: '#80d7e4', icon: '✉' },
  telegram: { label: 'Telegram', color: '#5fb3f5', icon: '➤' },
  chat: { label: 'Чат', color: '#7fe0c1', icon: '◍' },
}
const FOCUSABLE = new Set(['employee', 'entity', 'task', 'meeting'])
const SOURCES = new Set(['document', 'email', 'telegram', 'chat'])
type P = { x: number; y: number }

const searchText = (text: string) => text.trim().toLocaleLowerCase('ru-RU').replaceAll('ё', 'е')

/** Небольшая силовая раскладка: отталкивание узлов, пружины по рёбрам, фокус — в центре. */
function layout(nodes: S['GraphNode'][], edges: S['GraphEdge'][], focus: string, w = 900, h = 560): Record<string, P> {
  const pos: Record<string, P> = {}
  nodes.forEach((n, i) => {
    const a = (i / Math.max(nodes.length, 1)) * Math.PI * 2
    pos[n.id] = n.id === focus ? { x: w / 2, y: h / 2 } : { x: w / 2 + Math.cos(a) * 220, y: h / 2 + Math.sin(a) * 180 }
  })
  const k = Math.sqrt((w * h) / Math.max(nodes.length, 1)) * 0.55
  for (let it = 0; it < 220; it++) {
    const disp: Record<string, P> = Object.fromEntries(nodes.map(n => [n.id, { x: 0, y: 0 }]))
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = pos[nodes[i].id], b = pos[nodes[j].id]
      const dx = a.x - b.x, dy = a.y - b.y, d = Math.max(Math.hypot(dx, dy), 0.1), f = (k * k) / d
      disp[nodes[i].id].x += (dx / d) * f; disp[nodes[i].id].y += (dy / d) * f
      disp[nodes[j].id].x -= (dx / d) * f; disp[nodes[j].id].y -= (dy / d) * f
    }
    for (const e of edges) {
      const a = pos[e.source], b = pos[e.target]
      if (!a || !b) continue
      const dx = a.x - b.x, dy = a.y - b.y, d = Math.max(Math.hypot(dx, dy), 0.1), f = (d * d) / k
      disp[e.source].x -= (dx / d) * f; disp[e.source].y -= (dy / d) * f
      disp[e.target].x += (dx / d) * f; disp[e.target].y += (dy / d) * f
    }
    const t = 18 * (1 - it / 220) + 1
    for (const n of nodes) {
      if (n.id === focus) continue
      const d = disp[n.id], len = Math.max(Math.hypot(d.x, d.y), 0.1)
      pos[n.id].x = Math.min(w - 40, Math.max(40, pos[n.id].x + (d.x / len) * Math.min(len, t) + (w / 2 - pos[n.id].x) * 0.01))
      pos[n.id].y = Math.min(h - 30, Math.max(30, pos[n.id].y + (d.y / len) * Math.min(len, t) + (h / 2 - pos[n.id].y) * 0.01))
    }
  }
  return pos
}

export function GraphPage({ focus: initial }: { focus?: string }) {
  const ws = useWorkspace()
  const options = useLoad(() => api.get<{ focus: string; label: string; type: string }[]>('/graph/focus-options'), [])
  const focus = initial || 'entity:sup-b'
  const graph = useLoad(() => api.get<S['GraphOut']>(`/graph?focus=${encodeURIComponent(focus)}`), [focus, ws.refreshTick])
  const [selectedEdge, setEdge] = useState<S['GraphEdge']>()
  const [doc, setDoc] = useState<S['SourceRef']>()
  const [hover, setHover] = useState<string>()
  const [query, setQuery] = useState('')
  const [type, setType] = useState('')
  const centerPicker = useRef<HTMLSelectElement>(null)
  const g = graph.data
  const visible = useMemo(() => {
    const term = searchText(query)
    const nodes = (g?.nodes ?? []).filter(n => (!type || n.type === type) && searchText(n.label).includes(term))
    const ids = new Set(nodes.map(n => n.id))
    const edges = (g?.edges ?? []).filter(e => ids.has(e.source) && ids.has(e.target))
    const center = g && ids.has(g.focus) ? g.focus : nodes[0]?.id ?? ''
    return { nodes, edges, positions: layout(nodes, edges, center) }
  }, [g, query, type])
  const edge = selectedEdge && visible.edges.includes(selectedEdge) ? selectedEdge : undefined
  const neighbors = visible.nodes.filter(n => n.id !== g?.focus)
  const nodeLabel = (id: string) => g?.nodes.find(n => n.id === id)?.label ?? id

  function changeFocus(next: string) {
    setQuery('')
    setEdge(undefined)
    ws.go('graph', { focus: next })
    centerPicker.current?.focus({ preventScroll: true })
  }

  function clickNode(n: S['GraphNode']) {
    if (FOCUSABLE.has(n.type)) changeFocus(n.id)
    else if (SOURCES.has(n.type)) setDoc({ source_type: n.type as S['SourceRef']['source_type'], source_id: n.ref_id, title: n.label, quote: '' })
  }

  return <div className="page">
    <div className="page-heading"><div><span className="eyebrow">Граф компании</span><h1>Кто, что и где сказал</h1>
      <p className="muted">Люди, поставщики, партии, поручения, встречи и переписка — со связями. У каждой связи есть первоисточник. Граф у каждого свой: он строится только из доступного вам.</p></div>
      <label className="graph-pick">Центр графа<select ref={centerPicker} value={focus} onChange={e => changeFocus(e.target.value)}>
        {!options.data?.some(o => o.focus === focus) && <option value={focus}>{nodeLabel(focus)}</option>}
        {options.data?.map(o => <option key={o.focus} value={o.focus}>{o.label}</option>)}
      </select></label>
    </div>
    <ErrorNote error={graph.error} onRetry={graph.reload} />
    <ErrorNote error={options.error} onRetry={options.reload} />
    <div className="graph-layout">
      <Card className="graph-card">
        <div className="graph-filters">
          <label>Поиск узла<input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Название узла" /></label>
          <label>Тип узла<select value={type} onChange={e => setType(e.target.value)}>
            <option value="">Все типы</option>
            {Object.entries(TYPE).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}
          </select></label>
        </div>
        {graph.loading ? <Loading label="Строим граф…" /> : g && <>
          <p className="muted small graph-count" role="status">Узлов: {visible.nodes.length} из {g.nodes.length} · Связей: {visible.edges.length}</p>
          {visible.nodes.length === 0 ? <Empty icon="⬡">{query.trim() || type ? 'По заданным условиям узлы не найдены.' : 'Нет доступных узлов.'}</Empty> :
          <svg viewBox="0 0 900 560" className="graph-svg" role="group" aria-label={`Граф вокруг ${nodeLabel(g.focus)}`}>
            {visible.edges.map((e, i) => {
              const a = visible.positions[e.source], b = visible.positions[e.target]
              if (!a || !b) return null
              const on = hover === e.source || hover === e.target || edge === e
              return <g key={i} aria-hidden="true" className={`gedge ${on ? 'on' : ''} ${e.evidence ? 'has-ev' : ''}`} onClick={() => setEdge(e)}>
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} />
                <line className="hit" x1={a.x} y1={a.y} x2={b.x} y2={b.y} />
                {on && <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 4}>{e.label}</text>}
              </g>
            })}
            {visible.nodes.map(n => {
              const p = visible.positions[n.id], t = TYPE[n.type] ?? TYPE.document, isFocus = n.id === g.focus
              const interactive = FOCUSABLE.has(n.type) || SOURCES.has(n.type)
              return <g key={n.id} className={`gnode ${isFocus ? 'focus' : ''} ${interactive ? 'clickable' : ''}`} transform={`translate(${p.x},${p.y})`}
                onClick={interactive ? () => clickNode(n) : undefined} onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(undefined)}
                tabIndex={interactive ? 0 : undefined} role={interactive ? 'button' : undefined} aria-label={`${t.label}: ${n.label}`}
                onKeyDown={interactive ? e => {
                  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); clickNode(n) }
                } : undefined}>
                <title>{n.label}</title>
                <circle r={isFocus ? 26 : 17} fill={t.color} />
                <text className="gicon" dy="5">{t.icon}</text>
                <text className="glabel" dy={isFocus ? 42 : 32}>{n.label.length > 34 ? n.label.slice(0, 33) + '…' : n.label}</text>
              </g>
            })}
          </svg>}
        </>}
        <div className="legend">{Object.entries(TYPE).map(([k, t]) => <span key={k}><i style={{ background: t.color }} />{t.label}</span>)}</div>
      </Card>
      <div className="stack">
        <Card title="Связь и первоисточник" icon="↔">
          {visible.edges.length > 0 && <label>Связь<select value={edge ? visible.edges.indexOf(edge) : ''}
            onChange={e => setEdge(e.target.value === '' ? undefined : visible.edges[Number(e.target.value)])}>
            <option value="">Выберите связь</option>
            {visible.edges.map((e, i) => <option key={i} value={i}>{nodeLabel(e.source)} · {e.label} · {nodeLabel(e.target)}</option>)}
          </select></label>}
          {!edge ? <p className="muted">{visible.edges.length ? 'Выберите связь, чтобы увидеть её основание.' : 'Между показанными узлами нет связей.'}</p> :
            <div className="edge-info">
              <p><strong>{nodeLabel(edge.source)}</strong> <Badge>{edge.label}</Badge> <strong>{nodeLabel(edge.target)}</strong></p>
              {edge.evidence ? <SourceChip source={edge.evidence} /> : <p className="muted small">Связь из реестра (ответственный, участник, основание) — без цитаты.</p>}
            </div>}
        </Card>
        {neighbors.length > 0 && <Card title="Что рядом" icon="⬡">
          <ul className="neighbors">{neighbors.slice(0, 30).map(n => <li key={n.id}>
            <span className="dot-color" style={{ background: (TYPE[n.type] ?? TYPE.document).color }} />
            {FOCUSABLE.has(n.type) || SOURCES.has(n.type)
              ? <button className="text-button" onClick={() => clickNode(n)}>{n.label}</button>
              : <span>{n.label}</span>}
            <small className="muted">{(TYPE[n.type] ?? TYPE.document).label}</small>
          </li>)}</ul>
        </Card>}
      </div>
    </div>
    {doc && <SourceViewer source={doc} onClose={() => setDoc(undefined)} />}
  </div>
}
