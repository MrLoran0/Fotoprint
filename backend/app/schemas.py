"""Контракт API — единственный источник правды для фронтенда.

Фронтенд генерирует типы из OpenAPI:
    npx openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.d.ts
Меняем форматы только по договорённости ведущего и фронтенда.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Route = Literal["local", "external"]
ItemKind = Literal["decision", "proposal", "discussion", "question", "risk", "task"]
DecisionStatus = Literal["decided", "proposed", "discussed", "needs_clarification"]
Origin = Literal["explicit", "suggested", "user_entered", "unknown"]
TaskStatus = Literal["assigned", "accepted", "in_progress", "done", "cancelled"]
ActionStatus = Literal["pending", "executing", "executed", "failed", "partially_failed", "cancelled"]
ActionType = Literal["publish_protocol", "create_tasks", "create_event", "send_summary", "delegate_task", "update_task",
                     "send_reminders", "send_brief", "create_decision"]


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- auth


class LoginIn(BaseModel):
    login: str
    password: str


class UserOut(Out):
    id: str
    login: str
    full_name: str
    role: str
    department_id: str | None = None
    employee_id: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


class DemoAccountOut(BaseModel):
    login: str
    full_name: str
    role: str
    role_label: str


# ---------------------------------------------------------------- система


class LLMStatus(BaseModel):
    provider: str
    model: str
    route: Route
    reachable: bool
    detail: str = ""


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    db: bool
    llm: LLMStatus
    scenario_now: datetime
    smtp: bool = False  # почтовый шлюз (Mailpit) принимает соединения


# ---------------------------------------------------------------- источники и ответы


SourceType = Literal["document", "email", "telegram", "chat", "transcript", "task", "metric"]


class SourceRef(BaseModel):
    """Ссылка на фрагмент источника. Отдаётся только после серверной проверки прав."""

    source_type: SourceType = "document"
    source_id: str
    title: str
    version: int | None = None
    source_date: date | None = None
    chunk_id: str | None = None
    segment_id: int | None = None
    quote: str = ""
    # для сообщений из почты/Telegram/чатов: кто и когда написал
    author: str | None = None
    sent_at: datetime | None = None
    channel_id: str | None = None


class Claim(BaseModel):
    text: str
    kind: Literal["fact", "recommendation", "assumption"] = "fact"
    sources: list[SourceRef] = []


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ExpertHint(BaseModel):
    """Кто может знать ответ, если в доступных источниках его нет (по графу и зонам ответственности)."""

    employee_id: str
    full_name: str
    position: str
    reason: str


class AskOut(BaseModel):
    # answered — всё подтверждено; partial — часть вопроса без основания (см. gaps);
    # not_found — недостаточно данных в доступных источниках; refused — запрещённая категория
    status: Literal["answered", "partial", "not_found", "refused"]
    answer: str
    claims: list[Claim] = []
    gaps: list[str] = []  # что именно не удалось подтвердить источниками
    experts: list[ExpertHint] = []  # к кому обратиться, когда данных не хватает
    route: Route
    model: str
    warnings: list[str] = []
    audit_id: int | None = None


# ---------------------------------------------------------------- источники данных и граф


class SourceOut(BaseModel):
    id: str
    kind: Literal["email", "telegram", "chat", "files"]
    name: str
    connector: str
    access_level: str
    status: Literal["new", "ok", "error"]
    last_sync_at: datetime | None = None
    items_count: int = 0
    member_count: int = 0
    last_error: str | None = None  # только директору


GraphNodeType = Literal["employee", "entity", "meeting", "protocol_item", "task", "document", "email", "telegram", "chat"]


class GraphNode(BaseModel):
    id: str  # "<type>:<ref_id>"
    type: GraphNodeType
    label: str
    ref_id: str
    meta: dict = {}


class GraphEdge(BaseModel):
    source: str
    target: str
    type: Literal["mentions", "authored", "owns", "participates", "from_meeting", "from_item", "item_of", "about"]
    label: str
    evidence: SourceRef | None = None  # первоисточник ребра


class GraphOut(BaseModel):
    focus: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# ---------------------------------------------------------------- протокол


class ProtocolItemOut(Out):
    id: str
    position: int
    kind: ItemKind
    statement: str
    decision_status: DecisionStatus | None = None
    segment_id: int | None = None
    evidence_quote: str = ""
    quote_verified: bool = False
    owner_employee_id: str | None = None
    owner_raw: str | None = None
    owner_origin: Origin = "unknown"
    due_at: datetime | None = None
    due_raw: str | None = None
    due_origin: Origin = "unknown"
    needs_clarification: list[str] = []
    removed: bool = False


class ProtocolItemPatch(BaseModel):
    """Правка пользователя: заполненные поля получают origin=user_entered."""

    id: str
    statement: str | None = None
    kind: ItemKind | None = None
    decision_status: DecisionStatus | None = None
    owner_employee_id: str | None = None
    due_at: datetime | None = None
    removed: bool | None = None


class TranscriptSegment(BaseModel):
    n: int
    speaker: str
    text: str
    ts: str | None = None
    injection_flag: bool = False


class ProtocolDraftOut(Out):
    id: str
    meeting_id: str
    transcript_id: str
    status: Literal["draft", "published"]
    revision: int
    summary: str = ""
    route: Route
    model: str
    items: list[ProtocolItemOut]
    segments: list[TranscriptSegment] = []


class AudioSegment(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    speaker: str
    text: str


class AudioTranscriptOut(BaseModel):
    text: str
    segments: list[AudioSegment]
    route: Route
    model: str
    duration_s: float = Field(ge=0, allow_inf_nan=False)
    warnings: list[str]


class STTStatusOut(BaseModel):
    configured: bool
    available: bool
    route: Route | None
    max_mb: int
    message: str | None


class TranscriptIn(BaseModel):
    text: str = Field(min_length=1, max_length=60000)


class ProtocolItemNew(BaseModel):
    """Пропущенный моделью пункт, который добавляет человек. Основание — реплика расшифровки (segment_id):
    сервер сам берёт её текст как цитату, поэтому правило «каждый пункт опирается на цитату» сохраняется."""

    model_config = ConfigDict(extra="forbid")
    kind: ItemKind
    statement: str = Field(min_length=1, max_length=500)
    segment_id: int
    decision_status: DecisionStatus | None = None
    owner_employee_id: str | None = None
    due_at: datetime | None = None


class DraftPatchIn(BaseModel):
    revision: int  # текущая ревизия у клиента — защита от потери чужих правок
    items: list[ProtocolItemPatch] = Field(default_factory=list)
    new_items: list[ProtocolItemNew] = Field(default_factory=list, max_length=20)
    summary: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------- действия


class ActionOut(Out):
    id: str
    type: ActionType
    status: ActionStatus
    origin: Literal["ai", "user"]
    revision: int
    payload_hash: str
    payload: dict
    # человекочитаемый предпросмотр строится сервером из того же payload
    preview: dict = {}
    result: dict | None = None
    error: str | None = None
    created_at: datetime


class ActionEditIn(BaseModel):
    revision: int
    payload: dict


class ConfirmIn(BaseModel):
    revision: int
    payload_hash: str


class TaskChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    expected_result: str | None = None
    owner_employee_id: str | None = None
    due_at: datetime | None = None
    status: TaskStatus | None = None
    result: str | None = None


# ---------------------------------------------------------------- поручения, встречи, обзор


class EmployeeOut(Out):
    id: str
    full_name: str
    position: str
    department_id: str
    email: str
    responsibilities: list[str] = []


class TaskOut(Out):
    id: str
    title: str
    expected_result: str = ""
    owner_employee_id: str | None = None
    owner_origin: Origin = "unknown"
    due_at: datetime | None = None
    due_origin: Origin = "unknown"
    status: TaskStatus
    result: str = ""
    meeting_id: str | None = None
    protocol_item_id: str | None = None
    delegation_reason: str = ""
    overdue: bool = False


class PrepareIn(BaseModel):
    topic: str
    participant_ids: list[str]
    previous_meeting_id: str | None = None


class AgendaPoint(BaseModel):
    title: str
    minutes: int | None = None
    sources: list[SourceRef] = []
    returned_task_id: str | None = None  # вернувшееся незакрытое поручение


class MeetingPrepOut(BaseModel):
    meeting_id: str
    agenda: list[AgendaPoint]
    open_tasks: list[TaskOut]
    questions: list[Claim]
    route: Route
    model: str


class FreeSlotsIn(BaseModel):
    participant_ids: list[str]
    duration_minutes: int = 45
    days_ahead: int = 5


class SlotOut(BaseModel):
    starts_at: datetime
    ends_at: datetime


class MetricOut(BaseModel):
    id: str
    name: str
    value: float | None  # None — «нет данных»
    unit: str
    plan: float | None = None
    period: str
    formula: str
    source_id: str
    source_name: str
    updated_at: datetime
    stale: bool
    status: Literal["ok", "warning", "critical", "no_data"]


class MeetingOut(Out):
    id: str
    title: str
    topic: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str
    department_id: str | None = None
    participant_ids: list[str] = []
    previous_meeting_id: str | None = None
    status: Literal["planned", "held", "cancelled"]


class EventOut(Out):
    id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    attendee_ids: list[str] = []
    meeting_id: str | None = None
    source: Literal["seed", "fokus"]


# ---- payload новых действий (валидирует сервер; фронтенд показывает preview и правит поля через PATCH /actions/{id})


class CreateEventPayload(BaseModel):
    """create_event: событие в календаре + приглашения с .ics (отправка — отдельный DeliveryJob)."""

    model_config = ConfigDict(extra="forbid")
    meeting_id: str | None = None  # None — сервер создаст встречу со статусом planned
    title: str = Field(min_length=1, max_length=300)
    topic: str = ""
    starts_at: datetime
    ends_at: datetime
    timezone: str = "Europe/Minsk"
    attendee_ids: list[str] = Field(min_length=1)
    agenda: list[AgendaPoint] = []
    message: str = ""  # текст приглашения, редактируется человеком


class SummaryLetter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient_id: str  # employee.id
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)
    task_ids: list[str] = []


class SendSummaryPayload(BaseModel):
    """send_summary: итоги опубликованного протокола — каждому участнику его поручения и общие решения."""

    model_config = ConfigDict(extra="forbid")
    protocol_id: str
    letters: list[SummaryLetter] = Field(min_length=1)


class DelegateTaskPayload(BaseModel):
    """delegate_task: назначить владельца задаче без владельца; основание — зона ответственности или фраза на встрече."""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    expected_hash: str  # снимок задачи на момент предложения (как в update_task)
    owner_employee_id: str
    due_at: datetime | None = None
    expected_result: str = ""
    reason: str = Field(min_length=1)


# ---- «Мой день»


class DecisionNeeded(BaseModel):
    kind: Literal["clarification", "overdue_task", "critical_metric", "stale_source", "pending_action", "decision_check",
                  "contradiction", "commitment"]
    title: str
    detail: str = ""
    object_type: Literal["protocol_item", "task", "metric", "data_source", "action", "decision"]
    object_id: str
    severity: Literal["info", "warning", "critical"] = "warning"


class MeetingBrief(BaseModel):
    id: str
    title: str
    starts_at: datetime | None
    participant_ids: list[str] = []
    returned_task_ids: list[str] = []  # незакрытые поручения прошлой встречи, которые вернутся в повестку


class MyDayOut(BaseModel):
    now: datetime
    needs_decision: list[DecisionNeeded]
    delegation_suggestions: list[ActionOut]
    task_results: list[TaskOut]  # недавно выполненные поручения с результатом
    upcoming_meetings: list[MeetingBrief]
    metrics: list[MetricOut]
    overdue: list[TaskOut]


class AuditOut(Out):
    id: int
    at: datetime
    user_id: str | None
    user_name: str | None = None
    role: str | None
    action: str
    object_type: str
    object_id: str
    result: str
    details: dict


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_type: Literal["answer", "protocol", "protocol_item"]
    target_id: str = Field(min_length=1, max_length=100)  # answer → audit_id ответа; protocol/protocol_item → их id
    rating: Literal[1, -1]
    comment: str = Field(default="", max_length=1000)


class FeedbackOut(Out):
    id: str
    target_type: str
    target_id: str
    rating: int
    comment: str = ""
    created_at: datetime


class ReminderLetter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient_id: str  # employee.id — владелец просроченных поручений
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)
    task_ids: list[str] = Field(min_length=1)


class SendRemindersPayload(BaseModel):
    """send_reminders: пакет напоминаний по просрочкам — одно письмо каждому владельцу, одна карточка на всех."""

    model_config = ConfigDict(extra="forbid")
    letters: list[ReminderLetter] = Field(min_length=1)


class SendBriefPayload(BaseModel):
    """send_brief: утренний брифинг «Мой день» себе на почту."""

    model_config = ConfigDict(extra="forbid")
    recipient_id: str  # только сам пользователь
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)


# ---- контур решений (из трека 1): разбор отклонения → решение → повторная проверка


class BreakdownRow(BaseModel):
    key: str
    value: float | None
    numerator: int
    denominator: int
    share: float  # доля в знаменателе, %


class Breakdown(BaseModel):
    dimension: str
    label: str
    period: str
    rows: list[BreakdownRow]


class TrendPoint(BaseModel):
    period: str
    label: str
    value: float | None
    numerator: int
    denominator: int


class MetricAnalysisOut(BaseModel):
    metric: MetricOut
    trend: list[TrendPoint]
    breakdowns: list[Breakdown]  # текущий период и предыдущий месяц для сравнения
    top_contributor: str | None = None  # разрез с наибольшим вкладом в отклонение (посчитано кодом)
    hypothesis: Claim | None = None  # гипотеза причины с источниками (kind="assumption")
    evidence: list[SourceRef] = []  # документы и переписка про вкладчик — из графа, только доступное
    suggested_action: str = ""
    owner_employee_id: str | None = None  # владелец показателя по справочнику
    route: Route
    model: str


class CreateDecisionPayload(BaseModel):
    """create_decision: карточка решения + поручение владельцу. Базовое значение KPI фиксирует сервер."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    metric_id: str | None = None
    impact: str = ""
    cause_hypothesis: str = ""
    cause_sources: list[SourceRef] = []
    action_text: str = Field(min_length=1, max_length=1000)
    owner_employee_id: str
    due_at: datetime
    priority: Literal["high", "medium", "low"] = "medium"
    create_task: bool = True


