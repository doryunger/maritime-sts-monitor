import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, HotspotTestArea, HotspotTestParquet, label, RunSummary, when } from "../api";

function size(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export default function HotspotTests() {
  const [areas, setAreas] = useState<HotspotTestArea[] | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [parquets, setParquets] = useState<HotspotTestParquet[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const [nextAreas, nextRuns, nextParquets] = await Promise.all([
          api.hotspotTestAreas(),
          api.hotspotTestRuns(),
          api.hotspotTestParquets(),
        ]);
        if (!active) return;
        setAreas(nextAreas);
        setRuns(nextRuns.sort((a, b) => b.run - a.run));
        setParquets(nextParquets.sort((a, b) => b.last_modified.localeCompare(a.last_modified)));
        setError(null);
      } catch (e) {
        if (active) setError((e as Error).message);
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 60000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  if (!areas || !runs || !parquets) {
    return <section><h1>Hotspot tests</h1>{error ? <p className="error">Could not load test results: {error}</p> : <p className="muted">Loading test results…</p>}</section>;
  }

  const areaLabels = Object.fromEntries(areas.map((area) => [area.name, area.label]));

  return (
    <section>
      <h1>Hotspot tests</h1>
      <p className="muted">Exploratory pair findings only. These scans do not query AIS or publish to the regular findings feed.</p>
      {error && <p className="error">Some data could not refresh: {error}</p>}
      <h2>Scan areas</h2>
      <div className="wrap">
        <table>
          <thead><tr><th>Area</th><th>Version</th><th>Scan bounds W, S, E, N</th><th>Reference bounds</th><th>Why it is included</th></tr></thead>
          <tbody>
            {areas.map((area) => (
              <tr key={area.name}>
                <td>{area.label}</td>
                <td className="n">{area.area_version}</td>
                <td className="mono">{area.bbox.join(", ")}</td>
                <td className="mono">{area.reference_bbox.join(", ")}</td>
                <td>{area.rationale}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>Scan runs</h2>
      {runs.length ? (
        <div className="wrap">
          <table>
            <thead><tr><th>Run</th><th>Area</th><th>Captured</th><th>Status</th><th>Scenes</th><th>Pair candidates</th><th>Review</th></tr></thead>
            <tbody>
              {runs.map((run) => {
                const pairs = (run.run_verdicts?.pair || 0) + (run.run_verdicts?.possible_pair || 0);
                return (
                  <tr key={run.run}>
                    <td className="mono"><Link to={`/hotspot-tests/runs/${run.run}`}>{run.run}</Link></td>
                    <td>{areaLabels[run.area || ""] || label(run.area)}</td>
                    <td className="mono">{run.captured?.[0] ? when(run.captured[0]) : "—"}</td>
                    <td><span className={`status s-${run.status}`}>{run.status}</span></td>
                    <td className="n">{run.scenes ?? 0}{run.scenes_failed ? ` (${run.scenes_failed} failed)` : ""}</td>
                    <td className="n">{pairs}</td>
                    <td><span className={`status r-${(run.review || "nothing to review").replace(/ /g, "-")}`}>{run.review || "nothing to review"}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">No hotspot-test scenes have completed yet.</p>}
      <h2>Findings Parquets</h2>
      {parquets.length ? (
        <div className="wrap">
          <table>
            <thead><tr><th>Object key</th><th>Size</th><th>Updated</th></tr></thead>
            <tbody>
              {parquets.map((item) => (
                <tr key={item.key}>
                  <td className="mono"><a href={api.hotspotTestParquet(item.key)}>{item.key}</a></td>
                  <td className="n">{size(item.size)}</td>
                  <td className="mono">{when(item.last_modified)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">No findings Parquets have been written yet.</p>}
    </section>
  );
}
