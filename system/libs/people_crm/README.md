# people-crm

A durable, normalized data foundation for a people-CRM / relationship graph,
built from the user's Google Calendar and Gmail.

It replaces the flat `all_contacts.json` rollup that the `meeting-contacts` web
view currently reads with a proper SQLite store split into three clearly
separated layers:

- **Source** (`email_identity`, `interaction`, `interaction_participant`) — the
  raw calendar events and email messages, upserted by their stable Google id,
  with the full raw payload and a deep link back to the origin. Never hand-edited.
- **Derived** (`person`, `person_email`, `org`, `org_domain`, `person_metric`) —
  merged people, the auditable email→person merge map, orgs, and the metrics
  *view*, all recomputed from the source log. Never hand-edited.
- **User data** (`person_user_data`, `person_tag`, `person_category_feedback`,
  `identity_override`, `filter_rule`) — everything the user owns. A re-sync NEVER
  writes here, so tags, notes, manual merges, and filters always survive.

## Layout

```
people_crm.cli          click CLI (sync / migrate / recompute / add-email / show / export-web-payload)
people_crm.web_view     build_web_view_payload() — the JSON the meeting-contacts view consumes
people_crm.migration    import all_contacts.json + filters.json (nothing lost)
people_crm.backfill     manual add-email to a person + on-demand Google backfill
people_crm.sync         orchestrates ingest -> resolve identities/orgs -> recompute metrics
people_crm.google_api   LatchkeyGoogleApiClient (calendar + gmail via `latchkey curl`)
people_crm.ai_judge     LiteLlmCategoryJudge — interprets free-form category feedback
people_crm.store        SqliteCrmStore — the only stateful component
people_crm.metrics      pure metric functions (reproduces the meeting-contacts rules)
people_crm.identity     pure identity-grouping + org-resolution functions
people_crm.ingest       pure parsers: Google JSON -> Interaction / participant / identity
people_crm.interfaces   CrmStoreInterface, GoogleApiClientInterface, AiJudgeInterface
people_crm.data_types   Person, Org, EmailIdentity, Interaction, ... (FrozenModels)
people_crm.errors       PeopleCrmError hierarchy
people_crm.primitives   PersonId, OrgId, InteractionId, EmailAddress, DomainName, ...
```

## Store location

`runtime/people-crm/crm.db` (created on first `sync` / `migrate`).

## Web-view interface (for a later hand-off)

`people_crm.web_view.build_web_view_payload(store)` returns the exact shape the
existing `meeting-contacts` `app.html` consumes (`{today, people[], filters}`),
computed from the store. The live service is intentionally NOT rewired here; when
ready, its `_load_page` can switch to call this function instead of reading
`all_contacts.json`. See `docs/web_view_interface.md`.
