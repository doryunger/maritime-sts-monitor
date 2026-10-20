export type Counts = Record<string, number>;

export interface RunSummary {
  run: number;
  created: string;
  area: string | null;
  status: string;
  review?: string;
  window?: [string, string] | null;
  sources?: string | null;
  captured?: [string | null, string | null];
  finished?: string | null;
  scenes?: number;
  scenes_failed?: number;
  run_verdicts?: Counts;
  verdicts?: Counts;
  reviewed?: number;
  listed?: number;
}

export interface SceneRow {
  run: number;
  area: string;
  source: string;
  scene_id: string;
  captured: string;
  created: string | null;
  status: string;
  review: string;
}

export interface LocalScene {
  run: number;
  site: string;
  source: string;
  scene_id: string;
  captured: string;
  status: string;
}

export interface QueueJob {
  job_id: string;
  site: string;
  status: string;
  created_utc: string;
  scene_details: { scene_id: string; source: string; captured: string; local_runs: LocalScene[] }[];
}

export interface QueueSnapshot {
  checked_utc: string;
  jobs: QueueJob[] | null;
  local: LocalScene[];
  error: string | null;
}

export interface AisParty {
  is_sanctioned: boolean;
  slot: string;
  name: string | null;
  flag: string | null;
  imo: string | null;
  mmsi: string | null;
  distance_m: number | null;
  age_min: number | null;
}

export interface AisCandidate {
  is_sanctioned: boolean;
  ship_id: string;
  name: string;
  type: string | null;
  type_code: string | null;
  flag: string | null;
  seen_utc: string;
  age_min: number;
  method: string;
  before_age_min: number | null;
  after_age_min: number | null;
  bracket_gap_min: number | null;
  distance_m: number;
  ais_length_m: number | null;
  ais_width_m: number | null;
  dwt: number | null;
  length_diff_m: number | null;
  heading_diff_deg: number | null;
  decision: string;
  reasons: string;
}

export interface AisSlotDiagnostics {
  candidate_count: number;
  satellite_excluded: number;
  unnamed_excluded: number;
  candidates: AisCandidate[];
}

export interface Hull {
  id: string;
  scene_id: string;
  site: string;
  hull_id: string;
  source: string;
  time: string;
  lat: number;
  lon: number;
  verdict: string;
  final_verdict: string;
  role: string | null;
  image: boolean;
  ai2: Record<string, number | null>;
  measures: Record<string, number | string | null>;
  ais: AisParty[];
  ais_diagnostics?: Record<"a" | "b", AisSlotDiagnostics> | null;
  ais_status?: "matched" | "no_match" | "no_data" | "not_checked";
  vessels?: { n: number; fx: number; fy: number }[];
}

export interface Flag {
  hull_id: string;
  status: string;
  correct_verdict: string | null;
  note: string | null;
  reviewer: string | null;
  updated_utc: string | null;
  final_verdict?: string;
}

export interface Scene {
  scene_id: string;
  site: string;
  source: string;
  time: string;
  status: string;
  detector_s: number | null;
  raw: number | null;
  kept: number | null;
  checked: number | null;
  error: string | null;
  [verdict: string]: unknown;
}

export interface FixtureItem {
  key: string | number;
  label_user?: string | null;
  fixture_verdict: string;
  run_verdict: string | null;
  status: string;
  reason?: string;
  distance_m: number | null;
}

export interface FixturePass {
  name: string;
  source: string;
  date: string | null;
  covered: boolean;
  items: FixtureItem[];
  totals: { objects: number; agree: number; differ: number; missing: number; outside_area?: number } | null;
}

export interface RunDetail {
  number: number;
  summary: RunSummary;
  run: {
    hot_spot: string;
    window: [string, string];
    sources: string;
    rules_sha1: string;
    commit: string;
    runs: { run_id: number; start: string; end: string | null; status: string | null }[];
    singles_sample: { n: number; of: number; seed: number };
  };
  scenes: Scene[];
  hulls: Hull[];
  flags: Flag[];
  fixtures: { passes: FixturePass[]; totals: { objects: number; agree: number; differ: number; missing: number } | null };
}

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || r.statusText);
  }
  return r.json();
}

export const api = {
  jobs: () => fetch("/api/jobs").then((r) => json<QueueSnapshot>(r)),
  runs: () => fetch("/api/runs").then((r) => json<RunSummary[]>(r)),
  scenes: () => fetch("/api/scenes").then((r) => json<SceneRow[]>(r)),
  run: (n: number) => fetch(`/api/runs/${n}`).then((r) => json<RunDetail>(r)),
  image: (n: number, id: string, kind: "raw" | "overlay") => `/api/runs/${n}/hulls/${encodeURIComponent(id)}/${kind}.png`,
  saveFlag: (n: number, id: string, body: { status: string; correct_verdict: string | null; note: string }) =>
    fetch(`/api/runs/${n}/flags/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<Flag>(r)),
};

export const sceneTag = (id: string) => {
  const tile = id.match(/_(T\d{2}[A-Z]{3})_/);
  if (tile) return tile[1];
  const orbit = id.split("_");
  return orbit.length > 6 ? `orbit ${orbit[6]}` : id.slice(0, 12);
};

export const when = (t?: string | null) => (t ? t.replace("T", " ").slice(0, 16) + "Z" : "–");
export const label = (v?: string | null) => (v ? v.replace(/_/g, " ") : "–");
export const num = (v: unknown, d = 2) =>
  v === null || v === undefined ? "n/a" : typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(d)) : String(v);
