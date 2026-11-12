from datetime import date

from app.audit import mask
from app.ingest import chunk_text, parse_front_matter, parse_transcript


def test_front_matter():
    meta, body = parse_front_matter("---\nid: x\nversion: 2\ndate: 2026-10-28\nai_allowed: false\ntags: [a, b]\n---\nТекст")
    assert meta == {"id": "x", "version": 2, "date": date(2026, 10, 28), "ai_allowed": False, "tags": ["a", "b"]}
    assert body == "Текст"


def test_transcript_segments_keep_real_timestamps_only():
    segs = parse_transcript("[00:14] Анна Смирнова: Решили вернуть контроль.\nИлья Морозов: Сделаю\nпродолжение реплики")
    assert [s["n"] for s in segs] == [1, 2]
    assert segs[0]["ts"] == "00:14" and segs[1]["ts"] is None
    assert segs[1]["text"] == "Сделаю продолжение реплики"


def test_chunk_offsets_point_into_source():
    text = "Абзац один.\n\nАбзац два, подлиннее.\n\n" + "Длинный абзац. " * 100
    for c in chunk_text(text, target=200):
        assert text[c.start : c.end].strip() == c.text


def test_mask_hides_contacts_and_salaries():
    out = mask("Пишите ivan@fotoprint.test, тел. +375 29 123-45-67, оклад оператора 1 850 BYN")
    assert "ivan@" not in out and "123-45-67" not in out and "1 850" not in out
