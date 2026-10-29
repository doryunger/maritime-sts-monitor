import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, GridSummary, GridTile, label, when } from "../api";

const WEST = 53;
const NORTH = 28;
const SCALE = 10;
const WIDTH = (109 - WEST) * SCALE;
const HEIGHT = (NORTH + 2) * SCALE;
const ORDER = ["scanned_pairs", "scanned_no_pairs", "queued", "no_imagery", "failed", "unscanned"];
const NAMES: Record<string, string> = {
  scanned_pairs: "Scanned, pairs",
  scanned_no_pairs: "Scanned, no pairs",
  queued: "Queued",
  no_imagery: "No imagery",
  failed: "Failed",
  unscanned: "Unscanned",
};
const name = (status: string) => NAMES[status] ?? label(status);
const LIVE: [string, [number, number, number, number]][] = [
  ["Fujairah", [56.3, 24.8, 57.2, 25.8]],
  ["Sohar", [56.6, 24.2, 57.6, 24.9]],
  ["EOPL", [103.0, 0.3, 106.5, 1.8]],
  ["Johor", [103.5, 1.8, 106.5, 3.3]],
];

const px = (lon: number) => (lon - WEST) * SCALE;
const py = (lat: number) => (NORTH - lat) * SCALE;

export default function GridMap() {
  const [tiles, setTiles] = useState<GridTile[] | null>(null);
  const [summary, setSummary] = useState<GridSummary | null>(null);
  const [land, setLand] = useState<[number, number][][]>([]);
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    api.hotspotTestGridLand().then((d) => setLand(d.rings)).catch(() => setLand([]));
  }, []);

  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const data = await api.hotspotTestGrid();
        if (!alive) return;
        setTiles(data.tiles);
        setSummary(data.summary);
        setChecked(new Date().toISOString());
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

  const chosen = useMemo(() => (tiles ?? []).find((t) => t.id === selected) ?? null, [tiles, selected]);

  if (error && !tiles) return <p className="error">Could not load the grid scan: {error}</p>;
  if (!tiles || !summary) return <p className="muted">Loading the grid scan…</p>;

  return (
    <div className="grid-scan">
      <div className="grid-head">
        <strong>Grid scan, round {summary.round}</strong>
        <span className="muted small">
          Today {summary.today.scans}/{summary.today.max_scans} scenes ({summary.today.firsts}/{summary.today.max_firsts} first dates) ·{" "}
          {summary.queued_waiting_for_result} waiting for a result · {checked ? `Updated ${when(checked)}` : ""}
        </span>
      </div>
      {error && <p className="error">Could not refresh: {error}. The map shows the previous state.</p>}
      <svg className="grid-map" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Grid scan status map">
        {land.map((ring, i) => (
          <polygon key={i} className="land" points={ring.map(([lon, lat]) => `${px(lon)},${py(lat)}`).join(" ")} />
        ))}
        {tiles.map((t) => (
          <rect
            key={t.id}
            className={`tile ${t.status}${t.id === selected ? " selected" : ""}`}
            x={px(t.bbox[0])}
            y={py(t.bbox[3])}
            width={(t.bbox[2] - t.bbox[0]) * SCALE}
            height={(t.bbox[3] - t.bbox[1]) * SCALE}
            onClick={() => setSelected(t.id === selected ? null : t.id)}
          >
            <title>{`${t.id}: ${name(t.status)}${t.scans.length ? ` (${t.scans.length} scene${t.scans.length > 1 ? "s" : ""})` : ""}`}</title>
          </rect>
        ))}
        {LIVE.map(([name, b]) => (
          <g key={name}>
            <rect className="live" x={px(b[0])} y={py(b[3])} width={(b[2] - b[0]) * SCALE} height={(b[3] - b[1]) * SCALE} />
            <text className="live-label" x={px(b[0])} y={py(b[3]) - 2}>{name}</text>
          </g>
        ))}
      </svg>
      <div className="grid-legend">
        {ORDER.filter((k) => summary.by_status[k]).map((k) => (
          <span key={k}><i className={`swatch ${k}`} /> {name(k)} ({summary.by_status[k]})</span>
        ))}
      </div>
      {chosen ? (
        <div className="grid-detail">
          <strong>{chosen.id}</strong> · {name(chosen.status)}{chosen.decision ? ` (closed: ${label(chosen.decision)})` : ""}{chosen.round ? ` · round ${chosen.round}` : ""}
          {chosen.scans.length ? (
            <table className="runs">
              <thead><tr><th>Date</th><th>Source</th><th>Captured</th><th>State</th><th>Hulls</th><th>Pairs</th><th>Review</th></tr></thead>
              <tbody>
                {chosen.scans.map((s) => (
                  <tr key={s.date_no}>
                    <td>{s.date_no}</td>
                    <td>{s.source.toUpperCase()}</td>
                    <td>{when(s.scene_time)}</td>
                    <td>{label(s.status)}</td>
                    <td>{s.hulls ?? "–"}</td>
                    <td>{s.pairs ?? "–"}</td>
                    <td>{s.run_no ? <Link to={`/hotspot-tests/runs/${s.run_no}?scene=${encodeURIComponent(s.scene)}`}>Run {s.run_no}</Link> : "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <span className="muted small"> · not scanned yet</span>
          )}
        </div>
      ) : (
        <p className="muted small">Select a tile for its scenes. Pair labels are automatic: a tile with pairs still needs a review.</p>
      )}
    </div>
  );
}
