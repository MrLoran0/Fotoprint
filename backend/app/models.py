"""Модель данных AI-ассистента.

Имена сущностей совместимы с п. 4.2 ТЗ: user, meeting, task, document, metric, alert
+ собственные: transcript, protocol, protocol_item, action_proposal, delivery_job, audit_log.
Все моменты времени — timestamptz; исходный часовой пояс встречи хранится отдельно.
"""

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

EMBED_DIM = 1024  # bge-m3


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------- люди и доступ


class Department(Base):
    __tablename__ = "department"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # production, commercial, support, management
    name: Mapped[str] = mapped_column(String(200))


class Employee(Base):
    """Справочник сотрудников: участники встреч, владельцы задач, кандидаты на делегирование."""

    __tablename__ = "employee"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    position: Mapped[str] = mapped_column(String(200))
    department_id: Mapped[str] = mapped_column(ForeignKey("department.id"))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    # зоны ответственности — основа для предложения делегирования
    responsibilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)  # «Петрова», «Маша» — для сопоставления из расшифровки
    work_start: Mapped[str] = mapped_column(String(5), default="09:00")
    work_end: Mapped[str] = mapped_column(String(5), default="18:00")


ROLE_DIRECTOR = "director"
ROLE_PRODUCTION_HEAD = "production_head"
ROLES = (ROLE_DIRECTOR, ROLE_PRODUCTION_HEAD)


class User(Base):
    __tablename__ = "user_account"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    login: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(40))
    department_id: Mapped[str | None] = mapped_column(ForeignKey("department.id"))
    employee_id: Mapped[str | None] = mapped_column(ForeignKey("employee.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------- документы и поиск

# public — все; department — своё подразделение и директор; confidential — только директор;
# participants — только участники переписки/чата (allowed_employee_ids), даже директор не видит чужую переписку
ACCESS_PUBLIC = "public"
ACCESS_DEPARTMENT = "department"
ACCESS_CONFIDENTIAL = "confidential"
ACCESS_PARTICIPANTS = "participants"

# что за материал: документ из хранилища или сообщение из канала
DOC_KINDS = ("document", "email", "telegram", "chat")


class SourceChannel(Base):
    """Источник данных: почтовый ящик, Telegram-группа, рабочий чат, папка документов.

    Коннектор забирает материалы и превращает их в документы с правами участников. На хакатоне
    источники — синтетические экспорты в стандартных форматах (.eml, Telegram Desktop result.json, JSON чата);
    в пилоте те же коннекторы читают IMAP/Exchange, Telegram Bot API, Битрикс24.
    """

    __tablename__ = "source_channel"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # email | telegram | chat | files
    name: Mapped[str] = mapped_column(String(200))
    connector: Mapped[str] = mapped_column(String(40))  # email_eml | telegram_export | chat_json
    path: Mapped[str] = mapped_column(String(300), default="")  # относительно DATA_DIR
    access_level: Mapped[str] = mapped_column(String(20), default=ACCESS_PARTICIPANTS)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("department.id"))
    member_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)  # employee.id участников канала
    status: Mapped[str] = mapped_column(String(20), default="new")  # new | ok | error
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class Document(Base):
    __tablename__ = "document"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    version: Mapped[int] = mapped_column(Integer, default=1)
    doc_date: Mapped[date] = mapped_column(Date)
    author: Mapped[str] = mapped_column(String(200), default="")
    access_level: Mapped[str] = mapped_column(String(20), default=ACCESS_PUBLIC)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("department.id"))
    # False — документ никогда не попадает в контекст AI (оклады, ПДн), даже если роль может его открыть
    ai_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    content: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(String(300), default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(20), default="document")
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("source_channel.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(300))
    # для access_level=participants: кто видит (employee.id)
    allowed_employee_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = _now_col()

    chunks: Mapped[list["DocumentChunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "document_chunk"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('russian', text)", persisted=True))
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    # пометка детектора инъекций (дополнительный сигнал, не защита сама по себе)
    injection_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    # для сообщений: кто и когда написал (одна реплика чата = один фрагмент)
    author_employee_id: Mapped[str | None] = mapped_column(ForeignKey("employee.id"))
    author_raw: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (Index("ix_chunk_tsv", "tsv", postgresql_using="gin"),)


class Entity(Base):
    """Сущность предметной области для графа: продукт, поставщик, оборудование, тема (из data/glossary.json)."""

    __tablename__ = "entity"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    type: Mapped[str] = mapped_column(String(20))  # product | supplier | equipment | topic
    name: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)


