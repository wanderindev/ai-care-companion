"""Family chat (DESIGN §5.7, §6.6): Claude with read-only tools over the data.

- ``prompt``: the static Spanish system prompt and the per-turn context line.
- ``tools``: the tool registry (definitions + handlers). Every tool is
  read-only, patient-aware and returns compact JSON with ids and dates so the
  answer can cite them. #33 adds file tools through the same registry.
- ``history``: turns the stored ``chat_messages`` into the ``messages`` list
  of the next request (exact replay, with a length cap).
- ``engine``: one turn — the streaming tool-use loop, SSE events, persistence.
- ``title``: the session title from the first exchange.

Privacy: message text, tool inputs and tool results are never logged; only
ids, counts and token usage are.
"""
