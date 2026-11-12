import { useEffect, useRef, useState } from 'react'
import { api, type S } from '../api/client'
import { employeeName, useLoad, useWorkspace } from '../lib/hooks'
import { dateTime } from '../lib/format'
import { Badge, Button, ErrorNote, Modal } from './ui'
import { AudioProgress, AudioRoute } from './AudioRecognition'

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

const RECORDING_FORMATS = [
  ['audio/webm;codecs=opus', 'webm'],
  ['audio/ogg;codecs=opus', 'ogg'],
  ['audio/mp4', 'm4a'],
] as const

export function VoiceModal({ onClose }: { onClose: () => void }) {
  const ws = useWorkspace()
  const stt = useLoad(() => api.get<S['STTStatusOut']>('/stt/status'), [])
  const [text, setText] = useState('')
  const [isRecording, setIsRecording] = useState(false)
  const [recordSeconds, setRecordSeconds] = useState(0)
  const [starting, setStarting] = useState(false)
  const [progress, setProgress] = useState<number>()
  const [route, setRoute] = useState<S['AudioTranscriptOut']['route']>()
  const [origin, setOrigin] = useState<NonNullable<S['VoiceTaskIn']['audio_origin']>>('dictation')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<S['VoiceStructureOut']>()

  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null)
  const recorderRef = useRef<MediaRecorder>(null)
  const uploadRef = useRef<AbortController>(null)
  const alive = useRef(false)
  const speechWindow = window as Window & {
    SpeechRecognition?: new () => SpeechRecognitionInstance
    webkitSpeechRecognition?: new () => SpeechRecognitionInstance
  }
  const SpeechAPI = speechWindow.SpeechRecognition || speechWindow.webkitSpeechRecognition
  const format = typeof MediaRecorder === 'undefined' ? undefined
    : RECORDING_FORMATS.find(([mime]) => MediaRecorder.isTypeSupported(mime))
  const canRecord = !!(format && navigator.mediaDevices?.getUserMedia)
  const locked = busy || isRecording || starting || progress !== undefined

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      uploadRef.current?.abort()
      recorderRef.current?.stream.getTracks().forEach(track => track.stop())
      if (recognitionRef.current) {
        try { recognitionRef.current.abort() } catch {}
      }
    }
  }, [])

  useEffect(() => {
    if (!isRecording) return
    const timer = window.setInterval(() => setRecordSeconds(seconds => seconds + 1), 1000)
    return () => window.clearInterval(timer)
  }, [isRecording])

  async function recognizeRecording(file: File) {
    const upload = new AbortController()
    uploadRef.current = upload
    setProgress(0)
    try {
      const audio = await api.uploadAudio('/voice/audio', file, upload.signal, setProgress)
      if (upload.signal.aborted) return
      setText(audio.segments.map(segment => segment.text).join('\n'))
      setRoute(audio.route)
      setOrigin('audio_file')
      setResult(undefined)
    } catch (e) {
      if (!upload.signal.aborted) setError((e as Error).message)
    } finally {
      if (!upload.signal.aborted) setProgress(undefined)
      uploadRef.current = null
    }
  }

  async function startServerRecording() {
    if (!stt.data?.available || !format) return
    const [mimeType, extension] = format
    const maxMb = stt.data.max_mb
    setStarting(true)
    setError('')
    let stream: MediaStream | undefined
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (!alive.current) { stream.getTracks().forEach(track => track.stop()); return }
      const recorder = new MediaRecorder(stream, { mimeType })
      const chunks: Blob[] = []
      let size = 0
      let failed = false
      recorder.ondataavailable = e => {
        if (!alive.current || failed || !e.data.size) return
        size += e.data.size
        if (size > maxMb * 1024 * 1024) {
          failed = true
          setError(`Запись больше допустимого размера (${maxMb} МБ).`)
          if (recorder.state !== 'inactive') recorder.stop()
        } else chunks.push(e.data)
      }
      recorder.onerror = () => {
        failed = true
        if (alive.current) setError('Не удалось записать звук с микрофона.')
      }
      recorder.onstop = () => {
        recorder.stream.getTracks().forEach(track => track.stop())
        recorderRef.current = null
        if (!alive.current) return
        setIsRecording(false)
        if (failed) return
        if (!size) { setError('Запись пуста. Попробуйте ещё раз.'); return }
        void recognizeRecording(new File(chunks, `dictation.${extension}`, { type: mimeType }))
      }
      recorder.start(1000)
      recorderRef.current = recorder
      setRecordSeconds(0)
      setIsRecording(true)
    } catch (e) {
      stream?.getTracks().forEach(track => track.stop())
      if (alive.current) setError(`Не удалось включить микрофон: ${(e as Error).message}`)
    } finally {
      if (alive.current) setStarting(false)
    }
  }

  function startBrowserRecording() {
    if (!SpeechAPI) return
    setError('')
    try {
      const rec = new SpeechAPI()
      rec.lang = 'ru-RU'
      rec.continuous = true
      rec.interimResults = true
      rec.onresult = e => {
        if (!alive.current) return
        const words = Array.from(e.results, result => result[0].transcript).join(' ').trim()
        if (words) {
          setText(words); setOrigin('speech_recognition'); setRoute(undefined); setResult(undefined)
        }
      }
      rec.onerror = e => { if (alive.current) setError(`Ошибка распознавания браузером: ${e.error}`) }
      rec.onend = () => {
        recognitionRef.current = null
        if (alive.current) setIsRecording(false)
      }
      rec.start()
      recognitionRef.current = rec
      setRecordSeconds(0)
      setIsRecording(true)
    } catch (e) {
      setError(`Не удалось включить микрофон: ${(e as Error).message}`)
    }
  }

  function stopRecording() {
    const recorder = recorderRef.current
    if (recorder && recorder.state !== 'inactive') recorder.stop()
    else if (recognitionRef.current) {
      try { recognitionRef.current.stop() } catch { setIsRecording(false) }
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
      const res = await api.post<S['VoiceStructureOut']>('/voice/structure', { text: text.trim(), audio_origin: origin })
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
          Продиктуйте поручения и проверьте текст. Поручения попадут в реестр после подтверждения карточек.
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
              <span className="muted small">
                {stt.loading ? 'Проверяем настройки распознавания…' : stt.error || stt.data?.message ||
                  (canRecord ? (stt.data?.route === 'external' ? 'Аудио будет передано внешнему сервису.' : 'Аудио распознаётся в контуре.') :
                    'Для записи нужен поддерживаемый браузер и HTTPS или localhost.')}
              </span>
            )}
          </div>
          {(isRecording || (canRecord && stt.data?.available)) && (
            <button
              type="button"
              className={`button voice-rec-btn ${isRecording ? 'recording danger' : 'primary'}`}
              onClick={isRecording ? stopRecording : () => void startServerRecording()}
              disabled={!isRecording && locked}
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
      {progress !== undefined && <AudioProgress percent={progress} />}
      {route && <div><AudioRoute route={route} /></div>}
      {SpeechAPI && !isRecording && <details>
        <summary>Запасной вариант — распознавание браузером</summary>
        <p className="muted small">Браузер может отправить аудио в облако. Chrome использует сервис Google.</p>
        <Button disabled={locked} onClick={startBrowserRecording}>Начать распознавание браузером</Button>
      </details>}

      {/* Быстрые примеры для проверки */}
      <div className="voice-chips-section">
        <span className="small muted">Быстрые примеры для проверки:</span>
        <div className="voice-chips-list">
          {DEMO_PROMPTS.map((p, idx) => (
            <button
              key={idx}
              type="button"
              className="voice-chip-btn"
              disabled={locked}
              onClick={() => {
                setText(p.text)
                setOrigin('dictation')
                setRoute(undefined)
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
          disabled={locked}
          maxLength={5000}
        />
        <div className="voice-input-actions">
          {text && (
            <button
              type="button"
              className="text-button"
              disabled={locked}
              onClick={() => { setText(''); setResult(undefined); setError(''); setRoute(undefined); setOrigin('dictation') }}
            >
              Очистить
            </button>
          )}
          <Button
            kind="primary"
            className="push"
            busy={busy}
            disabled={!text.trim() || text.trim().length > 5000 || locked}
            onClick={handleStructure}
          >
            ✦ Структурировать поручения
          </Button>
        </div>
      </div>

      <ErrorNote error={error} />
      {text.trim().length > 5000 && <ErrorNote error="Сократите диктовку до 5 000 символов перед структурированием." />}

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