class Mention(Base):
    """Упоминание сотрудника или сущности во фрагменте документа/сообщения — рёбра графа.

    Заполняется детерминированно при загрузке (словарь имён и сущностей), без LLM.
    """

    __tablename__ = "mention"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("document_chunk.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(20))  # employee | entity
    target_id: Mapped[str] = mapped_column(String(60), index=True)
    surface: Mapped[str] = mapped_column(String(200))  # как написано в тексте


# ---------------------------------------------------------------- встречи и протоколы


class Meeting(Base):
    __tablename__ = "meeting"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    topic: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(60), default="Europe/Minsk")
    organizer_user_id: Mapped[str | None] = mapped_column(ForeignKey("user_account.id"))
    department_id: Mapped[str | None] = mapped_column(ForeignKey("department.id"))
    participant_ids: Mapped[list[str]] = mapped_column(JSON, default=list)  # employee.id
    previous_meeting_id: Mapped[str | None] = mapped_column(ForeignKey("meeting.id"))
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned | held | cancelled
    agenda: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _now_col()


class Transcript(Base):
    __tablename__ = "transcript"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meeting.id"), index=True)
    source: Mapped[str] = mapped_column(String(20), default="text")  # text | audio
    raw_text: Mapped[str] = mapped_column(Text)
    # [{"n": 1, "speaker": "Илья Морозов", "text": "...", "ts": "00:14" | null}]
    segments: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _now_col()


class Protocol(Base):
    __tablename__ = "protocol"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meeting.id"), index=True)
    transcript_id: Mapped[str] = mapped_column(ForeignKey("transcript.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published
    revision: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="")
    route: Mapped[str] = mapped_column(String(20), default="local")  # local | external — фактический маршрут LLM
    model: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("user_account.id"))
    created_at: Mapped[datetime] = _now_col()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["ProtocolItem"]] = relationship(
        back_populates="protocol", cascade="all, delete-orphan", order_by="ProtocolItem.position"
    )


class ProtocolItem(Base):
    __tablename__ = "protocol_item"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    protocol_id: Mapped[str] = mapped_column(ForeignKey("protocol.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(20))  # decision | proposal | discussion | question | risk | task
    statement: Mapped[str] = mapped_column(Text)
    decision_status: Mapped[str | None] = mapped_column(String(30))  # decided | proposed | discussed | needs_clarification
    segment_id: Mapped[int | None] = mapped_column(Integer)
    evidence_quote: Mapped[str] = mapped_column(Text, default="")
    quote_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_employee_id: Mapped[str | None] = mapped_column(ForeignKey("employee.id"))
    owner_raw: Mapped[str | None] = mapped_column(String(200))  # «как сказано»
    owner_origin: Mapped[str] = mapped_column(String(20), default="unknown")  # explicit | suggested | user_entered | unknown
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_raw: Mapped[str | None] = mapped_column(String(200))
    due_origin: Mapped[str] = mapped_column(String(20), default="unknown")
    needs_clarification: Mapped[list[str]] = mapped_column(JSON, default=list)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)

    protocol: Mapped[Protocol] = relationship(back_populates="items")


# ---------------------------------------------------------------- поручения

# assigned → accepted → in_progress → done; cancelled — отдельно
TASK_STATUSES = ("assigned", "accepted", "in_progress", "done", "cancelled")


class Task(Base):
    __tablename__ = "task"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    expected_result: Mapped[str] = mapped_column(Text, default="")
    owner_employee_id: Mapped[str | None] = mapped_column(ForeignKey("employee.id"), index=True)
    owner_origin: Mapped[str] = mapped_column(String(20), default="unknown")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_origin: Mapped[str] = mapped_column(String(20), default="unknown")
    status: Mapped[str] = mapped_column(String(20), default="assigned")
    result: Mapped[str] = mapped_column(Text, default="")
    department_id: Mapped[str | None] = mapped_column(ForeignKey("department.id"))
    meeting_id: Mapped[str | None] = mapped_column(ForeignKey("meeting.id"), index=True)
    protocol_item_id: Mapped[str | None] = mapped_column(ForeignKey("protocol_item.id"))
    delegation_reason: Mapped[str] = mapped_column(Text, default="")
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decision.id"), index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("user_account.id"))
    created_at: Mapped[datetime] = _now_col()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- контур решений (из трека 1)

# open → in_progress → (повторная проверка) effective | partial | ineffective | no_data; cancelled
DECISION_STATUSES = ("open", "in_progress", "effective", "partial", "ineffective", "no_data", "cancelled")


class Decision(Base):
    """Управленческое решение: сигнал → причина → действие → владелец → срок → повторная проверка.

    Причина — гипотеза с первоисточниками, а не доказанный факт. Базовое значение KPI фиксирует сервер
    в момент подтверждения; повторная проверка считает KPI за период после решения и выносит вердикт.
    """

    __tablename__ = "decision"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))  # проблема
    metric_id: Mapped[str | None] = mapped_column(String(40))
    impact: Mapped[str] = mapped_column(Text, default="")
    cause_hypothesis: Mapped[str] = mapped_column(Text, default="")
    cause_sources: Mapped[list[dict]] = mapped_column(JSON, default=list)
    action_text: Mapped[str] = mapped_column(Text)
    owner_employee_id: Mapped[str | None] = mapped_column(ForeignKey("employee.id"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str] = mapped_column(String(10), default="medium")  # high | medium | low
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    baseline: Mapped[dict | None] = mapped_column(JSON)  # {value, numerator, denominator, period, start, end, plan}
    recheck: Mapped[dict | None] = mapped_column(JSON)  # {value, ..., verdict, delta, checked_at}
    task_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("department.id"))
    protocol_item_id: Mapped[str | None] = mapped_column(String(40))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("user_account.id"))
    created_at: Mapped[datetime] = _now_col()
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # с какого момента меряем «после»
    history: Mapped[list[dict]] = mapped_column(JSON, default=list)


