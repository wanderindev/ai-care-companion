"""WhatsApp analysis: messages → suggestions (DESIGN §6.7 steps 4 and 6).

- ``context``: the record context (patients, doctors, current meds, recent
  entries, upcoming appointments) sent with every call.
- ``analyze``: one Claude call (prompt and schema from
  ``app.prompts.daily_analysis_v2``).
- ``pipeline``: readiness, grouping by Panamá day, rendering, payloads,
  dedupe and ``create_suggestion``.
- ``jobs``: the ``after_import`` hook and the ``analyze_import`` handler.
- ``backfill``: admin-started runs over a date range (DESIGN §6.7 step 6):
  estimate, start/stop/resume, undo and the ``analyze_backfill`` handler.
"""
