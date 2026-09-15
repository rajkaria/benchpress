import { useCallback, useEffect, useMemo, useState } from "react";
import { createApi, sessionKeyStore, Unauthorized, type Meta, type ReceiptDetail as ReceiptDetailModel, type ReceiptPage } from "./api";
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

/** Owns the hash route, the `/v1/meta` load and the "needs a key" gate every other screen renders behind. */
export function App() {
  const api = useMemo(() => createApi(), []);
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
        setNeedsKey(true);
      } else {
        throw error;
      }
    } finally {
      setMetaChecked(true);
    }
  }, [api]);

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
    sessionKeyStore.set(key);
    try {
      const loaded = await api.meta();
      setMeta(loaded);
      setNeedsKey(false);
      setKeyError(null);
    } catch (error) {
      if (error instanceof Unauthorized) {
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
