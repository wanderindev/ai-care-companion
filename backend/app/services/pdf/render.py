"""HTML (stdin) → PDF (stdout) with WeasyPrint, run as a child process by
:func:`app.services.pdf.render_pdf`.

A separate process because WeasyPrint is heavy: importing and rendering keep
~40–70 MB resident in the process that did it (font caches, allocator
fragmentation), and the API container runs two uvicorn workers inside a
384 MB limit. The child exits after one document, so the memory goes back.

Run by file path, not as ``app.…``: it imports nothing from the app.
Nothing is fetched while rendering (every URL is refused), and WeasyPrint's
own warnings are silenced because they can quote document content.
"""

import logging
import sys


def render(html: str) -> bytes:
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    class NoFetch(URLFetcher):
        """Refuses every URL: a printable never loads external resources."""

        def fetch(self, url, headers=None):
            raise ValueError("external resources are disabled")

    return HTML(string=html, base_url=None, url_fetcher=NoFetch()).write_pdf()


def main() -> int:
    logging.disable(logging.CRITICAL)
    html = sys.stdin.buffer.read().decode("utf-8")
    sys.stdout.buffer.write(render(html))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
