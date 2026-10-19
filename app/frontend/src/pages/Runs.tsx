import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, label, RunSummary, when } from "../api";

type Key = "run" | "area" | "created" | "captured" | "finished" | "status" | "review";

const COLUMNS: { key: Key; title: string }[] = [
  { key: "run", title: "Run" },
  { key: "area", title: "Area" },
  { key: "created", title: "Created" },
  { key: "captured", title: "Captured" },
  { key: "finished", title: "Finished" },
  { key: "status", title: "Run status" },
  { key: "review", title: "Review status" },
];

const sortValue = (r: RunSummary, k: Key): string | number =>
  k === "run" ? r.run : k === "captured" ? r.captured?.[0] ?? "" : (r[k] as string | null) ?? "";

export default function Runs() {
  const [rows, setRows] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: Key; dir: 1 | -1 }>({ key: "run", dir: -1 });
  const go = useNavigate();

  useEffect(() => {
    api.runs().then(setRows).catch((e) => setError(e.message));
  }, []);

  const sorted = useMemo(() => {
    if (!rows) return [];
    return [...rows].sort((a, b) => {
      const x = sortValue(a, sort.key), y = sortValue(b, sort.key);
      return (x < y ? -1 : x > y ? 1 : a.run - b.run) * sort.dir;
    });
  }, [rows, sort]);

  const toggle = (key: Key) => setSort((s) => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : 1 }));

  if (error) return <p className="error">Could not load runs: {error}</p>;
  if (!rows) return <p className="muted">Loading runs…</p>;

  return (
    <section>
      <h1>Runs</h1>
      <p className="muted">{rows.length} runs. Click a column title to sort, a row to open the run.</p>
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
            {sorted.map((r) => (
              <tr key={r.run} onClick={() => go(`/runs/${r.run}`)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && go(`/runs/${r.run}`)}>
                <td className="mono">{r.run}</td>
                <td>{label(r.area)}</td>
                <td className="mono">{when(r.created)}</td>
                <td className="mono">
                  {when(r.captured?.[0])}
                  {r.captured?.[1] && r.captured[1] !== r.captured[0] ? <><br />{when(r.captured[1])}</> : null}
                </td>
                <td className="mono">{when(r.finished)}</td>
                <td><span className={`status s-${r.status}`}>{r.status}</span></td>
                <td><span className={`status r-${(r.review || "").replace(/ /g, "-")}`}>{r.review || "–"}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">Created: when the run started. Captured: first and last satellite capture in the run. Finished: when processing ended.</p>
    </section>
  );
}
