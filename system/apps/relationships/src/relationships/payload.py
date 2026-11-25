"""Builds the relationships view payload from the people-CRM SQLite store.

Reads through a consistent in-memory snapshot so the view is safe to query even
while a `people-crm sync` is writing to the same database file.
"""

import sqlite3
from pathlib import Path
from typing import Any

from people_crm.metrics import DEFAULT_SELF_EMAILS
from people_crm.store import create_and_initialize_store

from relationships.filters_store import read_rules

DB_PATH = Path("runtime/people-crm/crm.db")
# The user's own addresses must never appear as a relationship, even if an older
# sync ingested them before they were recognized as self. Beyond any configured
# ones, every address the sync recorded as the user (role SELF) counts.
_SELF_EMAILS = frozenset(str(e).lower() for e in DEFAULT_SELF_EMAILS)

# Personal/free mail domains -- never a person's "company", so they don't
# override a real employer when choosing the primary affiliation.
_FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "pm.me", "fastmail.com", "hey.com", "msn.com",
})

def _primary_email(canonical: str, recency_map: dict[str, str]) -> str:
    # The address a person currently uses: their most recently active *work*
    # address, or (if they only have personal ones) their most recent address.
    if not recency_map:
        return canonical
    dated = list(recency_map.items())
    nonfree = [(e, d) for e, d in dated if e.split("@")[-1].lower() not in _FREE_EMAIL_DOMAINS]
    pool = nonfree or dated
    return max(pool, key=lambda kv: kv[1])[0]


def _is_one_way(meetings: int, sent: int) -> bool:
    # Someone who emailed you but you never replied to, and never met -- not a
    # relationship. (Newsletters and cold inbound both fall here.)
    return meetings == 0 and sent == 0


def _snapshot(db_path: Path) -> sqlite3.Connection:
    # Before the first sync there is no store yet: create it empty, so the view
    # renders an empty list instead of failing.
    if not db_path.exists():
        create_and_initialize_store(db_path).close()
    # Read-only + a generous busy timeout so a concurrent `people-crm sync`
    # writing to the same file can't make the view error out.
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    source.execute("PRAGMA busy_timeout = 30000")
    memory = sqlite3.connect(":memory:")
    source.backup(memory)
    source.close()
    memory.row_factory = sqlite3.Row
    return memory


# Calendar-notification email subjects -- pure scheduling noise, not real
# correspondence, so they don't count toward a person's email signal.
_SCHEDULING_PREFIXES = (
    "invitation:", "updated invitation", "accepted:", "declined:", "tentative:",
    "canceled:", "cancelled:", "canceled event", "cancelled event",
    "new time proposed", "declined with note", "accepted with note",
    "tentative with note", "re: invitation", "fwd: invitation",
)


def _is_scheduling_email(title: str | None) -> bool:
    return (title or "").strip().lower().startswith(_SCHEDULING_PREFIXES)


# Email can lift a score only so far: a rich email thread must never out-index a
# real meeting relationship, and a burst of scheduling back-and-forth shouldn't
# inflate anyone.
_MAX_EMAIL_STRENGTH = 6.0


def _strength(
    meetings: int, one_on_one: int, focused: int, sent: int, received: int
) -> float:
    # Meetings dominate by design; email is a small, capped tie-breaker.
    email_score = min(sent * 1.0 + received * 0.2, _MAX_EMAIL_STRENGTH)
    return one_on_one * 5 + focused * 2 + meetings * 3 + email_score


def _channel(meetings: int, emails: int) -> str:
    if meetings and emails:
        return "both"
    if meetings:
        return "meetings"
    return "email"


