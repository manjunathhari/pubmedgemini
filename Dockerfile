FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Settings.migrations_dir is the relative path "migrations", so the working
# directory must hold it.
WORKDIR /app

# Runtime dependencies from the hashed lock, so every build installs the same versions.
COPY requirements.lock ./
RUN pip install --require-hashes --no-deps -r requirements.lock

COPY pyproject.toml ./
COPY src ./src
# A normal (non-editable) install: package-data puts the templates in site-packages.
# --no-deps: everything it needs came from the lock above.
RUN pip install --no-deps .

COPY migrations ./migrations

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "pubmedgemini.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
