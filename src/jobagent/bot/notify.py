"""Fire-and-forget Telegram sender via the Bot API (httpx).

Used by the cron digest push — no need to spin up the full bot Application just
to send a message. Splits long text on the 4096-char Telegram limit.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

_API = "https://api.telegram.org/bot{token}/sendMessage"
_LIMIT = 4000  # under Telegram's 4096 hard cap, leaving headroom


def chunk_text(text: str, size: int = _LIMIT) -> Iterator[str]:
    """Yield <=size chunks, preferring to break on newlines."""
    while text:
        if len(text) <= size:
            yield text
            return
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def send_message(
    token: str,
    chat_id: int | str,
    text: str,
    client: httpx.Client | None = None,
) -> int:
    """Send (chunked) text. Returns number of chunks sent. Raises on missing creds."""
    if not token or chat_id in (None, ""):
        raise ValueError("telegram token and chat_id are required to send")
    owns = client is None
    client = client or httpx.Client(timeout=30)
    sent = 0
    try:
        for chunk in chunk_text(text):
            resp = client.post(
                _API.format(token=token),
                data={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            )
            resp.raise_for_status()
            sent += 1
    finally:
        if owns:
            client.close()
    return sent
