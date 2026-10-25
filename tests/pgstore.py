from maritime_tip_cue.watch import registry
from maritime_tip_cue.watch.state import Store

TIME = "2026-10-01T10:00:00Z"


def new_store(monkeypatch, tmp_path, **kwargs) -> Store:
    monkeypatch.setattr(registry, "runs_root", lambda: tmp_path / "runs")
    return Store.create(**kwargs)


def hull(hull_id, verdict="pair", role=None, lat=25.0, lon=56.0, **extra) -> dict:
    return {"det_id": hull_id, "lat": lat, "lon": lon, "ai2_length_m": 200.0, "verdict": verdict, "role": role, **extra}


def add_scene(store: Store, scene_id="s", site="x", source="s2", time=TIME, hulls=(), invocation=None) -> dict:
    if invocation is None:
        invocation = store.db.execute(
            "select max(invocation_id) as n from review.run_invocations where run_key = %s", (store.run_key,)
        ).fetchone()["n"] or store.start_run({}, site, ("a", "b"), source, "r", "c")
    scene = {"scene": scene_id, "site": site, "source": source, "time": time}
    store.begin_scene(invocation, scene, store.path / "scenes" / source / scene_id, None)
    store.save_detected(scene, [], {"dets": {"kept": len(hulls)}}, 1.0, {"dets": [], "index": []})
    store.save_checked(scene, [h if isinstance(h, dict) else hull(h) for h in hulls])
    return scene


def add_match(store: Store, scene_id="s", site="x", hull_id="h0", ship_id="1", name="SHIP", imo=None, mmsi=None, flag=None, chosen=True, slot="a", **extra):
    store.db.execute(
        "insert into review.ais_matches (run_key, scene_id, site, hull_id, ship_id, name, imo, mmsi, flag, chosen, slot, ais_length_m) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (store.run_key, scene_id, site, hull_id, ship_id, name, imo, mmsi, flag, chosen, slot, extra.get("ais_length_m")),
    )
