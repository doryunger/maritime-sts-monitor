import importlib
import json
import math
import sys

import yaml

spec = json.load(open(sys.argv[1], encoding="utf-8"))
sys.argv = ["rslp.main", spec["project"], "predict"] + spec["args"]

import shapely
from rslearn.const import WGS84_PROJECTION
from rslearn.utils.geometry import STGeometry
from rslearn.utils.jsonargparse import init_jsonargparse
from rslp.main import main
from rslp.utils.mp import init_mp

MODULE = importlib.import_module(f"rslp.{spec['project']}.predict_pipeline")
SETUP = "setup_dataset_with_image_files" if spec["project"] == "sentinel1_vessels" else "setup_dataset_with_scene_ids"
DETECTOR_GROUPS = ("detector_predict", "default")
RECORDED = []


def model_patches() -> dict:
    config = yaml.safe_load(open(MODULE.DETECT_MODEL_CONFIG, encoding="utf-8"))["data"]["init_args"]["predict_config"]
    size = config["patch_size"]
    ratio = config.get("overlap_ratio", 0)
    return {"patch_size": size, "overlap_ratio": ratio, "overlap_pixels": round(size * ratio)}


def write_windows() -> None:
    document = model_patches() | {"windows": RECORDED}
    open(spec["windows_path"], "w", encoding="utf-8").write(json.dumps(document))


class RecordingWindow(MODULE.Window):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.group in DETECTOR_GROUPS:
            RECORDED.append({"name": self.name, "group": self.group, "crs": str(self.projection.crs), "x_resolution": self.projection.x_resolution,
                             "y_resolution": self.projection.y_resolution, "bounds": [int(v) for v in self.bounds]})
            write_windows()


MODULE.Window = RecordingWindow


def narrowed(*args, **kwargs):
    scene_datas = ORIGINAL(*args, **kwargs)
    if len(scene_datas) != len(spec["regions"]):
        raise SystemExit(f"{len(scene_datas)} windows for {len(spec['regions'])} regions")
    for scene_data, (w, s, e, n) in zip(scene_datas, spec["regions"]):
        x0, y0, x1, y1 = STGeometry(WGS84_PROJECTION, shapely.box(w, s, e, n), None).to_projection(scene_data.projection).shp.bounds
        b = scene_data.bounds
        scene_data.bounds = (max(b[0], math.floor(x0)), max(b[1], math.floor(y0)), min(b[2], math.ceil(x1)), min(b[3], math.ceil(y1)))
        if scene_data.bounds[2] <= scene_data.bounds[0] or scene_data.bounds[3] <= scene_data.bounds[1]:
            raise SystemExit(f"region {(w, s, e, n)} lies outside the scene")
        print(f"window narrowed to {scene_data.bounds[2] - scene_data.bounds[0]} x {scene_data.bounds[3] - scene_data.bounds[1]} px of {b[2] - b[0]} x {b[3] - b[1]}", flush=True)
    return scene_datas


if spec.get("regions"):
    ORIGINAL = getattr(MODULE, SETUP)
    setattr(MODULE, SETUP, narrowed)

if __name__ == "__main__":
    init_mp()
    init_jsonargparse()
    main()
