"""Пайплайн структурирования голосовых поручений руководителя.

Голос человека — недоверенные данные (проверка инъекций и запрещённых тем).
Модель и правила только ПРЕДЛАГАЮТ структуру; любое создание задачи проходит
через карточку ActionProposal (type="delegate_task", expected_hash="new").
"""

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actions import action_out, create_action, payload_hash
from ..config import get_settings
from ..delegation import candidates
from ..evidence import normalize, sensitive_request, suspicious
from ..models import Department, Employee, ROLE_DIRECTOR, User, new_id
from ..schemas import ActionOut, StructuredVoiceTask, VoiceStructureOut

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}

_WEEKDAYS = {
    "понедельник": 0, "понедельника": 0, "вторник": 1, "вторника": 1,
    "среда": 2, "среду": 2, "среды": 2, "четверг": 3, "четверга": 3,
    "пятница": 4, "пятницу": 4, "пятницы": 4, "суббота": 5, "субботу": 5, "субботы": 5,
    "воскресенье": 6, "воскресенья": 6,
}


def parse_voice_due(text: str, base_dt: datetime, tz_name: str) -> datetime | None:
    """Извлекает срок из русской голосовой речи."""
    tz = ZoneInfo(tz_name)
    base = base_dt.astimezone(tz).date()
    norm = normalize(text)

    # 1. "до конца недели" -> ближайшая пятница (или воскресенье)
    if "до конца недели" in norm or "к концу недели" in norm:
        days_ahead = (4 - base.weekday()) % 7
        if days_ahead == 0 and base_dt.astimezone(tz).hour >= 18:
            days_ahead = 7
        target_date = base + timedelta(days=days_ahead)
        return datetime.combine(target_date, time(18, 0), tzinfo=tz)

    # 2. "сегодня", "завтра", "послезавтра"
    if re.search(r"\bсегодня\b", norm):
        return datetime.combine(base, time(18, 0), tzinfo=tz)
    if re.search(r"\bзавтра\b", norm):
        return datetime.combine(base + timedelta(days=1), time(18, 0), tzinfo=tz)
    if re.search(r"\bпослезавтра\b", norm):
        return datetime.combine(base + timedelta(days=2), time(18, 0), tzinfo=tz)

    # 3. Число и месяц: "до 15 ноября", "к 20 ноября", "14 ноября"
    m_month = re.search(r"(?:до|к|на)?\s*(\d{1,2})\s+(" + "|".join(_MONTHS.keys()) + r")(?:\s+(\d{4}))?", norm)
    if m_month:
        day = int(m_month.group(1))
        month = _MONTHS[m_month.group(2)]
        year = int(m_month.group(3)) if m_month.group(3) else base.year
        try:
            target_date = datetime(year, month, day).date()
            return datetime.combine(target_date, time(18, 0), tzinfo=tz)
        except ValueError:
            pass

    # 4. Формат ДД.ММ: "до 15.11"
    m_dots = re.search(r"(?:до|к|на)?\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", norm)
    if m_dots:
        day = int(m_dots.group(1))
        month = int(m_dots.group(2))
        year = int(m_dots.group(3)) if m_dots.group(3) else base.year
        try:
            target_date = datetime(year, month, day).date()
            return datetime.combine(target_date, time(18, 0), tzinfo=tz)
        except ValueError:
            pass

    # 5. Дни недели: "до пятницы", "к четвергу"
    for w_name, w_idx in _WEEKDAYS.items():
        if re.search(r"(?:до|к|на)\s+" + w_name + r"\b", norm):
            days_ahead = (w_idx - base.weekday()) % 7
            if days_ahead == 0 and base_dt.astimezone(tz).hour >= 18:
                days_ahead = 7
            target_date = base + timedelta(days=days_ahead)
            return datetime.combine(target_date, time(18, 0), tzinfo=tz)

    return None


