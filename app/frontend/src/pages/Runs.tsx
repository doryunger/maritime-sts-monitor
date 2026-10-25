import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import SyncPanel from "../components/SyncPanel";
import { api, label, SceneRow, sceneTag, when } from "../api";

type Key = "run" | "area" | "source" | "captured" | "created" | "status" | "review";

const COLUMNS: { key: Key; title: string }[] = [
  { key: "run", title: "Run" },
  { key: "area", title: "Area" },
  { key: "source", title: "Source" },
  { key: "captured", title: "Captured" },
  { key: "created", title: "Created" },
  { key: "status", title: "Run status" },
  { key: "review", title: "Review status" },
];

const STORE = "runs.sort";
const PAGE_SIZE = 30;
const SHOW_EMPTY = "runs.show_empty";
const KEYS = COLUMNS.map((c) => c.key);
const SCENE_CACHE = "runs.scene_index.v1";

type SceneCache = { rows: SceneRow[]; etag: string | null; checked: string };

function rememberedScenes(): SceneCache | null {
  try {
    const cache = JSON.parse(localStorage.getItem(SCENE_CACHE) || "null");
    if (!cache || !Array.isArray(cache.rows) || typeof cache.checked !== "string") return null;
    if (cache.etag !== null && typeof cache.etag !== "string") return null;
    return cache.rows.every((row: SceneRow) => Number.isInteger(row.run) &&
      [row.area, row.source, row.scene_id, row.captured, row.status, row.review].every((v) => typeof v === "string")) ? cache : null;
  } catch {
    return null;
  }
}

function rememberedShowEmpty(): boolean {
  try {
    return localStorage.getItem(SHOW_EMPTY) === "true";
  } catch {
    return false;
  }
}

function remembered(): { key: Key; dir: 1 | -1 } | null {
  try {
    const v = JSON.parse(localStorage.getItem(STORE) || "null");
    return v && KEYS.includes(v.key) && (v.dir === 1 || v.dir === -1) ? v : null;
  } catch {
    return null;
  }
}

const sortValue = (r: SceneRow, k: Key): string | number => (k === "run" ? r.run : r[k] ?? "");

