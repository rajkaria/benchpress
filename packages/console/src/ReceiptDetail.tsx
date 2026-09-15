import type { ReceiptDetail as ReceiptDetailModel } from "./api";

export type ReceiptDetailProps = {
  detail: ReceiptDetailModel;
  htmlUrl: (id: string) => string;
};

type WriteVerdict = { action_id?: unknown; allowed?: unknown; rule?: unknown; reason?: unknown };
type WriteEvidence = {
  check?: unknown;
  expected?: unknown;
  observed?: unknown;
  match?: unknown;
};

function text(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function Field({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="field-row">
      <dt>{label}</dt>
      <dd>{text(value)}</dd>
    </div>
  );
}

function WriteReceipt({ payload }: { payload: Record<string, unknown> }) {
  const verdict = asRecord(payload["verdict"]) as WriteVerdict;
  const action = asRecord(payload["action"]);
  const evidence = asArray(payload["evidence"]) as WriteEvidence[];
  const approval = payload["approval"];
  const status = payload["status"];

  return (
    <div className="receipt-detail">
      <p className="status-badge" data-status={text(status)}>
        {text(status)}
      </p>
      <dl>
        <Field label="Allowed" value={verdict.allowed} />
        <Field label="Rule" value={verdict.rule} />
        <Field label="Reason" value={verdict.reason} />
        <Field label="Fingerprint" value={payload["fingerprint"]} />
        <Field label="Session" value={payload["session"]} />
        <Field label="Workspace" value={payload["workspace"]} />
      </dl>
      {approval != null && (
        <section className="approval">
          <h3>Approval</h3>
          <dl>
            {Object.entries(asRecord(approval)).map(([key, value]) => (
              <Field key={key} label={key} value={value} />
            ))}
          </dl>
        </section>
      )}
      {evidence.length > 0 && (
        <table>
          <caption>Evidence</caption>
          <thead>
            <tr>
              <th scope="col">Check</th>
              <th scope="col">Expected</th>
              <th scope="col">Observed</th>
              <th scope="col">Match</th>
            </tr>
          </thead>
          <tbody>
            {evidence.map((item, index) => (
              <tr key={`${text(item.check)}-${index}`}>
                <td>{text(item.check)}</td>
                <td>{text(item.expected)}</td>
                <td>{text(item.observed)}</td>
                <td data-match={String(item.match === true)}>{item.match === true ? "match" : "no match"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h3>Action</h3>
      <pre>{JSON.stringify(action, null, 2)}</pre>
    </div>
  );
}

function GuardReceipt({ payload }: { payload: Record<string, unknown> }) {
  return (
    <div className="receipt-detail">
      <dl>
        <Field label="Tool" value={payload["tool"]} />
        <Field label="Class" value={payload["class"]} />
        <Field label="Decision" value={payload["decision"]} />
        <Field label="Rule" value={payload["rule"]} />
        <Field label="Reason" value={payload["reason"]} />
        <Field label="Args digest" value={payload["args_digest"]} />
        <Field label="Latency (ms)" value={payload["latency_ms"]} />
      </dl>
    </div>
  );
}

/** `#/receipts/<id>`: a `write` verdict + evidence, a `guard` decision, or a `run` receipt in an iframe. */
export function ReceiptDetail({ detail, htmlUrl }: ReceiptDetailProps) {
  if (detail.kind === "run") {
    return (
      <div className="receipt-detail receipt-detail-run">
        <iframe title="Run receipt" sandbox="" src={htmlUrl(detail.id)} className="run-frame" />
      </div>
    );
  }
  if (detail.kind === "guard") {
    return <GuardReceipt payload={detail.payload} />;
  }
  return <WriteReceipt payload={detail.payload} />;
}
