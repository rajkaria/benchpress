import { useEffect, useId, useState, type FormEvent } from "react";
import type { ReceiptPage } from "./api";
import { detailHash, type Filters } from "./route";

const STATUS_OPTIONS = [
  "any",
  "verified",
  "mismatch",
  "unverified",
  "failed",
  "refused",
  "allow",
  "refuse",
  "completed",
  "partial",
  "escalated",
] as const;

const EVENT_OPTIONS = ["any", "write", "approval_requested", "approval_resolved", "guard", "run"] as const;

export type ReceiptsListProps = {
  mode: "gateway" | "local";
  filters: Filters;
  list: (f: Filters) => Promise<ReceiptPage>;
  onFilters: (f: Filters) => void;
};

function fieldFrom(filters: Filters, key: "customer" | "provider" | "rule"): string {
  return filters[key] ?? "";
}

function selectFrom(filters: Filters, key: "status" | "event"): string {
  return filters[key] ?? "any";
}

/** `#/`: the filter form, the receipts table, and the "Older" pager. */
export function ReceiptsList({ mode, filters, list, onFilters }: ReceiptsListProps) {
  const [page, setPage] = useState<ReceiptPage | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [customer, setCustomer] = useState(() => fieldFrom(filters, "customer"));
  const [provider, setProvider] = useState(() => fieldFrom(filters, "provider"));
  const [rule, setRule] = useState(() => fieldFrom(filters, "rule"));
  const [status, setStatus] = useState(() => selectFrom(filters, "status"));
  const [event, setEvent] = useState(() => selectFrom(filters, "event"));
  const formId = useId();

  // The form re-seeds from `filters` whenever the route changes (a link, a filter submit, "Older"),
  // so the visible fields always match what was actually applied.
  useEffect(() => {
    setCustomer(fieldFrom(filters, "customer"));
    setProvider(fieldFrom(filters, "provider"));
    setRule(fieldFrom(filters, "rule"));
    setStatus(selectFrom(filters, "status"));
    setEvent(selectFrom(filters, "event"));
  }, [filters]);

  useEffect(() => {
    let cancelled = false;
    setLoadError(false);
    list(filters).then(
      (result) => {
        if (!cancelled) setPage(result);
      },
      () => {
        // An Unauthorized here is handled by the caller (it flips to the key prompt); anything else
        // is shown inline. Either way this effect must not leave an unhandled rejection behind.
        if (!cancelled) setLoadError(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [list, filters]);

  function handleSubmit(event_: FormEvent<HTMLFormElement>) {
    event_.preventDefault();
    const next: Filters = {};
    if (customer) next.customer = customer;
    if (provider) next.provider = provider;
    if (rule) next.rule = rule;
    if (status !== "any") next.status = status;
    if (event !== "any") next.event = event;
    onFilters(next);
  }

  function handleOlder() {
    if (page?.next_before) onFilters({ ...filters, before: page.next_before });
  }

  const emptyMessage = mode === "gateway" ? "No receipts match." : "No receipts under this directory.";

  return (
    <div className="receipts-list">
      <form className="filters" onSubmit={handleSubmit} aria-label="Filter receipts">
        <div className="field">
          <label htmlFor={`${formId}-customer`}>Customer</label>
          <input id={`${formId}-customer`} value={customer} onChange={(e) => setCustomer(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor={`${formId}-provider`}>Provider</label>
          <input id={`${formId}-provider`} value={provider} onChange={(e) => setProvider(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor={`${formId}-rule`}>Rule</label>
          <input id={`${formId}-rule`} value={rule} onChange={(e) => setRule(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor={`${formId}-status`}>Status</label>
          <select id={`${formId}-status`} value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUS_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={`${formId}-event`}>Event</label>
          <select id={`${formId}-event`} value={event} onChange={(e) => setEvent(e.target.value)}>
            {EVENT_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>
        <button type="submit">Apply</button>
      </form>

      {loadError && (
        <p role="alert" className="load-error">
          Could not load receipts.
        </p>
      )}

      {page && page.receipts.length === 0 && !loadError && <p className="empty-state">{emptyMessage}</p>}

      {page && page.receipts.length > 0 && (
        <table>
          <thead>
            <tr>
              <th scope="col">Time</th>
              <th scope="col">Kind</th>
              <th scope="col">Event</th>
              <th scope="col">Provider</th>
              <th scope="col">Method</th>
              <th scope="col">Path</th>
              <th scope="col">Status</th>
              <th scope="col">Rule</th>
              <th scope="col">Resource</th>
            </tr>
          </thead>
          <tbody>
            {page.receipts.map((receipt) => (
              <tr key={receipt.id}>
                <td>
                  <a href={detailHash(receipt.id)}>{receipt.at}</a>
                </td>
                <td>{receipt.kind}</td>
                <td>{receipt.event}</td>
                <td>{receipt.provider}</td>
                <td>{receipt.method}</td>
                <td>{receipt.path}</td>
                <td data-status={receipt.status ?? "unknown"} className="status-cell">
                  {receipt.status ?? "—"}
                </td>
                <td>{receipt.rule}</td>
                <td>{receipt.resource}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {page?.next_before && (
        <button type="button" className="older" onClick={handleOlder}>
          Older
        </button>
      )}
    </div>
  );
}