def match_employee(text: str, employees: list[Employee]) -> Employee | None:
    """Ищет сотрудника по имени, фамилии, псевдониму или зоне ответственности."""
    norm = normalize(text)

    # 1. Прямое совпадение по имени / псевдонимам / частям имени (фамилия / имя) - наивысший приоритет
    best_direct: tuple[int, Employee | None] = (0, None)
    for emp in employees:
        tokens = [normalize(emp.full_name)] + [normalize(a) for a in (emp.aliases or [])]
        parts = normalize(emp.full_name).split()
        tokens.extend(parts)
        for tok in tokens:
            if not tok or len(tok) < 3:
                continue
            if re.search(r"(?<!\w)" + re.escape(tok) + r"(?!\w)", norm):
                if len(tok) > best_direct[0]:
                    best_direct = (len(tok), emp)

    if best_direct[1] is not None:
        return best_direct[1]

    # 2. Ранжирование по зонам ответственности (подсчёт совпавших основ)
    best_resp: tuple[int, Employee | None] = (0, None)
    for emp in employees:
        score = 0
        for resp in (emp.responsibilities or []):
            resp_norm = normalize(resp)
            if resp_norm:
                words = [w for w in resp_norm.split() if len(w) >= 4]
                for w in words:
                    stem = w[:5] if len(w) >= 5 else w
                    if re.search(r"(?<!\w)" + re.escape(stem) + r"\w*", norm):
                        score += 1
        if score > best_resp[0]:
            best_resp = (score, emp)

    return best_resp[1]


