# Web-view interface (hand-off to the meeting-contacts service)

This document describes how the live `meeting-contacts` web service can later read
from the people-CRM store instead of the flat `all_contacts.json` file. **The live
service is intentionally not rewired by this library** — this is the documented
contract for a future, deliberate switch.

## The function

```python
from datetime import date
from people_crm.store import create_and_initialize_store
from people_crm.web_view import build_web_view_payload

store = create_and_initialize_store(Path("runtime/people-crm/crm.db"))
payload = build_web_view_payload(store, date.today())
```

`build_web_view_payload(store, today)` returns a JSON-serializable `dict` with the
**same top-level shape the current `app.html` already consumes**:

```jsonc
{
  "today": "2026-07-06",
  "people": [ /* ranked list, see below */ ],
  "filters": { "domains": [{"domain": "...", "label": "...", "source": "SEED"}], "emails": ["..."] }
}
```

Each `people[]` entry preserves the field names the prototype used, so the existing
front-end keeps working unchanged:

`name`, `name_source`, `email`, `domain`, `default_org`, `count`, `one_on_one`,
`small_group` (focused count), `hours_total`, `hours_focused`, `first`, `last`,
`days_since_last`, `group_only`, `samples`.

It adds, without breaking anything:

- `person_id` — the stable id (use it for user-data actions: tags, notes, add-email).
- `emails` — every address merged into this person.
- each `samples[]` item carries `source_link` (a deep link back to the original
  Google record) and `source_ref` — the **preserve + surface** affordance.

The list is pre-sorted with the prototype's default ranking (people actually met
in small settings first).

## CLI

```bash
uv run --no-sync --project system/libs/people_crm people-crm export-web-payload --output runtime/people-crm/web_payload.json
```

## How the live service would adopt it (later, deliberately)

`libs/meeting_contacts/src/meeting_contacts/runner.py` currently builds its page
from `runtime/fetch-process-show/meeting-contacts/all_contacts.json` in
`_load_page()`. To switch it to the store, that one function would call
`build_web_view_payload(store, date.today())` instead of reading the JSON, and read
its editable exclusion list from `store.get_all_filter_rules()` instead of
`filters.json`. Writes to filters (the service's `POST` handler) would become
`store.upsert_filter_rule(...)`. No other part of the front-end needs to change,
because the payload shape is identical.

This switch is left to a future change so the running service the user relies on is
never disturbed by this foundation work.
