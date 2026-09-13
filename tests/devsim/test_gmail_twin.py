"""Gmail twin: seeded shapes match the recorded Arga twin, routes behave like Gmail, unsafe stays observable."""

from __future__ import annotations

import base64
import copy
import email
import email.policy
import json
import re
from collections.abc import AsyncIterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from devsim.twins import available, load_spec
from devsim.twins.base import Store
from devsim.twins.gmail import (
    ADMIN_PATHS,
    OWNER_ADDRESS,
    SPEC,
    SYSTEM_LABELS,
    GmailStore,
    QueryTerm,
    make_data_app,
    make_gmail_admin_app,
    parse_query,
)

ROOT = Path(__file__).resolve().parents[2]
CALIBRATION = ROOT / "devsim" / "calibration" / "gmail"
VENDORED_SCENARIOS = ROOT / "arga-twins-benchmark" / "benchmark" / "argabench_40" / "scenarios"
AUTH = {"Authorization": "Bearer ya29.gmail-twin-owner"}
ME = "/gmail/v1/users/me"

MESSAGE_ID = re.compile(r"^msg_[0-9a-f]{14}$")
LABEL_ID = re.compile(r"^Label_[0-9a-f]{14}$")
DRAFT_ID = re.compile(r"^r-[0-9]{19}$")

NOT_FOUND = {
    "error": {
        "code": 404,
        "message": "Requested entity was not found.",
        "errors": [{"message": "Requested entity was not found.", "domain": "global", "reason": "notFound"}],
        "status": "NOT_FOUND",
    }
}


def load_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def seed_slice(name: str) -> dict[str, Any]:
    return {key: value for key, value in load_json(CALIBRATION / f"seed.{name}.json").items() if key != "_source"}


def make_store(name: str = "ecom-02", seed_key: str = "gmail-test") -> GmailStore:
    store = GmailStore(seed_key)
    store.seed({"gmail": seed_slice(name)})
    return store


def mailbox_state(store: GmailStore) -> dict[str, Any]:
    return cast(dict[str, Any], store.admin_state()["mailboxes"][OWNER_ADDRESS])


def raw_message(to: str, subject: str, body: str, sender: str = OWNER_ADDRESS, *, urlsafe: bool = True) -> str:
    message = EmailMessage()
    message["From"] = sender
    if to:
        message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    encoded = base64.urlsafe_b64encode(message.as_bytes()) if urlsafe else base64.b64encode(message.as_bytes())
    return encoded.decode("ascii")


def decode_b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def header(message: dict[str, Any], name: str) -> str:
    headers = cast(list[dict[str, Any]], message["payload"]["headers"])
    return next(str(entry["value"]) for entry in headers if str(entry["name"]).casefold() == name.casefold())


def ids(payload: dict[str, Any]) -> list[str]:
    return [str(item["id"]) for item in cast(list[dict[str, Any]], payload.get("messages", []))]


def without_message_ids(payload: dict[str, Any]) -> dict[str, Any]:
    """The recorded twin embeds the message id in the Message-ID header; blank it before comparing payloads."""
    cleaned = copy.deepcopy(payload)
    stack: list[dict[str, Any]] = [cleaned]
    while stack:
        node = stack.pop()
        for entry in cast(list[dict[str, Any]], node.get("headers", [])):
            if str(entry["name"]).casefold() == "message-id":
                entry["value"] = "<id>"
        stack.extend(cast(list[dict[str, Any]], node.get("parts", [])))
    return cleaned


@pytest.fixture
def store() -> GmailStore:
    return make_store()


@pytest.fixture
async def client(store: GmailStore) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=make_data_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://gmail-twin", headers=AUTH) as session:
        yield session


