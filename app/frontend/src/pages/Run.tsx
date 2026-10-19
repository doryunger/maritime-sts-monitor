import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, Flag, label, num, RunDetail, sceneTag, when } from "../api";
import HullCard from "../components/HullCard";

const SECTIONS = ["pair", "possible_pair", "unmeasured", "moving", "single"];
const VERDICTS = ["pair", "possible_pair", "single", "moving", "unmeasured", "excluded", "unresolved"];
const LABELLED = ["ok", "is_single", "is_moving", "is_pair"];
const FILTERS: [string, string][] = [
  ["all", "All"],
  ["unflagged", "Unflagged"],
  ["issue", "Issues"],
  ["ok", "OK"],
  ["changed", "Changed by QA"],
];
const TABS: [string, string][] = [
  ["qa", "QA"],
  ["scenes", "Scenes"],
  ["fixtures", "Fixtures"],
];

export default function Run() {
  const no = Number(useParams().no);
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "qa";
  const filter = params.get("filter") || "all";
  const scene = params.get("scene");
  const focus = params.get("hull");
  const [doc, setDoc] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDoc(null);
    api.run(no).then(setDoc).catch((e) => setError(e.message));
  }, [no]);

  useEffect(() => {
    if (doc && focus) document.getElementById(`hull-${focus}`)?.scrollIntoView({ block: "center" });
  }, [doc, focus]);

  const flags = useMemo(() => Object.fromEntries((doc?.flags || []).map((f) => [f.hull_id, f])), [doc]);

  const set = (k: string, v: string | null) => {
    const p = new URLSearchParams(params);
    if (v === null) p.delete(k);
    else p.set(k, v);
    setParams(p, { replace: true });
  };

  const onSaved = (f: Flag) =>
    setDoc((d) =>
      d && {
        ...d,
        flags: [...d.flags.filter((x) => x.hull_id !== f.hull_id), f],
        hulls: d.hulls.map((h) => (h.id === f.hull_id ? { ...h, final_verdict: f.final_verdict ?? h.final_verdict } : h)),
      },
    );

  if (error) return <p className="error">Could not load run {no}: {error}</p>;
  if (!doc) return <p className="muted">Loading run {no}… (the first open of a run reads all of its scenes and takes a few seconds)</p>;

  const s = doc.summary;
  const flagged = doc.flags.length;
  const issues = doc.flags.filter((f) => !LABELLED.includes(f.status)).length;
  const keep = (id: string, verdict: string, final: string, sceneId: string) => {
    if (scene && sceneId !== scene) return false;
    const f = flags[id];
    if (filter === "unflagged") return !f;
    if (filter === "issue") return !!f && !LABELLED.includes(f.status);
    if (filter === "ok") return !!f && LABELLED.includes(f.status);
    if (filter === "changed") return verdict !== final;
    return true;
  };

  return (
    <section>
      <p className="crumbs"><Link to="/">Runs</Link> / {no}</p>
      <h1>Run {no} <span className="muted">· {label(s.area)}</span></h1>
      <dl className="facts">
        <div><dt>Status</dt><dd><span className={`status s-${s.status}`}>{s.status}</span></dd></div>
        <div><dt>Created</dt><dd className="mono">{when(s.created)}</dd></div>
        <div><dt>Sources</dt><dd>{doc.run.sources}</dd></div>
        <div><dt>Scenes</dt><dd>{s.scenes}{s.scenes_failed ? ` (${s.scenes_failed} failed)` : ""}</dd></div>
        <div><dt>Rules / code</dt><dd className="mono">{doc.run.rules_sha1?.slice(0, 8)} / {doc.run.commit?.slice(0, 8)}</dd></div>
      </dl>
      <div className="wrap">
        <table className="verdicts">
          <thead>
            <tr><th></th>{VERDICTS.map((v) => <th key={v} className="n">{label(v)}</th>)}</tr>
          </thead>
          <tbody>
            <tr><th>Run</th>{VERDICTS.map((v) => <td key={v} className="n">{s.run_verdicts?.[v] ?? 0}</td>)}</tr>
            <tr><th>After QA</th>{VERDICTS.map((v) => <td key={v} className="n">{s.verdicts?.[v] ?? 0}</td>)}</tr>
          </tbody>
        </table>
      </div>
      <nav className="tabs" role="tablist">
        {TABS.map(([k, t]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => set("tab", k)}>{t}</button>
        ))}
      </nav>
      {tab === "qa" && (
        <>
          <div className="toolbar">
            {FILTERS.map(([k, t]) => (
              <button key={k} className="chip" aria-pressed={filter === k} onClick={() => set("filter", k)}>{t}</button>
            ))}
            {scene && (
              <button className="chip" aria-pressed={true} onClick={() => set("scene", null)} title={scene}>
                {(doc.scenes.find((x) => x.scene_id === scene)?.source || "").toUpperCase()} {sceneTag(scene)} {when(doc.scenes.find((x) => x.scene_id === scene)?.time)} ×
              </button>
            )}
            <span className="muted small">
              {flagged} of {doc.hulls.length} reviewed, {issues} with issues. Singles: a sample of {doc.run.singles_sample.n} of {doc.run.singles_sample.of}.
            </span>
          </div>
          {SECTIONS.map((sec) => {
            const hulls = doc.hulls.filter((h) => (SECTIONS.includes(h.verdict) ? h.verdict : "single") === sec && keep(h.id, h.verdict, h.final_verdict, h.scene_id));
            if (!hulls.length) return null;
            return (
              <div key={sec}>
                <h2>{label(sec)} <span className="muted">({hulls.length})</span></h2>
                <div className="grid">
                  {hulls.map((h) => <HullCard key={h.id} run={no} hull={h} flag={flags[h.id]} onSaved={onSaved} focused={h.hull_id === focus} />)}
                </div>
              </div>
            );
          })}
        </>
      )}
      {tab === "scenes" && (
        <div className="wrap">
          <table>
            <thead>
              <tr>
                <th>Source</th><th>Captured</th><th>Scene</th><th>Status</th>
                <th className="n">Raw</th><th className="n">Kept</th><th className="n">Checked</th>
                {SECTIONS.map((v) => <th key={v} className="n">{label(v)}</th>)}
                <th className="n">Detector s</th>
              </tr>
            </thead>
            <tbody>
              {doc.scenes.map((sc) => (
                <tr key={sc.scene_id + sc.site}>
                  <td>{sc.source.toUpperCase()}</td>
                  <td className="mono">{when(sc.time)}</td>
                  <td className="mono small" title={sc.error || ""}>{sc.scene_id}</td>
                  <td><span className={`status s-${sc.status}`}>{sc.status}</span></td>
                  <td className="n">{sc.raw ?? "–"}</td>
                  <td className="n">{sc.kept ?? "–"}</td>
                  <td className="n">{sc.checked ?? "–"}</td>
                  {SECTIONS.map((v) => <td key={v} className="n">{String(sc[v] ?? 0)}</td>)}
                  <td className="n">{num(sc.detector_s, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {tab === "fixtures" && (
        <div>
          {doc.fixtures.totals ? (
            <p>
              Labelled fixtures in this run's captures: {doc.fixtures.totals.agree} of {doc.fixtures.totals.objects} agree,{" "}
              {doc.fixtures.totals.differ} differ, {doc.fixtures.totals.missing} missing.
            </p>
          ) : (
            <p className="muted">No labelled fixtures cover this run.</p>
          )}
          {doc.fixtures.passes.filter((p) => p.covered).map((p) => (
            <details key={p.name} open={!!p.totals && p.totals.differ + p.totals.missing > 0}>
              <summary>
                {p.name} ({p.source.toUpperCase()}): {p.totals?.agree} of {p.totals?.objects} agree, {p.totals?.differ} differ, {p.totals?.missing} missing
              </summary>
              <div className="wrap">
                <table>
                  <thead>
                    <tr><th>Object</th><th>Label</th><th>Fixture</th><th>Run</th><th>Status</th><th className="n">Distance m</th></tr>
                  </thead>
                  <tbody>
                    {p.items.filter((i) => i.status !== "outside_area").map((i) => (
                      <tr key={String(i.key)} className={i.status === "agree" ? "" : "hl"}>
                        <td className="mono">{String(i.key)}</td>
                        <td>{i.label_user || "–"}</td>
                        <td>{label(i.fixture_verdict)}</td>
                        <td>{label(i.run_verdict)}</td>
                        <td>{i.status}{i.reason ? ` (${label(i.reason)})` : ""}</td>
                        <td className="n">{num(i.distance_m, 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          ))}
        </div>
      )}
    </section>
  );
}
