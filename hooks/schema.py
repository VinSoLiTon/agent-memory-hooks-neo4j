"""Full schema migration for the agent-memory graph.

Runs from `njhook migrate`. NOT called from the hook hot path — see
log_event.ensure_minimal_constraints() for the lightweight per-event
guarantee that the canonical UNIQUE constraints exist.

This module is the right place for:
  - Dropping legacy / out-of-date constraints (requires SHOW CONSTRAINTS,
    which is a metadata operation that doesn't belong in event-write hot path)
  - Adding new constraints / indexes
  - Data backfills (e.g. populating session_key on pre-PR-B Sessions)

Idempotent: re-running is safe and quick once the migration has settled.
"""
from __future__ import annotations


def drop_legacy_constraints(tx) -> list[str]:
    """Drop constraints that conflict with the current schema. Returns the
    list of dropped constraint names (for logging)."""
    dropped: list[str] = []
    try:
        for record in tx.run("SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type"):
            labels = record.get("labelsOrTypes") or []
            props = record.get("properties") or []
            ctype = (record.get("type") or "").upper()
            # Legacy: Session.session_id was UNIQUE pre-PR-B; we now key by
            # session_key (composite) so different clients can reuse session_id.
            if "Session" in labels and props == ["session_id"] and "UNIQUE" in ctype:
                tx.run(f"DROP CONSTRAINT `{record['name']}`")
                dropped.append(record["name"])
    except Exception:
        # SHOW CONSTRAINTS isn't available on this Neo4j version; harmless.
        pass
    return dropped


def create_constraints_and_indexes(tx) -> None:
    tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Session) REQUIRE s.session_key IS UNIQUE")
    tx.run("CREATE INDEX session_id_lookup IF NOT EXISTS FOR (s:Session) ON (s.session_id)")
    tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Event) REQUIRE e.event_id IS UNIQUE")
    tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:Memory) REQUIRE m.path IS UNIQUE")
    tx.run("CREATE FULLTEXT INDEX memory_fulltext IF NOT EXISTS FOR (m:Memory) ON EACH [m.content, m.path]")
    tx.run("CREATE INDEX memory_project IF NOT EXISTS FOR (m:Memory) ON (m.project)")


def backfill_session_keys(tx) -> int:
    """Set session_key on any Session that predates PR-B. Returns rows touched."""
    r = tx.run(
        "MATCH (s:Session) WHERE s.session_key IS NULL "
        "SET s.session_key = coalesce(s.client, 'unknown') + ':' + coalesce(s.session_id, 'unknown') "
        "RETURN count(s) AS n"
    ).single()
    return int(r["n"]) if r else 0


def run_full_migration(driver) -> dict:
    """Run drop -> create -> backfill in three separate transactions (Neo4j
    forbids mixing schema and data writes). Returns a small report dict."""
    report: dict = {}
    with driver.session() as ses:
        report["dropped_constraints"] = ses.execute_write(drop_legacy_constraints)
        ses.execute_write(create_constraints_and_indexes)
        report["session_keys_backfilled"] = ses.execute_write(backfill_session_keys)
    return report
