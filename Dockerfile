FROM python:3.12-slim AS base

# WHY pinned to 3.12 (not 3.11 or 3.13): 3.11+ is the stated minimum for
# match-statement-free modern typing (X | None) and datetime.UTC; 3.12 is the
# newest version with mature wheels for asyncpg/pydantic-core at time of
# writing. Bump deliberately, not by "latest" drift, since asyncpg version
# support tends to lag new CPython releases by a few months.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
# Install deps in their own layer so `docker build` skips reinstalling every
# time only application code changes -- app/ is copied after this step.
RUN pip install --upgrade pip && pip install -e ".[dev]"

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
