---
title: "People CRM"
description: "A personal relationship view across your meetings and email, kept fresh by a daily sync of Google Calendar, Gmail and Contacts."
thumbnail: "template.svg"
version: v1
format: v2
---

# People CRM

This file is the manifest for the **People CRM** template (slug:
`people-crm`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

A personal relationship view across your meetings and email, kept fresh by a daily sync of Google Calendar, Gmail and Contacts.

Relationships is a personal CRM built from what you already do: it reads your
Google Calendar, Gmail and Contacts and turns them into one sortable list of
everyone you meet with or email. Each person shows how many meetings and 1:1s
you have had together, how many email threads, when you were last in touch, and
a relationship-strength score; clicking someone opens their detail drawer with
the underlying meetings and messages, each linking back to Google. You can hide
spam, automated senders and whole companies, or tag people (personal, spam,
other), and those choices survive every re-sync. A routine refreshes the data
every morning at 05:00, so the list is current when you open it.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/relationships/`
- `system/supervisord.conf.d/relationships.conf`
- `system/libs/people_crm/`
- `system/routines/people-crm-sync.toml`

- `system/apps/relationships/` -- the **Relationships** app: a Flask server on
  port 8085 (`relationships` package, `runner.py`) that serves the list page,
  `GET /api/person/<id>` for a person's detail, and `/api/filters/*` to add or
  remove hide/tag rules. It reads the store through an in-memory snapshot, so it
  stays usable while a sync is writing, and caches the rendered page until the
  store changes. If the store does not exist yet it creates it empty, so the
  page renders (with no people) before the first sync.
- `system/supervisord.conf.d/relationships.conf` -- the supervisord program
  `relationships`: syncs the app's own venv, registers port 8085 with
  `forward_port.py` (from the app's `app.toml`), and runs the server.
- `system/libs/people_crm/` -- the people-CRM library and its `people-crm` CLI
  (`sync`, `recompute`, `add-email`, `show`, `export-web-payload`, `migrate`).
  `people-crm sync` fetches three years of calendar events from the connected
  account's primary calendar, up to 5000 recent Gmail messages (metadata only),
  and your contacts, all through `latchkey curl`; it then merges addresses into
  people (using contacts to join someone's several addresses), resolves
  companies from email domains, and recomputes per-person metrics. Everything
  lands in a SQLite store at `runtime/people-crm/crm.db`, split into a raw
  source layer (every event and message, with a link back to Google), a derived
  layer that is rebuilt on each sync, and a user layer (filters, tags, manual
  merges) that a sync never overwrites. The user themselves is recognized from
  the connected Gmail account's address and from the calendar's own "this is
  you" marker on events, never from a hardcoded address. The app depends on this
  library by path.
- `system/routines/people-crm-sync.toml` -- the daily routine (05:00, 3 h
  timeout, retried after an hour on failure) that runs `people-crm sync` and
  then requests the app's page so it is rebuilt and ready.

Known limits: a full sync takes about two hours, because it fetches metadata
for each of up to 5000 emails one request at a time; and opening the page while
a sync is writing triggers a 15-20 second rebuild.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


- requires_permission: google-calendar-api / google-calendar-read-events (the sync reads events from the connected account's primary calendar; adopting agent initiates during setup)
- requires_permission: google-gmail-api / google-gmail-read-messages (the sync lists recent messages and reads their sender, recipient, date and subject headers only; adopting agent initiates during setup)
- requires_permission: google-gmail-api / google-gmail-read-profile (the sync reads the connected account's own address so the user is never listed as a contact; adopting agent initiates during setup)
- requires_permission: google-people-api / google-people-read-contacts (the sync reads contacts to join one person's several addresses and pick up names; adopting agent initiates during setup)
- requires_llm: the library includes an optional category-feedback judge (`people_crm.ai_judge`) that calls Claude via the keyed litellm path (ANTHROPIC_API_KEY); no command, page or routine calls it today, so nothing needs a key to run. Only an adopter who wires it in needs a key, or must switch it to the keyless path per use-ai-integration.

Adaptation:

- **The store starts empty.** No data ships with this template. After the
  Google permissions are approved, run the first sync by hand
  (`uv run --no-sync --project system/libs/people_crm people-crm sync`, about two
  hours) rather than waiting for the 05:00 routine, so the user sees their own
  people the same day.
- **Only the connected account counts as "you".** The user's other addresses
  (an old work address, a personal address that is not the connected account)
  will show up as contacts. Ask the user for any other addresses they use and
  add them to `DEFAULT_SELF_EMAILS` in
  `system/libs/people_crm/src/people_crm/metrics.py`.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `connect-external-service` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-11-25) -- first version: the Relationships app, the people-CRM sync library, and the daily 05:00 sync routine

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
