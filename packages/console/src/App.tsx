import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createApi,
  sessionKeyStore,
  Unauthorized,
  type KeyStore,
  type Meta,
  type ReceiptDetail as ReceiptDetailModel,
  type ReceiptPage,
} from "./api";
import { KeyPrompt } from "./KeyPrompt";
import { ReceiptDetail } from "./ReceiptDetail";
import { ReceiptsList } from "./ReceiptsList";
import { listHash, parseHash, type Filters, type Route } from "./route";

function currentRoute(): Route {
  return parseHash(window.location.hash);
}

function Header({ meta }: { meta: Meta | null }) {
  return (
    <header className="app-header">
      <a href={listHash({})} className="brand">
        Benchpress console
      </a>
      {meta && (
        <span className="meta">
          {meta.mode} &middot; v{meta.version}
        </span>
      )}
    </header>
  );
}

export type AppProps = {
  /** Injection seams for tests; production always uses the real `fetch` and `sessionKeyStore`. */
  fetchImpl?: typeof fetch;
  keyStore?: KeyStore;
};

/** Owns the hash route, the `/v1/meta` load and the "needs a key" gate every other screen renders behind. */
export function App({ fetchImpl = fetch, keyStore = sessionKeyStore }: AppProps = {}) {
  const api = useMemo(() => createApi(fetchImpl, keyStore), [fetchImpl, keyStore]);
  const [route, setRoute] = useState<Route>(currentRoute);
  const [lastListFilters, setLastListFilters] = useState<Filters>(() => {
    const initial = currentRoute();
    return initial.name === "list" ? initial.filters : {};
  });
  const [meta, setMeta] = useState<Meta | null>(null);
  const [metaChecked, setMetaChecked] = useState(false);
  const [needsKey, setNeedsKey] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReceiptDetailModel | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    function onHashChange() {
      setRoute(currentRoute());
    }
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    if (route.name === "list") setLastListFilters(route.filters);
  }, [route]);

  const loadMeta = useCallback(async () => {
    try {
      const loaded = await api.meta();
      setMeta(loaded);
      setNeedsKey(false);
      setKeyError(null);
    } catch (error) {
      if (error instanceof Unauthorized) {
        // A stored key (from an earlier session, or one rejected before a reload) is 401ing right
        // now: it is not coming back. Clear it so a reload doesn't silently retry the same bad key
        // forever, and show a bare prompt — the user hasn't typed anything yet, so no error message.
        keyStore.clear();
        setNeedsKey(true);
        setKeyError(null);
      } else {
        throw error;
      }
    } finally {
      setMetaChecked(true);
    }
  }, [api, keyStore]);

  useEffect(() => {
    void loadMeta();
  }, [loadMeta]);

  useEffect(() => {
    if (!metaChecked || needsKey || route.name !== "detail") {
      setDetail(null);
      setDetailError(null);
      return;
    }
    let cancelled = false;
    setDetail(null);
    setDetailError(null);
    api.get(route.id).then(
      (loaded) => {
        if (!cancelled) setDetail(loaded);
      },
      (error) => {
        if (cancelled) return;
        if (error instanceof Unauthorized) setNeedsKey(true);
        else setDetailError("Could not load this receipt.");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api, route, needsKey, metaChecked]);

  const list = useCallback(
    async (filters: Filters): Promise<ReceiptPage> => {
      try {
        return await api.list(filters);
      } catch (error) {
        if (error instanceof Unauthorized) setNeedsKey(true);
        throw error;
      }
    },
    [api],
  );

  function handleFilters(filters: Filters) {
    window.location.hash = listHash(filters);
  }

  async function handleKeySubmit(key: string) {
    keyStore.set(key);
    try {
      const loaded = await api.meta();
      setMeta(loaded);
      setNeedsKey(false);
      setKeyError(null);
    } catch (error) {
      if (error instanceof Unauthorized) {
        // The key just submitted is the one that got 401ed: leave it in the store and a reload
        // would retry it silently and re-show a blank prompt. Clear it so the next attempt starts
        // from a clean slate, and say why the prompt is back.
        keyStore.clear();
        setKeyError("That key was not accepted.");
      } else {
        throw error;
      }
    }
  }

  if (!metaChecked) {
    return (
      <div className="app">
        <Header meta={null} />
      </div>
    );
  }

  if (needsKey) {
    return (
      <div className="app">
        <Header meta={meta} />
        <main>
          <KeyPrompt onSubmit={(key) => void handleKeySubmit(key)} error={keyError} />
        </main>
      </div>
    );
  }

  return (
    <div className="app">
      <Header meta={meta} />
      <main>
        {route.name === "list" && (
          <ReceiptsList mode={meta?.mode ?? "gateway"} filters={route.filters} list={list} onFilters={handleFilters} />
        )}
        {route.name === "detail" && (
          <div className="receipt-page">
            <a href={listHash(lastListFilters)} className="back-link">
              &larr; Back to receipts
            </a>
            {detailError && (
              <p role="alert" className="load-error">
                {detailError}
              </p>
            )}
            {detail && detail.id === route.id && <ReceiptDetail detail={detail} htmlUrl={api.htmlUrl} />}
          </div>
        )}
      </main>
    </div>
  );
}
