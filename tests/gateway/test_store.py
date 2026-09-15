"""The gateway store: migrations, keys, sessions, receipts, write claims, approvals, policies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchpress.context import Context, GateVerdict
from benchpress.gateway.store import (
    ApprovalRow,
    SessionExists,
    SqlIdempotencyStore,
    Store,
    current_revision,
    normalize_url,
)
from benchpress.write_receipts import receipt_line
from tests.conftest import make_action


def test_normalize_url_selects_psycopg() -> None:
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"


def test_migrations_reach_head_and_match_the_models(tmp_path: Path) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from benchpress.gateway.store.models import Base

    url = f"sqlite:///{tmp_path / 'm.db'}"
    store = Store.open(url)
    assert current_revision(url) == "0001"
    with store.engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_api_keys_are_hashed_and_revocable(store: Store) -> None:
    ws = store.create_workspace("acme")
    key, plaintext = store.create_api_key(ws.id, "ci")
    assert plaintext.startswith("bp_") and len(plaintext) > 40
    assert key.prefix == plaintext[:10]
    found = store.key_for(plaintext)
    assert found is not None and found[0].name == "acme" and found[1].name == "ci"
    assert store.key_for(plaintext + "x") is None
    store.revoke_api_key(key.id)
    assert store.key_for(plaintext) is None


def test_sessions_round_trip_the_context(store: Store) -> None:
    ws = store.create_workspace("acme")
    ctx = Context(trial_id="s1", user_prompt="Rivermill Studio moves to ap@rivermill.example")
    ctx.protected.ids.add("702")
    sid = store.create_session(ws.id, ctx, session_id="s1", idempotency_scope="workspace")
    loaded = store.load_session(ws.id, sid)
    assert loaded is not None
    context, scope = loaded
    assert context.user_prompt == ctx.user_prompt and context.protected.ids == {"702"} and scope == "workspace"
    with pytest.raises(SessionExists):
        store.create_session(ws.id, ctx, session_id="s1")
    assert store.load_session(ws.id, "missing") is None


def _line(action_id: str, provider: str, status: str, rule: str, refs: tuple[str, ...]) -> str:
    action = make_action(action_id, provider=provider, path=f"/x/{action_id}").model_copy(update={"target_refs": refs})
    verdict = GateVerdict(action_id=action_id, allowed=rule == "allowed", rule=rule)
    return receipt_line(event="write", at="2026-09-14T00:00:00.000Z", workspace="acme", session="s1", action=action,
                        verdict=verdict, status=status, status_code=200, evidence=(), approval=None)


def test_receipts_filter_and_page(store: Store) -> None:
    ws = store.create_workspace("acme")
    store.append_receipt(ws.id, "s1", _line("a1", "hubspot", "verified", "allowed", ("company:Rivermill Studio",)))
    store.append_receipt(ws.id, "s1", _line("a2", "stripe", "refused", "protected", ("customer:cus_P2",)))
    third = store.append_receipt(ws.id, "s2", _line("a3", "hubspot", "mismatch", "allowed", ()))
    assert [r.path for r in store.receipts(ws.id)] == ["/x/a3", "/x/a2", "/x/a1"]
    assert [r.path for r in store.receipts(ws.id, provider="hubspot")] == ["/x/a3", "/x/a1"]
    assert [r.rule for r in store.receipts(ws.id, rule="protected")] == ["protected"]
    assert [r.path for r in store.receipts(ws.id, customer="rivermill")] == ["/x/a1"]
    assert [r.session_id for r in store.receipts(ws.id, session="s2")] == ["s2"]
    assert [r.path for r in store.receipts(ws.id, before=third.id, limit=1)] == ["/x/a2"]
    got = store.receipt(ws.id, third.id)
    assert got is not None and json.loads(got.line)["status"] == "mismatch"
    other = store.create_workspace("other")
    assert store.receipts(other.id) == [] and store.receipt(other.id, third.id) is None


def test_write_claims_follow_the_idempotency_contract(store: Store) -> None:
    assert store.claim_write("acme:s1", "fp", holder="a", now=100.0, lease_seconds=30) == "claimed"
    assert store.claim_write("acme:s1", "fp", holder="b", now=110.0, lease_seconds=30) == "in_flight"
    assert store.claim_write("acme:s1", "fp", holder="b", now=131.0, lease_seconds=30) == "claimed"
    store.complete_write("acme:s1", "fp", holder="a", succeeded=False, now=132.0)  # stale holder: no effect
    assert store.claim_write("acme:s1", "fp", holder="c", now=133.0, lease_seconds=30) == "in_flight"
    store.complete_write("acme:s1", "fp", holder="b", succeeded=False, now=134.0)
    assert store.claim_write("acme:s1", "fp", holder="c", now=135.0, lease_seconds=30) == "claimed"
    store.complete_write("acme:s1", "fp", holder="c", succeeded=True, now=136.0)
    assert store.claim_write("acme:s1", "fp", holder="d", now=9999.0, lease_seconds=30) == "done"


def test_a_success_is_recorded_even_after_a_lease_takeover_released_the_claim(store: Store) -> None:
    """succeeded=True marks a key done unconditionally, whoever holds the lease now — even if
    a stale holder's release already deleted the row (a takeover raced with the original holder's slow
    write actually succeeding). A later claim must see "done", never a fresh "claimed"."""
    assert store.claim_write("acme:s1", "fp", holder="a", now=100.0, lease_seconds=30) == "claimed"
    assert store.claim_write("acme:s1", "fp", holder="b", now=131.0, lease_seconds=30) == "claimed"
    store.complete_write("acme:s1", "fp", holder="b", succeeded=False, now=132.0)
    store.complete_write("acme:s1", "fp", holder="a", succeeded=True, now=133.0)
    assert store.claim_write("acme:s1", "fp", holder="d", now=9999.0, lease_seconds=30) == "done"


async def test_sql_idempotency_store_is_shared_by_two_store_handles(store: Store) -> None:
    other = Store(store.engine)  # a second replica on the same database
    a = SqlIdempotencyStore(store)
    b = SqlIdempotencyStore(other)
    assert await a.claim("acme:s1", "fp", holder="h1") == "claimed"
    assert await b.claim("acme:s1", "fp", holder="h2") == "in_flight"
    await a.complete("acme:s1", "fp", holder="h1", succeeded=True)
    assert await b.claim("acme:s1", "fp", holder="h2") == "done"


def test_approvals_compare_and_set_their_status(store: Store) -> None:
    ws = store.create_workspace("acme")
    action = make_action("w1")
    row = ApprovalRow(id="apr_1", workspace_id=ws.id, session_id="s1", action=action, fingerprint="fp",
                      rule_name="stripe", status="pending", requested_at="2026-09-14T00:00:00.000Z",
                      expires_at=500.0, resolved_at=None, resolved_by=None, note="")
    store.insert_approval(row)
    assert store.pending_approval_for(ws.id, "s1", "fp") == row
    assert store.update_approval("apr_1", status="approved", resolved_at="t", resolved_by="ci", note="ok")
    assert not store.update_approval("apr_1", status="denied", resolved_at="t", resolved_by="ci", note="late")
    got = store.approval(ws.id, "apr_1")
    assert got is not None and got.status == "approved" and got.action == action
    assert [a.id for a in store.approvals(ws.id, status="approved")] == ["apr_1"]


def test_policies_are_per_workspace(store: Store) -> None:
    ws = store.create_workspace("acme")
    store.put_policy(ws.id, "billing-extra", "name: billing-extra\n")
    store.put_policy(ws.id, "billing-extra", "name: billing-extra\ntitle: v2\n")
    assert [(p.name, p.body.endswith("v2\n")) for p in store.policies(ws.id)] == [("billing-extra", True)]
    assert store.delete_policy(ws.id, "billing-extra") and not store.delete_policy(ws.id, "billing-extra")


def test_db_upgrade_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from benchpress import cli

    url = f"sqlite:///{tmp_path / 'cli.db'}"
    assert cli.main(["db", "upgrade", "--store", url]) == 0
    assert "revision 0001" in capsys.readouterr().out
    assert cli.main(["db", "current", "--store", url]) == 0
    assert capsys.readouterr().out.strip() == "0001"


def test_receipt_ids_the_store_cannot_hold_find_nothing(store: Store) -> None:
    """Receipt ids are integer row ids; anything else (or out of the column's range) is simply not found."""
    ws = store.create_workspace("acme")
    row = store.append_receipt(ws.id, "s1", _line("a1", "hubspot", "verified", "allowed", ()))
    for bad in ("", "abc", "-1", "1.0", " 1", "1e3", "١", "9" * 30, "99999999999"):
        assert store.receipt(ws.id, bad) is None
    for bad in ("", "abc", "-1", "1.0", "١", "9" * 30):
        assert store.receipts(ws.id, before=bad) == []
    assert [r.id for r in store.receipts(ws.id, before=str(int(row.id) + 1))] == [row.id]
