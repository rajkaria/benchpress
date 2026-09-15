import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { KeyStore } from "../api";

function memoryKeys(initial: string | null = null): KeyStore {
  let key = initial;
  return {
    get: () => key,
    set: (k) => {
      key = k;
    },
    clear: () => {
      key = null;
    },
  };
}

/** A `fetch` stand-in: `/v1/meta` answers from `metaResponses` in call order, everything else is an
 * empty-but-valid 200 (so `ReceiptsList`'s own `list()` call, made once the app is past the key gate,
 * never throws and never needs its own assertions here). */
function fetchSequence(metaResponses: Response[]): typeof fetch {
  let metaCall = 0;
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/v1/meta")) {
      const response = metaResponses[metaCall];
      metaCall += 1;
      if (!response) throw new Error("no more /v1/meta responses queued");
      return response;
    }
    return new Response(JSON.stringify({ receipts: [], next_before: null }), { status: 200 });
  }) as unknown as typeof fetch;
}

describe("App key flow", () => {
  it("clears a rejected key (stale or freshly submitted) and requires a valid one before loading", async () => {
    const keyStore = memoryKeys("stale-key");
    const fetchImpl = fetchSequence([
      new Response("{}", { status: 401 }), // call 1: the stale stored key, on mount
      new Response("{}", { status: 401 }), // call 2: a freshly submitted, still-bad key
      new Response(JSON.stringify({ mode: "gateway", version: "1.0.0a2" }), { status: 200 }), // call 3: a good key
    ]);

    render(<App fetchImpl={fetchImpl} keyStore={keyStore} />);

    // Call 1: the stored key 401s on mount. The prompt shows with no error message (the user hasn't
    // typed anything yet), and the now-known-bad stored key is cleared so a reload won't retry it.
    const firstInput = await screen.findByLabelText("API key");
    expect(screen.queryByText("That key was not accepted.")).toBeNull();
    expect(keyStore.get()).toBeNull();

    // Call 2: submitting a new (still bad) key also 401s. The message shows, and that key is cleared
    // too — it must not survive under `benchpress.apiKey` for a future reload to retry silently.
    await userEvent.type(firstInput, "still-bad");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("That key was not accepted.")).toBeTruthy();
    expect(keyStore.get()).toBeNull();

    // Call 3: submitting a valid key loads the app, and the store holds that key.
    const secondInput = screen.getByLabelText("API key");
    await userEvent.clear(secondInput);
    await userEvent.type(secondInput, "good-key");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText("gateway", { exact: false });
    expect(keyStore.get()).toBe("good-key");
  });
});
