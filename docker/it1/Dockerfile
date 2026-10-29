FROM python:3.12.3-slim-bookworm@sha256:afc139a0a640942491ec481ad8dda10f2c5b753f5c969393b12480155fe15a63

RUN pip install --no-cache-dir uv==0.8.22
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen
COPY src ./src
COPY tests ./tests
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./
ENV PYTHONPATH=/app/src
ENV PATH=/app/.venv/bin:$PATH
CMD ["bash", "scripts/run-service.sh"]
