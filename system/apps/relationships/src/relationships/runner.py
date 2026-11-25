"""Your full relationship view across meetings and email, built on the people-CRM store.

Services run from /mngr/code (the repo root). Conventions:

- Runtime state files (anything written and read across runs, e.g.
  cursors, caches, last-visit timestamps): use cwd-relative paths like
  ``Path("runtime/relationships/...")``. Do NOT use ``Path(__file__)``-based
  paths for runtime state -- the bug to avoid is one process writing
  to ``/mngr/code/runtime/...`` while another reads from
  ``/mngr/code/libs/<pkg>/runtime/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.

This is a synchronous Flask app served by the threaded Werkzeug server.
The system_interface proxy at ``/service/relationships/`` rewrites absolute
paths in served HTML and installs a scoped service worker that prepends
the prefix to the page's own fetches, so the app can serve at ``/`` and
still work behind the proxy. Use ``flask_sock`` if you need WebSockets.
"""

import os
import threading
from pathlib import Path

from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from jinja2.utils import htmlsafe_json_dumps
from werkzeug.serving import run_simple

from relationships.filters_store import add_rule
from relationships.filters_store import read_rules
from relationships.filters_store import remove_rule
from relationships.filters_store import set_category
from relationships.payload import DB_PATH
from relationships.payload import build_person_detail
from relationships.payload import build_summary_payload

PORT = int(os.environ.get("RELATIONSHIPS_PORT", "8085"))

app = Flask("relationships", static_folder=None)

_ASSETS = Path(__file__).parent / "assets"


def render_page(payload: dict) -> str:
    """Inline the payload into the page's ``<script type="application/json">`` block.

    The payload carries attendee names and meeting titles, which are
    attacker-supplied: anyone who can send a calendar invite controls them.
    ``json.dumps`` escapes neither ``<`` nor ``/``, so a value containing
    ``</script>`` would close the data element early and the rest would parse as
    HTML. ``htmlsafe_json_dumps`` escapes the HTML-significant characters (and
    U+2028/U+2029) as ``\\uXXXX``, which ``JSON.parse`` decodes back to the
    original string.
    """
    template = (_ASSETS / "app.html").read_text()
    return template.replace("__DATA__", htmlsafe_json_dumps(payload))


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _page_signature(db_path: Path) -> tuple[tuple[int, int] | None, ...]:
    # Everything the page is built from: the CRM store (a write may sit in its
    # WAL before it reaches the main file; filter edits land in the store too)
    # and the page template.
    return (
        _file_signature(db_path),
        _file_signature(db_path.with_name(db_path.name + "-wal")),
        _file_signature(_ASSETS / "app.html"),
    )


# Building the summary scores every person from a full copy of the store, which
# takes seconds; the store changes only on a sync or a filter edit, so the
# rendered page is kept until one of its inputs changes.
_page_lock = threading.Lock()
_cached_pages: dict[Path, tuple[tuple[tuple[int, int] | None, ...], str]] = {}


def summary_page(db_path: Path = DB_PATH) -> str:
    with _page_lock:
        signature = _page_signature(db_path)
        cached = _cached_pages.get(db_path)
        if cached is None or cached[0] != signature:
            cached = (signature, render_page(build_summary_payload(db_path)))
            _cached_pages[db_path] = cached
        return cached[1]


@app.route("/")
def index() -> Response:
    return Response(summary_page(), mimetype="text/html")


@app.route("/api/person/<person_id>")
def person_detail(person_id: str) -> Response:
    return jsonify(build_person_detail(person_id))


def _mutate_filter(rule_type: str) -> Response | tuple[Response, int]:
    body = request.get_json(force=True, silent=True) or {}
    value = (body.get("value") or "").strip().lower().lstrip("@")
    if not value:
        return jsonify({"error": "value is required"}), 400
    if request.method == "POST":
        add_rule(rule_type, value, (body.get("label") or "").strip() or None, body.get("category"))
    else:
        remove_rule(rule_type, value)
    return jsonify(read_rules())


@app.route("/api/filters", methods=["GET"])
def get_filters() -> Response:
    return jsonify(read_rules())


@app.route("/api/filters/domain", methods=["POST", "DELETE"])
def filter_domain() -> Response | tuple[Response, int]:
    return _mutate_filter("DOMAIN")


@app.route("/api/filters/email", methods=["POST", "DELETE"])
def filter_email() -> Response | tuple[Response, int]:
    return _mutate_filter("EMAIL")


@app.route("/api/filters/category", methods=["POST"])
def filter_category() -> Response | tuple[Response, int]:
    body = request.get_json(force=True, silent=True) or {}
    rule_type = (body.get("rule_type") or "").strip().upper()
    value = (body.get("value") or "").strip().lower()
    category = (body.get("category") or "").strip().upper()
    if rule_type not in ("DOMAIN", "EMAIL") or not value:
        return jsonify({"error": "rule_type and value are required"}), 400
    set_category(rule_type, value, category)
    return jsonify(read_rules())


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    # Build the page once up front so the first window open doesn't wait for it.
    threading.Thread(target=summary_page, daemon=True).start()
    run_simple("127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