def build_summary_payload(db_path: Path = DB_PATH) -> dict[str, Any]:
    conn = _snapshot(db_path)
    try:
        org_name_by_id = {
            row["org_id"]: row["name"] for row in conn.execute("select org_id, name from org")
        }
        # domain -> org name, so a person's *current* domain resolves to its company
        org_name_by_domain = {
            row["domain"]: org_name_by_id.get(row["org_id"])
            for row in conn.execute("select domain, org_id from org_domain")
            if org_name_by_id.get(row["org_id"])
        }
        self_emails = _SELF_EMAILS | {
            row["email"].lower()
            for row in conn.execute(
                "select distinct email from interaction_participant where role = 'SELF'"
            )
        }
        emails_by_person: dict[str, list[str]] = {}
        for row in conn.execute("select person_id, email from person_email"):
            emails_by_person.setdefault(row["person_id"], []).append(row["email"])

        # Most recent interaction date per (person, email address), to pick the
        # address they currently use.
        recency: dict[str, dict[str, str]] = {}
        for row in conn.execute(
            """
            select pe.person_id as pid, ip.email as email, max(i.occurred_at) as last
            from interaction_participant ip
            join person_email pe on pe.email = ip.email
            join interaction i on i.interaction_id = ip.interaction_id
            group by pe.person_id, ip.email
            """
        ):
            if row["last"]:
                recency.setdefault(row["pid"], {})[row["email"]] = row["last"]

        metric_by_person = {
            row["person_id"]: row
            for row in conn.execute("select * from person_metric")
        }

        # Per person, aggregate their interactions (deduped to one row per
        # interaction even when several of their emails appear on it).
        agg: dict[str, dict[str, Any]] = {}
        query = """
            select distinct pe.person_id as person_id, ip.interaction_id as iid,
                   i.source_system as src, i.direction as dir, i.occurred_at as at,
                   i.title as title
            from interaction_participant ip
            join person_email pe on pe.email = ip.email
            join interaction i on i.interaction_id = ip.interaction_id
            where ip.role != 'SELF'
        """
        for row in conn.execute(query):
            a = agg.setdefault(
                row["person_id"],
                {"emails": 0, "sent": 0, "received": 0, "last_meeting": None,
                 "last_email": None, "last_contact": None, "first_contact": None},
            )
            at = row["at"]
            date = at[:10] if at else None
            if row["src"] == "GMAIL":
                # calendar invitations/accepts are scheduling noise, not correspondence
                if _is_scheduling_email(row["title"]):
                    continue
                a["emails"] += 1
                if row["dir"] == "SENT":
                    a["sent"] += 1
                elif row["dir"] == "RECEIVED":
                    a["received"] += 1
                if date and (a["last_email"] is None or date > a["last_email"]):
                    a["last_email"] = date
            else:
                if date and (a["last_meeting"] is None or date > a["last_meeting"]):
                    a["last_meeting"] = date
            if date and (a["last_contact"] is None or date > a["last_contact"]):
                a["last_contact"] = date
            if date and (a["first_contact"] is None or date < a["first_contact"]):
                a["first_contact"] = date

        people: list[dict[str, Any]] = []
        for row in conn.execute(
            "select person_id, display_name, display_name_source, canonical_email, org_id from person"
        ):
            pid = row["person_id"]
            person_emails = {e.lower() for e in emails_by_person.get(pid, [])}
            person_emails.add((row["canonical_email"] or "").lower())
            # Skip the user themselves (any of their own addresses).
            if person_emails & self_emails:
                continue
            m = metric_by_person.get(pid)
            a = agg.get(pid, {})
            meetings = m["meeting_count"] if m else 0
            one_on_one = m["one_on_one_count"] if m else 0
            focused = m["focused_count"] if m else 0
            hours_focused = round((m["minutes_focused"] or 0) / 60, 1) if m else 0.0
            emails = a.get("emails", 0)
            sent = a.get("sent", 0)
            received = a.get("received", 0)
            canonical = row["canonical_email"] or ""
            # the person's current affiliation, from their most recent work address
            primary = _primary_email(canonical, recency.get(pid, {}))
            domain = primary.split("@")[-1].lower() if primary else ""
            org = org_name_by_domain.get(domain)
            people.append(
                {
                    "person_id": pid,
                    "name": row["display_name"],
                    "name_source": row["display_name_source"],
                    "email": primary or canonical,
                    "emails": emails_by_person.get(pid, []),
                    "org": org or domain,
                    "org_named": bool(org),
                    "domain": domain,
                    "meetings": meetings,
                    "one_on_one": one_on_one,
                    "focused": focused,
                    "hours_focused": hours_focused,
                    "email_total": emails,
                    "email_sent": sent,
                    "email_received": received,
                    "last_meeting": a.get("last_meeting"),
                    "last_email": a.get("last_email"),
                    "last_contact": a.get("last_contact"),
                    "first_contact": a.get("first_contact"),
                    "channel": _channel(meetings, emails),
                    "strength": _strength(meetings, one_on_one, focused, sent, received),
                    # emailed you but you never replied (and never met) -> not a relationship
                    "one_way": _is_one_way(meetings, sent),
                }
            )
        people.sort(key=lambda p: -p["strength"])

        # Attach the editable exclusion ruleset and mark who each rule hides.
        rules = read_rules(db_path)
        excluded_domains = {d["value"].lower() for d in rules["domains"]}
        excluded_emails = {e["value"].lower() for e in rules["emails"]}
        for p in people:
            person_addrs = {e.lower() for e in p["emails"]} | {p["email"].lower()}
            p["excluded"] = bool(
                p["domain"].lower() in excluded_domains or person_addrs & excluded_emails
            )

        # The default population is people you've engaged with (met or replied to).
        visible = [p for p in people if not p["excluded"] and not p["one_way"]]
        stats = {
            "people": len(visible),
            "with_meetings": sum(1 for p in visible if p["meetings"]),
            "email_only": sum(1 for p in visible if p["channel"] == "email"),
            "orgs": len({p["org"] for p in visible if p["org_named"]}),
            "excluded": sum(1 for p in people if p["excluded"]),
            "one_way": sum(1 for p in people if p["one_way"] and not p["excluded"]),
        }
        return {"people": people, "stats": stats, "filters": rules}
    finally:
        conn.close()


def build_person_detail(person_id: str, db_path: Path = DB_PATH) -> dict[str, Any]:
    conn = _snapshot(db_path)
    try:
        query = """
            select distinct i.interaction_id as iid, i.source_system as src,
                   i.direction as dir, i.occurred_at as at, i.title as title,
                   i.source_link as link, i.duration_minutes as dur
            from interaction_participant ip
            join person_email pe on pe.email = ip.email
            join interaction i on i.interaction_id = ip.interaction_id
            where pe.person_id = ? and ip.role != 'SELF'
            order by i.occurred_at desc
            limit 40
        """
        timeline = [
            {
                "type": "email" if row["src"] == "GMAIL" else "meeting",
                "direction": row["dir"],
                "date": row["at"][:10] if row["at"] else None,
                "title": row["title"] or "(untitled)",
                "link": row["link"],
                "duration_min": row["dur"],
            }
            for row in conn.execute(query, (person_id,))
        ]
        return {"person_id": person_id, "timeline": timeline}
    finally:
        conn.close()
