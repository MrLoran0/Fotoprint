"""Dev-заглушка LLM (LLM_PROVIDER=fake): детерминированные ответы по тем же JSON-схемам.

Нужна, чтобы фронтенд и данные разрабатывались без GPU и без ключа. Это НЕ модель:
на стенде и в демо запрещена, в интерфейсе видна как model = «fake-dev (без ИИ)».
Сервер проверяет её ответы так же строго, как ответы настоящей модели.
"""

import json
import re

from .gateway import LLMError

_TASK = re.compile(r"\b(проведи|проведите|сделай|сделайте|подготовь|подготовьте|запроси|запросите|пришли|пришлите|"
                   r"проверь|проверьте|согласуй|согласуйте|отправь|отправьте|организуй|разберись|посчитай)\b", re.I)
_DUE = re.compile(r"\b((?:до|к)\s+(?:понедельника|вторника|среды|четверга|пятницы|субботы|воскресенья|"
                  r"завтра|послезавтра|\d{1,2}\.\d{1,2}(?:\.\d{4})?))", re.I)
_ADDRESS = re.compile(r"([А-ЯЁ][а-яё]+),\s*$")  # обращение прямо перед глаголом: «Дмитрий, проведи…»


def fake_chat(messages: list[dict]) -> str:
    try:
        payload = json.loads(messages[-1]["content"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise LLMError("Dev-заглушка понимает только запросы пайплайнов AI-ассистента") from e
    if "transcript_segments" in payload:
        return json.dumps(_protocol(payload["transcript_segments"]), ensure_ascii=False)
    if "metric_analysis" in payload:
        return json.dumps(_hypothesis(payload["source_fragments"]), ensure_ascii=False)
    if "meeting_topic" in payload:
        return json.dumps(_prep(payload["source_fragments"]), ensure_ascii=False)
    if "source_fragments" in payload:
        return json.dumps(_answer(payload["source_fragments"]), ensure_ascii=False)
    raise LLMError("Этот сценарий dev-заглушка пока не поддерживает — используйте ollama или gemini")


def _protocol(segments: list[dict]) -> dict:
    items = []
    for s in segments:
        text = s["text"].strip()
        low = text.lower()
        owner = due = None
        if "предлага" in low or "надо бы" in low:
            kind, status = "proposal", "proposed"
        elif re.search(r"\bрешили\b|\bрешено\b", low):
            kind, status = "decision", "decided"
        elif (verb := _TASK.search(text)) is not None:
            kind, status = "task", "decided"
            m = _ADDRESS.search(text[: verb.start()])
            owner = m.group(1) if m else None
            d = _DUE.search(text)
            due = d.group(1) if d else None
        elif "риск" in low:
            kind, status = "risk", "discussed"
        elif text.endswith("?"):
            kind, status = "question", "needs_clarification"
        else:
            kind, status = "discussion", "discussed"
        items.append({"kind": kind, "statement": text[:200], "decision_status": status, "segment_id": s["n"],
                      "evidence_quote": text, "owner_raw": owner, "due_raw": due})
    return {"summary": f"Черновик dev-заглушки (без ИИ): {len(items)} пунктов по репликам.", "items": items}


def _answer(fragments: list[dict]) -> dict:
    for f in fragments:
        if f.get("suspected_injection"):
            continue
        sentence = re.split(r"(?<=[.!?])\s+", f["text"].strip(), maxsplit=1)[0].lstrip("# ").strip()
        if sentence:
            return {"status": "answered", "claims": [{"text": sentence, "citations": [{"chunk_id": f["chunk_id"], "quote": sentence}]}]}
    return {"status": "not_found", "claims": []}


def _prep(fragments: list[dict]) -> dict:
    """Повестка: по пункту на каждый из первых трёх безопасных фрагментов, цитата — его первая фраза."""
    agenda = []
    for f in fragments:
        if f.get("suspected_injection") or len(agenda) == 3:
            continue
        text = f["text"].split(": ", 1)[-1] if f.get("source") in ("telegram", "chat") else f["text"]
        sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0].lstrip("# ").strip()
        if sentence:
            agenda.append({"title": f"Обсудить: {f['title']}"[:200], "minutes": 10,
                           "citations": [{"chunk_id": f["chunk_id"], "quote": sentence}]})
    return {"agenda": agenda, "questions": []}


def _hypothesis(fragments: list[dict]) -> dict:
    cites = []
    for f in fragments[:2]:
        text = f["text"].split(": ", 1)[-1] if f.get("source") in ("telegram", "chat") else f["text"]
        sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0].lstrip("# ").strip()
        if sentence:
            cites.append({"chunk_id": f["chunk_id"], "quote": sentence})
    return {"cause_hypothesis": "Гипотеза dev-заглушки: отклонение связано с разрезом наибольшего вклада; причинность не доказана.",
            "suggested_action": "Провести проверку по разрезу наибольшего вклада и повторно измерить показатель через неделю.",
            "citations": cites}