class DecisionOut(Out):
    id: str
    title: str
    metric_id: str | None = None
    impact: str = ""
    cause_hypothesis: str = ""
    cause_sources: list[SourceRef] = []
    action_text: str
    owner_employee_id: str | None = None
    due_at: datetime | None = None
    priority: Literal["high", "medium", "low"]
    status: Literal["open", "in_progress", "effective", "partial", "ineffective", "no_data", "cancelled"]
    baseline: dict | None = None
    recheck: dict | None = None
    task_ids: list[str] = []
    created_at: datetime
    effective_from: datetime | None = None
    history: list[dict] = []
    check_due: bool = False  # срок наступил, повторной проверки ещё не было




# ---- Голосовой ввод поручений


class VoiceTaskIn(BaseModel):
    """Входные данные голосового ввода поручений."""

    text: str = Field(min_length=1, max_length=5000)
    audio_origin: Literal["speech_recognition", "dictation", "audio_file"] = "speech_recognition"


class StructuredVoiceTask(BaseModel):
    """Структурированное поручение, выделенное из голоса."""

    task_id: str
    title: str
    owner_employee_id: str | None = None
    owner_name: str | None = None
    department_id: str | None = None
    department_name: str | None = None
    due_at: datetime | None = None
    expected_result: str = ""
    reason: str = ""
    quote: str = ""
    action_proposal_id: str | None = None


