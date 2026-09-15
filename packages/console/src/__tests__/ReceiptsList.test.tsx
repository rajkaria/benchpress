import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ReceiptsList } from "../ReceiptsList";

const page = {
  receipts: [
    { id: "r2", kind: "write", at: "2026-09-14T00:00:02.000Z", event: "write", session: "s1", provider: "hubspot",
      method: "PATCH", path: "/crm/v3/objects/companies/701", status: "verified", rule: "allowed", resource: "company:701" },
    { id: "r1", kind: "write", at: "2026-09-14T00:00:01.000Z", event: "write", session: "s1", provider: "hubspot",
      method: "PATCH", path: "/crm/v3/objects/companies/702", status: "refused", rule: "protected", resource: "company:702" },
  ],
  next_before: "r1",
};

describe("ReceiptsList", () => {
  it("renders rows with links and status text", async () => {
    const list = vi.fn(async () => page);
    render(<ReceiptsList mode="gateway" filters={{}} list={list} onFilters={() => {}} />);
    expect(await screen.findByText("verified")).toBeTruthy();
    expect(screen.getByText("protected")).toBeTruthy();
    expect(screen.getAllByRole("link")[0]?.getAttribute("href")).toBe("#/receipts/r2");
    expect(screen.getByRole("button", { name: "Older" })).toBeTruthy();
  });
  it("submits filters", async () => {
    const onFilters = vi.fn();
    render(<ReceiptsList mode="gateway" filters={{}} list={async () => ({ receipts: [], next_before: null })} onFilters={onFilters} />);
    await userEvent.type(screen.getByLabelText("Provider"), "stripe");
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(onFilters).toHaveBeenCalledWith({ provider: "stripe" });
    expect(await screen.findByText("No receipts match.")).toBeTruthy();
  });
});
