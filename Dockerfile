FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app
COPY pyproject.toml .
COPY google_ai_mode/ google_ai_mode/

RUN pip install --no-cache-dir -e . && \
    playwright install chromium

ENV CHROME_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu --headless=new --remote-debugging-port=9222 --disable-blink-features=AutomationControlled --window-size=1280,720"

EXPOSE 8080

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
