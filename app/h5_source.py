"""Read one 2D HU slice from a read-only patient HDF5. Never expose volumes or source paths."""
from pathlib import Path
import io
import h5py
import numpy as np
from PIL import Image

ROOT = Path("/packs").resolve()
PHASES = ("AP", "PVP", "DP")
DATASETS = {"NCCT": "ncct", "REAL": "real_cect", "VIRTUAL": "syn_cect"}

def patient_files():
    if not ROOT.is_dir():
        return []
    return sorted(p for p in ROOT.glob("*.h5") if p.is_file() and not p.is_symlink())

def resolve_h5(filename):
    if Path(filename).name != filename or not filename.endswith(".h5"):
        raise ValueError("Invalid HDF5 filename")
    path = (ROOT / filename).resolve()
    if path.parent != ROOT or not path.is_file():
        raise ValueError("HDF5 file unavailable")
    return path

def describe(path):
    with h5py.File(path, "r") as f:
        d = int(f["ncct"].shape[0])
        if d < 1 or f["ncct"].shape[1:] != (512, 512):
            raise ValueError("Invalid NCCT shape")
        for name in ("real_cect", "syn_cect"):
            if f[name].shape != (3, d, 512, 512):
                raise ValueError(f"Invalid {name} shape")
        middle = d // 2
        # Predeclare anatomic 2D slice independently of Real / Virtual appearance.
        if "liver_mask" in f:
            counts = np.asarray(f["liver_mask"][:], dtype=np.uint8).reshape(d, -1).sum(axis=1)
            valid = np.flatnonzero(counts > 0)
            if valid.size:
                middle = int(valid[len(valid) // 2])
        original_z = int(f["slice_indices"][middle]) if "slice_indices" in f else middle
        return d, middle, original_z

def encode(filename, dataset, z, phase):
    return "h5:" + filename + "|" + dataset + "|" + str(z) + "|" + phase

def render(image_ref, lo=-160.0, hi=240.0):
    if not (-1200 <= lo < hi <= 3000) or hi - lo < 1:
        raise ValueError("Invalid display window")
    data = image_ref[3:].split("|")
    if len(data) != 4:
        raise ValueError("Invalid image reference")
    filename, dataset, z, phase = data
    if dataset not in ("ncct", "real_cect", "syn_cect") or phase not in ("NCCT", *PHASES):
        raise ValueError("Invalid modality")
    with h5py.File(resolve_h5(filename), "r") as f:
        arr = f[dataset]
        index = int(z)
        if dataset == "ncct":
            image = np.asarray(arr[index], dtype=np.float32)
        else:
            image = np.asarray(arr[PHASES.index(phase), index], dtype=np.float32)
    image = np.nan_to_num(image, nan=lo, posinf=hi, neginf=lo)
    pixels = np.clip((image - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(pixels, mode="L").save(buf, format="PNG")
    return buf.getvalue()
