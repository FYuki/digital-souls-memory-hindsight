FROM python:3.12.3-slim-bookworm@sha256:afc139a0a640942491ec481ad8dda10f2c5b753f5c969393b12480155fe15a63

WORKDIR /app
COPY tests/support/openai_stub.py ./openai_stub.py
CMD ["python", "openai_stub.py"]
