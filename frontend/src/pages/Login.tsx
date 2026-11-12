import { useState } from 'react'
import { demoPassword, login, type S } from '../api/client'
import { CycleSteps } from '../components/CycleSteps'
import { Button } from '../components/ui'

export function Login({ accounts, onLogin }: { accounts: S['DemoAccountOut'][]; onLogin: (u: S['UserOut']) => void }) {
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(loginName = name, pwd = password) {
    if (busy) return
    setBusy(true); setError('')
    try { onLogin(await login(loginName, pwd)) } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  return <div className="login-page">
    <div className="login-hero">
      <div className="brand-mark big">✦</div>
      <h1>AI-ассистент</h1>
      <p>Помогает руководителю превратить отклонения в данных в поручения и проверить, сработало ли решение.</p>
      <CycleSteps />
    </div>
    <form className="card login-card" onSubmit={e => { e.preventDefault(); void submit() }}>
      <h2>Вход</h2>
      <label>Логин<input autoComplete="username" value={name} onChange={e => setName(e.target.value)} required /></label>
      <label>Пароль<input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></label>
      {error && <div className="notice error" role="alert">{error}</div>}
      <Button kind="primary" busy={busy} type="submit">Войти</Button>
      {accounts.length > 0 && demoPassword && <div className="demo-accounts">
        <small className="muted">Тестовые учётные записи стенда</small>
        {accounts.map(a => <button type="button" key={a.login} className="demo-account" disabled={busy}
          onClick={() => void submit(a.login, demoPassword)}>
          <span className="avatar">{a.full_name.slice(0, 1)}</span><span><strong>{a.full_name}</strong><small>{a.role_label}</small></span>
        </button>)}
      </div>}
    </form>
  </div>
}
