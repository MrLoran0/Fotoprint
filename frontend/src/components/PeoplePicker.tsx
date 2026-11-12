import { useWorkspace } from '../lib/hooks'

export function PeoplePicker({ value, onChange, label = 'Участники', disabled }: { value: string[]; onChange: (ids: string[]) => void; label?: string; disabled?: boolean }) {
  const ws = useWorkspace()
  const toggle = (id: string) => onChange(value.includes(id) ? value.filter(v => v !== id) : [...value, id])
  return <fieldset className="people-picker" disabled={disabled}><legend>{label}</legend>
    {ws.employees.map(e => <label key={e.id} className={`person ${value.includes(e.id) ? 'on' : ''}`}>
      <input type="checkbox" checked={value.includes(e.id)} onChange={() => toggle(e.id)} />
      <span className="avatar small" aria-hidden="true">{e.full_name.slice(0, 1)}</span>
      <span><strong>{e.full_name}</strong><small>{e.position}</small></span>
    </label>)}
  </fieldset>
}

export function PersonSelect({ value, onChange, label, sameDepartmentAs, allowEmpty = false }: { value: string | null | undefined; onChange: (id: string) => void; label: string; sameDepartmentAs?: string | null; allowEmpty?: boolean }) {
  const ws = useWorkspace()
  const people = ws.employees.filter(e => ws.user.role === 'director' || !sameDepartmentAs || e.department_id === sameDepartmentAs)
  return <label>{label}<select value={value ?? ''} onChange={e => onChange(e.target.value)}>
    <option value="" disabled={!allowEmpty}>{allowEmpty ? 'Без владельца' : 'Выберите сотрудника'}</option>
    {value && !people.some(e => e.id === value) && <option value={value} disabled>{ws.employees.find(e => e.id === value)?.full_name ?? value}</option>}
    {people.map(e => <option key={e.id} value={e.id}>{e.full_name} — {e.position}</option>)}
  </select></label>
}
