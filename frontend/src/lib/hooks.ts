import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { S } from '../api/client'

/** Загрузка данных с повтором и защитой от гонок. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)
  const reload = useCallback(() => {
    const n = ++seq.current
    setLoading(true)
    setError('')
    fn().then(d => { if (n === seq.current) setData(d) })
      .catch(e => { if (n === seq.current) setError((e as Error).message) })
      .finally(() => { if (n === seq.current) setLoading(false) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => {
    reload()
    return () => { seq.current++ }
  }, [reload])
  return { data, error, loading, reload, setData }
}

/** Черновик ввода переживает перезагрузку страницы (ОТ-05: ввод не теряется). */
export function useDraft(key: string, initial = '') {
  const { user } = useWorkspace()
  const storageKey = `fokus-draft:${user.id}:${key}`
  const [value, setValue] = useState(() => sessionStorage.getItem(storageKey) ?? initial)
  useEffect(() => { sessionStorage.setItem(storageKey, value) }, [storageKey, value])
  return [value, setValue] as const
}

export type Workspace = {
  user: S['UserOut']
  employees: S['EmployeeOut'][]
  openAction: (card: S['ActionOut']) => void
  refreshTick: number
  toast: (message: string) => void
  go: (page: string, params?: Record<string, string>) => void
  openVoice?: () => void
}

export const WorkspaceContext = createContext<Workspace | null>(null)

export function useWorkspace() {
  const ws = useContext(WorkspaceContext)
  if (!ws) throw new Error('WorkspaceContext missing')
  return ws
}

export function employeeName(ws: Workspace, id?: string | null) {
  if (!id) return 'без владельца'
  return ws.employees.find(e => e.id === id)?.full_name ?? id
}
