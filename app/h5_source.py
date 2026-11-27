"""Read authorized 2D HU slices from read-only HDF5 patient packs.

Supported CECT arrangements:
- newer: [D, 3, H, W]  (slice, AP/PVP/DP, Y, X)
- legacy: [3, D, H, W] (AP/PVP/DP, slice, Y, X)
Only a single requested slice is read; volumes and source paths are never exposed.
"""
from contextlib import ExitStack
from pathlib import Path
import io
import os
import h5py
import numpy as np
from PIL import Image

ROOT = Path(os.getenv("WEB4SYN_PACKS_ROOT", "/packs")).resolve()
_SYN_ENV = os.getenv("WEB4SYN_SYN_PACKS_ROOT", "").strip()
SYN_ROOT = Path(_SYN_ENV).resolve() if _SYN_ENV else None
PHASES = ("AP", "PVP", "DP")
DATASETS = {"NCCT": "ncct", "REAL": "real_cect", "VIRTUAL": "syn_cect"}


def patient_files():
    if not ROOT.is_dir():
        return []
    return sorted(
        p for p in ROOT.glob("*.h5")
        if p.is_file() and not p.is_symlink()
    )


def resolve_h5(filename, root=None):
    base = ROOT if root is None else root
    if (not isinstance(filename, str) or
        Path(filename).name != filename or
        filename in (".", "..") or
        not filename.endswith(".h5")):
        raise ValueError("Invalid HDF5 filename")
    path = (base / filename).resolve()
    if path.parent != base or not path.is_file():
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


def _cect_shape_info(ds):
    shape = ds.shape
    if ds.ndim != 4 or shape[2:] != (512, 512):
        raise ValueError(f"Invalid {ds.name} shape {shape}")
    if shape[1] == 3 and shape[0] >= 1:
        return "DCHW", int(shape[0])
    if shape[0] == 3 and shape[1] >= 1:
        return "CDHW", int(shape[1])
    raise ValueError(f"Invalid {ds.name} shape {shape}")


def _validate(f, syn_file=None):
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
        src = syn_file if (name == "syn_cect" and syn_file is not None) else f
        if name not in src or not isinstance(src[name], h5py.Dataset):
            raise ValueError(f"Missing {name} dataset")
        layouts[name] = _cect_layout(src[name], depth, height, width)

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
    with ExitStack() as stack:
        f = stack.enter_context(h5py.File(path, "r"))
        syn_file = None
        if SYN_ROOT is not None:
            syn_file = stack.enter_context(h5py.File(resolve_h5(Path(path).name, SYN_ROOT), "r"))
        depth, _ = _validate(f, syn_file=syn_file)
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
    if dataset == "syn_cect" and SYN_ROOT is not None:
        with h5py.File(resolve_h5(filename, SYN_ROOT), "r") as f:
            arr = f["syn_cect"]
            layout, depth = _cect_shape_info(arr)
            if index < 0 or index >= depth:
                raise IndexError("Slice index out of range")
            channel = PHASES.index(phase)
            image = np.asarray(arr[index, channel, :, :] if layout == "DCHW" else arr[channel, index, :, :], dtype=np.float32)
    else:
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


def render_batch(filename, dataset, phases, start, end, lo=-160.0, hi=240.0):
    """Render a bounded half-open slice interval, opening each HDF5 pack once.

    Returns (phase, z, png_bytes) tuples. The caller must authorize phases
    and the patient before invoking this function.
    """
    if not (-1200 <= lo < hi <= 3000) or hi - lo < 1:
        raise ValueError("Invalid display window")
    if not (0 <= start < end and end - start <= 12):
        raise ValueError("Invalid batch range")
    if dataset not in ("ncct", "real_cect", "syn_cect"):
        raise ValueError("Invalid dataset")
    if not phases or len(phases) > 4 or len(set(phases)) != len(phases):
        raise ValueError("Invalid phases")
    if (dataset == "ncct") != (tuple(phases) == ("NCCT",)):
        # Mixed NCCT and CECT is allowed only for one exact CECT data source.
        if dataset == "ncct" or "NCCT" not in phases:
            raise ValueError("Invalid phase and dataset pairing")
    if any(phase not in ("NCCT", *PHASES) for phase in phases):
        raise ValueError("Invalid phase")

    results = []
    with ExitStack() as stack:
        f = stack.enter_context(h5py.File(resolve_h5(filename), "r"))
        ncct = f["ncct"]
        depth, height, width = ncct.shape
        if end > depth:
            raise IndexError("Batch slice out of range")
        ce = None
        layout = None
        if any(phase != "NCCT" for phase in phases):
            if dataset == "ncct":
                raise ValueError("CECT requires real or synthetic dataset")
            if dataset == "syn_cect" and SYN_ROOT is not None:
                syn_f = stack.enter_context(h5py.File(resolve_h5(filename, SYN_ROOT), "r"))
                ce = syn_f["syn_cect"]
            else:
                ce = f[dataset]
            layout = _cect_layout(ce, depth, height, width)
        for z in range(start, end):
            for phase in phases:
                if phase == "NCCT":
                    image = np.asarray(ncct[z], dtype=np.float32)
                else:
                    channel = PHASES.index(phase)
                    array = ce[z, channel] if layout == "DCHW" else ce[channel, z]
                    image = np.asarray(array, dtype=np.float32)
                image = np.nan_to_num(image, nan=lo, posinf=hi, neginf=lo)
                pixels = np.clip((image - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
                buf = io.BytesIO()
                Image.fromarray(pixels, mode="L").save(buf, format="PNG")
                results.append((phase, z, buf.getvalue()))
    return results
