FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
COPY gemini_search/ gemini_search/
COPY gemini_search_mcp/ gemini_search_mcp/
COPY mcp_server.py .

RUN pip install --no-cache-dir -e . && playwright install chromium --with-deps

EXPOSE 8080

CMD ["python", "-m", "gemini_search", "--port", "8080", "--channel", "chromium"]
