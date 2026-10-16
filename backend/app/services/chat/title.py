"""Session title from the first exchange.

One small request to ``CHAT_TITLE_MODEL`` (a cheap model, low effort) with
the family member's first message and the start of the answer. Any failure —
no answer, an API error, an empty or odd reply — falls back to the first
message, shortened at a word boundary. Never logs the text.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 80
FALLBACK_CHARS = 60
TITLE_MAX_TOKENS = 1024
INPUT_CHARS = 2_000
TITLE_TIMEOUT_SECONDS = 30.0  # part of the turn's lock budget (engine.LOCK_STALE)

TITLE_SYSTEM = """\
Escribe un título breve (de 3 a 7 palabras, en español) para una conversación \
de una familia con su asistente sobre la salud de sus padres. Responde solo \
con el título: sin comillas, sin punto final, sin emojis."""


def fallback_title(text: str) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return "Nueva conversación"
    if len(clean) <= FALLBACK_CHARS:
        return clean
    cut = clean[:FALLBACK_CHARS].rsplit(" ", 1)[0] or clean[:FALLBACK_CHARS]
    return cut.rstrip(" ,.;:") + "…"


def clean_title(raw: str) -> str:
    line = next((ln for ln in (raw or "").splitlines() if ln.strip()), "")
    line = re.sub(r"^(t[ií]tulo\s*:\s*)", "", line.strip(), flags=re.IGNORECASE)
    previous = None
    while line != previous:
        previous = line
        line = line.strip().strip("\"'«»“”*#").rstrip(".").strip()
    if len(line) > MAX_TITLE_CHARS:
        line = line[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return line


def make_title(client: Any, model: str, question: str, answer: str) -> str:
    if not question.strip() or not answer.strip():
        return fallback_title(question)
    content = (
        f"Mensaje de la familia:\n{question[:INPUT_CHARS]}\n\n"
        f"Inicio de la respuesta:\n{answer[:INPUT_CHARS]}"
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=TITLE_MAX_TOKENS,
            timeout=TITLE_TIMEOUT_SECONDS,
            system=TITLE_SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return fallback_title(question)
        raw = "".join(
            getattr(b, "text", "") or ""
            for b in response.content
            if getattr(b, "type", None) == "text"
        )
        title = clean_title(raw)
    except Exception as exc:
        logger.warning("chat: title request failed (%s)", type(exc).__name__)
        return fallback_title(question)
    return title or fallback_title(question)
