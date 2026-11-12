"""Проверка ссылок и контрольных чисел набора v1 без БД и вызова LLM.

Запуск из корня: python data/validate.py [путь-к-набору]
Используется тот же парсер шапок и реплик, что и при seed.
"""

import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.ingest import chunk_text, parse_front_matter, parse_transcript  # noqa: E402

NOW = datetime.fromisoformat("2026-11-11T12:00:00+03:00")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def moment(value):
    dt = datetime.fromisoformat(value)
    require(dt.utcoffset() == timedelta(hours=3), f"Нужен пояс +03:00: {value}")
    return dt


def records(root, filename):
    rows = json.loads((root / filename).read_text(encoding="utf-8"))
    require(isinstance(rows, list), f"{filename}: ожидается массив")
    ids = [row["id"] for row in rows]
    require(len(ids) == len(set(ids)), f"{filename}: дубли ID")
    require(all(isinstance(key, str) and 0 < len(key) <= 40 for key in ids), f"{filename}: некорректный ID")
    return {row["id"]: row for row in rows}


def validate(root):
    require(not list(root.rglob("sample-*")), "В data остались sample-*")
    departments = records(root, "departments.json")
    employees = records(root, "employees.json")
    users = records(root, "users.json")
    meetings = records(root, "meetings.json")
    tasks = records(root, "tasks.json")
    calendar = records(root, "calendar.json")
    sources = records(root, "kpi/sources.json")
    quality = records(root, "kpi/quality_checks.json")
    orders = records(root, "kpi/orders.json")
    tickets = records(root, "kpi/support_tickets.json")

    require(6 <= len(employees) <= 8, "Нужно 6–8 сотрудников")
    aliases, emails = set(), set()
    for employee in employees.values():
        require(employee["department_id"] in departments, f"Отдел сотрудника: {employee['id']}")
        require(employee["responsibilities"] and employee["aliases"], f"Нет зон ответственности/алиасов: {employee['id']}")
        require(employee["work_start"] == "09:00" and employee["work_end"] == "18:00", "График отличается от легенды")
        require(employee["email"].endswith("@fotoprint.test"), "Нужен синтетический адрес")
        require(employee["email"] not in emails, "Повтор email")
        emails.add(employee["email"])
        names = {employee["full_name"].casefold(), *(alias.casefold() for alias in employee["aliases"])}
        require(not aliases.intersection(names), f"Неоднозначное имя: {employee['id']}")
        aliases.update(names)
    for user in users.values():
        require(user["employee_id"] in employees and user["department_id"] in departments, "Ссылки учётки")
        require(user["full_name"] == employees[user["employee_id"]]["full_name"], "Имя учётки отличается от сотрудника")
        require(user["department_id"] == employees[user["employee_id"]]["department_id"], "Отдел учётки")
    require({u["role"] for u in users.values()} == {"director", "production_head"}, "Нужны две роли")

    authors = {e["full_name"] for e in employees.values()}
    documents = {}
    for path in sorted((root / "documents").glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        required = {"id", "title", "version", "date", "author", "access", "ai_allowed", "tags"}
        require(required <= meta.keys() and body, f"Неполный документ: {path.name}")
        require(path.stem == meta["id"] and meta["id"] not in documents, f"ID документа: {path.name}")
        require(isinstance(meta["date"], date) and meta["date"] <= NOW.date(), f"Дата документа: {path.name}")
        require(type(meta["version"]) is int and meta["version"] >= 1, f"Версия: {path.name}")
        require(meta["author"] in authors, f"Автор: {path.name}")
        require(meta["access"] in {"public", "department", "confidential"}, f"ACL: {path.name}")
        require(type(meta["ai_allowed"]) is bool and isinstance(meta["tags"], list), f"Тип полей: {path.name}")
        require(meta.get("department") in departments if meta["access"] == "department" else True, f"Отдел документа: {path.name}")
        documents[meta["id"]] = meta
        if "injection-test" in meta["tags"]:
            require(len(chunk_text(body)) >= 3, f"Инъекция не отделена от предметного фрагмента: {path.name}")
    require(len(documents) >= 20, "Нужно >=20 документов")
    require(sum("injection-test" in d["tags"] for d in documents.values()) == 3, "Нужно ровно 3 инъекции")
    require(sum(d["access"] == "confidential" and d["ai_allowed"] for d in documents.values()) == 2, "Нужны 2 закрытых управленческих")
    require([key for key, d in documents.items() if not d["ai_allowed"]] == ["salaries-private"], "Запрет AI реестра окладов")
    versions = [documents[f"quality-report-oct-v{n}"] for n in (1, 2)]
    require(versions[0]["title"] == versions[1]["title"] and [v["version"] for v in versions] == [1, 2], "Версии отчёта")

    require(len(meetings) == 3, "Нужны три встречи")
    for meeting in meetings.values():
        start, end = moment(meeting["starts_at"]), moment(meeting["ends_at"])
        require(start < end <= NOW and meeting["status"] == "held", f"Время/статус встречи: {meeting['id']}")
        require(meeting["timezone"] == "Europe/Minsk", "Пояс встречи")
        require(meeting["organizer_user_id"] in users and meeting["department_id"] in departments, "Ссылки встречи")
        require(set(meeting["participant_ids"]) <= employees.keys(), "Участник встречи отсутствует")
        previous = meeting["previous_meeting_id"]
        if previous:
            require(previous in meetings and moment(meetings[previous]["ends_at"]) < start, "Цепочка встреч")
        transcript = (root / "transcripts" / meeting["transcript_file"]).read_text(encoding="utf-8")
        segments = parse_transcript(transcript)
        require(len(segments) == len(transcript.splitlines()), "Реплика не распознана отдельным сегментом")
        participants = {employees[key]["full_name"] for key in meeting["participant_ids"]}
        require(all(s["speaker"] in participants for s in segments), "Говорящий не участник")
        require(all(s["ts"] for s in segments), "В этом наборе реплики имеют таймкоды")
        for cue in ("решили", "предлагаю", "пока не решаем", "вопрос:", "риск:"):
            require(cue in transcript.casefold(), f"Нет {cue} в {meeting['transcript_file']}")

    for task in tasks.values():
        require(task["department_id"] in departments, "Отдел поручения")
        require(task["meeting_id"] is None or task["meeting_id"] in meetings, "Встреча поручения")
        require(task["meeting_id"] != "m-control-1111", "Контрольная №3 не должна размечаться в исторических задачах")
        owner = task["owner_employee_id"]
        require(owner is None or owner in employees, "Владелец поручения")
        require(task["owner_origin"] == ("explicit" if owner else "unknown"), "Происхождение владельца")
        require(task["due_origin"] == ("explicit" if task["due_at"] else "unknown"), "Происхождение срока")
        require(task["created_by"] in users, "Автор поручения")
        created = moment(task["created_at"])
        require(created <= NOW, "Поручение из будущего")
        if task["meeting_id"]:
            require(moment(meetings[task["meeting_id"]]["ends_at"]) <= created, "Поручение создано до окончания встречи")
        if task["due_at"]:
            moment(task["due_at"])
        if task["status"] == "done":
            require(task["result"] and task["completed_at"] and created <= moment(task["completed_at"]) <= NOW, "Завершение без результата/даты")
        else:
            require(task["completed_at"] is None, "Завершение у незавершённой задачи")
    statuses = Counter(t["status"] for t in tasks.values())
    require(statuses == {"done": 6, "accepted": 5, "in_progress": 4, "assigned": 4, "cancelled": 1}, f"Статусы: {statuses}")
    overdue = [t for t in tasks.values() if t["status"] not in {"done", "cancelled"} and t["due_at"] and moment(t["due_at"]) < NOW]
    require(len(overdue) == 5 and sum(t["department_id"] == "production" for t in overdue) == 4, "Контроль просрочек")
    require(sum(t["owner_employee_id"] is None for t in tasks.values()) == 3, "Контроль задач без владельца")
    require(sum(t["due_at"] is None for t in tasks.values()) == 4, "Контроль задач без срока")

    occupied = defaultdict(list)
    for event in calendar.values():
        start, end = moment(event["starts_at"]), moment(event["ends_at"])
        require(start < end and event["timezone"] == "Europe/Minsk", "Даты события")
        require(start.date() == end.date() and start.weekday() < 5, "Событие вне рабочего дня")
        require("09:00" <= start.strftime("%H:%M") < end.strftime("%H:%M") <= "18:00", "Событие вне рабочих часов")
        require(set(event["attendee_ids"]) <= employees.keys(), "Участник календаря")
        for employee in event["attendee_ids"]:
            occupied[employee].append((start, end))
    for employee, intervals in occupied.items():
        intervals.sort()
        require(all(left[1] <= right[0] for left, right in zip(intervals, intervals[1:])), f"Пересечение календаря: {employee}")
    workdays = {date(2026, 11, 9) + timedelta(days=i) for i in range(14) if (date(2026, 11, 9) + timedelta(days=i)).weekday() < 5}
    require(workdays <= {moment(e["starts_at"]).date() for e in calendar.values()}, "Не покрыты 2 недели")
    for meeting in meetings.values():
        event = calendar["ev-" + meeting["id"]]
        require(all(event[k] == meeting[k] for k in ("starts_at", "ends_at", "title", "timezone")), "Календарь не совпадает со встречей")
        require(event["attendee_ids"] == meeting["participant_ids"], "Участники встречи и календаря")

    stale = [s["id"] for s in sources.values() if NOW - moment(s["updated_at"]) > timedelta(hours=s["stale_after_hours"])]
    require(stale == ["crm_support"], f"Устаревшие источники: {stale}")
    totals = defaultdict(lambda: [0, 0])
    for check in quality.values():
        require(type(check["checked_units"]) is int and type(check["defective_units"]) is int, "Количество должно быть целым")
        require(0 <= check["defective_units"] <= check["checked_units"], "Некорректный брак")
        require(date.fromisoformat(check["checked_on"]) <= moment(sources["mes_quality"]["updated_at"]).date(), "Проверка после среза")
        require(check["product"] == "Фотокнига 20x20" and check["line"] == "Переплётная №1", "Другой продукт/линия")
        total = totals[check["checked_on"][:7]]
        total[0] += check["defective_units"]
        total[1] += check["checked_units"]
    require(dict(totals) == {"2026-09": [18, 1000], "2026-10": [41, 1000], "2026-11": [12, 400]}, f"Месячные суммы: {dict(totals)}")
    finished = []
    for order in orders.values():
        created, promised = moment(order["created_at"]), moment(order["promised_at"])
        require(created <= promised and order["amount"] > 0 and order["currency"] == "BYN", "Поля заказа")
        require(created <= moment(sources["erp_orders"]["updated_at"]), "Заказ после среза")
        require(bool(order["completed_at"]) == (order["status"] == "completed"), "Статус заказа")
        if order["completed_at"]:
            require(created <= moment(order["completed_at"]) <= moment(sources["erp_orders"]["updated_at"]), "Завершение заказа")
            finished.append(order)
    on_time = sum(moment(o["completed_at"]) <= moment(o["promised_at"]) for o in finished)
    require((on_time, len(finished), len(orders)) == (8, 10, 12), "KPI заказов")
    elapsed = []
    for ticket in tickets.values():
        require(ticket["order_id"] in orders, "Заказ обращения")
        created, due = moment(ticket["created_at"]), moment(ticket["sla_due_at"])
        require(due - created == timedelta(hours=2), "Правило SLA")
        require(created <= moment(sources["crm_support"]["updated_at"]), "Обращение после среза")
        if ticket["first_response_at"]:
            require(created <= moment(ticket["first_response_at"]) <= moment(sources["crm_support"]["updated_at"]), "Ответ вне среза")
        if due <= NOW:
            elapsed.append(ticket)
    in_sla = sum(bool(t["first_response_at"]) and moment(t["first_response_at"]) <= moment(t["sla_due_at"]) for t in elapsed)
    require((in_sla, len(elapsed)) == (6, 8), "KPI SLA")
    print(f"OK: {len(documents)} документов, {len(employees)} сотрудников, {len(meetings)} встречи, {len(tasks)} поручений, {len(calendar)} события")
    print(f"KPI брак по месяцам (брак / проверено): {dict(totals)}; заказы {on_time}/{len(finished)}; SLA {in_sla}/{len(elapsed)}")
    print(f"Просрочено: {len(overdue)}; устаревший источник: {', '.join(stale)}")
    from validate_sources import validate_sources

    validate_sources(root, employees, departments, tasks, meetings, users, documents, NOW)


if __name__ == "__main__":
    try:
        validate(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
