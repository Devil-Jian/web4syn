"""Read authorized 2D HU slices from read-only HDF5 patient packs.

Supported CECT arrangements:
- newer: [D, 3, H, W]  (slice, AP/PVP/DP, Y, X)
- legacy: [3, D, H, W] (AP/PVP/DP, slice, Y, X)
Only a single requested slice is read; volumes and source paths are never exposed.
"""
from pathlib import Path
import io
import os
import h5py
import numpy as np
from PIL import Image

ROOT = Path(os.getenv("WEB4SYN_PACKS_ROOT", "/packs")).resolve()
PHASES = ("AP", "PVP", "DP")
DATASETS = {"NCCT": "ncct", "REAL": "real_cect", "VIRTUAL": "syn_cect"}


def patient_files():
    if not ROOT.is_dir():
        return []
    return sorted(
        p for p in ROOT.glob("*.h5")
        if p.is_file() and not p.is_symlink()
    )


def resolve_h5(filename):
    if (not isinstance(filename, str) or
        Path(filename).name != filename or
        filename in (".", "..") or
        not filename.endswith(".h5")):
        raise ValueError("Invalid HDF5 filename")
    path = (ROOT / filename).resolve()
    if path.parent != ROOT or not path.is_file():
        raise ValueError("HDF5 file unavailable")
    if path.is_symlink():
        raise ValueError("Symlink HDF5 files are not allowed")
    return path


def _cect_layout(ds, depth, height, width):
    shape = ds.shape
    if shape == (depth, 3, height, width):
        return "DCHW"
    if shape == (3, depth, height, width):
        return "CDHW"
    raise ValueError(
        f"Invalid {ds.name} shape {shape}, expected "
        f"({depth},3,{height},{width}) or (3,{depth},{height},{width})"
    )


def _validate(f):
    if "ncct" not in f:
        raise ValueError("Missing ncct dataset")
    ncct = f["ncct"]
    if not isinstance(ncct, h5py.Dataset) or ncct.ndim != 3:
        raise ValueError("Invalid NCCT dataset")
    depth, height, width = (int(n) for n in ncct.shape)
    if depth < 1 or (height, width) != (512, 512):
        raise ValueError("Invalid NCCT shape; expected [D,512,512]")
    layouts = {}
    for name in ("real_cect", "syn_cect"):
        if name not in f or not isinstance(f[name], h5py.Dataset):
            raise ValueError(f"Missing {name} dataset")
        layouts[name] = _cect_layout(f[name], depth, height, width)

    for name in ("liver_mask", "tumor_mask"):
        if name in f and f[name].shape != (depth, height, width):
            raise ValueError(f"Invalid {name} shape")
    if "spacing_xyz" in f.attrs:
        spacing = np.asarray(f.attrs["spacing_xyz"], dtype=float)
        if spacing.shape != (3,) or not np.all(np.isfinite(spacing)) or np.any(spacing <= 0):
            raise ValueError("Invalid spacing_xyz attribute")
    return depth, layouts


def describe(path):
    """Validate pack and return (depth, representative_slice, original_z).

    A representative slice is selected using the liver label 5 if present.
    If label 5 is absent, falls back to nonzero mask voxels.
    """
    with h5py.File(path, "r") as f:
        depth, _ = _validate(f)
        middle = depth // 2
        if "liver_mask" in f:
            mask = f["liver_mask"]
            # Read mask slice-by-slice to avoid materializing entire volumes.
            label5 = []
            nonzero = []
            for z in range(depth):
                slice_mask = mask[z]
                if np.any(slice_mask == 5):
                    label5.append(z)
                if np.any(slice_mask != 0):
                    nonzero.append(z)
            selected = label5 or nonzero
            if selected:
                middle = selected[len(selected) // 2]
        if "slice_indices" in f:
            original_z = int(f["slice_indices"][middle])
        elif "crop_z_start" in f.attrs:
            original_z = int(f.attrs["crop_z_start"]) + middle
        else:
            original_z = middle
        return depth, middle, original_z


def encode(filename, dataset, z, phase):
    return "h5:" + filename + "|" + dataset + "|" + str(z) + "|" + phase


def render(image_ref, lo=-160.0, hi=240.0):
    if not (-1200 <= lo < hi <= 3000) or hi - lo < 1:
        raise ValueError("Invalid display window")
    if not isinstance(image_ref, str) or not image_ref.startswith("h5:"):
        raise ValueError("Invalid image reference")
    data = image_ref[3:].split("|")
    if len(data) != 4:
        raise ValueError("Invalid image reference")
    filename, dataset, z_text, phase = data
    if dataset not in ("ncct", "real_cect", "syn_cect") or phase not in ("NCCT", *PHASES):
        raise ValueError("Invalid modality")
    if (dataset == "ncct" and phase != "NCCT" or
        dataset != "ncct" and phase == "NCCT"):
        raise ValueError("Mismatched dataset and phase")
    index = int(z_text)
    with h5py.File(resolve_h5(filename), "r") as f:
        ncct = f["ncct"]
        depth, height, width = ncct.shape
        if index < 0 or index >= depth:
            raise IndexError("Slice index out of range")
        if dataset == "ncct":
            image = np.asarray(ncct[index, :, :], dtype=np.float32)
        else:
            arr = f[dataset]
            layout = _cect_layout(arr, depth, height, width)
            channel = PHASES.index(phase)
            if layout == "DCHW":
                image = np.asarray(arr[index, channel, :, :], dtype=np.float32)
            else:
                image = np.asarray(arr[channel, index, :, :], dtype=np.float32)
    image = np.nan_to_num(image, nan=lo, posinf=hi, neginf=lo)
    pixels = np.clip((image - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(pixels, mode="L").save(buf, format="PNG")
    return buf.getvalue()
