"""Protocol defaults for Gradio-powered Arena deployments."""

from __future__ import annotations

DEFAULT_BASE_URL = "https://arena.ai"
DEFAULT_TIMEOUT = 45.0
DEFAULT_QUEUE_TIMEOUT = 180.0
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "accept": "text/event-stream, application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://arena.ai",
    "referer": "https://arena.ai/",
    "user-agent": CHROME_USER_AGENT,
}

# Gradio has used both variants across releases.
QUEUE_JOIN_PATH = "/queue/join"
QUEUE_DATA_PATH = "/queue/data"
CONFIG_PATH = "/config"

