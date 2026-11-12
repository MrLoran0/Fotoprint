import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, clearDrafts, demoPassword, hasSession, login, logout, type S } from './api/client'
import { ActionDialog } from './components/ActionDialog'
import { VoiceModal } from './components/VoiceModal'
import { Badge, Button } from './components/ui'
import { ACTION_TITLE, ROLE_LABEL, dateTime } from './lib/format'
import { WorkspaceContext, useLoad, type Workspace } from './lib/hooks'
import { AssistantPage } from './pages/Assistant'
import { AuditPage, DataPage } from './pages/Service'
import { DecisionsPage } from './pages/Decisions'
import { GraphPage } from './pages/Graph'
import { Login } from './pages/Login'
import { MeetingsPage } from './pages/Meetings'
import { MyDayPage } from './pages/MyDay'
import { SourcesPage } from './pages/SourcesPage'
import { TasksPage } from './pages/Tasks'

const NAV = [
  { group: 'Работа', items: [
    { id: 'my-day', name: 'Мой день', icon: '◉', hint: 'Что требует решения сегодня' },
    { id: 'decisions', name: 'Решения', icon: '◆', hint: 'Сигнал → решение → проверка результата' },
    { id: 'meetings', name: 'Встречи', icon: '◷', hint: 'Подготовка, приглашения, протоколы' },
    { id: 'tasks', name: 'Поручения', icon: '☑', hint: 'Реестр поручений с основаниями' },
    { id: 'assistant', name: 'Ассистент', icon: '✦', hint: 'Ответы только с первоисточником' },
  ] },
  { group: 'Знания', items: [
    { id: 'sources', name: 'Источники', icon: '⇄', hint: 'Почта, Telegram, чаты, документы' },
    { id: 'graph', name: 'Граф компании', icon: '⬡', hint: 'Люди, решения и переписка — со связями' },
  ] },
  { group: 'Служебное', items: [
    { id: 'audit', name: 'Аудит', icon: '◎', hint: 'Кто, что и когда сделал' },
    { id: 'data', name: 'Данные и модель', icon: '◇', hint: 'Обработка данных, маршрут модели' },
  ] },
]
const ALL = NAV.flatMap(g => g.items)

function parseHash() {
  const [page, query] = location.hash.slice(1).split('?')
  return { page: ALL.some(n => n.id === page) ? page : 'my-day', params: Object.fromEntries(new URLSearchParams(query ?? '')) }
}