# ---------------------------------------------------------------- календарь


class CalendarEvent(Base):
    __tablename__ = "calendar_event"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(60), default="Europe/Minsk")
    attendee_ids: Mapped[list[str]] = mapped_column(JSON, default=list)  # employee.id
    meeting_id: Mapped[str | None] = mapped_column(ForeignKey("meeting.id"))
    source: Mapped[str] = mapped_column(String(20), default="seed")  # seed | fokus
    created_at: Mapped[datetime] = _now_col()


# ---------------------------------------------------------------- действия и доставка

# pending → executing → executed | failed | partially_failed;  pending → cancelled
ACTION_STATUSES = ("pending", "executing", "executed", "failed", "partially_failed", "cancelled")


class ActionProposal(Base):
    """Карточка действия. Модель может только создать pending; исполняет сервер после подтверждения."""

    __tablename__ = "action_proposal"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    type: Mapped[str] = mapped_column(String(40))  # publish_protocol | create_tasks | create_event | send_summary | delegate_task | update_task
    payload: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    origin: Mapped[str] = mapped_column(String(10), default="ai")  # ai | user
    created_by: Mapped[str] = mapped_column(ForeignKey("user_account.id"))
    confirmed_by: Mapped[str | None] = mapped_column(ForeignKey("user_account.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col()


class DeliveryJob(Base):
    """Отправка письма отделена от создания события: повтор доставки не создаёт второе событие."""

    __tablename__ = "delivery_job"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("action_proposal.id"), index=True)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("calendar_event.id"))
    channel: Mapped[str] = mapped_column(String(20), default="email")
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | sent | failed | unknown
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = _now_col()

    __table_args__ = (UniqueConstraint("action_id", "event_id", name="uq_delivery_action_event"),)


# ---------------------------------------------------------------- аудит и обратная связь


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = _now_col()
    user_id: Mapped[str | None] = mapped_column(String(40), index=True)
    role: Mapped[str | None] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80), index=True)
    object_type: Mapped[str] = mapped_column(String(40), default="")
    object_id: Mapped[str] = mapped_column(String(100), default="")
    result: Mapped[str] = mapped_column(String(20), default="ok")  # ok | denied | error | proposed | confirmed | cancelled
    # запрос, источники, маршрут LLM, предложение и т.п. — уже замаскировано
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"))
    target_type: Mapped[str] = mapped_column(String(40))  # answer | protocol | protocol_item
    target_id: Mapped[str] = mapped_column(String(100))
    rating: Mapped[int] = mapped_column(Integer)  # +1 | -1
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now_col()


# ---------------------------------------------------------------- данные для KPI (синтетика)


class DataSource(Base):
    """Источник данных KPI: свежесть и порог устаревания."""

    __tablename__ = "data_source"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # erp_orders, mes_quality, crm_support
    name: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stale_after_hours: Mapped[int] = mapped_column(Integer, default=24)


class QualityCheck(Base):
    __tablename__ = "quality_check"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    checked_on: Mapped[date] = mapped_column(Date, index=True)
    product: Mapped[str] = mapped_column(String(100))
    line: Mapped[str] = mapped_column(String(100), default="")
    paper_supplier: Mapped[str] = mapped_column(String(100), default="")
    checked_units: Mapped[int] = mapped_column(Integer)
    defective_units: Mapped[int] = mapped_column(Integer)


class Order(Base):
    __tablename__ = "customer_order"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    product: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(40), default="site")
    amount: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="BYN")
    promised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="accepted")


class SupportTicket(Base):
    __tablename__ = "support_ticket"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    topic: Mapped[str] = mapped_column(String(200))
    order_id: Mapped[str | None] = mapped_column(String(40))
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