class VoiceStructureOut(BaseModel):
    """Результат структурирования голосового ввода."""

    transcript: str
    tasks: list[StructuredVoiceTask]
    action_proposals: list[ActionOut] = []


class ContractBundle(BaseModel):
    """Служебная сборка всех типов контракта, чтобы они попали в OpenAPI до появления эндпоинтов."""

    ask_in: AskIn
    ask_out: AskOut
    transcript_in: TranscriptIn
    protocol_draft: ProtocolDraftOut
    draft_patch: DraftPatchIn
    action: ActionOut
    action_edit: ActionEditIn
    confirm: ConfirmIn
    task: TaskOut
    prepare_in: PrepareIn
    meeting_prep: MeetingPrepOut
    free_slots_in: FreeSlotsIn
    slot: SlotOut
    my_day: MyDayOut
    audit: AuditOut
    feedback: FeedbackIn
    meeting: MeetingOut
    event: EventOut
    create_event_payload: CreateEventPayload
    send_summary_payload: SendSummaryPayload
    delegate_task_payload: DelegateTaskPayload
    send_reminders_payload: SendRemindersPayload
    send_brief_payload: SendBriefPayload
    metric_analysis: MetricAnalysisOut
    create_decision_payload: CreateDecisionPayload
    decision: DecisionOut
    voice_task_in: VoiceTaskIn | None = None
    structured_voice_task: StructuredVoiceTask | None = None
    voice_structure_out: VoiceStructureOut | None = None

