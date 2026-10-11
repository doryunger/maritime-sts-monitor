CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS vessel_state (
    mmsi BIGINT PRIMARY KEY,
    name TEXT,
    ship_type INT,
    length_m REAL,
    destination TEXT,
    is_class_b BOOLEAN NOT NULL DEFAULT FALSE,
    region TEXT,
    last_time TIMESTAMPTZ NOT NULL,
    last_geom GEOGRAPHY(POINT, 4326) NOT NULL,
    last_sog REAL,
    last_cog REAL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    msg_count BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS vessel_state_last_time ON vessel_state (last_time);

CREATE TABLE IF NOT EXISTS track_points (
    mmsi BIGINT NOT NULL,
    time TIMESTAMPTZ NOT NULL,
    geom GEOGRAPHY(POINT, 4326) NOT NULL,
    sog REAL,
    cog REAL,
    PRIMARY KEY (mmsi, time)
);
CREATE INDEX IF NOT EXISTS track_points_time ON track_points (time);
CREATE INDEX IF NOT EXISTS track_points_geom ON track_points USING GIST (geom);

CREATE TABLE IF NOT EXISTS coverage_cells (
    cell_x INT NOT NULL,
    cell_y INT NOT NULL,
    msg_count INT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cell_x, cell_y)
);

CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY,
    rule TEXT NOT NULL,
    mmsi BIGINT NOT NULL,
    region TEXT,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ,
    geom GEOGRAPHY(POINT, 4326) NOT NULL,
    coverage_confidence REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alerts_mmsi ON alerts (mmsi, start_time);
CREATE INDEX IF NOT EXISTS alerts_geom ON alerts USING GIST (geom);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_open ON alerts (rule, mmsi) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS incidents (
    id BIGSERIAL PRIMARY KEY,
    mmsi BIGINT NOT NULL,
    region TEXT,
    event_time TIMESTAMPTZ NOT NULL,
    geom GEOGRAPHY(POINT, 4326) NOT NULL,
    score REAL NOT NULL,
    reasons JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    search_radius_km REAL,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incidents_status ON incidents (status);
CREATE INDEX IF NOT EXISTS incidents_event_time ON incidents (event_time DESC);

CREATE TABLE IF NOT EXISTS incident_alerts (
    incident_id BIGINT NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    alert_id BIGINT NOT NULL REFERENCES alerts (id) ON DELETE CASCADE,
    PRIMARY KEY (incident_id, alert_id)
);

CREATE TABLE IF NOT EXISTS findings (
    id BIGSERIAL PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    scene_id TEXT,
    capture_time TIMESTAMPTZ,
    offset_hours REAL,
    footprint GEOGRAPHY(POLYGON, 4326),
    crop_path TEXT,
    crop_bbox DOUBLE PRECISION[],
    outcome TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS findings_incident ON findings (incident_id);

CREATE TABLE IF NOT EXISTS detections (
    id BIGSERIAL PRIMARY KEY,
    finding_id BIGINT NOT NULL REFERENCES findings (id) ON DELETE CASCADE,
    geom GEOGRAPHY(POINT, 4326) NOT NULL,
    length_m REAL,
    score REAL,
    matched_mmsi BIGINT,
    is_candidate_subject BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS detections_finding ON detections (finding_id);
