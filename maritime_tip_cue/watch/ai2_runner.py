import importlib
import json
import math
import multiprocessing
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import torch
import yaml

CUDA_STOP = threading.Event()
RUNNER_OWNER = multiprocessing.current_process().name == "MainProcess"
LAST_GPU_SAMPLE = 0.0


def gpu_snapshot(stage: str) -> None:
    global LAST_GPU_SAMPLE
    now = datetime.now(timezone.utc).timestamp()
    if now - LAST_GPU_SAMPLE < 2:
        return
    LAST_GPU_SAMPLE = now
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
        values = [row.strip().replace(" ", "") for row in result.stdout.splitlines() if row.strip()]
        print(f"{datetime.now(timezone.utc).isoformat()} GPU_DIAGNOSTIC stage={stage} utilization_memory_mib={values}", flush=True)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"{datetime.now(timezone.utc).isoformat()} GPU_DIAGNOSTIC stage={stage} error={type(error).__name__}", flush=True)


def cuda_snapshot(stage: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    available = torch.cuda.is_available()
    if not available:
        print(f"{timestamp} CUDA_DIAGNOSTIC stage={stage} visible=false device_count=0", flush=True)
        return
    device_count = torch.cuda.device_count()
    devices = [torch.cuda.get_device_name(index) for index in range(device_count)]
    initialized = torch.cuda.is_initialized()
    if initialized:
        allocated = sum(torch.cuda.memory_allocated(index) for index in range(device_count))
        peak_allocated = sum(torch.cuda.max_memory_allocated(index) for index in range(device_count))
        reserved = sum(torch.cuda.memory_reserved(index) for index in range(device_count))
        peak_reserved = sum(torch.cuda.max_memory_reserved(index) for index in range(device_count))
        print(
            f"{timestamp} CUDA_DIAGNOSTIC stage={stage} visible=true initialized=true devices={devices} "
            f"allocated_bytes={allocated} peak_allocated_bytes={peak_allocated} "
            f"reserved_bytes={reserved} peak_reserved_bytes={peak_reserved}",
            flush=True,
        )
        return
    print(
        f"{timestamp} CUDA_DIAGNOSTIC stage={stage} visible=true initialized=false devices={devices} "
        "tensor_use=false allocated_bytes=0 peak_allocated_bytes=0 reserved_bytes=0 peak_reserved_bytes=0",
        flush=True,
    )


def monitor_cuda() -> None:
    cuda_snapshot("start")
    gpu_snapshot("start")
    while not CUDA_STOP.wait(2):
        gpu_snapshot("interval")
        if int(datetime.now(timezone.utc).timestamp()) % 60 < 2:
            cuda_snapshot("interval")


if RUNNER_OWNER:
    cuda_snapshot("process-start")

spec_path = os.environ.get("AI2_RUN_SPEC_PATH") or sys.argv[1]
spec = json.load(open(spec_path, encoding="utf-8"))
if RUNNER_OWNER:
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

if __name__ == "__main__" and RUNNER_OWNER:
    init_mp()
    init_jsonargparse()
    monitor = threading.Thread(target=monitor_cuda, daemon=True)
    monitor.start()
    try:
        print(f"{datetime.now(timezone.utc).isoformat()} AI2_RUN cwd={os.getcwd()} python={sys.executable} run_spec={spec_path}", flush=True)
        print(f"{datetime.now(timezone.utc).isoformat()} AI2_RUN writable_work={Path(spec_path).parent} scratch={spec['args'][spec['args'].index('--scratch_path') + 1]}", flush=True)
        print(f"{datetime.now(timezone.utc).isoformat()} TRAINER_DEVICE requested=auto cuda_available={torch.cuda.is_available()} device_count={torch.cuda.device_count()}", flush=True)
        main()
    finally:
        CUDA_STOP.set()
        monitor.join()
        cuda_snapshot("final")
