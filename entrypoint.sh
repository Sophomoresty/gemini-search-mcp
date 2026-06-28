#!/bin/bash
# Launch Chrome in background, then start the API server
chromium $CHROME_FLAGS &
sleep 3
exec python3 -m google_ai_mode --port 8080 --cdp-url http://127.0.0.1:9222