export default function Runs() {
  const [rows, setRows] = useState<SceneRow[] | null>(() => rememberedScenes()?.rows ?? null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [checked, setChecked] = useState<string | null>(() => rememberedScenes()?.checked ?? null);
  const [showEmpty, setShowEmpty] = useState(rememberedShowEmpty);
  const [params, setParams] = useSearchParams();
  const go = useNavigate();
  const fromUrl = params.get("sort") as Key | null;
  const sort: { key: Key; dir: 1 | -1 } =
    fromUrl && KEYS.includes(fromUrl)
      ? { key: fromUrl, dir: params.get("dir") === "asc" ? 1 : -1 }
      : remembered() ?? { key: "captured", dir: -1 };
  const setSort = (next: { key: Key; dir: 1 | -1 }) => {
    setParams({ sort: next.key, dir: next.dir === 1 ? "asc" : "desc", page: "1" }, { replace: true });
    try {
      localStorage.setItem(STORE, JSON.stringify(next));
    } catch {
      return;
    }
  };

  useEffect(() => {
    let cache = rememberedScenes();
    let pending = false;
    const controller = new AbortController();
    const refresh = async () => {
      if (pending || controller.signal.aborted) return;
      pending = true;
      setRefreshing(true);
      try {
        const result = await api.scenes(cache?.etag, controller.signal);
        if (controller.signal.aborted) return;
        const next = result.rows ?? cache?.rows;
        if (!next) throw new Error("No scene list available; refresh the page.");
        cache = { rows: next, etag: result.etag, checked: new Date().toISOString() };
        if (result.rows) setRows(result.rows);
        setChecked(cache.checked);
        setError(null);
        try {
          localStorage.setItem(SCENE_CACHE, JSON.stringify(cache));
        } catch {
          return;
        }
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
      } finally {
        pending = false;
        if (!controller.signal.aborted) setRefreshing(false);
      }
    };
    const visibleRefresh = () => { if (document.visibilityState === "visible") void refresh(); };
    void refresh();
    const timer = window.setInterval(visibleRefresh, 15000);
    window.addEventListener("focus", visibleRefresh);
    document.addEventListener("visibilitychange", visibleRefresh);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener("focus", visibleRefresh);
      document.removeEventListener("visibilitychange", visibleRefresh);
    };
  }, []);

  const sorted = useMemo(() => {
    if (!rows) return [];
    return [...rows].sort((a, b) => {
      const x = sortValue(a, sort.key), y = sortValue(b, sort.key);
      const tie = a.run - b.run || a.captured.localeCompare(b.captured) || a.scene_id.localeCompare(b.scene_id);
      return (x < y ? -1 : x > y ? 1 : tie) * sort.dir;
    });
  }, [rows, sort.key, sort.dir]);

  const toggle = (key: Key) => setSort({ key, dir: sort.key === key ? (sort.dir === 1 ? -1 : 1) : 1 });
  const toggleEmpty = (checked: boolean) => {
    setShowEmpty(checked);
    goto(1);
    try {
      localStorage.setItem(SHOW_EMPTY, String(checked));
    } catch {
      return;
    }
  };
  const open = (r: SceneRow) => go(`/runs/${r.run}?scene=${encodeURIComponent(r.scene_id)}`);
  const requested = Number(params.get("page"));
  const goto = (next: number) => {
    const copy = new URLSearchParams(params);
    copy.set("page", String(next));
    setParams(copy, { replace: true });
  };
  const matching = showEmpty ? sorted : sorted.filter((r) => r.review !== "nothing to review" || r.status === "failed");
  const pages = Math.max(1, Math.ceil(matching.length / PAGE_SIZE));
  const page = Number.isInteger(requested) && requested >= 1 ? Math.min(requested, pages) : 1;
  const visible = matching.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  if (error && !rows) return <p className="error">Could not load runs: {error}</p>;
  if (!rows) return <p className="muted">Loading runs…</p>;

  return (
    <section>
      <h1>Runs</h1>
      {error && <p className="error">Showing the cached list. Could not refresh: {error}</p>}
      <SyncPanel />
      <p className="muted">One row per scene. Click a column title to sort, a row to open its QA.</p>
      <div className="table-options">
        <label><input type="checkbox" checked={showEmpty} onChange={(e) => toggleEmpty(e.target.checked)} /> Show scenes with no review cards</label>
        <span className="muted small">Showing {matching.length} of {sorted.length} scenes</span>
        <span className="muted small">{refreshing ? "Checking for updates..." : checked ? `Updated ${when(checked)}` : ""}</span>
      </div>
      <div className="wrap">
        <table className="runs">
          <thead>
            <tr>
              {COLUMNS.map((c) => (
                <th key={c.key} aria-sort={sort.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                  <button className="sort" onClick={() => toggle(c.key)}>
                    {c.title}
                    <span className="arrow">{sort.key === c.key ? (sort.dir === 1 ? "▲" : "▼") : ""}</span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((r) => (
              <tr key={`${r.run}-${r.scene_id}`} onClick={() => open(r)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && open(r)}>
                <td className="mono">{r.run}</td>
                <td>{label(r.area)}</td>
                <td>
                  {r.source.toUpperCase()} <span className="muted mono small" title={r.scene_id}>{sceneTag(r.scene_id)}</span>
                </td>
                <td className="mono">{when(r.captured)}</td>
                <td className="mono">{when(r.created)}</td>
                <td><span className={`status s-${r.status}`}>{r.status}</span></td>
                <td><span className={`status r-${r.review.replace(/ /g, "-")}`}>{r.review}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {matching.length > PAGE_SIZE && <div className="table-options">
        <button onClick={() => goto(page - 1)} disabled={page <= 1}>Previous</button>
        <span className="small">Page {page} of {pages}</span>
        <button onClick={() => goto(page + 1)} disabled={page >= pages}>Next</button>
        <span className="muted small">Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, matching.length)} of {matching.length}</span>
      </div>}
      <p className="muted small">Captured: satellite capture time. Created: when processing of that scene finished.</p>
    </section>
  );
}
