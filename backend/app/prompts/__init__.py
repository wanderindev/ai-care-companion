"""Versioned prompts for Claude producers (DESIGN §6.7).

Each module holds one prompt version: its ``VERSION`` string (recorded with
what the prompt produced), the static system prompt and the strict JSON
schema of the answer, bundled as a :class:`PromptSpec`. A change to the
prompt or the schema is a new module (``daily_analysis_v2``…), never an edit
of a version already used, so a suggestion can always be traced to the exact
instructions behind it (``whatsapp_imports.stats.analysis.prompt_version``).
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptSpec:
    version: str
    system: str
    schema: dict[str, Any]
