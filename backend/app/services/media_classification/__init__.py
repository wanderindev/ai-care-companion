"""Image and PDF classification of WhatsApp media (DESIGN §6.7 step 3, #37).

- ``classify``: the Claude call for one file (image or PDF content block,
  strict JSON schema: ``category``, ``description``, ``patient_hint``;
  ``CLASSIFICATION_MODEL``, ``ANTHROPIC_API_KEY``).
- ``jobs``: the WhatsApp ``after_import`` hook and the ``classify_media`` job
  (dedupe by SHA-256 in ``media_classifications``; a lab report goes on to
  the labs pipeline with ``labs.process.enqueue_extraction``).
"""