def split_voice_utterances(text: str) -> list[str]:
    """Разбивает голосовую речь на отдельные поручения, если их несколько."""
    cleaned = text.strip()
    if not cleaned:
        return []

    # Проверка на явные маркеры нумерации: "Первое:... Второе:...", "1)... 2)...", "1. ... 2. ..."
    numbered = re.split(r"(?:^|(?<=[.!?\n])\s*|\s+)(?:первое|второе|третье|четвёртое|пятое|\d+[\.\)])\s*[:\-\.]?\s*", cleaned, flags=re.IGNORECASE)
    parts = [p.strip() for p in numbered if p.strip()]
    if len(parts) > 1:
        return parts

    # Разбивка по предложениям
    sentences = [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", cleaned) if s.strip()]
    if len(sentences) > 1:
        return sentences

    return [cleaned]


def extract_expected_result(text: str) -> str:
    """Извлекает ожидаемый результат из реплики."""
    norm = text.strip()
    m_res = re.search(r"(?:результат\s*[-—:]?\s*|итог\s*[-—:]?\s*)([^\.,;!\n]+)", norm, re.IGNORECASE)
    if m_res:
        res = m_res.group(1).strip()
        return res[:200]

    for pattern in (
        r"\b(сравнительный\s+отч[её]т[^\.,;!\n]*)",
        r"\b(отч[её]т[^\.,;!\n]*)",
        r"\b(сертификат\w*[^\.,;!\n]*)",
        r"\b(матриц\w*\s+коммуникаций[^\.,;!\n]*)",
        r"\b(шаблон\s+ответа[^\.,;!\n]*)",
        r"\b(карт\w*\s+настроек[^\.,;!\n]*)",
        r"\b(протокол[^\.,;!\n]*)",
    ):
        m = re.search(pattern, norm, re.IGNORECASE)
        if m:
            res = m.group(1).strip()
            return res[:200].capitalize()

    return ""


def clean_task_title(text: str, matched_emp: Employee | None) -> str:
    """Очищает заголовок поручения от обращений и вводных фраз."""
    title = text.strip()

    if matched_emp:
        names_to_strip = [matched_emp.full_name] + (matched_emp.aliases or []) + matched_emp.full_name.split()
        for n in names_to_strip:
            if len(n) >= 3:
                title = re.sub(r"^(?:" + re.escape(n) + r"|дима|маша|лена|рома|паша|олег)\s*[,:\-\s]\s*", "", title, flags=re.IGNORECASE)

    title = re.sub(r"^(?:пожалуйста|срочно|нужно|надо|необходимо|просьба)\s*[,:\-\s]\s*", "", title, flags=re.IGNORECASE)
    m_start_due = re.match(r"^(?:до|к|на)\s+[^\s,]+\s*[,:\-\s]\s*(.*)$", title, flags=re.IGNORECASE)
    if m_start_due and len(m_start_due.group(1)) > 5:
        title = m_start_due.group(1)

    title = re.sub(r"\s*[,;]?\s*(?:результат|итог)\s*[-—:]?\s*.*$", "", title, flags=re.IGNORECASE)

    title = title.strip().rstrip(".!?,")
    if not title:
        title = "Голосовое поручение"
    return title[0].upper() + title[1:499]


def structure_voice_tasks(db: Session, user: User, text: str, audio_origin: str = "speech_recognition") -> VoiceStructureOut:
    """Превращает неструктурированную голосовую речь в карточки ActionProposal."""
    if suspicious(text):
        raise HTTPException(422, "В голосовом сообщении обнаружены недопустимые системные инструкции")
    if sensitive_request(text):
        raise HTTPException(403, "Оклады и персональные данные запрещены для обработки голосовым ассистентом")

    settings = get_settings()
    now_dt = settings.now()
    tz_name = settings.company_tz

    all_employees = list(db.scalars(select(Employee)).all())
    allowed_candidates = candidates(db, user)
    all_depts = {d.id: d.name for d in db.scalars(select(Department)).all()}

    utterances = split_voice_utterances(text)
    tasks_out: list[StructuredVoiceTask] = []
    actions_out: list[ActionOut] = []

    for utt in utterances:
        matched_emp = match_employee(utt, list(allowed_candidates.values()))
        if not matched_emp:
            any_emp = match_employee(utt, all_employees)
            if any_emp and any_emp.id not in allowed_candidates:
                matched_emp = None

        due_at = parse_voice_due(utt, now_dt, tz_name)
        if due_at is not None and due_at < now_dt:
            due_at = None  # срок в прошлом — скорее оговорка: не создаём заведомо просроченное поручение, срок задаст человек
        title = clean_task_title(utt, matched_emp)
        expected_res = extract_expected_result(utt)

        task_id = new_id("task")
        owner_id = matched_emp.id if matched_emp else None
        owner_name = matched_emp.full_name if matched_emp else None
        dept_id = matched_emp.department_id if matched_emp else user.department_id
        dept_name = all_depts.get(dept_id, "")

        reason = f"Голосовое поручение руководителя ({user.full_name or user.login})"
        if matched_emp:
            reason += f": назначено на {matched_emp.full_name}"

        action_card_id = None
        if owner_id:
            payload = {
                "task_id": task_id,
                "expected_hash": "new",
                "owner_employee_id": owner_id,
                "due_at": due_at.isoformat() if due_at else None,
                "expected_result": expected_res,
                "reason": reason,
            }
            key = payload_hash({"user": user.id, "voice_task": task_id, "text": utt})
            action = create_action(db, user, "delegate_task", payload, key=key, origin="ai")
            action.result = {
                "proposed_title": title,
                "evidence": {
                    "quote": utt,
                    "source": "voice",
                    "audio_origin": audio_origin,
                },
            }
            db.flush()
            action_card_id = action.id
            actions_out.append(action_out(action))

        tasks_out.append(
            StructuredVoiceTask(
                task_id=task_id,
                title=title,
                owner_employee_id=owner_id,
                owner_name=owner_name,
                department_id=dept_id,
                department_name=dept_name,
                due_at=due_at,
                expected_result=expected_res,
                reason=reason,
                quote=utt,
                action_proposal_id=action_card_id,
            )
        )

    db.commit()
    return VoiceStructureOut(transcript=text, tasks=tasks_out, action_proposals=actions_out)
