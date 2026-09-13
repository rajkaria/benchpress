"""Generate test/fixtures/parity.json from the Python guard, the reference implementation.

Run from the repo root: `uv run python packages/benchpress-guard/scripts/gen_parity.py`.
The TypeScript tests assert that `benchpress-guard` reproduces every decision, reason, refusal string,
class, argument digest and receipt line recorded here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from benchpress.shims.guard_policy import GuardPolicy, PolicyGuard, arguments_digest
from benchpress.shims.openai_agents import AgentsToolGuard, classify_tool_name, refusal_message

OUT = Path(__file__).resolve().parent.parent / "test" / "fixtures" / "parity.json"
FIXED_TS = "2026-09-13T21:04:05.123000+00:00"
FIXED_LATENCY = 1.25

POLICIES: list[dict[str, Any]] = [
    {
        "name": "allow rule with argument regex and max_calls",
        "policy": {"rules": [{"tool": "update_invoice", "arguments": {"status": "sent|paid"}, "max_calls": 2}]},
        "calls": [
            ["get_invoice", {"invoice_id": "INV-7"}],
            ["update_invoice", {"invoice_id": "INV-7", "status": "paid"}],
            ["update_invoice", {"invoice_id": "INV-7", "status": "draft"}],
            ["update_invoice", {"invoice_id": "INV-7"}],
            ["update_invoice", {"invoice_id": "INV-8", "status": "sent"}],
            ["update_invoice", {"invoice_id": "INV-9", "status": "sent"}],
            ["delete_invoice", {"invoice_id": "INV-7"}],
            ["update_invoice", [1, 2]],
            ["update_invoice", "not an object"],
            ["update_invoice", None],
            ["send_reminder", {}],
        ],
    },
    {
        "name": "deny glob, catch-all allow, destructive opt-in, custom reasons, declared classes",
        "policy": {
            "classes": {"sync_everything": "write", "get_and_email_report": "write"},
            "rules": [
                {"tool": "admin_*", "effect": "deny", "reason": "admin tools are off limits for agents"},
                {"tool": "close_duplicate", "allow_destructive": True, "max_calls": 1, "reason": "dedupe is fine"},
                {"tool": "*"},
            ],
        },
        "calls": [
            ["admin_reset_password", {"user": "u1"}],
            ["close_duplicate", {"ticket_id": "T-1"}],
            ["close_duplicate", {"ticket_id": "T-2"}],
            ["delete_ticket", {"ticket_id": "T-3"}],
            ["sync_everything", {}],
            ["get_and_email_report", {"to": "ops"}],
            ["getReport", {"id": 3}],
            ["removeUser", {"id": 4}],
        ],
    },
    {
        "name": "reads denied, numeric and structured argument rendering, quotes in names",
        "policy": {
            "reads": "deny",
            "rules": [
                {"tool": "list_*", "arguments": {"limit": "\\d{1,3}"}},
                {"tool": "tag_record", "arguments": {"meta": '\\{"a": 1, "b": \\[true, null\\]\\}', "flag": "true"}},
                {"tool": "set_ratio", "arguments": {"ratio": "0\\.\\d+|1e-05"}},
            ],
        },
        "calls": [
            ["list_items", {"limit": 50}],
            ["list_items", {"limit": "7"}],
            ["list_items", {"limit": 5000}],
            ["get_item", {"id": "x"}],
            ["tag_record", {"meta": {"b": [True, None], "a": 1}, "flag": True}],
            ["tag_record", {"meta": {"a": 1}, "flag": True}],
            ["set_ratio", {"ratio": 0.25}],
            ["set_ratio", {"ratio": 0.00001}],
            ["set_ratio", {"ratio": 2.5}],
            ["don't_panic", {}],
            ['say_"hi"_don\'t', {}],
            ["back\\slash_tool", {}],
        ],
    },
    {
        "name": "python regex syntax, glob sets, max_calls zero, first mismatch wins",
        "policy": {
            "rules": [
                {"tool": "update_ticket", "arguments": {"ticket_id": "T-\\d+", "status": "pending|resolved"}},
                {"tool": "update_ticket", "arguments": {"status": "(?P<s>open)-(?P=s)"}},
                {"tool": "[!x]end_?mail", "max_calls": 3},
                {"tool": "notify_[abc]", "max_calls": 0},
                {"tool": "rename_*", "arguments": {"name": "\\A[A-Z][a-z]+\\Z"}},
            ],
        },
        "calls": [
            ["update_ticket", {"ticket_id": "T-12", "status": "resolved"}],
            ["update_ticket", {"ticket_id": "X-12", "status": "resolved"}],
            ["update_ticket", {"ticket_id": "X-12", "status": "open-open"}],
            ["update_ticket", {"ticket_id": "T-1", "status": "closed"}],
            ["send_email", {}],
            ["xend_email", {}],
            ["notify_a", {}],
            ["notify_d", {}],
            ["rename_user", {"name": "Ada"}],
            ["rename_user", {"name": "ada"}],
            ["rename_user", {"name": "Ada\n"}],
        ],
    },
]

INVALID_POLICIES: list[Any] = [
    {"rules": [{"tool": ""}]},
    {"rules": [{"tool": "x", "effect": "block"}]},
    {"rules": [{"tool": "x", "arguments": {"a": "("}}]},
    {"rules": [{"tool": "x", "max_calls": -1}]},
    {"rules": [{"tool": "x", "unknown": 1}]},
    {"reads": "maybe"},
    {"classes": {"a": "dangerous"}},
    {"rulez": []},
    {"rules": {"tool": "x"}},
]

CLASSIFY_NAMES = [
    "get_invoice", "getInvoice", "get_and_remove_item", "HTTPGet", "getHTTPResponse", "refundCharge",
    "list-items", "sync_everything", "", "123", "voidInvoice", "avoid_thing", "fetch", "Search_Docs",
    "download_export", "update__record", "create-ticket", "kill9", "view2Details", "Delete", "peekABoo",
]  # fmt: skip

DIGEST_ARGS: list[Any] = [
    {},
    {"b": 1, "a": 2},
    {"name": "Zoë \U0001f680", "city": "München"},
    {"s": "a\nb\tc" + chr(0x01) + 'd"e\\f' + chr(0x7F) + chr(0x2028)},
    {"x": 1.5, "y": 1e-07, "z": 1e16, "w": -2.5, "v": 0.00001, "u": 123456789.125, "t": 0.1},
    {"big": 9007199254740991, "neg": -42, "zero": 0},
    {"\U0001f600": 1, chr(0xFFFF): 2, "a": 3, "B": 4},
    {"nested": {"z": [1, {"y": None, "x": True}], "a": False}, "list": [], "obj": {}},
]


def main() -> None:
    policy_cases: list[dict[str, Any]] = []
    for case in POLICIES:
        guard = AgentsToolGuard(guard=PolicyGuard(GuardPolicy.model_validate(case["policy"])))
        calls: list[dict[str, Any]] = []
        for tool, args in case["calls"]:
            started = time.monotonic()
            decision, arguments = guard.decide(tool, json.dumps(args))
            upstream_error = False if decision.allowed else None
            line = guard.guard.record(tool, arguments, decision, started, upstream_error=upstream_error)
            line["ts"] = FIXED_TS
            line["latency_ms"] = FIXED_LATENCY
            calls.append(
                {
                    "tool": tool,
                    "args": args,
                    "expected": {
                        "allowed": decision.allowed,
                        "class": decision.tool_class,
                        "rule": decision.rule,
                        "reason": decision.reason,
                        "policy_rule": decision.policy_rule,
                        "refusal": None if decision.allowed else refusal_message(tool, decision),
                        "digest": arguments_digest(arguments),
                        "receipt_line": json.dumps(line, ensure_ascii=False, sort_keys=True),
                    },
                }
            )
        policy_cases.append({"name": case["name"], "policy": case["policy"], "calls": calls})

    invalid: list[Any] = []
    for policy in INVALID_POLICIES:
        try:
            GuardPolicy.model_validate(policy)
        except ValidationError:
            invalid.append(policy)
            continue
        raise SystemExit(f"expected the Python model to reject {policy!r}")

    fixture = {
        "generated_by": "packages/benchpress-guard/scripts/gen_parity.py (benchpress.shims.guard_policy)",
        "fixed_ts": FIXED_TS,
        "fixed_latency_ms": FIXED_LATENCY,
        "policies": policy_cases,
        "invalid_policies": invalid,
        "classify": [{"name": name, "class": classify_tool_name(name)} for name in CLASSIFY_NAMES],
        "digests": [{"args": args, "digest": arguments_digest(args)} for args in DIGEST_ARGS],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    count = sum(len(case["calls"]) for case in policy_cases) + len(invalid) + len(CLASSIFY_NAMES) + len(DIGEST_ARGS)
    print(f"wrote {OUT} ({count} cases)")


if __name__ == "__main__":
    main()
