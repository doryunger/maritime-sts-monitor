import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, GridRun, GridRunEntry } from "../api";

type Key = "tile" | "date" | "status" | "pairs";

const COLUMNS: { key: Key; title: string; numeric?: boolean }[] = [
  { key: "tile", title: "Grid" },
  { key: "date", title: "Finished" },
  { key: "status", title: "Status" },
  { key: "pairs", title: "Pairs", numeric: true },
];

const STATUS: Record<GridRunEntry["status"], string> = { waiting: "Waiting", processing: "Processing", processed: "Processed", failed: "Failed" };

const value = (g: GridRunEntry, key: Key): string | number =>
  key === "tile" ? g.tile_id : key === "date" ? g.finished ?? "9999" : key === "status" ? STATUS[g.status] : g.pairs;

export default function RunTable() {
  const [runs, setRuns] = useState<GridRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [sort, setSort] = useState<{ key: Key; dir: 1 | -1 }>({ key: "tile", dir: 1 });
  const go = useNavigate();

  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const data = await api.hotspotTestGridRuns();
        if (!alive) return;
        setRuns(data);
        setError(null);
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 30000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const index = useMemo(() => {
    if (!runs || !runs.length) return -1;
    const found = picked === null ? -1 : runs.findIndex((r) => r.run === picked);
    return found >= 0 ? found : runs.length - 1;
  }, [runs, picked]);

  const run = index >= 0 && runs ? runs[index] : null;
  const rows = useMemo(() => {
    if (!run) return [];
    return [...run.grids].sort((a, b) => {
      const x = value(a, sort.key), y = value(b, sort.key);
      return (x < y ? -1 : x > y ? 1 : a.tile_id.localeCompare(b.tile_id)) * sort.dir;
    });
  }, [run, sort.key, sort.dir]);

  if (error && !runs) return <p className="error">Could not load the runs: {error}</p>;
  if (!runs) return <p className="muted">Loading the runs…</p>;
  if (!runs.length || !run) return <p className="muted">No grid scan run has been queued yet.</p>;

  const latest = index === runs.length - 1;
  const move = (step: number) => {
    const next = runs[index + step];
    if (next) setPicked(index + step === runs.length - 1 ? null : next.run);
  };
  const toggle = (key: Key) => setSort((s) => (s.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }));
  const open = (g: GridRunEntry) => g.pairs > 0 && g.run_no && g.scene && go(`/hotspot-tests/runs/${g.run_no}?scene=${encodeURIComponent(g.scene)}`);

  return (
    <div className="run-table">
      <div className="run-nav">
        <button onClick={() => move(-1)} disabled={index === 0} aria-label="Earlier run">◀</button>
        <strong>Run {run.run}</strong>
        <span className="muted">{run.day}</span>
        <button onClick={() => move(1)} disabled={latest} aria-label="Later run">▶</button>
      </div>
      <table className="grids">
        <thead>
          <tr>
            {COLUMNS.map((c) => (
              <th key={c.key} className={c.numeric ? "n" : ""} aria-sort={sort.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                <button className="sort" onClick={() => toggle(c.key)}>
                  {c.title}
                  <span className="arrow">{sort.key === c.key ? (sort.dir === 1 ? "▲" : "▼") : ""}</span>
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((g) => (
            <tr key={g.tile_id} className={g.pairs ? "has-pairs" : ""} onClick={() => open(g)} tabIndex={g.pairs ? 0 : undefined} onKeyDown={(e) => e.key === "Enter" && open(g)}>
              <td>{g.tile_id}</td>
              <td title={g.finished ?? ""}>{g.finished ? g.finished.slice(0, 10) : "–"}</td>
              <td>{STATUS[g.status]}</td>
              <td className="n">{g.pairs}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
