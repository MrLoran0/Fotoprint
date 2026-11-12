import { useEffect, useRef, useState } from 'react'
import { api, type S } from '../api/client'
import { employeeName, useWorkspace } from '../lib/hooks'
import { dateTime } from '../lib/format'
import { Badge, Button, ErrorNote, Modal } from './ui'

interface SpeechRecognitionEvent {
  resultIndex: number
  results: {
    length: number
    [index: number]: {
      [index: number]: {
        transcript: string
      }
      isFinal: boolean
    }
  }
}

interface SpeechRecognitionInstance {
  lang: string
  continuous: boolean
  interimResults: boolean
  start: () => void
  stop: () => void
  abort: () => void
  onresult: ((e: SpeechRecognitionEvent) => void) | null
  onerror: ((e: { error: string }) => void) | null
  onend: (() => void) | null
}

const DEMO_PROMPTS = [
  {
    label: 'Смирнова · отчет по браку',
    text: 'Смирнова, подготовь итоговый отчет по браку УФ-печати к следующей пятнице, ожидаем снижение брака до 3%',
  },
  {
    label: 'Волков · калибровка станков',
    text: 'Волков, нужно провести калибровку печатных станков Мимаки до конца недели, ждем протокол испытаний',
  },
  {
    label: 'Белов · проверка договоров',
    text: 'Олег Белов, проверь договоры с новыми клиентами до завтра и согласуй шаблон',
  },
  {
    label: 'Несколько поручений сразу',
    text: 'Первое: Петрова, запроси сертификаты у поставщика чернил до среды. Второе: Волков, проверь вентиляцию цеха к пятнице',
  },
]

