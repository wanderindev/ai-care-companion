"""Lab reports from PDFs (DESIGN §5.5, §6.5).

- ``values``: analyte-name keys, tolerant number/range parsing, flags, ID keys.
- ``matching``: patient match (CIP, then name) and analyte match (aliases).
- ``extract``: the Claude call for one PDF (structured output).
- ``process``: ``process_attachment`` (one stored PDF, or one WhatsApp photo
  classified as a lab report → report + results, at most once per SHA-256),
  ``enqueue_extraction``, ``save_report``.
- ``links`` / ``fetch``: lab portal links in chat messages (#16): detection,
  ``lab_links`` rows, and the SSRF-guarded PDF download.
- ``jobs``: the ``lab_extract`` and ``lab_link_fetch`` job handlers and the
  WhatsApp ``after_import`` hooks that queue one extraction per new PDF of a
  ``labs_only`` import (``daily``/``backfill`` PDFs go through
  ``app.services.media_classification`` first) and record lab links
  (imported by the worker).
"""