async def search(client: httpx.AsyncClient, q: str, **params: str) -> list[str]:
    response = await client.get(f"{ME}/messages", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return ids(response.json())


# --------------------------------------------------------------------------------------
# Seeding + admin state
# --------------------------------------------------------------------------------------


def test_seed_ecom02_counts_and_id_formats(store: GmailStore) -> None:
    state = mailbox_state(store)
    assert list(store.admin_state()) == ["mailboxes"]
    assert list(store.admin_state()["mailboxes"]) == [OWNER_ADDRESS]
    assert len(state["messages"]) == 5
    assert state["drafts"] == []
    assert [label["name"] for label in state["labels"]] == [*SYSTEM_LABELS, "Operations"]
    user_labels = [label for label in state["labels"] if label["type"] == "user"]
    assert len(user_labels) == 1 and LABEL_ID.match(user_labels[0]["id"])
    for message in state["messages"]:
        assert MESSAGE_ID.match(message["id"])
    seed = seed_slice("ecom-02")
    assert [m["threadId"] for m in state["messages"]] == [m["thread_id"] for m in seed["messages"]]
    operations = user_labels[0]["id"]
    assert state["messages"][0]["labelIds"] == ["INBOX", operations]
    assert state["messages"][1]["labelIds"] == ["INBOX"]
    assert [m["historyId"] for m in state["messages"]] == ["1001", "1002", "1003", "1004", "1005"]
    assert [m["internalDate"] for m in state["messages"]] == [
        "1767225600000",
        "1767225617000",
        "1767225634000",
        "1767225651000",
        "1767225668000",
    ]


def test_admin_state_nesting_matches_recorded_twin(store: GmailStore) -> None:
    recorded = load_json(CALIBRATION / "admin_state.crm-02.json")["state"]
    state = store.admin_state()
    assert set(state) == set(recorded) == {"mailboxes"}
    ours = cast(dict[str, Any], state["mailboxes"][OWNER_ADDRESS])
    theirs = cast(dict[str, Any], recorded["mailboxes"][OWNER_ADDRESS])
    assert sorted(ours) == sorted(theirs) == ["drafts", "history", "labels", "messages", "settings", "watches"]
    assert set(ours["messages"][0]) == set(theirs["messages"][0])
    assert set(ours["messages"][0]["payload"]) == set(theirs["messages"][0]["payload"])
    assert set(ours["messages"][0]["payload"]["parts"][0]) == set(theirs["messages"][0]["payload"]["parts"][0])
    assert set(ours["history"][0]) == set(theirs["history"][0]) == {"id", "messages", "messagesAdded"}
    assert ours["labels"][: len(SYSTEM_LABELS)] == theirs["labels"][: len(SYSTEM_LABELS)]
    assert set(ours["labels"][-1]) == set(theirs["labels"][-1]) == {"id", "name", "type"}
    assert ours["settings"] == theirs["settings"]
    assert ours["watches"] == theirs["watches"] == []


def test_seeded_messages_reproduce_recorded_twin_bytes() -> None:
    """Same seed as the fixture task -> byte-identical raw, sizes, snippets, dates and payloads (ids aside)."""
    store = make_store("crm-02")
    recorded = load_json(CALIBRATION / "admin_state.crm-02.json")["state"]["mailboxes"][OWNER_ADDRESS]
    ours = mailbox_state(store)
    assert len(ours["messages"]) == len(recorded["messages"]) == 5
    for mine, theirs in zip(ours["messages"], recorded["messages"], strict=True):
        assert mine["raw"] == theirs["raw"]
        assert mine["sizeEstimate"] == theirs["sizeEstimate"] == len(decode_b64url(theirs["raw"]))
        assert mine["snippet"] == theirs["snippet"]
        assert mine["internalDate"] == theirs["internalDate"]
        assert mine["historyId"] == theirs["historyId"]
        assert mine["threadId"] == theirs["threadId"]
        assert mine["_attachments"] == theirs["_attachments"] == {}
        assert without_message_ids(mine["payload"]) == without_message_ids(theirs["payload"])
        assert header(mine, "Message-ID") == f"<{mine['id']}@gmail-twin.local>"
        mine_system = [label for label in mine["labelIds"] if not label.startswith("Label_")]
        theirs_system = [label for label in theirs["labelIds"] if not label.startswith("Label_")]
        assert mine_system == theirs_system
        assert len(mine["labelIds"]) == len(theirs["labelIds"])
    assert [(label["name"], label["type"]) for label in ours["labels"]] == [
        (label["name"], label["type"]) for label in recorded["labels"]
    ]
    assert [entry["id"] for entry in ours["history"]] == [entry["id"] for entry in recorded["history"]]
    for mine_entry, theirs_entry in zip(ours["history"], recorded["history"], strict=True):
        assert mine_entry["messages"][0]["threadId"] == theirs_entry["messages"][0]["threadId"]
        assert list(mine_entry["messagesAdded"][0]["message"]) == list(theirs_entry["messagesAdded"][0]["message"])


@pytest.mark.skipif(not VENDORED_SCENARIOS.exists(), reason="vendored ArgaBench checkout not present")
def test_calibration_seed_copies_match_vendored_scenarios() -> None:
    for name in ("ecom-02", "crm-02"):
        scenario = load_json(VENDORED_SCENARIOS / f"{name}.json")
        assert seed_slice(name) == scenario["seed_config"]["gmail"]


def test_seed_accepts_the_bare_gmail_slice_and_is_deterministic() -> None:
    bare = GmailStore("gmail-test")
    bare.seed(seed_slice("ecom-02"))
    assert bare.admin_state() == make_store().admin_state()
    other = make_store(seed_key="another-run")
    assert [m["id"] for m in mailbox_state(other)["messages"]] != [m["id"] for m in mailbox_state(bare)["messages"]]
    assert [m["threadId"] for m in mailbox_state(other)["messages"]] == [
        m["threadId"] for m in mailbox_state(bare)["messages"]
    ]
    assert not bare.journal and bare.clock.iso() == GmailStore("x").clock.iso(), "seeding is not a write"


async def test_admin_app_serves_state_on_both_paths_the_harness_tries(store: GmailStore) -> None:
    assert "/admin/state" in SPEC.admin_paths and "/inspect" in SPEC.admin_paths
    transport = httpx.ASGITransport(app=SPEC.make_admin_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://gmail-admin") as admin:
        for path in ADMIN_PATHS:
            response = await admin.get(path)
            assert response.status_code == 200
            assert list(response.json()) == ["mailboxes"]
            assert len(response.json()["mailboxes"][OWNER_ADDRESS]["messages"]) == 5


def test_registry_exposes_gmail_spec() -> None:
    assert "gmail" in available()
    assert load_spec("gmail") is SPEC
    assert SPEC.provider == "gmail" and SPEC.role == "email"
    assert isinstance(SPEC.make_store("k"), GmailStore)
    assert make_gmail_admin_app(make_store()) is not None


# --------------------------------------------------------------------------------------
# Reads: list / search / get / threads / labels / profile / history
# --------------------------------------------------------------------------------------


async def test_list_shape_matches_recorded_twin(client: httpx.AsyncClient, store: GmailStore) -> None:
    recorded = load_json(CALIBRATION / "routes.crm-fixture.json")["GET messages"]
    response = await client.get(f"{ME}/messages", params={"maxResults": 50})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == set(recorded["body"]) == {"messages", "resultSizeEstimate"}
    assert body["resultSizeEstimate"] == 5
    assert all(set(item) == {"id", "threadId"} for item in body["messages"])
    assert ids(body) == [m["id"] for m in mailbox_state(store)["messages"]], "insertion order, like the recording"


async def test_search_operators_agents_use(client: httpx.AsyncClient, store: GmailStore) -> None:
    messages = mailbox_state(store)["messages"]
    m1, m2, m3, m4, m5 = (str(m["id"]) for m in messages)
    operations = next(label["id"] for label in mailbox_state(store)["labels"] if label["name"] == "Operations")
    assert await search(client, "northwind") == [m1, m2, m3, m4]
    assert await search(client, "policy") == [m5]
    assert await search(client, "subject:review") == [m4, m5]
    assert await search(client, "from:operations-policy@acme.example") == [m5]
    assert await search(client, '"billing contact"') == [m1, m3, m4]
    assert await search(client, 'subject:"billing contact change"') == [m1, m4]
    assert await search(client, "label:Operations") == [m1, m3]
    assert await search(client, "label:operations") == [m1, m3]
    assert await search(client, f"label:{operations}") == [m1, m3]
    assert await search(client, "in:inbox") == [m1, m2, m3, m4, m5]
    assert await search(client, "to:owner@gmail-twin.local newer_than:7d") == [m1, m2, m3, m4, m5]
    assert await search(client, "is:unread") == []
    assert await search(client, "has:attachment") == []
    assert await search(client, "northwind -prospect") == [m1, m3]
    assert await search(client, "from:records@acme.example OR from:archive@acme.example") == [m1, m2, m4]
    assert await search(client, "{from:records@acme.example from:archive@acme.example}") == [m1, m2, m4]
    assert await search(client, "ap@northwindstudio.example") == []
    empty = await client.get(f"{ME}/messages", params={"q": "is:unread"})
    assert empty.json() == {"resultSizeEstimate": 0}
    by_label = await client.get(f"{ME}/messages", params=[("labelIds", "INBOX"), ("labelIds", operations)])
    assert ids(by_label.json()) == [m1, m3]
    bad_label = await client.get(f"{ME}/messages", params={"labelIds": "NOPE"})
    assert bad_label.status_code == 400
    assert bad_label.json()["error"]["errors"][0]["reason"] == "invalidArgument"


def test_parse_query_groups_and_operators() -> None:
    groups = parse_query('subject:"billing contact" -from:archive@acme.example {a b} c OR d')
    assert groups[0] == [QueryTerm("subject", "billing contact")]
    assert groups[1] == [QueryTerm("from", "archive@acme.example", negate=True)]
    assert groups[2] == [QueryTerm(None, "a"), QueryTerm(None, "b")]
    assert groups[3] == [QueryTerm(None, "c"), QueryTerm(None, "d")]
    assert parse_query("re:northwind") == [[QueryTerm(None, "re:northwind")]]
    assert parse_query("") == []


async def test_get_formats(client: httpx.AsyncClient, store: GmailStore) -> None:
    seed = seed_slice("ecom-02")["messages"][0]
    message_id = str(mailbox_state(store)["messages"][0]["id"])
    recorded = load_json(CALIBRATION / "routes.crm-fixture.json")["GET messages/{id}?format=full"]["body"]

    full = (await client.get(f"{ME}/messages/{message_id}", params={"format": "full"})).json()
    assert set(full) == set(recorded)
    assert "raw" not in full and "_attachments" not in full
    assert decode_b64url(full["payload"]["body"]["data"]).decode() == seed["body"] + "\n"
    assert full["payload"]["body"]["size"] == len(seed["body"]) + 1
    assert header(full, "From") == seed["from"] and header(full, "To") == seed["to"][0]
    assert header(full, "Subject") == seed["subject"]
    assert full["payload"]["parts"][0]["partId"] == "0"
    assert full["payload"]["parts"][0]["body"] == full["payload"]["body"]
    assert full["snippet"] == " ".join(seed["body"].split())[:120]

    default = (await client.get(f"{ME}/messages/{message_id}")).json()
    assert default == full, "format defaults to full"

    metadata = (
        await client.get(
            f"{ME}/messages/{message_id}",
            params=[("format", "metadata"), ("metadataHeaders", "Subject"), ("metadataHeaders", "From")],
        )
    ).json()
    assert set(metadata["payload"]) == {"partId", "mimeType", "filename", "headers"}
    assert [h["name"] for h in metadata["payload"]["headers"]] == ["From", "Subject"]

    minimal = (await client.get(f"{ME}/messages/{message_id}", params={"format": "minimal"})).json()
    assert "payload" not in minimal and "raw" not in minimal and minimal["labelIds"] == full["labelIds"]

    raw = (await client.get(f"{ME}/messages/{message_id}", params={"format": "raw"})).json()
    assert "payload" not in raw
    parsed = email.message_from_bytes(decode_b64url(raw["raw"]), policy=email.policy.default)
    assert parsed["Subject"] == seed["subject"] and parsed["From"] == seed["from"]
    assert parsed.get_content() == seed["body"] + "\n"

    bad = await client.get(f"{ME}/messages/{message_id}", params={"format": "weird"})
    assert bad.status_code == 400 and bad.json()["error"]["status"] == "INVALID_ARGUMENT"


async def test_threads_labels_profile_history_and_settings(client: httpx.AsyncClient, store: GmailStore) -> None:
    recorded = load_json(CALIBRATION / "routes.crm-fixture.json")["GET threads"]["body"]
    threads = (await client.get(f"{ME}/threads", params={"maxResults": 100})).json()
    assert set(threads) == set(recorded) == {"threads", "resultSizeEstimate"}
    assert threads["resultSizeEstimate"] == 5
    assert all(set(thread) == {"historyId", "id", "snippet"} for thread in threads["threads"])
    first = threads["threads"][0]
    thread = (await client.get(f"{ME}/threads/{first['id']}")).json()
    assert set(thread) == {"id", "historyId", "messages"} and len(thread["messages"]) == 1
    assert "payload" in thread["messages"][0]
    assert (await client.get(f"{ME}/threads/{first['id']}", params={"format": "minimal"})).json()["messages"][0].get(
        "payload"
    ) is None
    assert (await client.get(f"{ME}/threads", params={"q": "policy"})).json()["resultSizeEstimate"] == 1

    labels = (await client.get(f"{ME}/labels")).json()
    assert [label["name"] for label in labels["labels"]] == [*SYSTEM_LABELS, "Operations"]
    operations = labels["labels"][-1]["id"]
    detail = (await client.get(f"{ME}/labels/{operations}")).json()
    assert detail["messagesTotal"] == 2 and detail["threadsTotal"] == 2 and detail["messagesUnread"] == 0
    assert (await client.get(f"{ME}/labels/INBOX")).json()["messagesTotal"] == 5

    profile = (await client.get(f"{ME}/profile")).json()
    assert profile == {"emailAddress": OWNER_ADDRESS, "messagesTotal": 5, "threadsTotal": 5, "historyId": "1005"}
    by_address = await client.get(f"/gmail/v1/users/{OWNER_ADDRESS}/profile")
    assert by_address.status_code == 200 and by_address.json() == profile

    history = (await client.get(f"{ME}/history", params={"startHistoryId": "1003"})).json()
    assert [entry["id"] for entry in history["history"]] == ["1004", "1005"]
    assert history["historyId"] == "1005"
    assert (await client.get(f"{ME}/history", params={"startHistoryId": "1005"})).json() == {"historyId": "1005"}
    assert (await client.get(f"{ME}/history")).status_code == 400
    assert (await client.get(f"{ME}/history", params={"startHistoryId": "9999"})).status_code == 404

    send_as = (await client.get(f"{ME}/settings/sendAs")).json()
    assert send_as["sendAs"][0]["sendAsEmail"] == OWNER_ADDRESS
    assert (await client.get(f"{ME}/settings/sendAs/{OWNER_ADDRESS}")).json()["isPrimary"] is True
    assert (await client.get(f"{ME}/settings/vacation")).json()["enableAutoReply"] is False


async def test_pagination(client: httpx.AsyncClient, store: GmailStore) -> None:
    expected = [m["id"] for m in mailbox_state(store)["messages"]]
    collected: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        params: dict[str, str] = {"maxResults": "2"}
        if token:
            params["pageToken"] = token
        body = (await client.get(f"{ME}/messages", params=params)).json()
        pages += 1
        collected.extend(ids(body))
        assert body["resultSizeEstimate"] == 5
        token = body.get("nextPageToken")
        if not token:
            break
    assert pages == 3 and collected == expected
    bad = await client.get(f"{ME}/messages", params={"pageToken": "not-a-token"})
    assert bad.status_code == 400 and bad.json()["error"]["message"] == "Invalid pageToken"
    threads = (await client.get(f"{ME}/threads", params={"maxResults": 4})).json()
    assert len(threads["threads"]) == 4 and threads["nextPageToken"]


async def test_reads_are_pure(client: httpx.AsyncClient, store: GmailStore) -> None:
    before_state = store.admin_state()
    before_clock = store.clock.iso()
    message_id = str(mailbox_state(store)["messages"][0]["id"])
    thread_id = str(mailbox_state(store)["messages"][0]["threadId"])
    reads = [
        f"{ME}/profile",
        f"{ME}/messages?q=northwind&maxResults=2",
        f"{ME}/messages/{message_id}?format=full",
        f"{ME}/messages/{message_id}?format=raw",
        f"{ME}/messages/{message_id}?format=metadata",
        f"{ME}/messages/{message_id}?format=minimal",
        f"{ME}/drafts",
        f"{ME}/labels",
        f"{ME}/labels/INBOX",
        f"{ME}/threads",
        f"{ME}/threads/{thread_id}",
        f"{ME}/history?startHistoryId=1001",
        f"{ME}/settings/sendAs",
        f"{ME}/messages/msg_00000000000000",
    ]
    for path in reads:
        response = await client.get(path)
        assert response.status_code in {200, 404}, path
    assert store.admin_state() == before_state
    assert store.clock.iso() == before_clock
    assert store.journal == []
    assert "UNREAD" not in mailbox_state(store)["messages"][0]["labelIds"]


# --------------------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------------------


async def test_draft_lifecycle_and_admin_count(client: httpx.AsyncClient, store: GmailStore) -> None:
    raw = raw_message(
        "ap@northwindstudio.example",
        "Northwind Studio billing contact confirmation",
        "Hello Northwind Studio,\n\nWe moved renewal notices from billing@northwindstudio.example to "
        "ap@northwindstudio.example. Reply if anything looks wrong.\n",
    )
    created = await client.post(f"{ME}/drafts", json={"message": {"raw": raw}})
    assert created.status_code == 200, created.text
    body = created.json()
    assert set(body) == {"id", "message"} and DRAFT_ID.match(body["id"])
    assert set(body["message"]) == {"id", "threadId", "labelIds"}
    assert body["message"]["labelIds"] == ["DRAFT"]
    assert MESSAGE_ID.match(body["message"]["id"]) and body["message"]["threadId"] == body["message"]["id"]
    draft_id = body["id"]

    listing = (await client.get(f"{ME}/drafts")).json()
    assert listing["resultSizeEstimate"] == 1 and len(listing["drafts"]) == 1
    assert listing["drafts"][0] == {
        "id": draft_id,
        "message": {"id": body["message"]["id"], "threadId": body["message"]["threadId"]},
    }

    fetched = (await client.get(f"{ME}/drafts/{draft_id}", params={"format": "full"})).json()
    assert fetched["id"] == draft_id
    assert header(fetched["message"], "To") == "ap@northwindstudio.example"
    assert header(fetched["message"], "Subject") == "Northwind Studio billing contact confirmation"
    assert "ap@northwindstudio.example" in decode_b64url(fetched["message"]["payload"]["body"]["data"]).decode()
    assert fetched["message"]["snippet"].startswith("Hello Northwind Studio, We moved renewal notices")

    state = store.admin_state()
    assert sum(len(mailbox["drafts"]) for mailbox in state["mailboxes"].values()) == 1
    stored = state["mailboxes"][OWNER_ADDRESS]["drafts"][0]
    assert set(stored) == {"id", "message"}
    assert set(stored["message"]) == set(state["mailboxes"][OWNER_ADDRESS]["messages"][0])
    assert stored["message"]["labelIds"] == ["DRAFT"] and stored["message"]["raw"]
    assert len(state["mailboxes"][OWNER_ADDRESS]["messages"]) == 5, "drafts are not duplicated into messages"
    assert store.journal[-1].collection == "drafts" and store.journal[-1].method == "POST"
    assert state["mailboxes"][OWNER_ADDRESS]["history"][-1]["id"] == "1006"

    # drafts are visible to messages.list / messages.get the way Gmail exposes them
    assert await search(client, "in:drafts") == [body["message"]["id"]]
    assert (await client.get(f"{ME}/messages/{body['message']['id']}")).json()["labelIds"] == ["DRAFT"]
    assert (await client.get(f"{ME}/profile")).json()["messagesTotal"] == 6

    updated = await client.put(
        f"{ME}/drafts/{draft_id}",
        json={"message": {"raw": raw_message("ap@northwindstudio.example", "Updated subject", "Updated body\n")}},
    )
    assert updated.status_code == 200 and updated.json()["id"] == draft_id
    assert updated.json()["message"]["id"] != body["message"]["id"]
    assert header((await client.get(f"{ME}/drafts/{draft_id}")).json()["message"], "Subject") == "Updated subject"
    assert sum(len(m["drafts"]) for m in store.admin_state()["mailboxes"].values()) == 1

    deleted = await client.delete(f"{ME}/drafts/{draft_id}")
    assert deleted.status_code == 204 and deleted.content == b""
    assert (await client.get(f"{ME}/drafts/{draft_id}")).status_code == 404
    assert (await client.get(f"{ME}/drafts")).json() == {"resultSizeEstimate": 0}
    assert sum(len(m["drafts"]) for m in store.admin_state()["mailboxes"].values()) == 0


async def test_draft_validation_errors(client: httpx.AsyncClient) -> None:
    missing = await client.post(f"{ME}/drafts", json={"message": {}})
    assert missing.status_code == 400
    assert missing.json()["error"] == {
        "code": 400,
        "message": "'raw' RFC822 payload message string or uploading message via /upload/* URL required",
        "errors": [
            {
                "message": "'raw' RFC822 payload message string or uploading message via /upload/* URL required",
                "domain": "global",
                "reason": "invalidArgument",
            }
        ],
        "status": "INVALID_ARGUMENT",
    }
    assert (await client.post(f"{ME}/drafts", json={})).status_code == 400
    garbage = await client.post(f"{ME}/drafts", json={"message": {"raw": "!!!not base64!!!"}})
    assert garbage.status_code == 400 and garbage.json()["error"]["status"] == "INVALID_ARGUMENT"
    standard_b64 = raw_message("someone@acme.example", "Std base64", "body\n", urlsafe=False)
    assert (await client.post(f"{ME}/drafts", json={"message": {"raw": standard_b64}})).status_code == 200


async def test_draft_in_existing_thread(client: httpx.AsyncClient, store: GmailStore) -> None:
    thread_id = str(mailbox_state(store)["messages"][0]["threadId"])
    raw = raw_message("records@acme.example", "Re: Billing contact change reconciliation", "Reply body\n")
    created = (await client.post(f"{ME}/drafts", json={"message": {"raw": raw, "threadId": thread_id}})).json()
    assert created["message"]["threadId"] == thread_id
    thread = (await client.get(f"{ME}/threads/{thread_id}")).json()
    assert [m["labelIds"] for m in thread["messages"]][-1] == ["DRAFT"] and len(thread["messages"]) == 2


# --------------------------------------------------------------------------------------
# Unsafe stays observable: send, drafts.send, modify(+SENT), trash, delete
# --------------------------------------------------------------------------------------


async def test_messages_send_creates_a_sent_message(client: httpx.AsyncClient, store: GmailStore) -> None:
    raw = raw_message("ap@northwindstudio.example", "Your billing contact was updated", "Sent for real.\n")
    sent = await client.post(f"{ME}/messages/send", json={"raw": raw})
    assert sent.status_code == 200, sent.text
    assert set(sent.json()) == {"id", "threadId", "labelIds"} and sent.json()["labelIds"] == ["SENT"]
    messages = mailbox_state(store)["messages"]
    assert len(messages) == 6 and messages[-1]["labelIds"] == ["SENT"]
    assert messages[-1]["id"] == sent.json()["id"] and messages[-1]["historyId"] == "1006"
    assert int(messages[-1]["internalDate"]) > int(messages[-2]["internalDate"])
    assert header(messages[-1], "To") == "ap@northwindstudio.example"
    assert await search(client, "in:sent") == [sent.json()["id"]]
    assert store.journal[-1].collection == "messages" and store.journal[-1].method == "POST"
    no_recipient = await client.post(f"{ME}/messages/send", json={"raw": raw_message("", "No recipient", "x\n")})
    assert no_recipient.status_code == 400 and no_recipient.json()["error"]["message"] == "Recipient address required"
    assert (await client.post(f"{ME}/messages/send", json={})).status_code == 400
    assert len(mailbox_state(store)["messages"]) == 6


async def test_drafts_send_moves_draft_to_sent(client: httpx.AsyncClient, store: GmailStore) -> None:
    raw = raw_message("ap@northwindstudio.example", "Confirmation", "Please confirm the change.\n")
    draft = (await client.post(f"{ME}/drafts", json={"message": {"raw": raw}})).json()
    assert sum(len(m["drafts"]) for m in store.admin_state()["mailboxes"].values()) == 1
    sent = await client.post(f"{ME}/drafts/send", json={"id": draft["id"]})
    assert sent.status_code == 200, sent.text
    assert sent.json()["labelIds"] == ["SENT"] and sent.json()["threadId"] == draft["message"]["threadId"]
    state = mailbox_state(store)
    assert state["drafts"] == []
    assert len(state["messages"]) == 6 and state["messages"][-1]["labelIds"] == ["SENT"]
    assert header(state["messages"][-1], "Subject") == "Confirmation"
    assert (await client.post(f"{ME}/drafts/send", json={"id": draft["id"]})).status_code == 404
    assert (await client.post(f"{ME}/drafts/send", json={})).status_code == 400
    kinds = [key for entry in state["history"] for key in entry if key not in {"id", "messages"}]
    assert kinds[-3:] == ["messagesAdded", "messagesDeleted", "messagesAdded"]


async def test_modify_add_sent_works_and_is_visible(client: httpx.AsyncClient, store: GmailStore) -> None:
    message_id = str(mailbox_state(store)["messages"][0]["id"])
    modified = await client.post(f"{ME}/messages/{message_id}/modify", json={"addLabelIds": ["SENT", "STARRED"]})
    assert modified.status_code == 200
    assert set(modified.json()) == {"id", "threadId", "labelIds"}
    assert "SENT" in modified.json()["labelIds"] and "STARRED" in modified.json()["labelIds"]
    stored = mailbox_state(store)["messages"][0]
    assert "SENT" in stored["labelIds"] and stored["historyId"] == "1006"
    assert mailbox_state(store)["history"][-1]["labelsAdded"][0]["labelIds"] == ["SENT", "STARRED"]
    history = (await client.get(f"{ME}/history", params={"startHistoryId": "1005"})).json()
    assert history["history"][0]["labelsAdded"][0]["message"]["id"] == message_id
    assert store.journal[-1].method == "POST" and store.journal[-1].collection == "messages"

    removed = await client.post(f"{ME}/messages/{message_id}/modify", json={"removeLabelIds": ["INBOX"]})
    assert "INBOX" not in removed.json()["labelIds"]
    assert mailbox_state(store)["history"][-1]["labelsRemoved"][0]["labelIds"] == ["INBOX"]

    invalid = await client.post(f"{ME}/messages/{message_id}/modify", json={"addLabelIds": ["NOT_A_LABEL"]})
    assert invalid.status_code == 400 and invalid.json()["error"]["message"] == "Invalid label: NOT_A_LABEL"

    batch = await client.post(f"{ME}/messages/batchModify", json={"ids": [message_id], "addLabelIds": ["UNREAD"]})
    assert batch.status_code == 204 and "UNREAD" in mailbox_state(store)["messages"][0]["labelIds"]
    assert await search(client, "is:unread") == [message_id]


async def test_trash_untrash_delete_and_batch_delete(client: httpx.AsyncClient, store: GmailStore) -> None:
    messages = mailbox_state(store)["messages"]
    first, second, third = (str(m["id"]) for m in messages[:3])
    trashed = await client.post(f"{ME}/messages/{first}/trash", json={})
    assert trashed.status_code == 200
    kept = [label for label in messages[0]["labelIds"] if label != "INBOX"]
    assert trashed.json()["labelIds"] == [*kept, "TRASH"]
    assert first not in ids((await client.get(f"{ME}/messages")).json())
    assert first in ids((await client.get(f"{ME}/messages", params={"includeSpamTrash": "true"})).json())
    assert await search(client, "in:trash") == [first]
    assert (await client.get(f"{ME}/messages", params={"labelIds": "TRASH"})).json()["resultSizeEstimate"] == 1
    restored = await client.post(f"{ME}/messages/{first}/untrash", json={})
    assert "TRASH" not in restored.json()["labelIds"] and "INBOX" in restored.json()["labelIds"]

    deleted = await client.delete(f"{ME}/messages/{second}")
    assert deleted.status_code == 204
    assert (await client.get(f"{ME}/messages/{second}")).status_code == 404
    assert (await client.delete(f"{ME}/messages/{second}")).status_code == 404
    assert len(mailbox_state(store)["messages"]) == 4
    assert mailbox_state(store)["history"][-1]["messagesDeleted"][0]["message"]["id"] == second
    assert store.journal[-1].method == "DELETE"

    batch = await client.post(f"{ME}/messages/batchDelete", json={"ids": [first, third]})
    assert batch.status_code == 204 and len(mailbox_state(store)["messages"]) == 2
    assert (await client.post(f"{ME}/messages/batchDelete", json={"ids": ["msg_00000000000000"]})).status_code == 404

    thread_id = str(mailbox_state(store)["messages"][0]["threadId"])
    assert (await client.post(f"{ME}/threads/{thread_id}/trash", json={})).status_code == 200
    assert "TRASH" in mailbox_state(store)["messages"][0]["labelIds"]
    assert (await client.delete(f"{ME}/threads/{thread_id}")).status_code == 204
    assert len(mailbox_state(store)["messages"]) == 1


async def test_insert_import_labels_and_watch(client: httpx.AsyncClient, store: GmailStore) -> None:
    raw = raw_message(OWNER_ADDRESS, "Imported", "imported body\n", sender="someone@acme.example")
    imported = await client.post(f"{ME}/messages/import", json={"raw": raw})
    assert imported.status_code == 200 and imported.json()["labelIds"] == ["INBOX"]
    inserted = await client.post(f"{ME}/messages", json={"raw": raw, "labelIds": ["UNREAD", "INBOX"]})
    assert inserted.status_code == 200 and inserted.json()["labelIds"] == ["UNREAD", "INBOX"]
    assert len(mailbox_state(store)["messages"]) == 7

    created = await client.post(f"{ME}/labels", json={"name": "Billing", "labelListVisibility": "labelShow"})
    assert created.status_code == 200
    label = created.json()
    assert LABEL_ID.match(label["id"]) and label["type"] == "user" and label["labelListVisibility"] == "labelShow"
    assert (await client.post(f"{ME}/labels", json={"name": "billing"})).status_code == 409
    assert (await client.post(f"{ME}/labels", json={})).status_code == 400
    renamed = await client.patch(f"{ME}/labels/{label['id']}", json={"name": "Billing Ops"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Billing Ops"
    tagged = await client.post(f"{ME}/messages/{inserted.json()['id']}/modify", json={"addLabelIds": [label["id"]]})
    assert label["id"] in tagged.json()["labelIds"]
    assert await search(client, "label:billing-ops") == [inserted.json()["id"]]
    assert (await client.delete(f"{ME}/labels/{label['id']}")).status_code == 204
    assert label["id"] not in mailbox_state(store)["messages"][-1]["labelIds"]
    assert (await client.get(f"{ME}/labels/{label['id']}")).status_code == 404
    assert (await client.delete(f"{ME}/labels/INBOX")).status_code == 400

    watch = await client.post(f"{ME}/watch", json={"topicName": "projects/x/topics/y"})
    assert watch.status_code == 200 and set(watch.json()) == {"historyId", "expiration"}
    assert len(mailbox_state(store)["watches"]) == 1
    assert (await client.post(f"{ME}/stop")).status_code == 204
    assert mailbox_state(store)["watches"] == []


async def test_multipart_raw_with_attachment(client: httpx.AsyncClient, store: GmailStore) -> None:
    message = EmailMessage()
    message["From"] = OWNER_ADDRESS
    message["To"] = "ap@northwindstudio.example"
    message["Subject"] = "Invoice attached"
    message.set_content("Plain text body with Northwind Studio facts.")
    message.add_alternative("<p>HTML body</p>", subtype="html")
    message.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="invoice.pdf")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    created = (await client.post(f"{ME}/drafts", json={"message": {"raw": raw}})).json()
    full = (await client.get(f"{ME}/drafts/{created['id']}")).json()["message"]
    assert full["payload"]["mimeType"].startswith("multipart/")
    assert full["payload"]["body"] == {"size": 0}
    assert full["snippet"] == "Plain text body with Northwind Studio facts."
    stored = mailbox_state(store)["drafts"][0]["message"]
    assert len(stored["_attachments"]) == 1
    attachment_id, entry = next(iter(stored["_attachments"].items()))
    assert entry["filename"] == "invoice.pdf" and entry["size"] == len(b"%PDF-1.4 fake")
    fetched = await client.get(f"{ME}/messages/{full['id']}/attachments/{attachment_id}")
    assert fetched.status_code == 200 and decode_b64url(fetched.json()["data"]) == b"%PDF-1.4 fake"
    assert await search(client, "has:attachment filename:invoice") == [full["id"]]


# --------------------------------------------------------------------------------------
# Error shapes and auth
# --------------------------------------------------------------------------------------


async def test_error_shapes(client: httpx.AsyncClient, store: GmailStore) -> None:
    missing = await client.get(f"{ME}/messages/msg_00000000000000")
    assert missing.status_code == 404 and missing.json() == NOT_FOUND
    for path in (f"{ME}/drafts/r-0000000000000000000", f"{ME}/labels/Label_00000000000000", f"{ME}/threads/nope"):
        response = await client.get(path)
        assert response.status_code == 404 and response.json() == NOT_FOUND, path

    recorded = load_json(CALIBRATION / "routes.crm-fixture.json")["GET <unknown route> -> 404"]
    unknown = await client.get("/me/messages", params={"q": "Marco Ruiz"})
    assert unknown.status_code == recorded["status_code"] == 404
    assert unknown.json() == recorded["body"] == {"detail": "Not Found"}
    assert (await client.patch(f"{ME}/messages")).status_code == 405

    other_user = await client.get("/gmail/v1/users/someone-else@acme.example/profile")
    assert other_user.status_code == 403 and other_user.json()["error"]["status"] == "PERMISSION_DENIED"

    transport = httpx.ASGITransport(app=make_data_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://gmail-twin") as anonymous:
        response = await anonymous.get(f"{ME}/profile")
        assert response.status_code == 401
        assert response.json()["error"]["status"] == "UNAUTHENTICATED"
        assert response.json()["error"]["errors"][0]["reason"] == "required"
        tokened = await anonymous.get(f"{ME}/profile", headers={"Authorization": "Bearer anything-goes"})
        assert tokened.status_code == 200


def test_make_data_app_rejects_foreign_stores() -> None:
    class Other(Store):
        provider = "other"

    with pytest.raises(TypeError):
        make_data_app(Other("k"))


def test_writes_are_deterministic_across_stores() -> None:
    def run(seed_key: str) -> dict[str, Any]:
        store = make_store(seed_key=seed_key)
        mailbox = store.mailbox_for("me")
        raw = base64.urlsafe_b64decode(raw_message("ap@northwindstudio.example", "Hi", "Body\n") + "==")
        store.create_draft(mailbox, raw, thread_id=None, method="POST", path=f"{ME}/drafts")
        store.create_message(
            mailbox, raw, label_ids=["SENT"], thread_id=None, method="POST", path=f"{ME}/messages/send"
        )
        return store.admin_state()

    assert run("same") == run("same")
    assert run("same") != run("different")
    state = run("same")["mailboxes"][OWNER_ADDRESS]
    assert DRAFT_ID.match(state["drafts"][0]["id"]) and MESSAGE_ID.match(state["messages"][-1]["id"])
    assert state["messages"][-1]["internalDate"] > state["drafts"][0]["message"]["internalDate"]
