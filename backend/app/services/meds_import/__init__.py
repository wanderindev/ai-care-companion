"""One-time import of the two medication lists (Word) as version 1 (DESIGN §6.4).

The pipeline is ``parse → extract → stage → load``, like the bitácora
importer (``app.services.bitacora_import``):

- ``docx_parse``: DOCX → markdown lines (tables as markdown tables), then a
  deterministic pass that finds the section headers (slot table), the
  "DESDE <fecha>" line and drops the patient header (everything before the
  first section header).
- ``extract``: one Claude call per document (structured output) that turns
  the lines into items, the EVITAR list and a list-level note; the slot of
  each item is mapped in code from the header the model reports.
- ``load``: staging JSON → ``save_list`` (version 1), refused when the
  patient already has a version.

The CLI is ``python -m scripts.import_meds`` (``docs/import.md``). Everything
here handles PHI: never log medication names or text.
"""
