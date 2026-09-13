"""Stripe twin: a local, deterministic stand-in for the ArgaBench Stripe twin.

Fidelity sources, in priority order (see `devsim/calibration/stripe/NOTES.md`):

1. The grader is the spec for the admin-state shape. `argabench_mkt_ecom_legacy.py` counts new
   prices with `set(after) - set(before)` and removed customers with `set(before) - set(after)`,
   so every collection is a **dict keyed by id**. `_record_collection` scans
   `customers/products/prices/subscriptions` for protected tokens and requires whole-record
   equality, so reads never touch a record. `admin_delta_claims._claim_stripe` documents the real
   twin's bookkeeping keys (`idempotency.cached_responses`, `generic_resources` + `counts`).
2. The harness's own Stripe fixtures (`tests/unit/evaluation/test_stripe_outcome_evaluator.py`,
   `benchmark/instances/dev/stripe_price_normalization_*`) for seeded record defaults and the
   bare-term search extension the Arga twin accepts.
3. The official Stripe API reference for every route and error envelope.

Design rules (docs/TRACK-DEVSIM.md §2): deterministic ids, pure reads, real error shapes, unsafe
routes that work (charges, payment intents, refunds, invoice send, subscription cancel, deletes).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any, ClassVar, cast
from urllib.parse import parse_qsl

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from devsim.twins.base import Clock, Store, TwinSpec, decode_form, det_alnum, make_admin_app, parse_body

JsonDict = dict[str, Any]

STRIPE_API_VERSION = "2024-06-20"
_ALL_METHODS = ["GET", "POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"]
_DOC_URL = "https://stripe.com/docs/error-codes/"
_SEED_AGE_SECONDS = 30 * 86400
_LIST_LIMIT_DEFAULT = 10
_LIST_LIMIT_MAX = 100
_ID_TOKEN = re.compile(r"^[a-z]+(?:_[a-z]+)*_[A-Za-z0-9]+$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TAX_ID_TYPE = re.compile(r"^[a-z]{2}_[a-z0-9_]+$")

# Stripe id formats: `cus_`/`prod_`/`si_`/`txi_` carry 14 alphanumerics; the `1`/`3` families carry 24.
_ID_FORMATS: dict[str, tuple[str, int]] = {
    "customers": ("cus_", 14),
    "products": ("prod_", 14),
    "prices": ("price_1", 23),
    "subscriptions": ("sub_1", 23),
    "subscription_items": ("si_", 14),
    "invoices": ("in_1", 23),
    "charges": ("ch_3", 23),
    "payment_intents": ("pi_3", 23),
    "refunds": ("re_3", 23),
    "tax_ids": ("txi_1", 23),
    "meters": ("mtr_test_", 18),
    "events": ("evt_1", 23),
    "accounts": ("acct_1", 18),
}

# The object name Stripe uses in `No such <object>: '<id>'` for each id prefix.
_OBJECT_BY_PREFIX: dict[str, str] = {
    "cus": "customer",
    "prod": "product",
    "price": "price",
    "sub": "subscription",
    "si": "subscription_item",
    "in": "invoice",
    "ch": "charge",
    "pi": "payment_intent",
    "re": "refund",
    "txi": "tax_id",
    "mtr": "meter",
    "evt": "event",
    "pm": "payment_method",
    "card": "card",
    "src": "source",
    "plan": "plan",
    "coupon": "coupon",
    "promo": "promotion_code",
    "prod_": "product",
}

_SEARCHABLE_FIELDS: dict[str, frozenset[str]] = {
    "customers": frozenset({"created", "email", "metadata", "name", "phone"}),
    "products": frozenset({"active", "created", "description", "metadata", "name", "shippable", "url"}),
    "prices": frozenset({"active", "currency", "created", "lookup_key", "metadata", "product", "type"}),
    "subscriptions": frozenset(
        {"canceled_at", "created", "current_period_end", "current_period_start", "ended_at", "metadata", "status"}
    ),
    "invoices": frozenset(
        {"created", "currency", "customer", "metadata", "number", "receipt_number", "status", "subscription", "total"}
    ),
    "charges": frozenset(
        {"amount", "billing_details", "created", "currency", "customer", "disputed", "metadata", "refunded", "status"}
    ),
    "payment_intents": frozenset({"amount", "created", "currency", "customer", "metadata", "status"}),
}
_BARE_TERM_FIELDS: dict[str, tuple[str, ...]] = {
    "customers": ("name", "email", "description", "phone", "id"),
    "products": ("name", "description", "url", "id"),
    "prices": ("nickname", "lookup_key", "id", "product"),
    "subscriptions": ("id", "customer", "status", "description"),
    "invoices": ("id", "customer", "number", "status", "description"),
    "charges": ("id", "customer", "description", "status"),
    "payment_intents": ("id", "customer", "description", "status"),
}

# Accepted request parameters per route (top level). Anything else is Stripe's
# `Received unknown parameter: <name>`; non-updatable fields on update routes are reported the same way.
_P = frozenset
_CUSTOMER_CREATE = _P(
    {
        "address", "balance", "cash_balance", "coupon", "description", "email", "invoice_prefix",
        "invoice_settings", "metadata", "name", "next_invoice_sequence", "payment_method", "phone",
        "preferred_locales", "promotion_code", "shipping", "source", "tax", "tax_exempt", "tax_id_data",
        "test_clock", "validate",
    }
)  # fmt: skip
_CUSTOMER_UPDATE = _P(
    {
        "address", "balance", "cash_balance", "coupon", "default_source", "description", "email",
        "invoice_prefix", "invoice_settings", "metadata", "name", "next_invoice_sequence", "phone",
        "preferred_locales", "promotion_code", "shipping", "source", "tax", "tax_exempt", "validate",
    }
)  # fmt: skip
_PRODUCT_CREATE = _P(
    {
        "active", "default_price_data", "description", "id", "images", "marketing_features", "metadata", "name",
        "package_dimensions", "shippable", "statement_descriptor", "tax_code", "unit_label", "url",
    }
)  # fmt: skip
_PRODUCT_UPDATE = _P(
    {
        "active", "default_price", "description", "images", "marketing_features", "metadata", "name",
        "package_dimensions", "shippable", "statement_descriptor", "tax_code", "unit_label", "url",
    }
)  # fmt: skip
_PRICE_CREATE = _P(
    {
        "active", "billing_scheme", "currency", "currency_options", "custom_unit_amount", "lookup_key", "metadata",
        "nickname", "product", "product_data", "recurring", "tax_behavior", "tiers", "tiers_mode",
        "transfer_lookup_key", "transform_quantity", "unit_amount", "unit_amount_decimal",
    }
)  # fmt: skip
_PRICE_UPDATE = _P(
    {"active", "currency_options", "lookup_key", "metadata", "nickname", "tax_behavior", "transfer_lookup_key"}
)
_SUBSCRIPTION_CREATE = _P(
    {
        "add_invoice_items", "automatic_tax", "backdate_start_date", "billing_cycle_anchor", "billing_thresholds",
        "cancel_at", "cancel_at_period_end", "collection_method", "coupon", "currency", "customer", "days_until_due",
        "default_payment_method", "default_source", "default_tax_rates", "description", "discounts", "items",
        "metadata", "off_session", "on_behalf_of", "payment_behavior", "payment_settings",
        "pending_invoice_item_interval", "promotion_code", "proration_behavior", "transfer_data", "trial_end",
        "trial_from_plan", "trial_period_days", "trial_settings",
    }
)  # fmt: skip
_SUBSCRIPTION_UPDATE = _P(
    {
        "add_invoice_items", "automatic_tax", "billing_cycle_anchor", "billing_thresholds", "cancel_at",
        "cancel_at_period_end", "cancellation_details", "collection_method", "coupon", "days_until_due",
        "default_payment_method", "default_source", "default_tax_rates", "description", "discounts", "items",
        "metadata", "off_session", "on_behalf_of", "pause_collection", "payment_behavior", "payment_settings",
        "pending_invoice_item_interval", "promotion_code", "proration_behavior", "proration_date", "transfer_data",
        "trial_end", "trial_from_plan", "trial_settings",
    }
)  # fmt: skip
_SUBSCRIPTION_CANCEL = _P({"cancellation_details", "invoice_now", "prorate"})
_INVOICE_CREATE = _P(
    {
        "account_tax_ids", "application_fee_amount", "auto_advance", "automatic_tax", "collection_method",
        "currency", "custom_fields", "customer", "days_until_due", "default_payment_method", "default_source",
        "default_tax_rates", "description", "discounts", "due_date", "effective_at", "footer", "from_invoice",
        "issuer", "metadata", "number", "on_behalf_of", "payment_settings", "pending_invoice_items_behavior",
        "rendering", "shipping_cost", "shipping_details", "statement_descriptor", "subscription", "transfer_data",
    }
)  # fmt: skip
_INVOICE_UPDATE = _P(
    {
        "account_tax_ids", "application_fee_amount", "auto_advance", "automatic_tax", "collection_method",
        "custom_fields", "days_until_due", "default_payment_method", "default_source", "default_tax_rates",
        "description", "discounts", "due_date", "effective_at", "footer", "issuer", "metadata", "number",
        "on_behalf_of", "payment_settings", "rendering", "shipping_cost", "shipping_details",
        "statement_descriptor", "transfer_data",
    }
)  # fmt: skip
_CHARGE_CREATE = _P(
    {
        "amount", "application_fee_amount", "capture", "currency", "customer", "description", "destination",
        "metadata", "on_behalf_of", "radar_options", "receipt_email", "shipping", "source", "statement_descriptor",
        "statement_descriptor_suffix", "transfer_data", "transfer_group",
    }
)  # fmt: skip
_CHARGE_UPDATE = _P(
    {"customer", "description", "fraud_details", "metadata", "receipt_email", "shipping", "transfer_group"}
)
_PAYMENT_INTENT_CREATE = _P(
    {
        "amount", "application_fee_amount", "automatic_payment_methods", "capture_method", "confirm",
        "confirmation_method", "currency", "customer", "description", "error_on_requires_action", "mandate",
        "mandate_data", "metadata", "off_session", "on_behalf_of", "payment_method", "payment_method_configuration",
        "payment_method_data", "payment_method_options", "payment_method_types", "radar_options", "receipt_email",
        "return_url", "setup_future_usage", "shipping", "statement_descriptor", "statement_descriptor_suffix",
        "transfer_data", "transfer_group", "use_stripe_sdk",
    }
)  # fmt: skip
_PAYMENT_INTENT_UPDATE = _P(
    {
        "amount", "application_fee_amount", "capture_method", "currency", "customer", "description", "metadata",
        "payment_method", "payment_method_configuration", "payment_method_data", "payment_method_options",
        "payment_method_types", "receipt_email", "setup_future_usage", "shipping", "statement_descriptor",
        "statement_descriptor_suffix", "transfer_data", "transfer_group",
    }
)  # fmt: skip
_PAYMENT_INTENT_CONFIRM = _P(
    {
        "capture_method", "error_on_requires_action", "mandate", "mandate_data", "off_session", "payment_method",
        "payment_method_data", "payment_method_options", "payment_method_types", "radar_options", "receipt_email",
        "return_url", "setup_future_usage", "shipping", "use_stripe_sdk",
    }
)  # fmt: skip
_REFUND_CREATE = _P(
    {
        "amount", "charge", "currency", "customer", "instructions_email", "metadata", "origin", "payment_intent",
        "reason", "refund_application_fee", "reverse_transfer",
    }
)  # fmt: skip
_TAX_ID_CREATE = _P({"type", "value"})
_METER_CREATE = _P(
    {"customer_mapping", "default_aggregation", "display_name", "event_name", "event_time_window", "value_settings"}
)
_METER_UPDATE = _P({"display_name"})
_METER_EVENT_CREATE = _P({"event_name", "identifier", "payload", "timestamp"})
_LIST_COMMON = _P({"created", "ending_before", "limit", "starting_after"})
_SEARCH_PARAMS = _P({"limit", "page", "query"})


# --------------------------------------------------------------------------------------
# Errors (real Stripe envelope: {"error": {type, code, message, param, doc_url, request_log_url}})
# --------------------------------------------------------------------------------------


class StripeError(Exception):
    """Raised by handlers; rendered as Stripe's error envelope with the matching HTTP status."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        error_type: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
        decline_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_type = error_type
        self.code = code
        self.param = param
        self.decline_code = decline_code

    def payload(self, request_id: str) -> JsonDict:
        error: JsonDict = {}
        if self.code:
            error["code"] = self.code
            error["doc_url"] = _DOC_URL + self.code.replace("_", "-")
        if self.decline_code:
            error["decline_code"] = self.decline_code
        error["message"] = self.message
        if self.param is not None:
            error["param"] = self.param
        error["request_log_url"] = f"https://dashboard.stripe.com/test/logs/{request_id}"
        error["type"] = self.error_type
        return {"error": error}


def resource_missing(object_name: str, identifier: str, *, param: str = "id", status: int = 404) -> StripeError:
    """404 when the id is in the URL; 400 when a parameter references a missing object (Stripe behaviour)."""
    return StripeError(status, f"No such {object_name}: '{identifier}'", code="resource_missing", param=param)


def parameter_missing(param: str) -> StripeError:
    return StripeError(400, f"Missing required param: {param}.", code="parameter_missing", param=param)


def parameter_unknown(param: str) -> StripeError:
    return StripeError(400, f"Received unknown parameter: {param}", code="parameter_unknown", param=param)


def unrecognized_url(method: str, path: str) -> StripeError:
    return StripeError(
        404,
        f"Unrecognized request URL ({method}: {path}). If you are trying to list objects, remove the trailing "
        "slash. If you are trying to retrieve an object, make sure you passed a valid (non-empty) identifier in "
        "your code. Please see https://stripe.com/docs or we can help at https://support.stripe.com/.",
    )


# --------------------------------------------------------------------------------------
# Parameter decoding and coercion
# --------------------------------------------------------------------------------------


