import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { CycleSteps } from './CycleSteps'

export function Badge({ children, tone = 'neutral', title }: { children: ReactNode; tone?: string; title?: string }) {
  return <span className={`chip chip-${tone}`} title={title}>{children}</span>
}

export function Card({ children, className = '', title, icon, actions, subtitle }: {
  children: ReactNode; className?: string; title?: ReactNode; icon?: string; actions?: ReactNode; subtitle?: ReactNode
}) {
  return <section className={`card ${className}`}>
    {(title || actions) && <header className="card-head">
      <div className="card-title">{icon && <span className="section-icon" aria-hidden="true">{icon}</span>}<div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div></div>
      {actions && <div className="card-actions">{actions}</div>}
    </header>}
    {children}
  </section>
}

export function Empty({ children, icon = '○', steps }: { children: ReactNode; icon?: string; steps?: readonly string[] }) {
  return <div className="empty-state" role="status">
    <span aria-hidden="true">{icon}</span><p>{children}</p>
    {steps && <CycleSteps steps={steps} />}
  </div>
}

export function Loading({ label = 'Загружаем…' }: { label?: string }) {
  return <div className="loading" role="status"><span className="spinner" aria-hidden="true" />{label}</div>
}

export function ErrorNote({ error, onRetry }: { error?: string; onRetry?: () => void }) {
  if (!error) return null
  return <div className="notice error" role="alert"><span>{error}</span>{onRetry && <button className="text-button" onClick={onRetry}>Повторить</button>}</div>
}

export function Button({ children, kind = 'secondary', busy, ...rest }: React.ButtonHTMLAttributes<HTMLButtonElement> & { kind?: 'primary' | 'secondary' | 'ghost' | 'danger'; busy?: boolean }) {
  return <button {...rest} className={`button ${kind} ${rest.className ?? ''}`} disabled={busy || rest.disabled}>
    {busy && <span className="spinner small" aria-hidden="true" />}{children}
  </button>
}

type DialogProps = { children: ReactNode; onClose: () => void; label: string }

function Dialog({ children, onClose, label, className }: DialogProps & { className: string }) {
  const ref = useRef<HTMLDialogElement>(null)
  useLayoutEffect(() => {
    const dialog = ref.current!
    const opener = document.activeElement
    dialog.showModal()
    return () => {
      dialog.close()
      if ((opener instanceof HTMLElement || opener instanceof SVGElement) && opener.isConnected) {
        opener.focus({ preventScroll: true })
      }
    }
  }, [])
  return <dialog ref={ref} aria-label={label} className={className}
    onCancel={e => {
      if (e.target !== e.currentTarget) return
      e.preventDefault()
      e.stopPropagation()
      onClose()
    }}
    onMouseDown={e => { if (e.target === ref.current) onClose() }}>
    {children}
  </dialog>
}

export function Modal({ children, onClose, wide, label }: DialogProps & { wide?: boolean }) {
  return <Dialog onClose={onClose} label={label} className={`modal ${wide ? 'wide' : ''}`}>
    <div className="modal-body">{children}</div>
  </Dialog>
}

export function Drawer({ children, onClose, label, busy }: DialogProps & { busy?: boolean }) {
  return <Dialog onClose={() => { if (!busy) onClose() }} label={label} className="drawer">
    <div className="drawer-body">
      <button className="icon-button drawer-close" aria-label="Закрыть" onClick={onClose} disabled={busy}>×</button>
      {children}
    </div>
  </Dialog>
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: string }) {
  return <div className={`stat ${tone ? `stat-${tone}` : ''}`}><small>{label}</small><strong>{value}</strong>{hint && <span>{hint}</span>}</div>
}

/** Горизонтальные столбики: значение относительно плана. */
export function Bars({ rows, unit, plan, higherIsBetter }: {
  rows: { label: string; value: number | null; hint?: string }[]; unit: string; plan?: number | null; higherIsBetter?: boolean
}) {
  const max = Math.max(...rows.map(r => r.value ?? 0), plan ?? 0, 1)
  return <div className="bars">
    {rows.map(r => {
      const bad = r.value !== null && plan !== null && plan !== undefined && (higherIsBetter ? r.value < plan : r.value > plan)
      return <div className="bar-row" key={r.label}>
        <span className="bar-label" title={r.label}>{r.label}</span>
        <span className="bar-track">
          <span className={`bar-fill ${bad ? 'bad' : 'good'}`} style={{ width: `${((r.value ?? 0) / max) * 100}%` }} />
          {plan !== null && plan !== undefined && <span className="bar-plan" style={{ left: `${(plan / max) * 100}%` }} title={`план ${plan}${unit}`} />}
        </span>
        <span className="bar-value">{r.value === null ? '—' : `${r.value.toLocaleString('ru-RU')}${unit}`}{r.hint && <small>{r.hint}</small>}</span>
      </div>
    })}
  </div>
}