export function VoiceModal({ onClose }: { onClose: () => void }) {
  const ws = useWorkspace()
  const [text, setText] = useState('')
  const [isRecording, setIsRecording] = useState(false)
  const [recordSeconds, setRecordSeconds] = useState(0)
  const [speechSupported, setSpeechSupported] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<S['VoiceStructureOut']>()

  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    const SpeechAPI = (window as unknown as {
      SpeechRecognition?: new () => SpeechRecognitionInstance
      webkitSpeechRecognition?: new () => SpeechRecognitionInstance
    }).SpeechRecognition || (window as unknown as {
      SpeechRecognition?: new () => SpeechRecognitionInstance
      webkitSpeechRecognition?: new () => SpeechRecognitionInstance
    }).webkitSpeechRecognition

    if (!SpeechAPI) {
      setSpeechSupported(false)
      return
    }

    try {
      const rec = new SpeechAPI()
      rec.lang = 'ru-RU'
      rec.continuous = true
      rec.interimResults = true

      rec.onresult = (e: SpeechRecognitionEvent) => {
        let fullTranscript = ''
        for (let i = 0; i < e.results.length; i++) {
          fullTranscript += e.results[i][0].transcript + ' '
        }
        setText(fullTranscript.trim())
      }

      rec.onerror = (e) => {
        if (e.error !== 'no-speech') {
          setError(`Ошибка распознавания речи: ${e.error}`)
        }
        stopRecording()
      }

      rec.onend = () => {
        setIsRecording(false)
        if (timerRef.current) {
          clearInterval(timerRef.current)
          timerRef.current = null
        }
      }

      recognitionRef.current = rec
    } catch {
      setSpeechSupported(false)
    }

    return () => {
      if (recognitionRef.current) {
        try { recognitionRef.current.abort() } catch {}
      }
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  function startRecording() {
    setError('')
    if (!recognitionRef.current) return
    try {
      setText('')
      setResult(undefined)
      setRecordSeconds(0)
      recognitionRef.current.start()
      setIsRecording(true)
      timerRef.current = window.setInterval(() => {
        setRecordSeconds(s => s + 1)
      }, 1000)
    } catch (e) {
      setError(`Не удалось включить микрофон: ${(e as Error).message}`)
      setIsRecording(false)
    }
  }

  function stopRecording() {
    if (recognitionRef.current && isRecording) {
      try { recognitionRef.current.stop() } catch {}
    }
    setIsRecording(false)
    if (timerRef.current) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  function formatDuration(sec: number) {
    const m = Math.floor(sec / 60).toString().padStart(2, '0')
    const s = (sec % 60).toString().padStart(2, '0')
    return `${m}:${s}`
  }

  async function handleStructure() {
    if (!text.trim()) return
    setBusy(true)
    setError('')
    try {
      const res = await api.post<S['VoiceStructureOut']>('/voice/structure', { text: text.trim() })
      setResult(res)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  function openProposal(proposal: S['ActionOut']) {
    ws.openAction(proposal)
    onClose()
  }

  return <Modal onClose={onClose} label="Голосовой ввод поручений" wide>
    <div className="modal-head">
      <div>
        <span className="eyebrow">Автоматизация руководителя</span>
        <h2>🎙️ Голосовой ввод поручений</h2>
        <p className="muted">
          Диктуйте задания голосом. AI-ассистент выделит исполнителей, сроки и критерии готовности и подготовит карточки — без вашего подтверждения ничего не создаётся. Речь распознаёт браузер (в Chrome — облачный сервис Google); в пилоте — распознавание в контуре компании.
        </p>
      </div>
      <button className="icon-button" onClick={onClose} aria-label="Закрыть">×</button>
    </div>

    <div className="voice-modal-content">
      {/* Секция микрофона и записи */}
      <div className={`voice-recorder-card ${isRecording ? 'recording' : ''}`}>
        <div className="voice-recorder-top">
          <div className="voice-rec-status">
            {isRecording ? (
              <span className="voice-live-indicator">
                <span className="rec-dot" /> Запись речи: <strong>{formatDuration(recordSeconds)}</strong>
              </span>
            ) : (
              <span className="muted small">Нажмите кнопку, чтобы начать диктовку</span>
            )}
          </div>
          {speechSupported ? (
            <button
              type="button"
              className={`button voice-rec-btn ${isRecording ? 'recording danger' : 'primary'}`}
              onClick={isRecording ? stopRecording : startRecording}
              disabled={busy}
            >
              {isRecording ? (
                <>
                  <span className="btn-icon">⏹</span> Остановить запись
                </>
              ) : (
                <>
                  <span className="btn-icon">🎙️</span> Начать запись
                </>
              )}
            </button>
          ) : (
            <Badge tone="warning">Микрофон в браузере недоступен</Badge>
          )}
        </div>

        {isRecording && (
          <div className="voice-wave-container" aria-hidden="true">
            <span className="wave-bar bar-1" />
            <span className="wave-bar bar-2" />
            <span className="wave-bar bar-3" />
            <span className="wave-bar bar-4" />
            <span className="wave-bar bar-5" />
            <span className="wave-bar bar-6" />
            <span className="wave-bar bar-7" />
            <span className="wave-bar bar-8" />
          </div>
        )}
      </div>

      {/* Быстрые примеры для проверки */}
      <div className="voice-chips-section">
        <span className="small muted">Быстрые примеры для проверки:</span>
        <div className="voice-chips-list">
          {DEMO_PROMPTS.map((p, idx) => (
            <button
              key={idx}
              type="button"
              className="voice-chip-btn"
              onClick={() => {
                stopRecording()
                setText(p.text)
                setResult(undefined)
                setError('')
              }}
            >
              ✦ {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* Поле текста поручения */}
      <div className="voice-input-section">
        <label htmlFor="voice-transcript-text">
          <span>Текст поручения (из речи или ввод вручную):</span>
        </label>
        <textarea
          id="voice-transcript-text"
          className="voice-textarea"
          rows={3}
          value={text}
          onChange={e => setText(e.target.value)}
          placeholder="Например: Смирнова, подготовь итоговый отчет по браку УФ-печати к следующей пятнице, ожидаем снижение брака до 3%..."
          disabled={busy || isRecording}
        />
        <div className="voice-input-actions">
          {text && (
            <button
              type="button"
              className="text-button"
              onClick={() => { setText(''); setResult(undefined); setError('') }}
            >
              Очистить
            </button>
          )}
          <Button
            kind="primary"
            className="push"
            busy={busy}
            disabled={!text.trim() || isRecording}
            onClick={handleStructure}
          >
            ✦ Структурировать поручения
          </Button>
        </div>
      </div>

      <ErrorNote error={error} />

      {/* Предупреждение сервера (например, отклонение инъекций или закрытая тема) */}


      {/* Результаты структурирования */}
      {result && (
        <div className="voice-results-section">
          <div className="voice-results-head">
            <h3>Структурированные поручения ({result.tasks.length})</h3>
            <span className="small muted">
              Перед сохранением каждое поручение проверяется через карточку действия
            </span>
          </div>

          {result.tasks.length === 0 ? (
            <div className="empty" style={{ padding: '16px' }}>
              <p className="muted">Поручений в тексте не обнаружено.</p>
            </div>
          ) : (
            <div className="voice-tasks-grid">
              {result.tasks.map((task, i) => (
                <div key={i} className="voice-task-card">
                  <div className="voice-task-header">
                    <h4>{task.title}</h4>
                    <div className="voice-task-chips">
                      {task.owner_employee_id ? (
                        <Badge tone="success">👤 {task.owner_name || employeeName(ws, task.owner_employee_id)}</Badge>
                      ) : (
                        <Badge tone="warning">⚠️ Без исполнителя</Badge>
                      )}
                      {task.due_at ? (
                        <Badge tone="info">📅 До {dateTime(task.due_at)}</Badge>
                      ) : (
                        <Badge tone="warning">📅 Срок не указан</Badge>
                      )}
                    </div>
                  </div>

                  {task.expected_result && (
                    <div className="voice-task-result">
                      <strong>🎯 Критерий готовности:</strong> {task.expected_result}
                    </div>
                  )}

                  <div className="voice-task-quote">
                    <small className="muted">Основание (цитата из речи):</small>
                    <blockquote>«{task.quote}»</blockquote>
                  </div>

                  {!task.owner_employee_id && (
                    <div className="notice warning" style={{ margin: '8px 0', padding: '8px 12px' }}>
                      <small>Исполнитель не распознан или не из вашего подразделения — карточка не создана. Назовите сотрудника по имени или поручите через «Поручения».</small>
                    </div>
                  )}

                  {(() => {
                    const card = result.action_proposals.find(a => a.id === task.action_proposal_id)
                    return card && <div className="voice-task-action">
                      <Button kind="primary" onClick={() => openProposal(card)}>Проверить карточку и подтвердить →</Button>
                    </div>
                  })()}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  </Modal>
}
