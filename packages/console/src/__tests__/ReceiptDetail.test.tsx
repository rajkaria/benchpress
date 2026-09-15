import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReceiptDetail } from "../ReceiptDetail";

describe("ReceiptDetail", () => {
  it("shows a write's verdict and evidence", () => {
    render(<ReceiptDetail detail={{ id: "r1", kind: "write", html: false, payload: {
      status: "mismatch", fingerprint: "f".repeat(64), session: "s1", workspace: "acme", approval: null,
      verdict: { action_id: "w1", allowed: true, rule: "allowed", reason: "" },
      action: { id: "w1", provider: "hubspot", method: "PATCH", path: "/x" },
      evidence: [{ check: "readback:w1:email", provider: "hubspot", resource: "/x", expected: "ap@rivermill.example",
                   observed: "missing", match: false, detail: "email not found at email" }],
    } }} htmlUrl={(id) => `v1/receipts/${id}/html`} />);
    expect(screen.getByText("mismatch")).toBeTruthy();
    expect(screen.getByRole("cell", { name: "missing" })).toBeTruthy();
    expect(screen.queryByTitle("Run receipt")).toBeNull();
  });
  it("embeds a run receipt in a sandboxed iframe", () => {
    render(<ReceiptDetail detail={{ id: "run1", kind: "run", html: true, payload: {} }} htmlUrl={(id) => `v1/receipts/${id}/html`} />);
    const frame = screen.getByTitle("Run receipt");
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("src")).toBe("v1/receipts/run1/html");
  });
});
