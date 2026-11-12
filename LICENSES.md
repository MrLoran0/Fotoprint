# Используемые компоненты и лицензии

> Дополняется по мере подключения. Перед сдачей сверить с `backend/requirements.txt` и `frontend/package-lock.json`.

## Модели

| Компонент | Назначение | Лицензия | Где работает |
|---|---|---|---|
| Qwen3‑8B (через Ollama) | Основная LLM | Apache‑2.0 | локально |
| BAAI/bge‑m3 (через Ollama) | Эмбеддинги для поиска | MIT | локально, CPU |
| Gemini API (Google) | Внешняя модель для сравнения и резерва | условия Google Gemini API | внешний сервис |

## Бэкенд

| Компонент | Лицензия |
|---|---|
| FastAPI, Pydantic, pydantic‑settings | MIT |
| Uvicorn, HTTPX | BSD‑3‑Clause |
| SQLAlchemy | MIT |
| psycopg 3 | LGPL‑3.0 |
| pgvector (Python) | MIT |
| PyJWT | MIT |
| bcrypt | Apache‑2.0 |
| python‑multipart | Apache‑2.0 |
| pytest | MIT |

## Инфраструктура

| Компонент | Лицензия |
|---|---|
| PostgreSQL | PostgreSQL License |
| pgvector | PostgreSQL License |
| Ollama | MIT |
| Mailpit | MIT |
| nginx | BSD‑2‑Clause |

## Фронтенд

| Компонент | Лицензия |
|---|---|
| React, React DOM | MIT |
| Vite | MIT |
| TypeScript | Apache‑2.0 |
| @fontsource/manrope (шрифт Manrope) | OFL‑1.1 |

## Данные

Все документы, расшифровки, календарь, задачи и показатели в `data/` написаны командой для прототипа и полностью синтетические. Внешние датасеты не используются.
