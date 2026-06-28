FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
COPY google_ai_mode/ google_ai_mode/

RUN pip install --no-cache-dir -e .

EXPOSE 8080

CMD ["python", "-m", "google_ai_mode", "--port", "8080"]