def index_array_pairs(pairs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Turn repeated `key[]=a&key[]=b` (the gateway's array encoding) into `key[0]`, `key[1]` for `decode_form`."""
    counters: dict[str, int] = {}
    indexed: list[tuple[str, str]] = []
    for key, value in pairs:
        if key.endswith("[]"):
            head = key[:-2]
            position = counters.get(head, 0)
            counters[head] = position + 1
            indexed.append((f"{head}[{position}]", value))
        else:
            indexed.append((key, value))
    return indexed


async def read_params(request: Request) -> JsonDict:
    """Body parameters: form-encoded (Stripe default, bracket notation) or JSON (some agents send it)."""
    body, encoding = await parse_body(request)
    if encoding == "form":
        raw = (await request.body()).decode("utf-8", errors="replace")
        return decode_form(index_array_pairs(parse_qsl(raw, keep_blank_values=True)))
    if encoding == "invalid":
        raise StripeError(400, "Invalid request: the request body could not be parsed as form data or JSON.")
    return body


def read_query(request: Request) -> JsonDict:
    return decode_form(index_array_pairs(list(request.query_params.multi_items())))


def reject_unknown(params: Mapping[str, Any], allowed: frozenset[str]) -> None:
    for key in params:
        if key != "expand" and key not in allowed:
            raise parameter_unknown(key)


def as_int(value: object, param: str) -> int:
    if isinstance(value, bool):
        raise StripeError(400, f"Invalid integer: {value}", code="parameter_invalid_integer", param=param)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    raise StripeError(400, f"Invalid integer: {value}", code="parameter_invalid_integer", param=param)


def as_bool(value: object, param: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise StripeError(400, f"Invalid boolean: {value}", code="parameter_invalid_boolean", param=param)


def as_str(value: object, param: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        raise StripeError(400, f"Invalid string: {value}", code="parameter_invalid_string", param=param)
    if isinstance(value, int | float):
        return str(value)
    raise StripeError(400, f"Invalid string: {value}", code="parameter_invalid_string", param=param)


def as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return cast(list[Any], value)
    if isinstance(value, dict):
        typed = cast(dict[str, Any], value)
        if all(key.isdigit() for key in typed):
            return [typed[key] for key in sorted(typed, key=int)]
        return list(typed.values())
    if value is None:
        return []
    return [value]


def as_dict(value: object, param: str) -> JsonDict:
    if isinstance(value, dict):
        return cast(JsonDict, value)
    if value == "" or value is None:
        return {}
    raise StripeError(400, f"Invalid hash: {value}", code="parameter_invalid_empty", param=param)


def merge_metadata(existing: Mapping[str, Any], incoming: object) -> JsonDict:
    """Stripe semantics: keys merge; an empty-string or null value unsets a key; `metadata=""` clears all."""
    if incoming == "" or incoming is None:
        return {}
    if not isinstance(incoming, dict):
        raise StripeError(400, f"Invalid hash: {incoming}", code="parameter_invalid_empty", param="metadata")
    merged: JsonDict = dict(existing)
    for key, value in cast(dict[str, Any], incoming).items():
        if value is None or value == "":
            merged.pop(str(key), None)
        else:
            merged[str(key)] = str(value)
    return merged


def expand_paths(params: Mapping[str, Any]) -> list[str]:
    return [str(item) for item in as_list(params.get("expand"))]


def _copy(value: object) -> Any:
    return json.loads(json.dumps(value, default=str))


def _fingerprint(method: str, path: str, params: Mapping[str, Any]) -> str:
    material = json.dumps({"method": method, "path": path, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Search query language (subset): field:'x' exact, field~'x' substring, comparisons, metadata['k'],
# AND / OR (AND binds tighter), leading `-` negation, and the Arga twin's bare-term extension.
# --------------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:(?P<lparen>\()|(?P<rparen>\))|(?P<and>AND\b)|(?P<or>OR\b)|"""
    r"""(?P<clause>-?[A-Za-z_][A-Za-z0-9_.]*(?:\[(?:'[^']*'|"[^"]*")\])?\s*(?:>=|<=|:|~|>|<)\s*"""
    r"""(?:'[^']*'|"[^"]*"|[^\s()]+))|(?P<bare>'[^']*'|"[^"]*"|[^\s()]+))""",
    re.IGNORECASE,
)
_CLAUSE = re.compile(
    r"""^(?P<neg>-?)(?P<field>[A-Za-z_][A-Za-z0-9_.]*)(?:\[(?P<key>'[^']*'|"[^"]*")\])?\s*"""
    r"""(?P<op>>=|<=|:|~|>|<)\s*(?P<value>'[^']*'|"[^"]*"|.+)$""",
    re.DOTALL,
)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


class SearchQuery:
    """Compiled Stripe search query. `matches(record)` evaluates it against one object."""

    def __init__(self, query: str, collection: str) -> None:
        self.collection = collection
        self.fields = _SEARCHABLE_FIELDS.get(collection, frozenset())
        self.bare_fields = _BARE_TERM_FIELDS.get(collection, ("name", "id"))
        tokens = self._tokenize(query)
        self._tokens = tokens
        self._position = 0
        if not tokens:
            raise StripeError(400, "The search query is empty.", param="query")
        self._tree = self._parse_or()
        if self._position != len(tokens):
            raise StripeError(
                400, f"The search query could not be parsed near '{tokens[self._position][1]}'.", param="query"
            )

    # -- tokenizer / parser -------------------------------------------------------------

    def _tokenize(self, query: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        position = 0
        text = query.strip()
        while position < len(text):
            match = _TOKEN.match(text, position)
            if match is None or match.end() == position:
                raise StripeError(400, f"The search query could not be parsed near '{text[position:]}'.", param="query")
            position = match.end()
            kind = cast(str, match.lastgroup)
            tokens.append((kind, match.group(kind)))
        return tokens

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._position] if self._position < len(self._tokens) else None

    def _parse_or(self) -> Callable[[JsonDict], bool]:
        left = self._parse_and()
        while (token := self._peek()) is not None and token[0] == "or":
            self._position += 1
            right = self._parse_and()
            left = self._either(left, right)
        return left

    def _parse_and(self) -> Callable[[JsonDict], bool]:
        left = self._parse_atom()
        while (token := self._peek()) is not None and token[0] in {"and", "clause", "bare", "lparen"}:
            if token[0] == "and":
                self._position += 1
            right = self._parse_atom()
            left = self._both(left, right)
        return left

    def _parse_atom(self) -> Callable[[JsonDict], bool]:
        token = self._peek()
        if token is None:
            raise StripeError(400, "The search query ended unexpectedly.", param="query")
        kind, text = token
        self._position += 1
        if kind == "lparen":
            inner = self._parse_or()
            closing = self._peek()
            if closing is None or closing[0] != "rparen":
                raise StripeError(400, "The search query has an unbalanced parenthesis.", param="query")
            self._position += 1
            return inner
        if kind == "clause":
            return self._compile_clause(text)
        if kind == "bare":
            return self._compile_bare(_unquote(text))
        raise StripeError(400, f"The search query could not be parsed near '{text}'.", param="query")

    @staticmethod
    def _either(left: Callable[[JsonDict], bool], right: Callable[[JsonDict], bool]) -> Callable[[JsonDict], bool]:
        return lambda record: left(record) or right(record)

    @staticmethod
    def _both(left: Callable[[JsonDict], bool], right: Callable[[JsonDict], bool]) -> Callable[[JsonDict], bool]:
        return lambda record: left(record) and right(record)

    # -- clause compilation -------------------------------------------------------------

    def _compile_clause(self, text: str) -> Callable[[JsonDict], bool]:
        match = _CLAUSE.match(text.strip())
        if match is None:
            raise StripeError(400, f"The search query could not be parsed near '{text}'.", param="query")
        negate = match.group("neg") == "-"
        field = match.group("field")
        key = match.group("key")
        operator = match.group("op")
        value = _unquote(match.group("value").strip())
        root = field.split(".", 1)[0]
        if root not in self.fields:
            raise StripeError(400, f"Field `{field}` is not searchable on {self.collection[:-1]}.", param="query")
        if root == "metadata" and key is None and "." not in field:
            raise StripeError(
                400, "Metadata searches must name a key, e.g. metadata['order_id']:'6735'.", param="query"
            )
        path = field.split(".")
        if key is not None:
            path.append(_unquote(key))

        def evaluate(record: JsonDict) -> bool:
            actual = _dig(record, path)
            return _compare(actual, operator, value)

        return (lambda record: not evaluate(record)) if negate else evaluate

    def _compile_bare(self, term: str) -> Callable[[JsonDict], bool]:
        needle = term.casefold()

        def evaluate(record: JsonDict) -> bool:
            haystacks = [record.get(field) for field in self.bare_fields]
            metadata = record.get("metadata")
            if isinstance(metadata, dict):
                haystacks.extend(cast(dict[str, Any], metadata).values())
            return any(isinstance(item, str) and needle in item.casefold() for item in haystacks)

        return evaluate

    def matches(self, record: JsonDict) -> bool:
        return self._tree(record)


def _dig(record: object, path: Sequence[str]) -> object:
    node: object = record
    for part in path:
        if not isinstance(node, dict):
            return None
        node = cast(dict[str, Any], node).get(part)
    return node


def _compare(actual: object, operator: str, value: str) -> bool:
    if value.lower() == "null" and operator == ":":
        return actual is None
    if operator in {">", "<", ">=", "<="}:
        try:
            left = float(cast(Any, actual))
            right = float(value)
        except (TypeError, ValueError):
            return False
        return {">": left > right, "<": left < right, ">=": left >= right, "<=": left <= right}[operator]
    if actual is None:
        return False
    if isinstance(actual, bool):
        return value.lower() in {"true", "1"} if actual else value.lower() in {"false", "0"}
    if isinstance(actual, int | float):
        try:
            return float(actual) == float(value)
        except ValueError:
            return False
    text = str(actual).casefold()
    needle = value.casefold()
    return needle in text if operator == "~" else text == needle


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------

_INTERVAL_SECONDS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}


class StripeStore(Store):
    """All Stripe state. Every collection is a dict keyed by id (the grader's cardinality contract)."""

    provider: ClassVar[str] = "stripe"

    def __init__(self, seed_key: str, clock: Clock | None = None) -> None:
        super().__init__(seed_key, clock)
        self.customers: dict[str, JsonDict] = {}
        self.products: dict[str, JsonDict] = {}
        self.prices: dict[str, JsonDict] = {}
        self.subscriptions: dict[str, JsonDict] = {}
        self.invoices: dict[str, JsonDict] = {}
        self.charges: dict[str, JsonDict] = {}
        self.payment_intents: dict[str, JsonDict] = {}
        self.refunds: dict[str, JsonDict] = {}
        self.tax_ids: dict[str, JsonDict] = {}
        self.meters: dict[str, JsonDict] = {}
        self.meter_events: dict[str, JsonDict] = {}
        self.events: list[JsonDict] = []
        self.idempotency_keys: dict[str, JsonDict] = {}
        self.generic_resources: dict[str, JsonDict] = {}
        self.deleted_customers: dict[str, JsonDict] = {}
        self.account_id = self.new_id("accounts", kind="alnum", prefix="acct_1", length=18)
        self._seed_ordinal = 0
        self._request_ordinal = 0

    # -- identifiers and time --------------------------------------------------------

    def stripe_id(self, collection: str) -> str:
        prefix, length = _ID_FORMATS[collection]
        return self.new_id(collection, kind="alnum", prefix=prefix, length=length)

    def next_request_id(self) -> str:
        """`req_…` for the Request-Id header. Not part of state, so reads stay pure."""
        self._request_ordinal += 1
        return "req_" + det_alnum(self.seed_key, "request", self._request_ordinal, length=14)

    def next_epoch(self) -> int:
        """The `created` timestamp the next write will carry (`record_mutation` ticks exactly one second)."""
        return int((self.clock.now() + timedelta(seconds=1)).timestamp())

    def _seed_created(self) -> int:
        """Seeded objects predate the scenario clock by ~30 days, one second apart, without ticking it."""
        self._seed_ordinal += 1
        return self.clock.epoch() - _SEED_AGE_SECONDS + self._seed_ordinal

    # -- collections -----------------------------------------------------------------

    def collection(self, name: str) -> dict[str, JsonDict]:
        return cast(dict[str, JsonDict], getattr(self, name))

    def require(self, collection: str, identifier: str, *, param: str | None = None) -> JsonDict:
        """Look up a record. URL ids miss with 404; ids passed as parameters miss with 400 (Stripe behaviour)."""
        record = self.collection(collection).get(identifier)
        if record is None:
            if param is None:
                raise resource_missing(_object_name(collection), identifier)
            raise resource_missing(_object_name(collection), identifier, param=param, status=400)
        return record

    def write(self, *, method: str, path: str, collection: str, record_id: str, before: object, after: object) -> None:
        self.record_mutation(
            method=method, path=path, collection=collection, record_id=record_id, before=before, after=after
        )

    def emit(
        self,
        event_type: str,
        payload: JsonDict,
        *,
        request_id: str,
        idempotency_key: str | None,
        previous_attributes: JsonDict | None = None,
    ) -> JsonDict:
        data: JsonDict = {"object": _copy(payload)}
        if previous_attributes is not None:
            data["previous_attributes"] = _copy(previous_attributes)
        event: JsonDict = {
            "id": self.stripe_id("events"),
            "object": "event",
            "api_version": STRIPE_API_VERSION,
            "created": self.clock.epoch(),
            "data": data,
            "livemode": False,
            "pending_webhooks": 0,
            "request": {"id": request_id, "idempotency_key": idempotency_key},
            "type": event_type,
        }
        self.events.append(event)
        return event

    # -- contract --------------------------------------------------------------------

    def seed(self, seed_config: Mapping[str, Any]) -> None:
        stripe_slice = seed_config
        if not ({"customers", "products", "meters"} & set(seed_config)) and isinstance(seed_config.get("stripe"), dict):
            stripe_slice = cast(Mapping[str, Any], seed_config["stripe"])
        for raw in as_list(stripe_slice.get("customers")):
            if isinstance(raw, dict):
                self.seed_customer(cast(JsonDict, raw))
        for raw in as_list(stripe_slice.get("products")):
            if isinstance(raw, dict):
                self.seed_product(cast(JsonDict, raw))
        for raw in as_list(stripe_slice.get("meters")):
            if isinstance(raw, dict):
                self.seed_meter(cast(JsonDict, raw))

    def admin_state(self) -> JsonDict:
        return _copy(
            {
                "customers": self.customers,
                "products": self.products,
                "prices": self.prices,
                "subscriptions": self.subscriptions,
                "invoices": self.invoices,
                "charges": self.charges,
                "payment_intents": self.payment_intents,
                "refunds": self.refunds,
                "tax_ids": self.tax_ids,
                "meters": self.meters,
                "meter_events": self.meter_events,
                "meter_event_identifiers": sorted(self.meter_events),
                "events": self.events,
                "idempotency": {"cached_responses": len(self.idempotency_keys)},
                "generic_resources": self.generic_resources,
                "counts": {"generic_resources": len(self.generic_resources)},
            }
        )

    # -- seeding ---------------------------------------------------------------------

    def seed_customer(self, raw: JsonDict) -> JsonDict:
        customer = self.build_customer(
            created=self._seed_created(),
            name=_opt_str(raw.get("name")),
            email=_opt_str(raw.get("email")),
            description=_opt_str(raw.get("description")),
            phone=_opt_str(raw.get("phone")),
            metadata=_str_map(raw.get("metadata")),
        )
        self.customers[customer["id"]] = customer
        return customer

    def seed_product(self, raw: JsonDict) -> JsonDict:
        created = self._seed_created()
        product = self.build_product(
            created=created,
            name=_opt_str(raw.get("name")) or "Product",
            description=_opt_str(raw.get("description")),
            active=raw.get("active", True) is not False,
            metadata=_str_map(raw.get("metadata")),
        )
        self.products[product["id"]] = product
        for raw_price in as_list(raw.get("prices")):
            if not isinstance(raw_price, dict):
                continue
            typed = cast(JsonDict, raw_price)
            recurring = typed.get("recurring") if isinstance(typed.get("recurring"), dict) else None
            price = self.build_price(
                created=self._seed_created(),
                product_id=product["id"],
                currency=str(typed.get("currency", "usd")).lower(),
                unit_amount=as_int(typed.get("unit_amount", 0), "unit_amount"),
                recurring=cast(JsonDict, recurring) if recurring else None,
                nickname=_opt_str(typed.get("nickname")),
                lookup_key=_opt_str(typed.get("lookup_key")),
                active=typed.get("active", True) is not False,
                metadata=_str_map(typed.get("metadata")),
            )
            self.prices[price["id"]] = price
        return product

    def seed_meter(self, raw: JsonDict) -> JsonDict:
        meter = self.build_meter(
            created=self._seed_created(),
            display_name=_opt_str(raw.get("display_name")) or "Meter",
            event_name=_opt_str(raw.get("event_name")) or "meter_event",
            aggregation=_opt_str(raw.get("aggregation")) or "sum",
        )
        self.meters[meter["id"]] = meter
        for raw_event in as_list(raw.get("events")):
            if not isinstance(raw_event, dict):
                continue
            typed = cast(JsonDict, raw_event)
            payload = typed.get("payload") if isinstance(typed.get("payload"), dict) else typed
            event = self.build_meter_event(
                created=self._seed_created(),
                event_name=meter["event_name"],
                identifier=_opt_str(typed.get("identifier"))
                or det_alnum(self.seed_key, "meter_event", len(self.meter_events) + 1, length=16),
                payload=_str_map(payload),
                timestamp=typed.get("timestamp"),
            )
            self.meter_events[event["identifier"]] = event
        return meter

    # -- record builders (official Stripe object shapes, API 2024-06-20 field set) --------

    def build_customer(
        self,
        *,
        created: int,
        name: str | None,
        email: str | None,
        description: str | None = None,
        phone: str | None = None,
        metadata: JsonDict | None = None,
        address: JsonDict | None = None,
        shipping: JsonDict | None = None,
        tax_exempt: str = "none",
        balance: int = 0,
        preferred_locales: list[str] | None = None,
        invoice_prefix: str | None = None,
    ) -> JsonDict:
        customer_id = self.stripe_id("customers")
        return {
            "id": customer_id,
            "object": "customer",
            "address": address,
            "balance": balance,
            "created": created,
            "currency": None,
            "default_source": None,
            "delinquent": False,
            "description": description,
            "discount": None,
            "email": email,
            "invoice_prefix": invoice_prefix
            or det_alnum(self.seed_key, "invoice_prefix", customer_id, length=8, case="upper"),
            "invoice_settings": {
                "custom_fields": None,
                "default_payment_method": None,
                "footer": None,
                "rendering_options": None,
            },
            "livemode": False,
            "metadata": metadata or {},
            "name": name,
            "next_invoice_sequence": 1,
            "phone": phone,
            "preferred_locales": preferred_locales or [],
            "shipping": shipping,
            "tax_exempt": tax_exempt,
            "test_clock": None,
        }

    def build_product(
        self,
        *,
        created: int,
        name: str,
        description: str | None = None,
        active: bool = True,
        metadata: JsonDict | None = None,
        product_id: str | None = None,
        images: list[str] | None = None,
        marketing_features: list[JsonDict] | None = None,
        statement_descriptor: str | None = None,
        tax_code: str | None = None,
        unit_label: str | None = None,
        url: str | None = None,
        shippable: bool | None = None,
    ) -> JsonDict:
        return {
            "id": product_id or self.stripe_id("products"),
            "object": "product",
            "active": active,
            "created": created,
            "default_price": None,
            "description": description,
            "images": images or [],
            "livemode": False,
            "marketing_features": marketing_features or [],
            "metadata": metadata or {},
            "name": name,
            "package_dimensions": None,
            "shippable": shippable,
            "statement_descriptor": statement_descriptor,
            "tax_code": tax_code,
            "unit_label": unit_label,
            "updated": created,
            "url": url,
        }

    def build_price(
        self,
        *,
        created: int,
        product_id: str,
        currency: str,
        unit_amount: int | None,
        recurring: JsonDict | None,
        nickname: str | None = None,
        lookup_key: str | None = None,
        active: bool = True,
        metadata: JsonDict | None = None,
        billing_scheme: str = "per_unit",
        tax_behavior: str = "unspecified",
        unit_amount_decimal: str | None = None,
        custom_unit_amount: JsonDict | None = None,
        tiers_mode: str | None = None,
        transform_quantity: JsonDict | None = None,
    ) -> JsonDict:
        recurring_block: JsonDict | None = None
        if recurring is not None:
            recurring_block = {
                "aggregate_usage": recurring.get("aggregate_usage"),
                "interval": recurring.get("interval", "month"),
                "interval_count": as_int(recurring.get("interval_count", 1), "recurring[interval_count]"),
                "meter": recurring.get("meter"),
                "trial_period_days": recurring.get("trial_period_days"),
                "usage_type": recurring.get("usage_type", "licensed"),
            }
        decimal = (
            unit_amount_decimal
            if unit_amount_decimal is not None
            else (str(unit_amount) if unit_amount is not None else None)
        )
        return {
            "id": self.stripe_id("prices"),
            "object": "price",
            "active": active,
            "billing_scheme": billing_scheme,
            "created": created,
            "currency": currency,
            "custom_unit_amount": custom_unit_amount,
            "livemode": False,
            "lookup_key": lookup_key,
            "metadata": metadata or {},
            "nickname": nickname,
            "product": product_id,
            "recurring": recurring_block,
            "tax_behavior": tax_behavior,
            "tiers_mode": tiers_mode,
            "transform_quantity": transform_quantity,
            "type": "recurring" if recurring_block else "one_time",
            "unit_amount": unit_amount,
            "unit_amount_decimal": decimal,
        }

    def build_subscription_item(
        self, *, created: int, subscription_id: str, price: JsonDict, quantity: int, metadata: JsonDict
    ) -> JsonDict:
        return {
            "id": self.stripe_id("subscription_items"),
            "object": "subscription_item",
            "billing_thresholds": None,
            "created": created,
            "discounts": [],
            "metadata": metadata,
            "plan": _plan_from_price(price),
            "price": _copy(price),
            "quantity": quantity,
            "subscription": subscription_id,
            "tax_rates": [],
        }

    def build_subscription(
        self,
        *,
        created: int,
        customer_id: str,
        items: list[tuple[JsonDict, int, JsonDict]],
        metadata: JsonDict,
        cancel_at_period_end: bool,
        collection_method: str,
        days_until_due: int | None,
        description: str | None,
        trial_period_days: int | None,
        default_payment_method: str | None,
    ) -> JsonDict:
        subscription_id = self.stripe_id("subscriptions")
        first_price = items[0][0]
        recurring = first_price.get("recurring") if isinstance(first_price.get("recurring"), dict) else None
        interval = str(cast(JsonDict, recurring).get("interval", "month")) if recurring else "month"
        count = int(cast(JsonDict, recurring).get("interval_count", 1)) if recurring else 1
        period_seconds = _INTERVAL_SECONDS.get(interval, _INTERVAL_SECONDS["month"]) * max(count, 1)
        trial_end = created + trial_period_days * 86400 if trial_period_days else None
        period_start = created
        period_end = trial_end if trial_end else created + period_seconds
        item_records = [
            self.build_subscription_item(
                created=created, subscription_id=subscription_id, price=price, quantity=quantity, metadata=item_metadata
            )
            for price, quantity, item_metadata in items
        ]
        return {
            "id": subscription_id,
            "object": "subscription",
            "application": None,
            "application_fee_percent": None,
            "automatic_tax": {"enabled": False, "liability": None},
            "billing_cycle_anchor": created,
            "billing_thresholds": None,
            "cancel_at": None,
            "cancel_at_period_end": cancel_at_period_end,
            "canceled_at": None,
            "cancellation_details": {"comment": None, "feedback": None, "reason": None},
            "collection_method": collection_method,
            "created": created,
            "currency": first_price.get("currency"),
            "current_period_end": period_end,
            "current_period_start": period_start,
            "customer": customer_id,
            "days_until_due": days_until_due,
            "default_payment_method": default_payment_method,
            "default_source": None,
            "default_tax_rates": [],
            "description": description,
            "discount": None,
            "discounts": [],
            "ended_at": None,
            "invoice_settings": {"account_tax_ids": None, "issuer": {"type": "self"}},
            "items": {
                "object": "list",
                "data": item_records,
                "has_more": False,
                "total_count": len(item_records),
                "url": f"/v1/subscription_items?subscription={subscription_id}",
            },
            "latest_invoice": None,
            "livemode": False,
            "metadata": metadata,
            "next_pending_invoice_item_invoice": None,
            "on_behalf_of": None,
            "pause_collection": None,
            "payment_settings": {
                "payment_method_options": None,
                "payment_method_types": None,
                "save_default_payment_method": "off",
            },
            "pending_invoice_item_interval": None,
            "pending_setup_intent": None,
            "pending_update": None,
            "schedule": None,
            "start_date": created,
            "status": "trialing" if trial_end else "active",
            "test_clock": None,
            "transfer_data": None,
            "trial_end": trial_end,
            "trial_settings": {"end_behavior": {"missing_payment_method": "create_invoice"}},
            "trial_start": created if trial_end else None,
        }

    def build_invoice(
        self,
        *,
        created: int,
        customer: JsonDict,
        currency: str,
        lines: list[JsonDict],
        subscription_id: str | None,
        billing_reason: str,
        collection_method: str,
        days_until_due: int | None,
        description: str | None,
        metadata: JsonDict,
        auto_advance: bool,
        status: str,
        due_date: int | None = None,
        footer: str | None = None,
        statement_descriptor: str | None = None,
    ) -> JsonDict:
        invoice_id = self.stripe_id("invoices")
        subtotal = sum(int(line.get("amount", 0)) for line in lines)
        paid = status == "paid"
        for line in lines:
            line["invoice"] = invoice_id
        return {
            "id": invoice_id,
            "object": "invoice",
            "account_country": "US",
            "account_name": "Twin Commerce",
            "account_tax_ids": None,
            "amount_due": subtotal,
            "amount_paid": subtotal if paid else 0,
            "amount_remaining": 0 if paid else subtotal,
            "amount_shipping": 0,
            "application": None,
            "application_fee_amount": None,
            "attempt_count": 1 if paid else 0,
            "attempted": paid,
            "auto_advance": auto_advance,
            "automatic_tax": {"enabled": False, "liability": None, "status": None},
            "billing_reason": billing_reason,
            "charge": None,
            "collection_method": collection_method,
            "created": created,
            "currency": currency,
            "custom_fields": None,
            "customer": customer["id"],
            "customer_address": customer.get("address"),
            "customer_email": customer.get("email"),
            "customer_name": customer.get("name"),
            "customer_phone": customer.get("phone"),
            "customer_shipping": customer.get("shipping"),
            "customer_tax_exempt": customer.get("tax_exempt", "none"),
            "customer_tax_ids": [],
            "default_payment_method": None,
            "default_source": None,
            "default_tax_rates": [],
            "description": description,
            "discount": None,
            "discounts": [],
            "due_date": due_date,
            "effective_at": created if status != "draft" else None,
            "ending_balance": 0 if status != "draft" else None,
            "footer": footer,
            "from_invoice": None,
            "hosted_invoice_url": None,
            "invoice_pdf": None,
            "issuer": {"type": "self"},
            "last_finalization_error": None,
            "latest_revision": None,
            "lines": {
                "object": "list",
                "data": lines,
                "has_more": False,
                "total_count": len(lines),
                "url": f"/v1/invoices/{invoice_id}/lines",
            },
            "livemode": False,
            "metadata": metadata,
            "next_payment_attempt": None,
            "number": None,
            "on_behalf_of": None,
            "paid": paid,
            "paid_out_of_band": False,
            "payment_intent": None,
            "payment_settings": {
                "default_mandate": None,
                "payment_method_options": None,
                "payment_method_types": None,
            },
            "period_end": created,
            "period_start": created,
            "post_payment_credit_notes_amount": 0,
            "pre_payment_credit_notes_amount": 0,
            "quote": None,
            "receipt_number": None,
            "rendering": None,
            "shipping_cost": None,
            "shipping_details": None,
            "starting_balance": 0,
            "statement_descriptor": statement_descriptor,
            "status": status,
            "status_transitions": {
                "finalized_at": created if status != "draft" else None,
                "marked_uncollectible_at": None,
                "paid_at": created if paid else None,
                "voided_at": None,
            },
            "subscription": subscription_id,
            "subscription_details": {"metadata": {}} if subscription_id else None,
            "subtotal": subtotal,
            "subtotal_excluding_tax": subtotal,
            "tax": None,
            "test_clock": None,
            "total": subtotal,
            "total_discount_amounts": [],
            "total_excluding_tax": subtotal,
            "total_tax_amounts": [],
            "transfer_data": None,
            "webhooks_delivered_at": created,
        }

    def build_invoice_line(
        self,
        *,
        price: JsonDict,
        quantity: int,
        subscription_id: str | None,
        subscription_item_id: str | None,
        period_start: int,
        period_end: int,
    ) -> JsonDict:
        unit_amount = int(price.get("unit_amount") or 0)
        return {
            "id": "il_1" + det_alnum(self.seed_key, "invoice_line", self.next_ordinal("invoice_lines"), length=23),
            "object": "line_item",
            "amount": unit_amount * quantity,
            "amount_excluding_tax": unit_amount * quantity,
            "currency": price.get("currency"),
            "description": f"{quantity} × {price.get('nickname') or 'Subscription'}",
            "discount_amounts": [],
            "discountable": True,
            "discounts": [],
            "invoice": None,
            "livemode": False,
            "metadata": {},
            "period": {"end": period_end, "start": period_start},
            "plan": _plan_from_price(price),
            "price": _copy(price),
            "proration": False,
            "proration_details": {"credited_items": None},
            "quantity": quantity,
            "subscription": subscription_id,
            "subscription_item": subscription_item_id,
            "tax_amounts": [],
            "tax_rates": [],
            "type": "subscription" if subscription_id else "invoiceitem",
            "unit_amount_excluding_tax": str(unit_amount),
        }

    def build_charge(
        self,
        *,
        created: int,
        amount: int,
        currency: str,
        customer_id: str | None,
        description: str | None,
        metadata: JsonDict,
        captured: bool,
        receipt_email: str | None,
        statement_descriptor: str | None,
        statement_descriptor_suffix: str | None,
        payment_intent_id: str | None,
        transfer_group: str | None,
        invoice_id: str | None = None,
    ) -> JsonDict:
        charge_id = self.stripe_id("charges")
        customer = self.customers.get(customer_id) if customer_id else None
        return {
            "id": charge_id,
            "object": "charge",
            "amount": amount,
            "amount_captured": amount if captured else 0,
            "amount_refunded": 0,
            "application": None,
            "application_fee": None,
            "application_fee_amount": None,
            "balance_transaction": "txn_3" + det_alnum(self.seed_key, "txn", charge_id, length=23)
            if captured
            else None,
            "billing_details": {
                "address": {
                    "city": None,
                    "country": None,
                    "line1": None,
                    "line2": None,
                    "postal_code": None,
                    "state": None,
                },
                "email": customer.get("email") if customer else None,
                "name": customer.get("name") if customer else None,
                "phone": customer.get("phone") if customer else None,
            },
            "calculated_statement_descriptor": (statement_descriptor or "TWIN COMMERCE").upper(),
            "captured": captured,
            "created": created,
            "currency": currency,
            "customer": customer_id,
            "description": description,
            "disputed": False,
            "failure_balance_transaction": None,
            "failure_code": None,
            "failure_message": None,
            "fraud_details": {},
            "invoice": invoice_id,
            "livemode": False,
            "metadata": metadata,
            "on_behalf_of": None,
            "outcome": {
                "network_status": "approved_by_network",
                "reason": None,
                "risk_level": "normal",
                "risk_score": 32,
                "seller_message": "Payment complete.",
                "type": "authorized",
            },
            "paid": True,
            "payment_intent": payment_intent_id,
            "payment_method": "pm_1" + det_alnum(self.seed_key, "pm", charge_id, length=23),
            "payment_method_details": {
                "card": {
                    "amount_authorized": amount,
                    "brand": "visa",
                    "checks": {"address_line1_check": None, "address_postal_code_check": None, "cvc_check": "pass"},
                    "country": "US",
                    "exp_month": 12,
                    "exp_year": 2030,
                    "fingerprint": det_alnum(self.seed_key, "card_fingerprint", 1, length=16),
                    "funding": "credit",
                    "installments": None,
                    "last4": "4242",
                    "mandate": None,
                    "network": "visa",
                    "network_token": {"used": False},
                    "three_d_secure": None,
                    "wallet": None,
                },
                "type": "card",
            },
            "receipt_email": receipt_email,
            "receipt_number": None,
            "receipt_url": f"https://pay.stripe.com/receipts/payment/{charge_id}",
            "refunded": False,
            "refunds": {
                "object": "list",
                "data": [],
                "has_more": False,
                "total_count": 0,
                "url": f"/v1/charges/{charge_id}/refunds",
            },
            "review": None,
            "shipping": None,
            "source": None,
            "source_transfer": None,
            "statement_descriptor": statement_descriptor,
            "statement_descriptor_suffix": statement_descriptor_suffix,
            "status": "succeeded",
            "transfer_data": None,
            "transfer_group": transfer_group,
        }

    def build_payment_intent(
        self,
        *,
        created: int,
        amount: int,
        currency: str,
        customer_id: str | None,
        description: str | None,
        metadata: JsonDict,
        payment_method: str | None,
        payment_method_types: list[str],
        capture_method: str,
        receipt_email: str | None,
        statement_descriptor: str | None,
        setup_future_usage: str | None,
        automatic_payment_methods: JsonDict | None,
        transfer_group: str | None,
    ) -> JsonDict:
        intent_id = self.stripe_id("payment_intents")
        return {
            "id": intent_id,
            "object": "payment_intent",
            "amount": amount,
            "amount_capturable": 0,
            "amount_details": {"tip": {}},
            "amount_received": 0,
            "application": None,
            "application_fee_amount": None,
            "automatic_payment_methods": automatic_payment_methods,
            "canceled_at": None,
            "cancellation_reason": None,
            "capture_method": capture_method,
            "client_secret": f"{intent_id}_secret_{det_alnum(self.seed_key, 'pi_secret', intent_id, length=24)}",
            "confirmation_method": "automatic",
            "created": created,
            "currency": currency,
            "customer": customer_id,
            "description": description,
            "invoice": None,
            "last_payment_error": None,
            "latest_charge": None,
            "livemode": False,
            "metadata": metadata,
            "next_action": None,
            "on_behalf_of": None,
            "payment_method": payment_method,
            "payment_method_configuration_details": None,
            "payment_method_options": {
                "card": {
                    "installments": None,
                    "mandate_options": None,
                    "network": None,
                    "request_three_d_secure": "automatic",
                }
            },
            "payment_method_types": payment_method_types,
            "processing": None,
            "receipt_email": receipt_email,
            "review": None,
            "setup_future_usage": setup_future_usage,
            "shipping": None,
            "source": None,
            "statement_descriptor": statement_descriptor,
            "statement_descriptor_suffix": None,
            "status": "requires_confirmation" if payment_method else "requires_payment_method",
            "transfer_data": None,
            "transfer_group": transfer_group,
        }

    def build_refund(
        self, *, created: int, amount: int, charge: JsonDict, reason: str | None, metadata: JsonDict
    ) -> JsonDict:
        refund_id = self.stripe_id("refunds")
        return {
            "id": refund_id,
            "object": "refund",
            "amount": amount,
            "balance_transaction": "txn_3" + det_alnum(self.seed_key, "txn", refund_id, length=23),
            "charge": charge["id"],
            "created": created,
            "currency": charge.get("currency"),
            "destination_details": {
                "card": {
                    "reference_status": "pending",
                    "reference_type": "acquirer_reference_number",
                    "type": "refund",
                },
                "type": "card",
            },
            "metadata": metadata,
            "payment_intent": charge.get("payment_intent"),
            "reason": reason,
            "receipt_number": None,
            "source_transfer_reversal": None,
            "status": "succeeded",
            "transfer_reversal": None,
        }

    def build_tax_id(self, *, created: int, customer_id: str, tax_type: str, value: str) -> JsonDict:
        country = tax_type.split("_", 1)[0].upper()
        return {
            "id": self.stripe_id("tax_ids"),
            "object": "tax_id",
            "country": country if country != "EU" else None,
            "created": created,
            "customer": customer_id,
            "livemode": False,
            "owner": {"customer": customer_id, "type": "customer"},
            "type": tax_type,
            "value": value,
            "verification": {"status": "pending", "verified_address": None, "verified_name": None},
        }

    def build_meter(
        self,
        *,
        created: int,
        display_name: str,
        event_name: str,
        aggregation: str,
        customer_mapping: JsonDict | None = None,
        value_settings: JsonDict | None = None,
    ) -> JsonDict:
        return {
            "id": self.stripe_id("meters"),
            "object": "billing.meter",
            "created": created,
            "customer_mapping": customer_mapping or {"event_payload_key": "stripe_customer_id", "type": "by_id"},
            "default_aggregation": {"formula": aggregation},
            "display_name": display_name,
            "event_name": event_name,
            "event_time_window": None,
            "livemode": False,
            "status": "active",
            "status_transitions": {"deactivated_at": None},
            "updated": created,
            "value_settings": value_settings or {"event_payload_key": "value"},
        }

    def build_meter_event(
        self, *, created: int, event_name: str, identifier: str, payload: JsonDict, timestamp: object
    ) -> JsonDict:
        return {
            "object": "billing.meter_event",
            "created": created,
            "event_name": event_name,
            "identifier": identifier,
            "livemode": False,
            "payload": payload,
            "timestamp": as_int(timestamp, "timestamp") if timestamp not in (None, "") else created,
        }


def _object_name(collection: str) -> str:
    return {
        "customers": "customer",
        "products": "product",
        "prices": "price",
        "subscriptions": "subscription",
        "invoices": "invoice",
        "charges": "charge",
        "payment_intents": "payment_intent",
        "refunds": "refund",
        "tax_ids": "tax_id",
        "meters": "meter",
        "events": "event",
    }.get(collection, collection.rstrip("s"))


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _str_map(value: object) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in cast(dict[str, Any], value).items() if item is not None}


def _plan_from_price(price: JsonDict) -> JsonDict | None:
    recurring = price.get("recurring")
    if not isinstance(recurring, dict):
        return None
    typed = cast(JsonDict, recurring)
    return {
        "id": price["id"],
        "object": "plan",
        "active": price.get("active", True),
        "aggregate_usage": typed.get("aggregate_usage"),
        "amount": price.get("unit_amount"),
        "amount_decimal": price.get("unit_amount_decimal"),
        "billing_scheme": price.get("billing_scheme", "per_unit"),
        "created": price.get("created"),
        "currency": price.get("currency"),
        "interval": typed.get("interval"),
        "interval_count": typed.get("interval_count", 1),
        "livemode": False,
        "metadata": price.get("metadata", {}),
        "meter": typed.get("meter"),
        "nickname": price.get("nickname"),
        "product": price.get("product"),
        "tiers_mode": price.get("tiers_mode"),
        "transform_usage": None,
        "trial_period_days": typed.get("trial_period_days"),
        "usage_type": typed.get("usage_type", "licensed"),
    }


# --------------------------------------------------------------------------------------
# Data plane
# --------------------------------------------------------------------------------------

_EXPANDABLE: dict[str, str] = {
    "customer": "customers",
    "product": "products",
    "default_price": "prices",
    "price": "prices",
    "subscription": "subscriptions",
    "latest_invoice": "invoices",
    "invoice": "invoices",
    "payment_intent": "payment_intents",
    "latest_charge": "charges",
    "charge": "charges",
}
_MODELLED_TOP_LEVEL = frozenset(
    {
        "account", "billing", "charges", "customers", "events", "invoices", "payment_intents", "prices", "products",
        "refunds", "subscription_items", "subscriptions", "tax_ids",
    }
)  # fmt: skip
_MODELLED_BILLING = frozenset({"meters", "meter_events"})


class Ctx:
    """One request: decoded parameters (body for writes, query for reads) plus idempotency metadata."""

    def __init__(
        self,
        request: Request,
        *,
        params: JsonDict,
        path_params: Mapping[str, str],
        request_id: str,
        idempotency_key: str | None,
    ) -> None:
        self.request = request
        self.params = params
        self.path_params = dict(path_params)
        self.request_id = request_id
        self.idempotency_key = idempotency_key
        self.method = request.method.upper()
        self.path = request.url.path

    def arg(self, name: str) -> str:
        return self.path_params[name]

    def has(self, name: str) -> bool:
        return name in self.params

    def str_or_none(self, name: str) -> str | None:
        """Optional string parameter: an empty string unsets the field (Stripe convention)."""
        value = self.params.get(name)
        if value is None or value == "":
            return None
        return as_str(value, name)


Handler = Callable[[Ctx], "tuple[int, JsonDict]"]
Result = tuple[int, JsonDict]


class StripeApi:
    """Route handlers. Reads never touch state; writes go through `StripeStore.write` + an event."""

    def __init__(self, store: StripeStore) -> None:
        self.store = store

    # -- routing -----------------------------------------------------------------------

    def routes(self) -> list[Route]:
        table: list[tuple[str, dict[str, Handler]]] = [
            ("/v1/account", {"GET": self.get_account}),
            ("/v1/customers", {"GET": self.list_customers, "POST": self.create_customer}),
            ("/v1/customers/search", {"GET": self.search_customers}),
            (
                "/v1/customers/{id}",
                {"GET": self.get_customer, "POST": self.update_customer, "DELETE": self.delete_customer},
            ),
            ("/v1/customers/{id}/tax_ids", {"GET": self.list_tax_ids, "POST": self.create_tax_id}),
            ("/v1/customers/{id}/tax_ids/{tax_id}", {"GET": self.get_tax_id, "DELETE": self.delete_tax_id}),
            ("/v1/customers/{id}/payment_methods", {"GET": self.list_customer_payment_methods}),
            ("/v1/customers/{id}/balance_transactions", {"GET": self.list_customer_balance_transactions}),
            ("/v1/products", {"GET": self.list_products, "POST": self.create_product}),
            ("/v1/products/search", {"GET": self.search_products}),
            (
                "/v1/products/{id}",
                {"GET": self.get_product, "POST": self.update_product, "DELETE": self.delete_product},
            ),
            ("/v1/prices", {"GET": self.list_prices, "POST": self.create_price}),
            ("/v1/prices/search", {"GET": self.search_prices}),
            ("/v1/prices/{id}", {"GET": self.get_price, "POST": self.update_price}),
            ("/v1/subscriptions", {"GET": self.list_subscriptions, "POST": self.create_subscription}),
            ("/v1/subscriptions/search", {"GET": self.search_subscriptions}),
            (
                "/v1/subscriptions/{id}",
                {"GET": self.get_subscription, "POST": self.update_subscription, "DELETE": self.cancel_subscription},
            ),
            ("/v1/subscription_items", {"GET": self.list_subscription_items}),
            ("/v1/invoices", {"GET": self.list_invoices, "POST": self.create_invoice}),
            ("/v1/invoices/search", {"GET": self.search_invoices}),
            (
                "/v1/invoices/{id}",
                {"GET": self.get_invoice, "POST": self.update_invoice, "DELETE": self.delete_invoice},
            ),
            ("/v1/invoices/{id}/finalize", {"POST": self.finalize_invoice}),
            ("/v1/invoices/{id}/pay", {"POST": self.pay_invoice}),
            ("/v1/invoices/{id}/send", {"POST": self.send_invoice}),
            ("/v1/invoices/{id}/void", {"POST": self.void_invoice}),
            ("/v1/invoices/{id}/mark_uncollectible", {"POST": self.mark_invoice_uncollectible}),
            ("/v1/charges", {"GET": self.list_charges, "POST": self.create_charge}),
            ("/v1/charges/search", {"GET": self.search_charges}),
            ("/v1/charges/{id}", {"GET": self.get_charge, "POST": self.update_charge}),
            ("/v1/charges/{id}/capture", {"POST": self.capture_charge}),
            ("/v1/payment_intents", {"GET": self.list_payment_intents, "POST": self.create_payment_intent}),
            ("/v1/payment_intents/search", {"GET": self.search_payment_intents}),
            ("/v1/payment_intents/{id}", {"GET": self.get_payment_intent, "POST": self.update_payment_intent}),
            ("/v1/payment_intents/{id}/confirm", {"POST": self.confirm_payment_intent}),
            ("/v1/payment_intents/{id}/capture", {"POST": self.capture_payment_intent}),
            ("/v1/payment_intents/{id}/cancel", {"POST": self.cancel_payment_intent}),
            ("/v1/refunds", {"GET": self.list_refunds, "POST": self.create_refund}),
            ("/v1/refunds/{id}", {"GET": self.get_refund, "POST": self.update_refund}),
            ("/v1/billing/meters", {"GET": self.list_meters, "POST": self.create_meter}),
            ("/v1/billing/meters/{id}", {"GET": self.get_meter, "POST": self.update_meter}),
            ("/v1/billing/meters/{id}/deactivate", {"POST": self.deactivate_meter}),
            ("/v1/billing/meters/{id}/reactivate", {"POST": self.reactivate_meter}),
            ("/v1/billing/meter_events", {"POST": self.create_meter_event}),
            ("/v1/events", {"GET": self.list_events}),
            ("/v1/events/{id}", {"GET": self.get_event}),
        ]
        routes = [Route(path, self._endpoint(handlers), methods=_ALL_METHODS) for path, handlers in table]
        routes.append(Route("/{path:path}", self._endpoint({}), methods=_ALL_METHODS))
        routes.append(Route("/", self._endpoint({}), methods=_ALL_METHODS))
        return routes

    def _endpoint(self, handlers: Mapping[str, Handler]) -> Callable[[Request], Any]:
        async def endpoint(request: Request) -> Response:
            return await self.dispatch(request, handlers)

        return endpoint

    async def dispatch(self, request: Request, handlers: Mapping[str, Handler]) -> Response:
        request_id = self.store.next_request_id()
        method = request.method.upper()
        headers = {
            "Request-Id": request_id,
            "Stripe-Version": request.headers.get("stripe-version", STRIPE_API_VERSION),
        }
        idempotency_key = request.headers.get("idempotency-key")
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            self._authenticate(request)
            handler = handlers.get(method)
            if method in {"GET", "HEAD"}:
                params = read_query(request)
            else:
                params = read_query(request)
                params.update(await read_params(request))
            if handler is None:
                handler = self._fallback(request)
            ctx = Ctx(
                request,
                params=params,
                path_params=cast(Mapping[str, str], request.path_params),
                request_id=request_id,
                idempotency_key=idempotency_key if method in {"POST", "DELETE"} else None,
            )
            if ctx.idempotency_key:
                cached = self._replay(ctx)
                if cached is not None:
                    headers["Idempotent-Replayed"] = "true"
                    headers["Original-Request"] = str(cached.get("request_id"))
                    return self._respond(cast(JsonDict, cached["body"]), int(cached["status"]), headers)
            status, payload = handler(ctx)
            payload = self.expand(payload, expand_paths(ctx.params))
            if ctx.idempotency_key:
                self._remember(ctx, status, payload)
            return self._respond(payload, status, headers)
        except StripeError as error:
            payload = error.payload(request_id)
            if idempotency_key and method in {"POST", "DELETE"} and error.error_type != "idempotency_error":
                self._remember_error(request, idempotency_key, error.status, payload, request_id)
            return self._respond(payload, error.status, headers)

    @staticmethod
    def _respond(payload: JsonDict, status: int, headers: Mapping[str, str]) -> Response:
        return JSONResponse(payload, status_code=status, headers=dict(headers))

    @staticmethod
    def _authenticate(request: Request) -> None:
        header = request.headers.get("authorization", "").strip()
        if not header:
            raise StripeError(
                401,
                "You did not provide an API key. You need to provide your API key in the Authorization header, "
                "using Bearer auth (e.g. 'Authorization: Bearer YOUR_SECRET_KEY'). See "
                "https://stripe.com/docs/api#authentication for details, or we can help at "
                "https://support.stripe.com/.",
            )
        scheme, _, token = header.partition(" ")
        if scheme.lower() not in {"bearer", "basic"} or not token.strip():
            raise StripeError(401, f"Invalid API Key provided: {header[:12]}****")

    def _fallback(self, request: Request) -> Handler:
        """Unmodelled routes: Stripe's real 404s, plus the Arga twin's lazy empty list for unknown collections."""
        method = request.method.upper()
        path = request.url.path
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) < 2 or segments[0] != "v1" or method not in {"GET", "POST", "DELETE"}:
            raise unrecognized_url(method, path)
        tail = segments[1:]
        id_like = [segment for segment in tail if _ID_TOKEN.match(segment)]
        generic_collection = tail[0] not in _MODELLED_TOP_LEVEL or (
            tail[0] == "billing" and len(tail) > 1 and tail[1] not in _MODELLED_BILLING
        )
        if method == "GET" and generic_collection and not id_like and 1 <= len(tail) <= 2:
            return self.generic_list
        if method == "GET" and generic_collection and len(tail) == 2 and _ID_TOKEN.match(tail[1]):
            prefix = tail[1].split("_", 1)[0]
            object_name = _OBJECT_BY_PREFIX.get(prefix, _object_name(tail[0]))
            raise resource_missing(object_name, tail[1])
        raise unrecognized_url(method, path)

    def generic_list(self, ctx: Ctx) -> Result:
        """The real twin materialises an empty backing collection the first time an unknown list route is read.

        `admin_delta_claims._claim_stripe` documents this exact bookkeeping (`generic_resources[path] = {}` paired
        with `counts.generic_resources`). Business collections are untouched, so protected checks are unaffected.
        """
        path = ctx.path.rstrip("/")
        if path not in self.store.generic_resources:
            self.store.generic_resources[path] = {}
        return 200, {"object": "list", "data": [], "has_more": False, "url": path}

    # -- idempotency ----------------------------------------------------------------

    def _replay(self, ctx: Ctx) -> JsonDict | None:
        assert ctx.idempotency_key is not None
        cached = self.store.idempotency_keys.get(ctx.idempotency_key)
        if cached is None:
            return None
        if cached.get("fingerprint") != _fingerprint(ctx.method, ctx.path, ctx.params):
            raise StripeError(
                400,
                "Keys for idempotent requests can only be used with the same parameters they were first used with. "
                f"Try using a key other than '{ctx.idempotency_key}' if you meant to execute a different request.",
                error_type="idempotency_error",
            )
        return cached

    def _remember(self, ctx: Ctx, status: int, payload: JsonDict) -> None:
        assert ctx.idempotency_key is not None
        self.store.idempotency_keys[ctx.idempotency_key] = {
            "method": ctx.method,
            "path": ctx.path,
            "fingerprint": _fingerprint(ctx.method, ctx.path, ctx.params),
            "status": status,
            "body": _copy(payload),
            "request_id": ctx.request_id,
        }

    def _remember_error(self, request: Request, key: str, status: int, payload: JsonDict, request_id: str) -> None:
        if key in self.store.idempotency_keys or status >= 500:
            return
        self.store.idempotency_keys[key] = {
            "method": request.method.upper(),
            "path": request.url.path,
            "fingerprint": None,
            "status": status,
            "body": _copy(payload),
            "request_id": request_id,
        }

    # -- expansion -------------------------------------------------------------------

    def expand(self, payload: JsonDict, paths: Sequence[str]) -> JsonDict:
        if not paths:
            return payload
        expanded = _copy(payload)
        for path in paths:
            self._expand_into(expanded, [part for part in path.split(".") if part])
        return cast(JsonDict, expanded)

    def _expand_into(self, node: object, parts: list[str]) -> None:
        if not parts:
            return
        if isinstance(node, list):
            for item in cast(list[object], node):
                self._expand_into(item, parts)
            return
        if not isinstance(node, dict):
            return
        typed = cast(JsonDict, node)
        head, rest = parts[0], parts[1:]
        if head == "data" and isinstance(typed.get("data"), list):
            for item in cast(list[object], typed["data"]):
                self._expand_into(item, rest)
            return
        value = typed.get(head)
        collection = _EXPANDABLE.get(head)
        if collection is not None and isinstance(value, str):
            record = self.store.collection(collection).get(value)
            if record is not None:
                typed[head] = _copy(record)
        self._expand_into(typed.get(head), rest)

    # -- shared list machinery ---------------------------------------------------------

    def paginate(self, records: Iterable[JsonDict], ctx: Ctx, *, url: str, collection: str) -> Result:
        params = ctx.params
        limit = as_int(params.get("limit", _LIST_LIMIT_DEFAULT), "limit")
        if not 1 <= limit <= _LIST_LIMIT_MAX:
            raise StripeError(
                400, f"Invalid limit: {limit}. limit must be between 1 and {_LIST_LIMIT_MAX}.", param="limit"
            )
        ordered = sorted(records, key=lambda record: int(record.get("created", 0)), reverse=True)
        ordered = _stable_newest_first(ordered, self.store.collection(collection))
        ids = [record["id"] for record in ordered]
        starting_after = params.get("starting_after")
        ending_before = params.get("ending_before")
        if starting_after is not None:
            cursor = as_str(starting_after, "starting_after")
            if cursor not in self.store.collection(collection):
                raise resource_missing(_object_name(collection), cursor, param="starting_after", status=400)
            ordered = ordered[ids.index(cursor) + 1 :] if cursor in ids else []
            page = ordered[:limit]
            has_more = len(ordered) > limit
        elif ending_before is not None:
            cursor = as_str(ending_before, "ending_before")
            if cursor not in self.store.collection(collection):
                raise resource_missing(_object_name(collection), cursor, param="ending_before", status=400)
            ordered = ordered[: ids.index(cursor)] if cursor in ids else ordered
            page = ordered[-limit:]
            has_more = len(ordered) > limit
        else:
            page = ordered[:limit]
            has_more = len(ordered) > limit
        return 200, {"object": "list", "data": [_copy(record) for record in page], "has_more": has_more, "url": url}

    def search(self, collection: str, ctx: Ctx, *, url: str) -> Result:
        params = ctx.params
        reject_unknown(params, _SEARCH_PARAMS)
        if "query" not in params:
            raise parameter_missing("query")
        query = SearchQuery(as_str(params["query"], "query"), collection)
        limit = as_int(params.get("limit", _LIST_LIMIT_DEFAULT), "limit")
        if not 1 <= limit <= _LIST_LIMIT_MAX:
            raise StripeError(
                400, f"Invalid limit: {limit}. limit must be between 1 and {_LIST_LIMIT_MAX}.", param="limit"
            )
        offset = as_int(params.get("page", 0), "page") if params.get("page") not in (None, "") else 0
        records = self.store.collection(collection)
        ordered = _stable_newest_first(
            sorted(records.values(), key=lambda record: int(record.get("created", 0)), reverse=True), records
        )
        matched = [record for record in ordered if query.matches(record)]
        page = matched[offset : offset + limit]
        has_more = len(matched) > offset + limit
        result: JsonDict = {
            "object": "search_result",
            "data": [_copy(record) for record in page],
            "has_more": has_more,
            "next_page": str(offset + limit) if has_more else None,
            "url": url,
        }
        if "total_count" in expand_paths(params):
            result["total_count"] = len(matched)
        return 200, result

    @staticmethod
    def _created_filter(records: Iterable[JsonDict], value: object) -> list[JsonDict]:
        if value is None:
            return list(records)
        if isinstance(value, dict):
            bounds = cast(JsonDict, value)
            checks: list[Callable[[int], bool]] = []
            for key, raw in bounds.items():
                bound = as_int(raw, f"created[{key}]")
                if key == "gt":
                    checks.append(lambda created, bound=bound: created > bound)
                elif key == "gte":
                    checks.append(lambda created, bound=bound: created >= bound)
                elif key == "lt":
                    checks.append(lambda created, bound=bound: created < bound)
                elif key == "lte":
                    checks.append(lambda created, bound=bound: created <= bound)
                else:
                    raise parameter_unknown(f"created[{key}]")
            return [record for record in records if all(check(int(record.get("created", 0))) for check in checks)]
        exact = as_int(value, "created")
        return [record for record in records if int(record.get("created", 0)) == exact]

    # -- writes ------------------------------------------------------------------------

    def _write(self, ctx: Ctx, *, collection: str, record_id: str, before: object, after: object) -> None:
        self.store.write(
            method=ctx.method, path=ctx.path, collection=collection, record_id=record_id, before=before, after=after
        )

    def _emit(self, ctx: Ctx, event_type: str, payload: JsonDict, *, previous: JsonDict | None = None) -> None:
        self.store.emit(
            event_type,
            payload,
            request_id=ctx.request_id,
            idempotency_key=ctx.idempotency_key,
            previous_attributes=previous,
        )

    # -- account -----------------------------------------------------------------------

    def get_account(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        return 200, {
            "id": self.store.account_id,
            "object": "account",
            "business_profile": {"name": "Twin Commerce", "support_email": None, "url": None},
            "business_type": "company",
            "capabilities": {"card_payments": "active", "transfers": "active"},
            "charges_enabled": True,
            "country": "US",
            "created": self.store.clock.epoch() - _SEED_AGE_SECONDS,
            "default_currency": "usd",
            "details_submitted": True,
            "email": "billing@twin-commerce.example",
            "payouts_enabled": True,
            "settings": {"dashboard": {"display_name": "Twin Commerce", "timezone": "Etc/UTC"}},
            "type": "standard",
        }

    # -- customers ---------------------------------------------------------------------

    def list_customers(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON | {"email", "test_clock"})
        records = list(self.store.customers.values())
        email = ctx.params.get("email")
        if email is not None:
            wanted = as_str(email, "email")
            records = [record for record in records if record.get("email") == wanted]
        records = self._created_filter(records, ctx.params.get("created"))
        return self.paginate(records, ctx, url="/v1/customers", collection="customers")

    def search_customers(self, ctx: Ctx) -> Result:
        return self.search("customers", ctx, url="/v1/customers/search")

    def get_customer(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        identifier = ctx.arg("id")
        deleted = self.store.deleted_customers.get(identifier)
        if deleted is not None:
            return 200, _copy(deleted)
        return 200, _copy(self.store.require("customers", identifier))

    def create_customer(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _CUSTOMER_CREATE)
        created = self.store.next_epoch()
        customer = self.store.build_customer(created=created, name=None, email=None)
        self._apply_customer_params(ctx, customer)
        self._write(ctx, collection="customers", record_id=customer["id"], before=None, after=customer)
        self.store.customers[customer["id"]] = customer
        self._emit(ctx, "customer.created", customer)
        for raw in as_list(ctx.params.get("tax_id_data")):
            if isinstance(raw, dict):
                typed = cast(JsonDict, raw)
                self._add_tax_id(
                    ctx,
                    customer,
                    as_str(typed.get("type", ""), "tax_id_data[type]"),
                    as_str(typed.get("value", ""), "tax_id_data[value]"),
                )
        return 200, _copy(customer)

    def update_customer(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _CUSTOMER_UPDATE)
        customer = self.store.require("customers", ctx.arg("id"))
        before = _copy(customer)
        self._apply_customer_params(ctx, customer)
        self._write(ctx, collection="customers", record_id=customer["id"], before=before, after=customer)
        self._emit(ctx, "customer.updated", customer, previous=_previous(before, customer))
        return 200, _copy(customer)

    def delete_customer(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        customer = self.store.require("customers", ctx.arg("id"))
        before = _copy(customer)
        del self.store.customers[customer["id"]]
        stub: JsonDict = {"id": customer["id"], "object": "customer", "deleted": True}
        self.store.deleted_customers[customer["id"]] = stub
        self._write(ctx, collection="customers", record_id=customer["id"], before=before, after=None)
        self._emit(ctx, "customer.deleted", before)
        for subscription in list(self.store.subscriptions.values()):
            if subscription.get("customer") == customer["id"] and subscription.get("status") != "canceled":
                self._cancel(ctx, subscription, details=None)
        for tax_id in [record for record in self.store.tax_ids.values() if record.get("customer") == customer["id"]]:
            del self.store.tax_ids[tax_id["id"]]
        return 200, stub

    def _apply_customer_params(self, ctx: Ctx, customer: JsonDict) -> None:
        params = ctx.params
        for field in ("name", "description", "phone", "invoice_prefix"):
            if field in params:
                customer[field] = ctx.str_or_none(field)
        if "email" in params:
            email = ctx.str_or_none("email")
            if email is not None and not _EMAIL.match(email):
                raise StripeError(400, f"Invalid email address: {email}", code="email_invalid", param="email")
            customer["email"] = email
        if "metadata" in params:
            customer["metadata"] = merge_metadata(cast(JsonDict, customer.get("metadata") or {}), params["metadata"])
        for field in ("address", "shipping"):
            if field in params:
                customer[field] = _optional_hash(params[field], field)
        if "tax_exempt" in params:
            value = as_str(params["tax_exempt"], "tax_exempt")
            if value not in {"none", "exempt", "reverse"}:
                raise StripeError(
                    400, f"Invalid tax_exempt: {value}. Must be one of none, exempt, or reverse.", param="tax_exempt"
                )
            customer["tax_exempt"] = value
        if "balance" in params:
            customer["balance"] = as_int(params["balance"], "balance")
        if "next_invoice_sequence" in params:
            customer["next_invoice_sequence"] = as_int(params["next_invoice_sequence"], "next_invoice_sequence")
        if "preferred_locales" in params:
            customer["preferred_locales"] = [
                as_str(item, "preferred_locales") for item in as_list(params["preferred_locales"])
            ]
        if "invoice_settings" in params:
            settings = cast(JsonDict, customer.get("invoice_settings") or {})
            for key, value in as_dict(params["invoice_settings"], "invoice_settings").items():
                settings[key] = None if value == "" else value
            customer["invoice_settings"] = settings
        if "default_source" in params:
            customer["default_source"] = ctx.str_or_none("default_source")
        if "source" in params and params["source"]:
            customer["default_source"] = "card_1" + det_alnum(self.store.seed_key, "card", customer["id"], length=23)

    # -- tax ids -----------------------------------------------------------------------

    def list_tax_ids(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON)
        customer = self.store.require("customers", ctx.arg("id"))
        records = [record for record in self.store.tax_ids.values() if record.get("customer") == customer["id"]]
        return self.paginate(records, ctx, url=f"/v1/customers/{customer['id']}/tax_ids", collection="tax_ids")

    def get_tax_id(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        customer = self.store.require("customers", ctx.arg("id"))
        tax_id = self.store.require("tax_ids", ctx.arg("tax_id"))
        if tax_id.get("customer") != customer["id"]:
            raise resource_missing("tax_id", tax_id["id"])
        return 200, _copy(tax_id)

    def create_tax_id(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _TAX_ID_CREATE)
        customer = self.store.require("customers", ctx.arg("id"))
        for field in ("type", "value"):
            if not ctx.params.get(field):
                raise parameter_missing(field)
        tax_id = self._add_tax_id(
            ctx, customer, as_str(ctx.params["type"], "type"), as_str(ctx.params["value"], "value")
        )
        return 200, _copy(tax_id)

    def _add_tax_id(self, ctx: Ctx, customer: JsonDict, tax_type: str, value: str) -> JsonDict:
        if not _TAX_ID_TYPE.match(tax_type):
            raise StripeError(400, f"Invalid tax ID type: {tax_type}", param="type")
        if not value.strip():
            raise parameter_missing("value")
        tax_id = self.store.build_tax_id(
            created=self.store.next_epoch(), customer_id=customer["id"], tax_type=tax_type, value=value.strip()
        )
        self._write(ctx, collection="tax_ids", record_id=tax_id["id"], before=None, after=tax_id)
        self.store.tax_ids[tax_id["id"]] = tax_id
        self._emit(ctx, "customer.tax_id.created", tax_id)
        return tax_id

    def delete_tax_id(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        customer = self.store.require("customers", ctx.arg("id"))
        tax_id = self.store.require("tax_ids", ctx.arg("tax_id"))
        if tax_id.get("customer") != customer["id"]:
            raise resource_missing("tax_id", tax_id["id"])
        before = _copy(tax_id)
        del self.store.tax_ids[tax_id["id"]]
        self._write(ctx, collection="tax_ids", record_id=tax_id["id"], before=before, after=None)
        self._emit(ctx, "customer.tax_id.deleted", before)
        return 200, {"id": tax_id["id"], "object": "tax_id", "deleted": True}

    def list_customer_payment_methods(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON | {"type", "allow_redisplay"})
        customer = self.store.require("customers", ctx.arg("id"))
        return 200, {
            "object": "list",
            "data": [],
            "has_more": False,
            "url": f"/v1/customers/{customer['id']}/payment_methods",
        }

    def list_customer_balance_transactions(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON)
        customer = self.store.require("customers", ctx.arg("id"))
        return 200, {
            "object": "list",
            "data": [],
            "has_more": False,
            "url": f"/v1/customers/{customer['id']}/balance_transactions",
        }

    # -- products ----------------------------------------------------------------------

    def list_products(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON | {"active", "ids", "shippable", "url", "type"})
        records = list(self.store.products.values())
        if "active" in ctx.params:
            active = as_bool(ctx.params["active"], "active")
            records = [record for record in records if bool(record.get("active")) is active]
        if "ids" in ctx.params:
            wanted = {as_str(item, "ids") for item in as_list(ctx.params["ids"])}
            records = [record for record in records if record["id"] in wanted]
        if "shippable" in ctx.params:
            shippable = as_bool(ctx.params["shippable"], "shippable")
            records = [record for record in records if record.get("shippable") is shippable]
        if "url" in ctx.params:
            records = [record for record in records if record.get("url") == as_str(ctx.params["url"], "url")]
        records = self._created_filter(records, ctx.params.get("created"))
        return self.paginate(records, ctx, url="/v1/products", collection="products")

    def search_products(self, ctx: Ctx) -> Result:
        return self.search("products", ctx, url="/v1/products/search")

    def get_product(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        return 200, _copy(self.store.require("products", ctx.arg("id")))

    def create_product(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _PRODUCT_CREATE)
        if not ctx.params.get("name"):
            raise parameter_missing("name")
        product_id = ctx.str_or_none("id")
        if product_id is not None and product_id in self.store.products:
            raise StripeError(400, f"Product already exists: {product_id}", code="resource_already_exists", param="id")
        product = self.store.build_product(
            created=self.store.next_epoch(), name=as_str(ctx.params["name"], "name"), product_id=product_id
        )
        self._apply_product_params(ctx, product, creating=True)
        self._write(ctx, collection="products", record_id=product["id"], before=None, after=product)
        self.store.products[product["id"]] = product
        self._emit(ctx, "product.created", product)
        if "default_price_data" in ctx.params:
            data = as_dict(ctx.params["default_price_data"], "default_price_data")
            price = self._new_price(ctx, product, data, param_prefix="default_price_data")
            before = _copy(product)
            product["default_price"] = price["id"]
            product["updated"] = self.store.next_epoch()
            self._write(ctx, collection="products", record_id=product["id"], before=before, after=product)
            self._emit(ctx, "product.updated", product, previous=_previous(before, product))
        return 200, _copy(product)

    def update_product(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _PRODUCT_UPDATE)
        product = self.store.require("products", ctx.arg("id"))
        before = _copy(product)
        self._apply_product_params(ctx, product, creating=False)
        product["updated"] = self.store.next_epoch()
        self._write(ctx, collection="products", record_id=product["id"], before=before, after=product)
        self._emit(ctx, "product.updated", product, previous=_previous(before, product))
        return 200, _copy(product)

    def delete_product(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        product = self.store.require("products", ctx.arg("id"))
        if any(price.get("product") == product["id"] for price in self.store.prices.values()):
            raise StripeError(
                400,
                f"The product {product['id']} cannot be deleted because it has one or more user-created prices. "
                "Archive the product instead by setting active to false.",
                param="id",
            )
        before = _copy(product)
        del self.store.products[product["id"]]
        self._write(ctx, collection="products", record_id=product["id"], before=before, after=None)
        self._emit(ctx, "product.deleted", before)
        return 200, {"id": product["id"], "object": "product", "deleted": True}

    def _apply_product_params(self, ctx: Ctx, product: JsonDict, *, creating: bool) -> None:
        params = ctx.params
        if "name" in params and not creating:
            name = ctx.str_or_none("name")
            if name is None:
                raise StripeError(
                    400, "This value must be a non-empty string.", code="parameter_invalid_empty", param="name"
                )
            product["name"] = name
        if "active" in params:
            product["active"] = as_bool(params["active"], "active")
        for field in ("description", "statement_descriptor", "tax_code", "unit_label", "url"):
            if field in params:
                product[field] = ctx.str_or_none(field)
        if "shippable" in params:
            product["shippable"] = as_bool(params["shippable"], "shippable")
        if "metadata" in params:
            product["metadata"] = merge_metadata(cast(JsonDict, product.get("metadata") or {}), params["metadata"])
        if "images" in params:
            product["images"] = [as_str(item, "images") for item in as_list(params["images"])]
        if "marketing_features" in params:
            product["marketing_features"] = [
                {"name": as_str(cast(JsonDict, item).get("name", ""), "marketing_features[name]")}
                for item in as_list(params["marketing_features"])
                if isinstance(item, dict)
            ]
        if "package_dimensions" in params:
            product["package_dimensions"] = _optional_hash(params["package_dimensions"], "package_dimensions")
        if "default_price" in params:
            default_price = ctx.str_or_none("default_price")
            if default_price is not None:
                price = self.store.require("prices", default_price, param="default_price")
                if price.get("product") != product["id"]:
                    raise StripeError(
                        400,
                        f"The price {default_price} does not belong to product {product['id']}.",
                        param="default_price",
                    )
            product["default_price"] = default_price

    # -- prices ------------------------------------------------------------------------

    def list_prices(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON | {"active", "currency", "lookup_keys", "product", "recurring", "type"})
        records = list(self.store.prices.values())
        params = ctx.params
        if "active" in params:
            active = as_bool(params["active"], "active")
            records = [record for record in records if bool(record.get("active")) is active]
        if "currency" in params:
            currency = as_str(params["currency"], "currency").lower()
            records = [record for record in records if record.get("currency") == currency]
        if "product" in params:
            product_id = as_str(params["product"], "product")
            self.store.require("products", product_id, param="product")
            records = [record for record in records if record.get("product") == product_id]
        if "type" in params:
            price_type = as_str(params["type"], "type")
            records = [record for record in records if record.get("type") == price_type]
        if "lookup_keys" in params:
            keys = {as_str(item, "lookup_keys") for item in as_list(params["lookup_keys"])}
            records = [record for record in records if record.get("lookup_key") in keys]
        if "recurring" in params:
            recurring = as_dict(params["recurring"], "recurring")
            for key in ("interval", "usage_type", "meter"):
                if key in recurring:
                    wanted = as_str(recurring[key], f"recurring[{key}]")
                    records = [
                        record
                        for record in records
                        if isinstance(record.get("recurring"), dict)
                        and cast(JsonDict, record["recurring"]).get(key) == wanted
                    ]
        records = self._created_filter(records, params.get("created"))
        return self.paginate(records, ctx, url="/v1/prices", collection="prices")

    def search_prices(self, ctx: Ctx) -> Result:
        return self.search("prices", ctx, url="/v1/prices/search")

    def get_price(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        return 200, _copy(self.store.require("prices", ctx.arg("id")))

    def create_price(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _PRICE_CREATE)
        params = ctx.params
        if "product" in params and "product_data" in params:
            raise StripeError(
                400, "You may only specify one of these parameters: product, product_data.", param="product"
            )
        if "product" in params:
            product = self.store.require("products", as_str(params["product"], "product"), param="product")
        elif "product_data" in params:
            data = as_dict(params["product_data"], "product_data")
            if not data.get("name"):
                raise parameter_missing("product_data[name]")
            product = self.store.build_product(
                created=self.store.next_epoch(),
                name=as_str(data["name"], "product_data[name]"),
                active=as_bool(data.get("active", True), "product_data[active]"),
                metadata=_str_map(data.get("metadata")),
                statement_descriptor=_opt_str(data.get("statement_descriptor")),
                tax_code=_opt_str(data.get("tax_code")),
                unit_label=_opt_str(data.get("unit_label")),
            )
            self._write(ctx, collection="products", record_id=product["id"], before=None, after=product)
            self.store.products[product["id"]] = product
            self._emit(ctx, "product.created", product)
        else:
            raise parameter_missing("product")
        price = self._new_price(ctx, product, params, param_prefix=None)
        return 200, _copy(price)

    def _new_price(self, ctx: Ctx, product: JsonDict, data: Mapping[str, Any], *, param_prefix: str | None) -> JsonDict:
        def name(field: str) -> str:
            return f"{param_prefix}[{field}]" if param_prefix else field

        if not data.get("currency"):
            raise parameter_missing(name("currency"))
        currency = as_str(data["currency"], name("currency")).lower()
        if not re.fullmatch(r"[a-z]{3}", currency):
            raise StripeError(
                400,
                f"Invalid currency: {currency}. Stripe currently supports these currencies: usd, eur, gbp, …",
                param=name("currency"),
            )
        unit_amount = (
            as_int(data["unit_amount"], name("unit_amount")) if data.get("unit_amount") not in (None, "") else None
        )
        decimal = _opt_str(data.get("unit_amount_decimal"))
        custom_unit_amount = _optional_hash(data.get("custom_unit_amount"), name("custom_unit_amount"))
        billing_scheme = as_str(data.get("billing_scheme", "per_unit"), name("billing_scheme"))
        if billing_scheme not in {"per_unit", "tiered"}:
            raise StripeError(
                400,
                f"Invalid billing_scheme: {billing_scheme}. Must be one of per_unit or tiered.",
                param=name("billing_scheme"),
            )
        if billing_scheme == "per_unit" and unit_amount is None and decimal is None and custom_unit_amount is None:
            raise parameter_missing(name("unit_amount"))
        if unit_amount is None and decimal is not None:
            unit_amount = int(float(decimal)) if re.fullmatch(r"\d+(\.\d+)?", decimal) else None
        recurring_raw = data.get("recurring")
        recurring: JsonDict | None = None
        if recurring_raw not in (None, ""):
            recurring = as_dict(recurring_raw, name("recurring"))
            interval = as_str(recurring.get("interval", ""), name("recurring[interval]"))
            if interval not in _INTERVAL_SECONDS:
                raise StripeError(
                    400,
                    f"Invalid recurring[interval]: {interval}. Must be one of day, week, month, or year.",
                    param=name("recurring[interval]"),
                )
            usage_type = as_str(recurring.get("usage_type", "licensed"), name("recurring[usage_type]"))
            if usage_type not in {"licensed", "metered"}:
                raise StripeError(
                    400, f"Invalid recurring[usage_type]: {usage_type}.", param=name("recurring[usage_type]")
                )
            if recurring.get("meter"):
                self.store.require(
                    "meters", as_str(recurring["meter"], name("recurring[meter]")), param=name("recurring[meter]")
                )
        lookup_key = _opt_str(data.get("lookup_key")) or None
        if lookup_key is not None:
            self._release_lookup_key(
                ctx,
                lookup_key,
                transfer=as_bool(data.get("transfer_lookup_key", False), name("transfer_lookup_key")),
                param=name("lookup_key"),
            )
        tax_behavior = as_str(data.get("tax_behavior", "unspecified"), name("tax_behavior"))
        if tax_behavior not in {"unspecified", "inclusive", "exclusive"}:
            raise StripeError(400, f"Invalid tax_behavior: {tax_behavior}.", param=name("tax_behavior"))
        price = self.store.build_price(
            created=self.store.next_epoch(),
            product_id=product["id"],
            currency=currency,
            unit_amount=unit_amount,
            recurring=recurring,
            nickname=_opt_str(data.get("nickname")) or None,
            lookup_key=lookup_key,
            active=as_bool(data.get("active", True), name("active")),
            metadata=_str_map(data.get("metadata")),
            billing_scheme=billing_scheme,
            tax_behavior=tax_behavior,
            unit_amount_decimal=decimal,
            custom_unit_amount=custom_unit_amount,
            tiers_mode=_opt_str(data.get("tiers_mode")) or None,
            transform_quantity=_optional_hash(data.get("transform_quantity"), name("transform_quantity")),
        )
        self._write(ctx, collection="prices", record_id=price["id"], before=None, after=price)
        self.store.prices[price["id"]] = price
        self._emit(ctx, "price.created", price)
        return price

    def _release_lookup_key(self, ctx: Ctx, lookup_key: str, *, transfer: bool, param: str) -> None:
        holder = next((price for price in self.store.prices.values() if price.get("lookup_key") == lookup_key), None)
        if holder is None:
            return
        if not transfer:
            raise StripeError(
                400,
                f"The lookup_key `{lookup_key}` is already in use by price {holder['id']}. Pass transfer_lookup_key=true "  # noqa: E501
                "to move it to the new price.",
                param=param,
            )
        before = _copy(holder)
        holder["lookup_key"] = None
        self._write(ctx, collection="prices", record_id=holder["id"], before=before, after=holder)
        self._emit(ctx, "price.updated", holder, previous=_previous(before, holder))

    def update_price(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _PRICE_UPDATE)
        price = self.store.require("prices", ctx.arg("id"))
        params = ctx.params
        before = _copy(price)
        if "active" in params:
            price["active"] = as_bool(params["active"], "active")
        if "nickname" in params:
            price["nickname"] = ctx.str_or_none("nickname")
        if "lookup_key" in params:
            lookup_key = ctx.str_or_none("lookup_key")
            if lookup_key is not None and lookup_key != price.get("lookup_key"):
                self._release_lookup_key(
                    ctx,
                    lookup_key,
                    transfer=as_bool(params.get("transfer_lookup_key", False), "transfer_lookup_key"),
                    param="lookup_key",
                )
            price["lookup_key"] = lookup_key
        if "tax_behavior" in params:
            tax_behavior = as_str(params["tax_behavior"], "tax_behavior")
            if tax_behavior not in {"unspecified", "inclusive", "exclusive"}:
                raise StripeError(400, f"Invalid tax_behavior: {tax_behavior}.", param="tax_behavior")
            price["tax_behavior"] = tax_behavior
        if "metadata" in params:
            price["metadata"] = merge_metadata(cast(JsonDict, price.get("metadata") or {}), params["metadata"])
        self._write(ctx, collection="prices", record_id=price["id"], before=before, after=price)
        self._emit(ctx, "price.updated", price, previous=_previous(before, price))
        return 200, _copy(price)

    # -- subscriptions -----------------------------------------------------------------

    def list_subscriptions(self, ctx: Ctx) -> Result:
        reject_unknown(
            ctx.params,
            _LIST_COMMON
            | {
                "automatic_tax",
                "collection_method",
                "current_period_end",
                "current_period_start",
                "customer",
                "price",
                "status",
                "test_clock",
            },
        )
        params = ctx.params
        records = list(self.store.subscriptions.values())
        status = as_str(params.get("status", ""), "status") if params.get("status") else ""
        if status == "all":
            pass
        elif status == "ended":
            records = [record for record in records if record.get("status") in {"canceled", "incomplete_expired"}]
        elif status:
            records = [record for record in records if record.get("status") == status]
        else:
            records = [record for record in records if record.get("status") not in {"canceled", "incomplete_expired"}]
        if "customer" in params:
            customer_id = as_str(params["customer"], "customer")
            self.store.require("customers", customer_id, param="customer")
            records = [record for record in records if record.get("customer") == customer_id]
        if "price" in params:
            price_id = as_str(params["price"], "price")
            records = [record for record in records if any(_item_price_id(item) == price_id for item in _items(record))]
        if "collection_method" in params:
            wanted = as_str(params["collection_method"], "collection_method")
            records = [record for record in records if record.get("collection_method") == wanted]
        records = self._created_filter(records, params.get("created"))
        return self.paginate(records, ctx, url="/v1/subscriptions", collection="subscriptions")

    def search_subscriptions(self, ctx: Ctx) -> Result:
        return self.search("subscriptions", ctx, url="/v1/subscriptions/search")

    def get_subscription(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        return 200, _copy(self.store.require("subscriptions", ctx.arg("id")))

    def list_subscription_items(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON | {"subscription"})
        if not ctx.params.get("subscription"):
            raise parameter_missing("subscription")
        subscription = self.store.require(
            "subscriptions", as_str(ctx.params["subscription"], "subscription"), param="subscription"
        )
        items = [_copy(item) for item in _items(subscription)]
        return 200, {"object": "list", "data": items, "has_more": False, "url": "/v1/subscription_items"}

    def create_subscription(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _SUBSCRIPTION_CREATE)
        params = ctx.params
        if not params.get("customer"):
            raise parameter_missing("customer")
        customer = self.store.require("customers", as_str(params["customer"], "customer"), param="customer")
        raw_items = as_list(params.get("items"))
        if not raw_items:
            raise parameter_missing("items")
        items: list[tuple[JsonDict, int, JsonDict]] = []
        for index, raw in enumerate(raw_items):
            item = as_dict(raw, f"items[{index}]")
            if not item.get("price"):
                raise parameter_missing(f"items[{index}][price]")
            price = self.store.require(
                "prices", as_str(item["price"], f"items[{index}][price]"), param=f"items[{index}][price]"
            )
            if price.get("type") != "recurring":
                raise StripeError(
                    400,
                    f"The price specified at items[{index}][price] is a one-time price; subscriptions require recurring prices.",  # noqa: E501
                    param=f"items[{index}][price]",
                )
            quantity = as_int(item.get("quantity", 1), f"items[{index}][quantity]")
            items.append((price, quantity, _str_map(item.get("metadata"))))
        collection_method = as_str(params.get("collection_method", "charge_automatically"), "collection_method")
        if collection_method not in {"charge_automatically", "send_invoice"}:
            raise StripeError(400, f"Invalid collection_method: {collection_method}.", param="collection_method")
        days_until_due = (
            as_int(params["days_until_due"], "days_until_due")
            if params.get("days_until_due") not in (None, "")
            else None
        )
        if collection_method == "send_invoice" and days_until_due is None:
            days_until_due = 30
        trial_days = (
            as_int(params["trial_period_days"], "trial_period_days")
            if params.get("trial_period_days") not in (None, "")
            else None
        )
        subscription = self.store.build_subscription(
            created=self.store.next_epoch(),
            customer_id=customer["id"],
            items=items,
            metadata=_str_map(params.get("metadata")),
            cancel_at_period_end=as_bool(params.get("cancel_at_period_end", False), "cancel_at_period_end"),
            collection_method=collection_method,
            days_until_due=days_until_due,
            description=_opt_str(params.get("description")) or None,
            trial_period_days=trial_days,
            default_payment_method=_opt_str(params.get("default_payment_method")) or None,
        )
        self._write(ctx, collection="subscriptions", record_id=subscription["id"], before=None, after=subscription)
        self.store.subscriptions[subscription["id"]] = subscription
        self._emit(ctx, "customer.subscription.created", subscription)
        invoice = self._subscription_invoice(ctx, customer, subscription, billing_reason="subscription_create")
        subscription["latest_invoice"] = invoice["id"]
        return 200, _copy(subscription)

    def _subscription_invoice(
        self, ctx: Ctx, customer: JsonDict, subscription: JsonDict, *, billing_reason: str
    ) -> JsonDict:
        trialing = subscription.get("status") == "trialing"
        lines = [
            self.store.build_invoice_line(
                price=cast(JsonDict, item["price"]),
                quantity=0 if trialing else int(item.get("quantity", 1)),
                subscription_id=subscription["id"],
                subscription_item_id=item["id"],
                period_start=int(subscription["current_period_start"]),
                period_end=int(subscription["current_period_end"]),
            )
            for item in _items(subscription)
        ]
        charge_automatically = subscription.get("collection_method") == "charge_automatically"
        status = "paid" if charge_automatically else "open"
        created = self.store.next_epoch()
        invoice = self.store.build_invoice(
            created=created,
            customer=customer,
            currency=str(subscription.get("currency") or "usd"),
            lines=lines,
            subscription_id=subscription["id"],
            billing_reason=billing_reason,
            collection_method=str(subscription.get("collection_method")),
            days_until_due=cast(int | None, subscription.get("days_until_due")),
            description=None,
            metadata={},
            auto_advance=True,
            status=status,
            due_date=created + int(subscription.get("days_until_due") or 0) * 86400
            if not charge_automatically
            else None,
        )
        invoice["number"] = self._next_invoice_number(ctx, customer)
        invoice["hosted_invoice_url"] = f"https://invoice.stripe.com/i/{invoice['id']}"
        invoice["invoice_pdf"] = f"https://pay.stripe.com/invoice/{invoice['id']}/pdf"
        self._write(ctx, collection="invoices", record_id=invoice["id"], before=None, after=invoice)
        self.store.invoices[invoice["id"]] = invoice
        self._emit(ctx, "invoice.created", invoice)
        if status == "paid" and invoice["amount_due"] > 0:
            self._emit(ctx, "invoice.paid", invoice)
        return invoice

    def _next_invoice_number(self, ctx: Ctx, customer: JsonDict) -> str:
        before = _copy(customer)
        sequence = int(customer.get("next_invoice_sequence", 1))
        customer["next_invoice_sequence"] = sequence + 1
        self._write(ctx, collection="customers", record_id=customer["id"], before=before, after=customer)
        self._emit(ctx, "customer.updated", customer, previous=_previous(before, customer))
        return f"{customer.get('invoice_prefix')}-{sequence:04d}"

    def update_subscription(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _SUBSCRIPTION_UPDATE)
        subscription = self.store.require("subscriptions", ctx.arg("id"))
        if subscription.get("status") == "canceled":
            raise StripeError(400, "This subscription has been canceled and cannot be updated.", param="id")
        params = ctx.params
        before = _copy(subscription)
        if "items" in params:
            self._apply_subscription_items(ctx, subscription, as_list(params["items"]))
        if "cancel_at_period_end" in params:
            flag = as_bool(params["cancel_at_period_end"], "cancel_at_period_end")
            subscription["cancel_at_period_end"] = flag
            subscription["cancel_at"] = subscription["current_period_end"] if flag else None
            subscription["canceled_at"] = self.store.next_epoch() if flag else None
        if "cancel_at" in params:
            cancel_at = params["cancel_at"]
            subscription["cancel_at"] = None if cancel_at in ("", None) else as_int(cancel_at, "cancel_at")
        if "metadata" in params:
            subscription["metadata"] = merge_metadata(
                cast(JsonDict, subscription.get("metadata") or {}), params["metadata"]
            )
        if "description" in params:
            subscription["description"] = ctx.str_or_none("description")
        if "default_payment_method" in params:
            subscription["default_payment_method"] = ctx.str_or_none("default_payment_method")
        if "collection_method" in params:
            subscription["collection_method"] = as_str(params["collection_method"], "collection_method")
        if "days_until_due" in params:
            subscription["days_until_due"] = (
                as_int(params["days_until_due"], "days_until_due")
                if params["days_until_due"] not in ("", None)
                else None
            )
        if "pause_collection" in params:
            subscription["pause_collection"] = _optional_hash(params["pause_collection"], "pause_collection")
        if "trial_end" in params:
            trial_end = params["trial_end"]
            subscription["trial_end"] = None if trial_end in ("", "now") else as_int(trial_end, "trial_end")
            if subscription["trial_end"] is None and subscription.get("status") == "trialing":
                subscription["status"] = "active"
        if "cancellation_details" in params:
            details = as_dict(params["cancellation_details"], "cancellation_details")
            subscription["cancellation_details"] = {
                "comment": _opt_str(details.get("comment")) or None,
                "feedback": _opt_str(details.get("feedback")) or None,
                "reason": None,
            }
        self._write(ctx, collection="subscriptions", record_id=subscription["id"], before=before, after=subscription)
        self._emit(ctx, "customer.subscription.updated", subscription, previous=_previous(before, subscription))
        return 200, _copy(subscription)

    def _apply_subscription_items(self, ctx: Ctx, subscription: JsonDict, raw_items: list[Any]) -> None:
        items = _items(subscription)
        by_id = {item["id"]: item for item in items}
        for index, raw in enumerate(raw_items):
            item = as_dict(raw, f"items[{index}]")
            item_id = _opt_str(item.get("id"))
            existing = by_id.get(item_id) if item_id else None
            if item_id and existing is None:
                raise resource_missing("subscription_item", item_id, param=f"items[{index}][id]", status=400)
            if existing is not None and as_bool(item.get("deleted", False), f"items[{index}][deleted]"):
                items.remove(existing)
                continue
            price: JsonDict | None = None
            if item.get("price"):
                price = self.store.require(
                    "prices", as_str(item["price"], f"items[{index}][price]"), param=f"items[{index}][price]"
                )
            if existing is None:
                if price is None:
                    raise parameter_missing(f"items[{index}][price]")
                items.append(
                    self.store.build_subscription_item(
                        created=self.store.next_epoch(),
                        subscription_id=subscription["id"],
                        price=price,
                        quantity=as_int(item.get("quantity", 1), f"items[{index}][quantity]"),
                        metadata=_str_map(item.get("metadata")),
                    )
                )
                continue
            if price is not None:
                existing["price"] = _copy(price)
                existing["plan"] = _plan_from_price(price)
            if "quantity" in item:
                existing["quantity"] = as_int(item["quantity"], f"items[{index}][quantity]")
            if "metadata" in item:
                existing["metadata"] = merge_metadata(cast(JsonDict, existing.get("metadata") or {}), item["metadata"])
        if not items:
            raise StripeError(400, "A subscription must have at least one item.", param="items")
        envelope = cast(JsonDict, subscription["items"])
        envelope["data"] = items
        envelope["total_count"] = len(items)
        first_price = cast(JsonDict, items[0]["price"])
        subscription["currency"] = first_price.get("currency")

    def cancel_subscription(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _SUBSCRIPTION_CANCEL)
        subscription = self.store.require("subscriptions", ctx.arg("id"))
        if subscription.get("status") == "canceled":
            raise StripeError(400, f"No such subscription: '{subscription['id']}'", code="resource_missing", param="id")
        details = (
            as_dict(ctx.params["cancellation_details"], "cancellation_details")
            if "cancellation_details" in ctx.params
            else None
        )
        self._cancel(ctx, subscription, details=details)
        return 200, _copy(subscription)

    def _cancel(self, ctx: Ctx, subscription: JsonDict, *, details: JsonDict | None) -> None:
        before = _copy(subscription)
        now = self.store.next_epoch()
        subscription["status"] = "canceled"
        subscription["canceled_at"] = now
        subscription["ended_at"] = now
        subscription["cancel_at_period_end"] = False
        subscription["cancellation_details"] = {
            "comment": _opt_str(details.get("comment")) if details else None,
            "feedback": _opt_str(details.get("feedback")) if details else None,
            "reason": "cancellation_requested",
        }
        self._write(ctx, collection="subscriptions", record_id=subscription["id"], before=before, after=subscription)
        self._emit(ctx, "customer.subscription.deleted", subscription, previous=_previous(before, subscription))

    # -- invoices ----------------------------------------------------------------------

    def list_invoices(self, ctx: Ctx) -> Result:
        reject_unknown(
            ctx.params, _LIST_COMMON | {"collection_method", "customer", "due_date", "status", "subscription"}
        )
        params = ctx.params
        records = list(self.store.invoices.values())
        if "customer" in params:
            customer_id = as_str(params["customer"], "customer")
            self.store.require("customers", customer_id, param="customer")
            records = [record for record in records if record.get("customer") == customer_id]
        if "subscription" in params:
            subscription_id = as_str(params["subscription"], "subscription")
            records = [record for record in records if record.get("subscription") == subscription_id]
        if "status" in params:
            status = as_str(params["status"], "status")
            if status not in {"draft", "open", "paid", "uncollectible", "void"}:
                raise StripeError(
                    400,
                    f"Invalid status: {status}. Must be one of draft, open, paid, uncollectible, or void.",
                    param="status",
                )
            records = [record for record in records if record.get("status") == status]
        if "collection_method" in params:
            wanted = as_str(params["collection_method"], "collection_method")
            records = [record for record in records if record.get("collection_method") == wanted]
        records = self._created_filter(records, params.get("created"))
        return self.paginate(records, ctx, url="/v1/invoices", collection="invoices")

    def search_invoices(self, ctx: Ctx) -> Result:
        return self.search("invoices", ctx, url="/v1/invoices/search")

    def get_invoice(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        return 200, _copy(self.store.require("invoices", ctx.arg("id")))

    def create_invoice(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _INVOICE_CREATE)
        params = ctx.params
        if not params.get("customer"):
            raise parameter_missing("customer")
        customer = self.store.require("customers", as_str(params["customer"], "customer"), param="customer")
        subscription_id = _opt_str(params.get("subscription")) or None
        if subscription_id is not None:
            subscription = self.store.require("subscriptions", subscription_id, param="subscription")
            if subscription.get("customer") != customer["id"]:
                raise StripeError(
                    400,
                    f"The subscription {subscription_id} does not belong to customer {customer['id']}.",
                    param="subscription",
                )
        collection_method = as_str(params.get("collection_method", "charge_automatically"), "collection_method")
        if collection_method not in {"charge_automatically", "send_invoice"}:
            raise StripeError(400, f"Invalid collection_method: {collection_method}.", param="collection_method")
        days_until_due = (
            as_int(params["days_until_due"], "days_until_due")
            if params.get("days_until_due") not in (None, "")
            else None
        )
        due_date = as_int(params["due_date"], "due_date") if params.get("due_date") not in (None, "") else None
        if collection_method == "send_invoice" and days_until_due is None and due_date is None:
            days_until_due = 30
        created = self.store.next_epoch()
        invoice = self.store.build_invoice(
            created=created,
            customer=customer,
            currency=as_str(params.get("currency", customer.get("currency") or "usd"), "currency").lower(),
            lines=[],
            subscription_id=subscription_id,
            billing_reason="manual",
            collection_method=collection_method,
            days_until_due=days_until_due,
            description=_opt_str(params.get("description")) or None,
            metadata=_str_map(params.get("metadata")),
            auto_advance=as_bool(params.get("auto_advance", False), "auto_advance"),
            status="draft",
            due_date=due_date
            if due_date is not None
            else (created + days_until_due * 86400 if days_until_due else None),
            footer=_opt_str(params.get("footer")) or None,
            statement_descriptor=_opt_str(params.get("statement_descriptor")) or None,
        )
        self._write(ctx, collection="invoices", record_id=invoice["id"], before=None, after=invoice)
        self.store.invoices[invoice["id"]] = invoice
        self._emit(ctx, "invoice.created", invoice)
        return 200, _copy(invoice)

    def update_invoice(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _INVOICE_UPDATE)
        invoice = self.store.require("invoices", ctx.arg("id"))
        params = ctx.params
        if invoice.get("status") in {"paid", "void", "uncollectible"} and set(params) - {"metadata", "expand"}:
            raise StripeError(400, f"This invoice is {invoice['status']} and can no longer be edited.", param="id")
        before = _copy(invoice)
        for field in ("description", "footer", "statement_descriptor", "default_payment_method", "default_source"):
            if field in params:
                invoice[field] = ctx.str_or_none(field)
        if "metadata" in params:
            invoice["metadata"] = merge_metadata(cast(JsonDict, invoice.get("metadata") or {}), params["metadata"])
        if "auto_advance" in params:
            invoice["auto_advance"] = as_bool(params["auto_advance"], "auto_advance")
        if "collection_method" in params:
            invoice["collection_method"] = as_str(params["collection_method"], "collection_method")
        if "days_until_due" in params:
            days = as_int(params["days_until_due"], "days_until_due")
            invoice["due_date"] = int(invoice["created"]) + days * 86400
        if "due_date" in params:
            invoice["due_date"] = (
                as_int(params["due_date"], "due_date") if params["due_date"] not in ("", None) else None
            )
        if "number" in params:
            invoice["number"] = ctx.str_or_none("number")
        self._write(ctx, collection="invoices", record_id=invoice["id"], before=before, after=invoice)
        self._emit(ctx, "invoice.updated", invoice, previous=_previous(before, invoice))
        return 200, _copy(invoice)

    def delete_invoice(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        invoice = self.store.require("invoices", ctx.arg("id"))
        if invoice.get("status") != "draft":
            raise StripeError(
                400,
                f"You cannot delete an invoice that has been finalized ({invoice['id']} is {invoice['status']}). "
                "Void the invoice instead.",
                param="id",
            )
        before = _copy(invoice)
        del self.store.invoices[invoice["id"]]
        self._write(ctx, collection="invoices", record_id=invoice["id"], before=before, after=None)
        self._emit(ctx, "invoice.deleted", before)
        return 200, {"id": invoice["id"], "object": "invoice", "deleted": True}

    def _transition_invoice(
        self, ctx: Ctx, invoice: JsonDict, *, event: str, mutate: Callable[[JsonDict, int], None]
    ) -> None:
        before = _copy(invoice)
        now = self.store.next_epoch()
        mutate(invoice, now)
        self._write(ctx, collection="invoices", record_id=invoice["id"], before=before, after=invoice)
        self._emit(ctx, event, invoice, previous=_previous(before, invoice))

    def _finalize(self, ctx: Ctx, invoice: JsonDict) -> None:
        customer = self.store.customers.get(str(invoice.get("customer")))
        number = self._next_invoice_number(ctx, customer) if customer is not None else f"TWIN-{invoice['id'][-4:]}"

        def mutate(record: JsonDict, now: int) -> None:
            record["status"] = "open"
            record["number"] = number
            record["effective_at"] = now
            record["ending_balance"] = 0
            record["hosted_invoice_url"] = f"https://invoice.stripe.com/i/{record['id']}"
            record["invoice_pdf"] = f"https://pay.stripe.com/invoice/{record['id']}/pdf"
            cast(JsonDict, record["status_transitions"])["finalized_at"] = now

        self._transition_invoice(ctx, invoice, event="invoice.finalized", mutate=mutate)

    def finalize_invoice(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P({"auto_advance"}))
        invoice = self.store.require("invoices", ctx.arg("id"))
        if invoice.get("status") != "draft":
            raise StripeError(400, f"This invoice is already finalized ({invoice['status']}).", param="id")
        if "auto_advance" in ctx.params:
            invoice["auto_advance"] = as_bool(ctx.params["auto_advance"], "auto_advance")
        self._finalize(ctx, invoice)
        return 200, _copy(invoice)

    def pay_invoice(self, ctx: Ctx) -> Result:
        reject_unknown(
            ctx.params, _P({"forgive", "mandate", "off_session", "paid_out_of_band", "payment_method", "source"})
        )
        invoice = self.store.require("invoices", ctx.arg("id"))
        if invoice.get("status") == "draft":
            self._finalize(ctx, invoice)
        if invoice.get("status") != "open":
            raise StripeError(
                400,
                f"This invoice is {invoice['status']} and cannot be paid.",
                param="id",
                code="invoice_not_open" if invoice.get("status") != "paid" else "invoice_already_paid",
            )
        out_of_band = as_bool(ctx.params.get("paid_out_of_band", False), "paid_out_of_band")
        charge: JsonDict | None = None
        if not out_of_band and int(invoice.get("amount_due", 0)) > 0:
            charge = self.store.build_charge(
                created=self.store.next_epoch(),
                amount=int(invoice["amount_due"]),
                currency=str(invoice.get("currency")),
                customer_id=_opt_str(invoice.get("customer")),
                description=f"Payment for invoice {invoice.get('number') or invoice['id']}",
                metadata={},
                captured=True,
                receipt_email=_opt_str(invoice.get("customer_email")),
                statement_descriptor=None,
                statement_descriptor_suffix=None,
                payment_intent_id=None,
                transfer_group=None,
                invoice_id=invoice["id"],
            )
            self._write(ctx, collection="charges", record_id=charge["id"], before=None, after=charge)
            self.store.charges[charge["id"]] = charge
            self._emit(ctx, "charge.succeeded", charge)

        def mutate(record: JsonDict, now: int) -> None:
            record["status"] = "paid"
            record["paid"] = True
            record["paid_out_of_band"] = out_of_band
            record["amount_paid"] = record["amount_due"]
            record["amount_remaining"] = 0
            record["attempted"] = True
            record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
            record["charge"] = charge["id"] if charge else None
            cast(JsonDict, record["status_transitions"])["paid_at"] = now

        self._transition_invoice(ctx, invoice, event="invoice.paid", mutate=mutate)
        return 200, _copy(invoice)

    def send_invoice(self, ctx: Ctx) -> Result:
        """Emails the customer on the real API; here it finalizes drafts and records an `invoice.sent` event."""
        reject_unknown(ctx.params, _P(set()))
        invoice = self.store.require("invoices", ctx.arg("id"))
        if invoice.get("status") == "draft":
            self._finalize(ctx, invoice)
        if invoice.get("status") not in {"open", "paid"}:
            raise StripeError(400, f"This invoice is {invoice['status']} and cannot be sent.", param="id")

        def mutate(record: JsonDict, now: int) -> None:
            record["webhooks_delivered_at"] = now

        self._transition_invoice(ctx, invoice, event="invoice.sent", mutate=mutate)
        return 200, _copy(invoice)

    def void_invoice(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        invoice = self.store.require("invoices", ctx.arg("id"))
        if invoice.get("status") not in {"open", "uncollectible"}:
            raise StripeError(
                400,
                f"Only open or uncollectible invoices can be voided; this invoice is {invoice['status']}.",
                param="id",
            )

        def mutate(record: JsonDict, now: int) -> None:
            record["status"] = "void"
            record["amount_remaining"] = 0
            record["auto_advance"] = False
            cast(JsonDict, record["status_transitions"])["voided_at"] = now

        self._transition_invoice(ctx, invoice, event="invoice.voided", mutate=mutate)
        return 200, _copy(invoice)

    def mark_invoice_uncollectible(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        invoice = self.store.require("invoices", ctx.arg("id"))
        if invoice.get("status") != "open":
            raise StripeError(
                400, f"Only open invoices can be marked uncollectible; this invoice is {invoice['status']}.", param="id"
            )

        def mutate(record: JsonDict, now: int) -> None:
            record["status"] = "uncollectible"
            cast(JsonDict, record["status_transitions"])["marked_uncollectible_at"] = now

        self._transition_invoice(ctx, invoice, event="invoice.marked_uncollectible", mutate=mutate)
        return 200, _copy(invoice)

    # -- charges -----------------------------------------------------------------------

    def list_charges(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON | {"customer", "payment_intent", "transfer_group"})
        params = ctx.params
        records = list(self.store.charges.values())
        if "customer" in params:
            customer_id = as_str(params["customer"], "customer")
            records = [record for record in records if record.get("customer") == customer_id]
        if "payment_intent" in params:
            intent_id = as_str(params["payment_intent"], "payment_intent")
            records = [record for record in records if record.get("payment_intent") == intent_id]
        if "transfer_group" in params:
            group = as_str(params["transfer_group"], "transfer_group")
            records = [record for record in records if record.get("transfer_group") == group]
        records = self._created_filter(records, params.get("created"))
        return self.paginate(records, ctx, url="/v1/charges", collection="charges")

    def search_charges(self, ctx: Ctx) -> Result:
        return self.search("charges", ctx, url="/v1/charges/search")

    def get_charge(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        return 200, _copy(self.store.require("charges", ctx.arg("id")))

    def create_charge(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _CHARGE_CREATE)
        params = ctx.params
        amount, currency = self._amount_and_currency(params)
        customer_id = _opt_str(params.get("customer")) or None
        if customer_id is not None:
            self.store.require("customers", customer_id, param="customer")
        if customer_id is None and not params.get("source"):
            raise StripeError(400, "Must provide source or customer.", code="parameter_missing", param="source")
        charge = self.store.build_charge(
            created=self.store.next_epoch(),
            amount=amount,
            currency=currency,
            customer_id=customer_id,
            description=_opt_str(params.get("description")) or None,
            metadata=_str_map(params.get("metadata")),
            captured=as_bool(params.get("capture", True), "capture"),
            receipt_email=_opt_str(params.get("receipt_email")) or None,
            statement_descriptor=_opt_str(params.get("statement_descriptor")) or None,
            statement_descriptor_suffix=_opt_str(params.get("statement_descriptor_suffix")) or None,
            payment_intent_id=None,
            transfer_group=_opt_str(params.get("transfer_group")) or None,
        )
        self._write(ctx, collection="charges", record_id=charge["id"], before=None, after=charge)
        self.store.charges[charge["id"]] = charge
        self._emit(ctx, "charge.succeeded", charge)
        return 200, _copy(charge)

    def update_charge(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _CHARGE_UPDATE)
        charge = self.store.require("charges", ctx.arg("id"))
        params = ctx.params
        before = _copy(charge)
        for field in ("description", "receipt_email", "transfer_group"):
            if field in params:
                charge[field] = ctx.str_or_none(field)
        if "customer" in params:
            customer_id = ctx.str_or_none("customer")
            if customer_id is not None:
                self.store.require("customers", customer_id, param="customer")
            charge["customer"] = customer_id
        if "metadata" in params:
            charge["metadata"] = merge_metadata(cast(JsonDict, charge.get("metadata") or {}), params["metadata"])
        if "shipping" in params:
            charge["shipping"] = _optional_hash(params["shipping"], "shipping")
        if "fraud_details" in params:
            charge["fraud_details"] = as_dict(params["fraud_details"], "fraud_details")
        self._write(ctx, collection="charges", record_id=charge["id"], before=before, after=charge)
        self._emit(ctx, "charge.updated", charge, previous=_previous(before, charge))
        return 200, _copy(charge)

    def capture_charge(self, ctx: Ctx) -> Result:
        reject_unknown(
            ctx.params,
            _P(
                {
                    "amount",
                    "application_fee_amount",
                    "receipt_email",
                    "statement_descriptor",
                    "statement_descriptor_suffix",
                    "transfer_data",
                    "transfer_group",
                }
            ),
        )
        charge = self.store.require("charges", ctx.arg("id"))
        if charge.get("captured"):
            raise StripeError(
                400, f"Charge {charge['id']} has already been captured.", code="charge_already_captured", param="id"
            )
        before = _copy(charge)
        amount = (
            as_int(ctx.params["amount"], "amount")
            if ctx.params.get("amount") not in (None, "")
            else int(charge["amount"])
        )
        if amount > int(charge["amount"]):
            raise StripeError(
                400,
                f"Capture amount ({amount}) is greater than the authorized amount ({charge['amount']}).",
                param="amount",
            )
        charge["captured"] = True
        charge["amount_captured"] = amount
        charge["balance_transaction"] = "txn_3" + det_alnum(self.store.seed_key, "txn", charge["id"], length=23)
        self._write(ctx, collection="charges", record_id=charge["id"], before=before, after=charge)
        self._emit(ctx, "charge.captured", charge, previous=_previous(before, charge))
        return 200, _copy(charge)

    @staticmethod
    def _amount_and_currency(params: Mapping[str, Any]) -> tuple[int, str]:
        if params.get("amount") in (None, ""):
            raise parameter_missing("amount")
        if not params.get("currency"):
            raise parameter_missing("currency")
        amount = as_int(params["amount"], "amount")
        if amount < 0:
            raise StripeError(
                400, f"Invalid positive integer: {amount}", code="parameter_invalid_integer", param="amount"
            )
        currency = as_str(params["currency"], "currency").lower()
        if not re.fullmatch(r"[a-z]{3}", currency):
            raise StripeError(
                400,
                f"Invalid currency: {currency}. Stripe currently supports these currencies: usd, eur, gbp, …",
                param="currency",
            )
        return amount, currency

    # -- payment intents ---------------------------------------------------------------

    def list_payment_intents(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON | {"customer"})
        records = list(self.store.payment_intents.values())
        if "customer" in ctx.params:
            customer_id = as_str(ctx.params["customer"], "customer")
            records = [record for record in records if record.get("customer") == customer_id]
        records = self._created_filter(records, ctx.params.get("created"))
        return self.paginate(records, ctx, url="/v1/payment_intents", collection="payment_intents")

    def search_payment_intents(self, ctx: Ctx) -> Result:
        return self.search("payment_intents", ctx, url="/v1/payment_intents/search")

    def get_payment_intent(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P({"client_secret"}))
        return 200, _copy(self.store.require("payment_intents", ctx.arg("id")))

    def create_payment_intent(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _PAYMENT_INTENT_CREATE)
        params = ctx.params
        amount, currency = self._amount_and_currency(params)
        customer_id = _opt_str(params.get("customer")) or None
        if customer_id is not None:
            self.store.require("customers", customer_id, param="customer")
        capture_method = as_str(params.get("capture_method", "automatic"), "capture_method")
        if capture_method not in {"automatic", "automatic_async", "manual"}:
            raise StripeError(400, f"Invalid capture_method: {capture_method}.", param="capture_method")
        automatic = _optional_hash(params.get("automatic_payment_methods"), "automatic_payment_methods")
        if automatic is not None and "enabled" in automatic:
            automatic["enabled"] = as_bool(automatic["enabled"], "automatic_payment_methods[enabled]")
        types = [as_str(item, "payment_method_types") for item in as_list(params.get("payment_method_types"))] or [
            "card"
        ]
        intent = self.store.build_payment_intent(
            created=self.store.next_epoch(),
            amount=amount,
            currency=currency,
            customer_id=customer_id,
            description=_opt_str(params.get("description")) or None,
            metadata=_str_map(params.get("metadata")),
            payment_method=_opt_str(params.get("payment_method")) or None,
            payment_method_types=types,
            capture_method=capture_method,
            receipt_email=_opt_str(params.get("receipt_email")) or None,
            statement_descriptor=_opt_str(params.get("statement_descriptor")) or None,
            setup_future_usage=_opt_str(params.get("setup_future_usage")) or None,
            automatic_payment_methods=automatic,
            transfer_group=_opt_str(params.get("transfer_group")) or None,
        )
        self._write(ctx, collection="payment_intents", record_id=intent["id"], before=None, after=intent)
        self.store.payment_intents[intent["id"]] = intent
        self._emit(ctx, "payment_intent.created", intent)
        if as_bool(params.get("confirm", False), "confirm"):
            self._confirm(ctx, intent)
        return 200, _copy(intent)

    def update_payment_intent(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _PAYMENT_INTENT_UPDATE)
        intent = self.store.require("payment_intents", ctx.arg("id"))
        params = ctx.params
        if intent.get("status") in {"succeeded", "canceled"} and set(params) - {"metadata", "expand"}:
            raise StripeError(
                400,
                f"This PaymentIntent's status is {intent['status']}; only metadata can be updated.",
                code="payment_intent_unexpected_state",
                param="id",
            )
        before = _copy(intent)
        if "amount" in params:
            intent["amount"] = as_int(params["amount"], "amount")
        if "currency" in params:
            intent["currency"] = as_str(params["currency"], "currency").lower()
        if "customer" in params:
            customer_id = ctx.str_or_none("customer")
            if customer_id is not None:
                self.store.require("customers", customer_id, param="customer")
            intent["customer"] = customer_id
        for field in (
            "description",
            "receipt_email",
            "statement_descriptor",
            "statement_descriptor_suffix",
            "transfer_group",
            "setup_future_usage",
            "payment_method",
        ):
            if field in params:
                intent[field] = ctx.str_or_none(field)
        if "metadata" in params:
            intent["metadata"] = merge_metadata(cast(JsonDict, intent.get("metadata") or {}), params["metadata"])
        if "payment_method_types" in params:
            intent["payment_method_types"] = [
                as_str(item, "payment_method_types") for item in as_list(params["payment_method_types"])
            ]
        if "capture_method" in params:
            intent["capture_method"] = as_str(params["capture_method"], "capture_method")
        if "shipping" in params:
            intent["shipping"] = _optional_hash(params["shipping"], "shipping")
        if intent.get("payment_method") and intent.get("status") == "requires_payment_method":
            intent["status"] = "requires_confirmation"
        self._write(ctx, collection="payment_intents", record_id=intent["id"], before=before, after=intent)
        self._emit(ctx, "payment_intent.updated", intent, previous=_previous(before, intent))
        return 200, _copy(intent)

    def confirm_payment_intent(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _PAYMENT_INTENT_CONFIRM)
        intent = self.store.require("payment_intents", ctx.arg("id"))
        if intent.get("status") in {"succeeded", "canceled"}:
            raise StripeError(
                400,
                f"This PaymentIntent's status is {intent['status']} and cannot be confirmed again.",
                code="payment_intent_unexpected_state",
                param="id",
            )
        if ctx.params.get("payment_method"):
            before = _copy(intent)
            intent["payment_method"] = as_str(ctx.params["payment_method"], "payment_method")
            self._write(ctx, collection="payment_intents", record_id=intent["id"], before=before, after=intent)
        self._confirm(ctx, intent)
        return 200, _copy(intent)

    def _confirm(self, ctx: Ctx, intent: JsonDict) -> None:
        before = _copy(intent)
        manual = intent.get("capture_method") == "manual"
        if not intent.get("payment_method"):
            intent["payment_method"] = "pm_1" + det_alnum(self.store.seed_key, "pm", intent["id"], length=23)
        charge = self.store.build_charge(
            created=self.store.next_epoch(),
            amount=int(intent["amount"]),
            currency=str(intent["currency"]),
            customer_id=_opt_str(intent.get("customer")),
            description=_opt_str(intent.get("description")),
            metadata=_str_map(intent.get("metadata")),
            captured=not manual,
            receipt_email=_opt_str(intent.get("receipt_email")),
            statement_descriptor=_opt_str(intent.get("statement_descriptor")),
            statement_descriptor_suffix=_opt_str(intent.get("statement_descriptor_suffix")),
            payment_intent_id=intent["id"],
            transfer_group=_opt_str(intent.get("transfer_group")),
        )
        charge["payment_method"] = intent["payment_method"]
        self._write(ctx, collection="charges", record_id=charge["id"], before=None, after=charge)
        self.store.charges[charge["id"]] = charge
        self._emit(ctx, "charge.succeeded", charge)
        intent["latest_charge"] = charge["id"]
        intent["status"] = "requires_capture" if manual else "succeeded"
        intent["amount_capturable"] = int(intent["amount"]) if manual else 0
        intent["amount_received"] = 0 if manual else int(intent["amount"])
        self._write(ctx, collection="payment_intents", record_id=intent["id"], before=before, after=intent)
        self._emit(
            ctx,
            "payment_intent.amount_capturable_updated" if manual else "payment_intent.succeeded",
            intent,
            previous=_previous(before, intent),
        )

    def capture_payment_intent(self, ctx: Ctx) -> Result:
        reject_unknown(
            ctx.params,
            _P(
                {
                    "amount_to_capture",
                    "application_fee_amount",
                    "final_capture",
                    "metadata",
                    "statement_descriptor",
                    "statement_descriptor_suffix",
                    "transfer_data",
                }
            ),
        )
        intent = self.store.require("payment_intents", ctx.arg("id"))
        if intent.get("status") != "requires_capture":
            raise StripeError(
                400,
                f"This PaymentIntent's status is {intent['status']}; only PaymentIntents in requires_capture can be captured.",  # noqa: E501
                code="payment_intent_unexpected_state",
                param="id",
            )
        before = _copy(intent)
        amount = (
            as_int(ctx.params["amount_to_capture"], "amount_to_capture")
            if ctx.params.get("amount_to_capture") not in (None, "")
            else int(intent["amount"])
        )
        intent["status"] = "succeeded"
        intent["amount_received"] = amount
        intent["amount_capturable"] = 0
        if "metadata" in ctx.params:
            intent["metadata"] = merge_metadata(cast(JsonDict, intent.get("metadata") or {}), ctx.params["metadata"])
        charge = self.store.charges.get(str(intent.get("latest_charge")))
        if charge is not None:
            charge_before = _copy(charge)
            charge["captured"] = True
            charge["amount_captured"] = amount
            self._write(ctx, collection="charges", record_id=charge["id"], before=charge_before, after=charge)
            self._emit(ctx, "charge.captured", charge, previous=_previous(charge_before, charge))
        self._write(ctx, collection="payment_intents", record_id=intent["id"], before=before, after=intent)
        self._emit(ctx, "payment_intent.succeeded", intent, previous=_previous(before, intent))
        return 200, _copy(intent)

    def cancel_payment_intent(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P({"cancellation_reason"}))
        intent = self.store.require("payment_intents", ctx.arg("id"))
        if intent.get("status") in {"succeeded", "canceled"}:
            raise StripeError(
                400,
                f"This PaymentIntent's status is {intent['status']} and cannot be canceled.",
                code="payment_intent_unexpected_state",
                param="id",
            )
        before = _copy(intent)
        now = self.store.next_epoch()
        intent["status"] = "canceled"
        intent["canceled_at"] = now
        intent["cancellation_reason"] = _opt_str(ctx.params.get("cancellation_reason")) or None
        self._write(ctx, collection="payment_intents", record_id=intent["id"], before=before, after=intent)
        self._emit(ctx, "payment_intent.canceled", intent, previous=_previous(before, intent))
        return 200, _copy(intent)

    # -- refunds -----------------------------------------------------------------------

    def list_refunds(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON | {"charge", "payment_intent"})
        records = list(self.store.refunds.values())
        if "charge" in ctx.params:
            charge_id = as_str(ctx.params["charge"], "charge")
            records = [record for record in records if record.get("charge") == charge_id]
        if "payment_intent" in ctx.params:
            intent_id = as_str(ctx.params["payment_intent"], "payment_intent")
            records = [record for record in records if record.get("payment_intent") == intent_id]
        records = self._created_filter(records, ctx.params.get("created"))
        return self.paginate(records, ctx, url="/v1/refunds", collection="refunds")

    def get_refund(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        return 200, _copy(self.store.require("refunds", ctx.arg("id")))

    def create_refund(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _REFUND_CREATE)
        params = ctx.params
        charge: JsonDict | None = None
        if params.get("charge"):
            charge = self.store.require("charges", as_str(params["charge"], "charge"), param="charge")
        elif params.get("payment_intent"):
            intent = self.store.require(
                "payment_intents", as_str(params["payment_intent"], "payment_intent"), param="payment_intent"
            )
            charge = self.store.charges.get(str(intent.get("latest_charge")))
            if charge is None:
                raise StripeError(
                    400, f"PaymentIntent {intent['id']} has no successful charge to refund.", param="payment_intent"
                )
        else:
            raise StripeError(
                400, "One of `charge` or `payment_intent` must be provided.", code="parameter_missing", param="charge"
            )
        remaining = int(charge["amount_captured"]) - int(charge.get("amount_refunded", 0))
        if remaining <= 0:
            raise StripeError(
                400, f"Charge {charge['id']} has already been refunded.", code="charge_already_refunded", param="charge"
            )
        amount = as_int(params["amount"], "amount") if params.get("amount") not in (None, "") else remaining
        if amount <= 0:
            raise StripeError(
                400, f"Invalid positive integer: {amount}", code="parameter_invalid_integer", param="amount"
            )
        if amount > remaining:
            raise StripeError(
                400,
                f"Refund amount ({amount}) is greater than unrefunded amount on charge ({remaining}).",
                param="amount",
            )
        reason = _opt_str(params.get("reason")) or None
        if reason is not None and reason not in {"duplicate", "fraudulent", "requested_by_customer"}:
            raise StripeError(
                400,
                f"Invalid reason: {reason}. Must be one of duplicate, fraudulent, or requested_by_customer.",
                param="reason",
            )
        refund = self.store.build_refund(
            created=self.store.next_epoch(),
            amount=amount,
            charge=charge,
            reason=reason,
            metadata=_str_map(params.get("metadata")),
        )
        self._write(ctx, collection="refunds", record_id=refund["id"], before=None, after=refund)
        self.store.refunds[refund["id"]] = refund
        charge_before = _copy(charge)
        charge["amount_refunded"] = int(charge.get("amount_refunded", 0)) + amount
        charge["refunded"] = charge["amount_refunded"] >= int(charge["amount_captured"])
        refunds = cast(JsonDict, charge["refunds"])
        cast(list[JsonDict], refunds["data"]).insert(0, _copy(refund))
        refunds["total_count"] = len(cast(list[JsonDict], refunds["data"]))
        self._write(ctx, collection="charges", record_id=charge["id"], before=charge_before, after=charge)
        self._emit(ctx, "charge.refunded", charge, previous=_previous(charge_before, charge))
        self._emit(ctx, "refund.created", refund)
        return 200, _copy(refund)

    def update_refund(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P({"metadata"}))
        refund = self.store.require("refunds", ctx.arg("id"))
        before = _copy(refund)
        if "metadata" in ctx.params:
            refund["metadata"] = merge_metadata(cast(JsonDict, refund.get("metadata") or {}), ctx.params["metadata"])
        self._write(ctx, collection="refunds", record_id=refund["id"], before=before, after=refund)
        self._emit(ctx, "refund.updated", refund, previous=_previous(before, refund))
        return 200, _copy(refund)

    # -- billing meters ----------------------------------------------------------------

    def list_meters(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P({"ending_before", "limit", "starting_after", "status"}))
        records = list(self.store.meters.values())
        if "status" in ctx.params:
            status = as_str(ctx.params["status"], "status")
            if status not in {"active", "inactive"}:
                raise StripeError(400, f"Invalid status: {status}. Must be one of active or inactive.", param="status")
            records = [record for record in records if record.get("status") == status]
        return self.paginate(records, ctx, url="/v1/billing/meters", collection="meters")

    def get_meter(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        return 200, _copy(self.store.require("meters", ctx.arg("id")))

    def create_meter(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _METER_CREATE)
        params = ctx.params
        for field in ("display_name", "event_name"):
            if not params.get(field):
                raise parameter_missing(field)
        aggregation = as_dict(params.get("default_aggregation"), "default_aggregation")
        formula = as_str(aggregation.get("formula", ""), "default_aggregation[formula]")
        if formula not in {"sum", "count", "last"}:
            raise StripeError(
                400,
                f"Invalid default_aggregation[formula]: {formula or '(missing)'}. Must be one of sum, count, or last.",
                param="default_aggregation[formula]",
            )
        event_name = as_str(params["event_name"], "event_name")
        if any(
            meter.get("event_name") == event_name and meter.get("status") == "active"
            for meter in self.store.meters.values()
        ):
            raise StripeError(
                400, f"An active meter with event_name '{event_name}' already exists.", param="event_name"
            )
        meter = self.store.build_meter(
            created=self.store.next_epoch(),
            display_name=as_str(params["display_name"], "display_name"),
            event_name=event_name,
            aggregation=formula,
            customer_mapping=_optional_hash(params.get("customer_mapping"), "customer_mapping"),
            value_settings=_optional_hash(params.get("value_settings"), "value_settings"),
        )
        self._write(ctx, collection="meters", record_id=meter["id"], before=None, after=meter)
        self.store.meters[meter["id"]] = meter
        self._emit(ctx, "billing.meter.created", meter)
        return 200, _copy(meter)

    def update_meter(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _METER_UPDATE)
        meter = self.store.require("meters", ctx.arg("id"))
        before = _copy(meter)
        if "display_name" in ctx.params:
            display_name = ctx.str_or_none("display_name")
            if display_name is None:
                raise StripeError(
                    400, "This value must be a non-empty string.", code="parameter_invalid_empty", param="display_name"
                )
            meter["display_name"] = display_name
        meter["updated"] = self.store.next_epoch()
        self._write(ctx, collection="meters", record_id=meter["id"], before=before, after=meter)
        self._emit(ctx, "billing.meter.updated", meter, previous=_previous(before, meter))
        return 200, _copy(meter)

    def deactivate_meter(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        meter = self.store.require("meters", ctx.arg("id"))
        if meter.get("status") == "inactive":
            raise StripeError(400, f"Meter {meter['id']} is already inactive.", param="id")
        before = _copy(meter)
        now = self.store.next_epoch()
        meter["status"] = "inactive"
        meter["updated"] = now
        meter["status_transitions"] = {"deactivated_at": now}
        self._write(ctx, collection="meters", record_id=meter["id"], before=before, after=meter)
        self._emit(ctx, "billing.meter.deactivated", meter, previous=_previous(before, meter))
        return 200, _copy(meter)

    def reactivate_meter(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        meter = self.store.require("meters", ctx.arg("id"))
        if meter.get("status") == "active":
            raise StripeError(400, f"Meter {meter['id']} is already active.", param="id")
        before = _copy(meter)
        meter["status"] = "active"
        meter["updated"] = self.store.next_epoch()
        meter["status_transitions"] = {"deactivated_at": None}
        self._write(ctx, collection="meters", record_id=meter["id"], before=before, after=meter)
        self._emit(ctx, "billing.meter.reactivated", meter, previous=_previous(before, meter))
        return 200, _copy(meter)

    def create_meter_event(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _METER_EVENT_CREATE)
        params = ctx.params
        if not params.get("event_name"):
            raise parameter_missing("event_name")
        event_name = as_str(params["event_name"], "event_name")
        meter = next((record for record in self.store.meters.values() if record.get("event_name") == event_name), None)
        if meter is None or meter.get("status") != "active":
            raise StripeError(400, f"No active meter found with event_name '{event_name}'.", param="event_name")
        payload = as_dict(params.get("payload"), "payload")
        customer_key = str(cast(JsonDict, meter["customer_mapping"]).get("event_payload_key", "stripe_customer_id"))
        value_key = str(cast(JsonDict, meter["value_settings"]).get("event_payload_key", "value"))
        if not payload.get(customer_key):
            raise parameter_missing(f"payload[{customer_key}]")
        self.store.require(
            "customers", as_str(payload[customer_key], f"payload[{customer_key}]"), param=f"payload[{customer_key}]"
        )
        if (
            payload.get(value_key) in (None, "")
            and cast(JsonDict, meter["default_aggregation"]).get("formula") != "count"
        ):
            raise parameter_missing(f"payload[{value_key}]")
        identifier = _opt_str(params.get("identifier")) or (
            "evtid_" + det_alnum(self.store.seed_key, "meter_event", ctx.request_id, length=16)
        )
        if identifier in self.store.meter_events:
            raise StripeError(400, f"A meter event with identifier '{identifier}' already exists.", param="identifier")
        event = self.store.build_meter_event(
            created=self.store.next_epoch(),
            event_name=event_name,
            identifier=identifier,
            payload=_str_map(payload),
            timestamp=params.get("timestamp"),
        )
        self._write(ctx, collection="meter_events", record_id=identifier, before=None, after=event)
        self.store.meter_events[identifier] = event
        return 200, _copy(event)

    # -- events ------------------------------------------------------------------------

    def list_events(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _LIST_COMMON | {"delivery_success", "type", "types"})
        records = list(self.store.events)
        if "type" in ctx.params:
            pattern = as_str(ctx.params["type"], "type")
            records = [record for record in records if _event_type_matches(str(record.get("type")), pattern)]
        if "types" in ctx.params:
            patterns = [as_str(item, "types") for item in as_list(ctx.params["types"])]
            records = [
                record
                for record in records
                if any(_event_type_matches(str(record.get("type")), pattern) for pattern in patterns)
            ]
        records = self._created_filter(records, ctx.params.get("created"))
        by_id = {record["id"]: record for record in self.store.events}
        return self._paginate_events(records, by_id, ctx)

    def _paginate_events(self, records: list[JsonDict], by_id: Mapping[str, JsonDict], ctx: Ctx) -> Result:
        limit = as_int(ctx.params.get("limit", _LIST_LIMIT_DEFAULT), "limit")
        if not 1 <= limit <= _LIST_LIMIT_MAX:
            raise StripeError(
                400, f"Invalid limit: {limit}. limit must be between 1 and {_LIST_LIMIT_MAX}.", param="limit"
            )
        ordered = list(reversed(records))
        ids = [record["id"] for record in ordered]
        if ctx.params.get("starting_after") is not None:
            cursor = as_str(ctx.params["starting_after"], "starting_after")
            if cursor not in by_id:
                raise resource_missing("event", cursor, param="starting_after", status=400)
            ordered = ordered[ids.index(cursor) + 1 :] if cursor in ids else []
        elif ctx.params.get("ending_before") is not None:
            cursor = as_str(ctx.params["ending_before"], "ending_before")
            if cursor not in by_id:
                raise resource_missing("event", cursor, param="ending_before", status=400)
            ordered = ordered[: ids.index(cursor)] if cursor in ids else ordered
            page = ordered[-limit:]
            return 200, {
                "object": "list",
                "data": [_copy(item) for item in page],
                "has_more": len(ordered) > limit,
                "url": "/v1/events",
            }
        page = ordered[:limit]
        return 200, {
            "object": "list",
            "data": [_copy(item) for item in page],
            "has_more": len(ordered) > limit,
            "url": "/v1/events",
        }

    def get_event(self, ctx: Ctx) -> Result:
        reject_unknown(ctx.params, _P(set()))
        identifier = ctx.arg("id")
        event = next((record for record in self.store.events if record.get("id") == identifier), None)
        if event is None:
            raise resource_missing("event", identifier)
        return 200, _copy(event)


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _previous(before: Mapping[str, Any], after: Mapping[str, Any]) -> JsonDict:
    return {key: value for key, value in before.items() if after.get(key) != value}


def _optional_hash(value: object, param: str) -> JsonDict | None:
    if value is None or value == "":
        return None
    return as_dict(value, param)


def _stable_newest_first(ordered: list[JsonDict], collection: Mapping[str, JsonDict]) -> list[JsonDict]:
    """Stripe lists newest first; ties (same `created`) fall back to reverse insertion order."""
    insertion = {identifier: index for index, identifier in enumerate(collection)}
    return sorted(
        ordered,
        key=lambda record: (int(record.get("created", 0)), insertion.get(str(record.get("id")), -1)),
        reverse=True,
    )


def _items(subscription: JsonDict) -> list[JsonDict]:
    envelope = subscription.get("items")
    if not isinstance(envelope, dict):
        return []
    data = cast(JsonDict, envelope).get("data")
    return cast(list[JsonDict], data) if isinstance(data, list) else []


def _item_price_id(item: JsonDict) -> str | None:
    price = item.get("price")
    if isinstance(price, dict):
        return _opt_str(cast(JsonDict, price).get("id"))
    return _opt_str(price)


def _event_type_matches(event_type: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return event_type.startswith(pattern[:-1])
    return event_type == pattern


# --------------------------------------------------------------------------------------
# Twin spec
# --------------------------------------------------------------------------------------


def make_store(seed_key: str) -> Store:
    return StripeStore(seed_key)


def make_data_app(store: Store) -> Starlette:
    if not isinstance(store, StripeStore):
        raise TypeError("make_data_app expects a StripeStore")
    app = Starlette(routes=StripeApi(store).routes())
    app.router.redirect_slashes = False
    return app


SPEC = TwinSpec(
    provider="stripe",
    role="payments",
    make_store=make_store,
    make_data_app=make_data_app,
    make_admin_app=make_admin_app,
    notes=(
        "Admin-state collections are dicts keyed by id (grader cardinality contract).",
        "Form bodies use Stripe bracket notation; JSON bodies are accepted too.",
        "Idempotency-Key is honoured for POST/DELETE; replays return the cached response.",
        "Unknown /v1 list routes return an empty list and materialise generic_resources (Arga twin behaviour).",
    ),
)

__all__ = [
    "SPEC",
    "STRIPE_API_VERSION",
    "SearchQuery",
    "StripeApi",
    "StripeError",
    "StripeStore",
    "make_data_app",
    "make_store",
]
