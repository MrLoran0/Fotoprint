FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

ARG INSTALL_WHISPER=false
COPY requirements.txt requirements-whisper.txt ./
RUN pip install -r requirements.txt && \
    if [ "$INSTALL_WHISPER" = "true" ]; then pip install -r requirements-whisper.txt; fi

COPY pyproject.toml .
COPY app ./app
COPY tests ./tests

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