export default function App() {
  const [user, setUser] = useState<S['UserOut']>()
  const [checking, setChecking] = useState(hasSession)
  const [switching, setSwitching] = useState(false)
  const [route, setRoute] = useState(parseHash)
  const [theme, setTheme] = useState(() => localStorage.getItem('fokus-theme') || 'dark')
  const [employees, setEmployees] = useState<S['EmployeeOut'][]>([])
  const healthQuery = useLoad(() => api.get<S['HealthOut']>('/health'), [])
  const health = healthQuery.data
  const [accounts, setAccounts] = useState<S['DemoAccountOut'][]>([])
  const [action, setAction] = useState<S['ActionOut']>()
  const [voiceOpen, setVoiceOpen] = useState(false)
  const [pending, setPending] = useState<S['ActionOut'][]>([])
  const [tick, setTick] = useState(0)
  const [toastText, setToastText] = useState('')
  const [trayOpen, setTrayOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const menuButton = useRef<HTMLButtonElement>(null)
  const trayButton = useRef<HTMLButtonElement>(null)
  const sidebar = useRef<HTMLElement>(null)
  const main = useRef<HTMLElement>(null)

  useEffect(() => {
    let active = true
    if (hasSession()) api.get<S['UserOut']>('/me')
      .then(next => { if (active) setUser(next) }).catch(() => {}).finally(() => { if (active) setChecking(false) })
    if (demoPassword) api.get<S['DemoAccountOut'][]>('/auth/demo-accounts')
      .then(rows => { if (active) setAccounts(rows) }).catch(() => {})
    return () => { active = false }
  }, [])
  useEffect(() => { const fn = () => setRoute(parseHash()); window.addEventListener('hashchange', fn); return () => window.removeEventListener('hashchange', fn) }, [])
  const resetWorkspace = useCallback(() => {
    setEmployees([]); setPending([]); setAction(undefined); setVoiceOpen(false); setTrayOpen(false); setMenuOpen(false); setToastText('')
  }, [])
  const signOut = useCallback(() => {
    logout(); resetWorkspace(); setUser(undefined)
  }, [resetWorkspace])
  useEffect(() => {
    window.addEventListener('fokus-unauthorized', signOut)
    return () => window.removeEventListener('fokus-unauthorized', signOut)
  }, [signOut])
  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem('fokus-theme', theme) }, [theme])
  useEffect(() => {
    if (!user) return
    let active = true
    api.get<S['EmployeeOut'][]>('/employees').then(rows => { if (active) setEmployees(rows) }).catch(() => {})
    return () => { active = false }
  }, [user])
  useEffect(() => {
    if (!user) return
    let active = true
    api.get<S['ActionOut'][]>('/actions').then(rows => {
      if (active) setPending(rows.filter(a => a.status === 'pending' || a.status === 'failed' || a.status === 'partially_failed'))
    }).catch(() => {})
    return () => { active = false }
  }, [user, tick, route.page, trayOpen])
  useEffect(() => { if (!toastText) return; const t = setTimeout(() => setToastText(''), 5000); return () => clearTimeout(t) }, [toastText])
  useEffect(() => {
    if (menuOpen) sidebar.current?.querySelector<HTMLButtonElement>('button[aria-current="page"]')?.focus()
  }, [menuOpen])

  function closeMenu() {
    setMenuOpen(false)
    menuButton.current?.focus()
  }

  function closeTray() {
    setTrayOpen(false)
    trayButton.current?.focus()
  }

  const go = useCallback((page: string, params?: Record<string, string>) => {
    const changedPage = parseHash().page !== page
    location.hash = page + (params ? `?${new URLSearchParams(params)}` : '')
    setMenuOpen(false)
    if (changedPage) main.current?.focus()
  }, [])

  const ws: Workspace | null = useMemo(() => user ? {
    user, employees, refreshTick: tick, go,
    openAction: (card: S['ActionOut']) => setAction(card),
    toast: (m: string) => setToastText(m),
    openVoice: () => setVoiceOpen(true),
  } : null, [user, employees, tick, go])

  async function switchRole(loginName: string) {
    if (!demoPassword || switching || loginName === user?.login) return
    setSwitching(true)
    try { const next = await login(loginName, demoPassword); clearDrafts(); resetWorkspace(); setUser(next); setTick(t => t + 1) }
    catch (e) { setToastText((e as Error).message) }
    finally { setSwitching(false) }
  }

  if (checking) return <div className="splash"><div className="brand-mark big">✦</div></div>
  if (!user || !ws) return <Login accounts={accounts} onLogin={setUser} />

  const current = ALL.find(n => n.id === route.page)!
  const llm = health?.llm
  return <WorkspaceContext.Provider value={ws}>
    <div className={`app-shell ${menuOpen ? 'menu-open' : ''}`}>
      <aside ref={sidebar} className="sidebar" id="workspace-sidebar" onKeyDown={e => {
        if (menuOpen && e.key === 'Escape') { e.preventDefault(); closeMenu() }
      }}>
        <div className="brand"><div className="brand-mark">✦</div><div><strong>AI-ассистент</strong><small>команды руководителя</small></div>
          <button className="icon-button menu-close" aria-label="Закрыть меню" onClick={closeMenu}>×</button>
        </div>
        <nav aria-label="Основная навигация">{NAV.map(g => <div key={g.group} className="nav-group">
          <div className="sidebar-label">{g.group}</div>
          {g.items.map(n => <button key={n.id} className={`nav-item ${route.page === n.id ? 'active' : ''}`}
            aria-current={route.page === n.id ? 'page' : undefined} title={n.hint} onClick={() => {
              go(n.id)
              main.current?.focus()
            }}>
            <span className="nav-icon" aria-hidden="true">{n.icon}</span>{n.name}
            {n.id === 'my-day' && pending.length > 0 && <span className="nav-count" aria-label="ожидают подтверждения">{pending.length}</span>}
          </button>)}
        </div>)}</nav>
        {llm && <div className="sidebar-bottom" title={llm.detail}>
          <span className={`dot ${llm.reachable ? 'on' : 'off'}`} />
          <span>{llm.route === 'local' ? '🔒 Модель в контуре' : '☁ Модель по API'}<small>{llm.model}</small></span>
        </div>}
      </aside>
      <div className="workspace">
        <header className="topbar">
          <button ref={menuButton} className="icon-button menu-toggle" aria-label="Меню" aria-expanded={menuOpen}
            aria-controls="workspace-sidebar" onClick={() => menuOpen ? closeMenu() : setMenuOpen(true)}>☰</button>
          <div className="topbar-title"><strong>{current.name}</strong><span className="muted">{current.hint}</span></div>
          <div className="topbar-actions">
            <Button kind="primary" className="voice-topbar-btn"
              onClick={() => setVoiceOpen(true)}
              aria-label="Голосовой ввод поручений"
              title="Раздать поручения голосом">
              <span aria-hidden="true">🎙️</span><span className="voice-btn-label"> Раздать голосом</span>
            </Button>
            {health && <span className="muted scenario" title="Время сценария стенда">🕒 {dateTime(health.scenario_now)}</span>}
            <div className="tray" onKeyDown={e => {
              if (trayOpen && e.key === 'Escape') { e.preventDefault(); closeTray() }
            }}>
              <button ref={trayButton} className={`icon-button ${pending.length ? 'has' : ''}`}
                aria-label={`Ожидают подтверждения: ${pending.length}`} aria-expanded={trayOpen}
                aria-controls="pending-actions" onClick={() => setTrayOpen(o => !o)}>
                🔔{pending.length > 0 && <span className="badge-dot">{pending.length}</span>}
              </button>
              {trayOpen && <div className="tray-panel" id="pending-actions" role="region" aria-label="Ожидающие действия">
                <strong>Ждут вашего подтверждения</strong>
                {pending.length === 0 && <p className="muted">Сейчас ничего не ждёт подтверждения.</p>}
                {pending.map(a => <button key={a.id} className="tray-item" onClick={() => { closeTray(); setAction(a) }}>
                  <span>{a.origin === 'ai' ? '✦' : '•'}</span><span>{(a.preview as { title?: string })?.title || ACTION_TITLE[a.type]}</span>
                  {a.status !== 'pending' && <Badge tone="danger">сбой</Badge>}
                </button>)}
              </div>}
            </div>
            <button className="icon-button" aria-label="Переключить тему" onClick={() => setTheme(t => t === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? '☼' : '☾'}</button>
            <div className="role-switch">
              <span className="avatar">{user.full_name.slice(0, 1)}</span>
              <span className="role-user"><strong>{user.full_name}</strong><small>{ROLE_LABEL[user.role] ?? user.role}</small></span>
              {accounts.length > 1 && accounts.some(a => a.login === user.login) && <select aria-label="Сменить тестовую роль" disabled={switching} value={user.login} onChange={e => void switchRole(e.target.value)}>
                {accounts.map(a => <option key={a.login} value={a.login}>{a.full_name} — {a.role_label}</option>)}
              </select>}
            </div>
            <button className="icon-button" aria-label="Выйти" title="Выйти" disabled={switching} onClick={signOut}>⎋</button>
          </div>
        </header>
        <main ref={main} tabIndex={-1} className="main" key={user.id} aria-label={current.name}>
          {route.page === 'my-day' && <MyDayPage />}
          {route.page === 'decisions' && <DecisionsPage focus={route.params.id} />}
          {route.page === 'meetings' && <MeetingsPage params={route.params} />}
          {route.page === 'tasks' && <TasksPage focus={route.params.id} />}
          {route.page === 'assistant' && <AssistantPage />}
          {route.page === 'sources' && <SourcesPage />}
          {route.page === 'graph' && <GraphPage focus={route.params.focus} />}
          {route.page === 'audit' && <AuditPage />}
          {route.page === 'data' && <DataPage health={health} loading={healthQuery.loading}
            error={healthQuery.error} onRetry={healthQuery.reload} />}
        </main>
        <footer className="footer">AI-ассистент · прототип для ООО «Фотопринт» · все данные синтетические · любое действие — только после вашего подтверждения</footer>
      </div>
      {action && <ActionDialog key={action.id} card={action} onClose={() => setAction(undefined)}
        onDone={m => { setAction(undefined); setToastText(m); setTick(t => t + 1) }} />}
      {voiceOpen && <VoiceModal onClose={() => setVoiceOpen(false)} />}
      {toastText && <div className="toast" role="status">{toastText}</div>}
    </div>
  </WorkspaceContext.Provider>
}
