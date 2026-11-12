import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import get_settings
from .db import SessionLocal, init_db
from .llm import LLMError
from .routers import assistant, audio, auth, decisions, directory, graph, history, planning, sources, system, voice, workflow
from .seed import is_seeded, seed_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fokus")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if get_settings().seed_on_start:
        with SessionLocal() as db:
            if not is_seeded(db):
                seed_all(db)
    yield


app = FastAPI(
    title="AI-ассистент API",
    description="Backend-сервер AI-участника команды руководителя (хакатон «Фабрика идей», трек 3).",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # dev-сервер Vite; в docker — тот же origin через nginx
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(LLMError)
async def llm_error(_: Request, exc: LLMError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "code": "llm_unavailable"})


app.include_router(system.router)
app.include_router(auth.router)
app.include_router(auth.me_router)
app.include_router(directory.router)
app.include_router(workflow.router)
app.include_router(audio.router)
app.include_router(assistant.router)
app.include_router(history.router)
app.include_router(sources.router)
app.include_router(graph.router)
app.include_router(planning.router)
app.include_router(decisions.router)
app.include_router(voice.router)
