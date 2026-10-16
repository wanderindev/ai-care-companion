"""One-time import of the family's bitácora Word document (DESIGN §6.2).

The pipeline is ``parse → split → extract → stage → load``:

- ``docx_parse``: DOCX → markdown lines (bold/underline kept), then a
  deterministic regex splitter into year sections and dated entries.
- ``extract``: one Claude call per entry (structured output) that fills the
  ``log_entries`` fields plus the next appointment and medication events.
- ``load``: staging JSON → ``log_entries`` rows (``pending_review``,
  ``origin=import_docx``), matching or creating doctors, idempotent.

The CLI is ``python -m scripts.import_bitacora`` (see its docstring and
``docs/import.md``). Everything here handles PHI: never log entry text.
"""
