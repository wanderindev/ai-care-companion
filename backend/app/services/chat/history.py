"""Stored chat messages → the ``messages`` list of the next request.

Replay is **exact**: each row's ``content`` goes back as stored, thinking
blocks included, because a thinking block is bound to the conversation prefix
that produced it and an edited prefix invalidates it (and the prompt cache).
The only transformations, all deterministic:

- an ``interrupted`` assistant row keeps only its text blocks, and an
  assistant row left with no content is skipped;
- a ``tool_use`` whose ``tool_result`` is not in the next message is dropped
  (it can only come from an interrupted turn);
- **length cap**: when the stored history is longer than ``MAX_HISTORY_CHARS``
  (≈ 75–100k tokens), the oldest family turns are dropped (always at a family
  message, never inside a tool round), a note says how many messages were
  left out, and every thinking block is stripped from what remains — those
  blocks were produced with the full history present and would no longer be
  valid (the requests also set ``drop_block``, see ``engine``).
"""

import json
from dataclasses import dataclass
from typing import Any

from app.models.chat import STATUS_INTERRUPTED

MAX_HISTORY_CHARS = 300_000
THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


@dataclass
class StoredMessage:
    role: str
    content: list[dict[str, Any]]
    status: str = "complete"
    is_prompt: bool = False  # a family member's turn (not tool results)


def omitted_note(count: int) -> dict[str, Any]:
    return {
        "type": "text",
        "text": (
            f"[Nota del sistema: se omitieron los {count} mensajes más antiguos de esta "
            "conversación por su extensión.]"
        ),
    }


def _size(content: list[dict[str, Any]]) -> int:
    return len(json.dumps(content, ensure_ascii=False))


def _pair_tools(messages: list[StoredMessage]) -> list[StoredMessage]:
    out: list[StoredMessage] = []
    for i, msg in enumerate(messages):
        content = msg.content
        if msg.role == "assistant":
            if msg.status == STATUS_INTERRUPTED:
                content = [b for b in content if b.get("type") == "text"]
            uses = {b.get("id") for b in content if b.get("type") == "tool_use"}
            if uses:
                nxt = messages[i + 1] if i + 1 < len(messages) else None
                answered = (
                    {b.get("tool_use_id") for b in nxt.content if b.get("type") == "tool_result"}
                    if nxt is not None and nxt.role == "user"
                    else set()
                )
                if not uses <= answered:
                    content = [
                        b
                        for b in content
                        if not (b.get("type") == "tool_use" and b.get("id") not in answered)
                    ]
            if not content:
                continue
        elif not msg.is_prompt:
            # Tool results whose tool_use was dropped above would be rejected.
            prev = out[-1] if out else None
            known = (
                {b.get("id") for b in prev.content if b.get("type") == "tool_use"}
                if prev is not None and prev.role == "assistant"
                else set()
            )
            content = [
                b
                for b in content
                if b.get("type") != "tool_result" or b.get("tool_use_id") in known
            ]
            if not content:
                continue
        out.append(StoredMessage(msg.role, content, msg.status, msg.is_prompt))
    return out


def build_messages(
    stored: list[StoredMessage], max_chars: int = MAX_HISTORY_CHARS
) -> tuple[list[dict[str, Any]], int]:
    """``(messages, omitted_count)`` for the request; the last stored message
    must be the family member's new turn."""
    messages = _pair_tools(stored)
    sizes = [_size(m.content) for m in messages]
    total = sum(sizes)
    start = 0
    if total > max_chars:
        prompts = [i for i, m in enumerate(messages) if m.is_prompt]
        remaining = total
        start = prompts[-1] if prompts else 0
        for i in prompts:
            if remaining - sum(sizes[:i]) <= max_chars:
                start = i
                break
    if start == 0:
        return [{"role": m.role, "content": m.content} for m in messages], 0
    kept = messages[start:]
    out: list[dict[str, Any]] = []
    for idx, m in enumerate(kept):
        content = [b for b in m.content if b.get("type") not in THINKING_TYPES]
        if idx == 0:
            content = [omitted_note(start), *content]
        if not content:
            continue
        out.append({"role": m.role, "content": content})
    return out, start
