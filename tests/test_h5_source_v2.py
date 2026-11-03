"""Real HDF5 layout tests: no clinical data required."""
import io

import h5py
import numpy as np
import pytest
from PIL import Image

from app import h5_source


@pytest.mark.parametrize("layout", ["DCHW", "CDHW"])
def test_new_and_legacy_cect_order(tmp_path, monkeypatch, layout):
    monkeypatch.setattr(h5_source, "ROOT", tmp_path.resolve())
    pack = tmp_path / "sample.h5"
    depth = 4
    with h5py.File(pack, "w") as f:
        f.attrs["patient_id"] = "anonymous"
        f.attrs["crop_z_start"] = 17
        f.attrs["crop_z_end"] = 21
        f.attrs["spacing_xyz"] = [0.8, 0.8, 1.0]
        f.attrs["label_type"] = "HCC"
        f.create_dataset("ncct", shape=(depth, 512, 512), dtype="f2", fillvalue=20)
        shape = (depth, 3, 512, 512) if layout == "DCHW" else (3, depth, 512, 512)
        for name in ("syn_cect", "real_cect"):
            ds = f.create_dataset(name, shape=shape, dtype="f2", fillvalue=0)
            for z in range(depth):
                for channel in range(3):
                    value = 20 + 40*channel + 10*z + (10 if name == "syn_cect" else 0)
                    if layout == "DCHW":
                        ds[z, channel, :, :] = value
                    else:
                        ds[channel, z, :, :] = value
        liver = f.create_dataset("liver_mask", shape=(depth,512,512), dtype="u1")
        liver[2, 10:30, 10:30] = 5
        f.create_dataset("tumor_mask", shape=(depth,512,512), dtype="u1")
    assert h5_source.describe(pack) == (depth, 2, 19)
    path = h5_source.resolve_h5("sample.h5")
    assert path == pack.resolve()
    for name in ("syn_cect","real_cect"):
        for phase,channel in zip(h5_source.PHASES,range(3)):
            out = h5_source.render(h5_source.encode("sample.h5",name,1,phase))
            with Image.open(io.BytesIO(out)) as image:
                value = 20 + 40*channel + 10 + (10 if name=="syn_cect" else 0)
                expected = int(np.uint8(np.clip((value+160)*255.0/400.0,0,255)))
                assert image.size == (512,512)
                assert image.getpixel((20,20)) == expected
    with Image.open(io.BytesIO(h5_source.render(h5_source.encode("sample.h5","ncct",1,"NCCT")))) as image:
        assert image.size == (512,512)
    with pytest.raises((ValueError, IndexError)):
        h5_source.render(h5_source.encode("sample.h5","syn_cect",99,"AP"))


def test_invalid_shape_rejected(tmp_path):
    filename = tmp_path / "bad.h5"
    with h5py.File(filename,"w") as f:
        f.create_dataset("ncct", shape=(5,512,512), dtype="f2")
        f.create_dataset("syn_cect", shape=(5,3,512,512), dtype="f2")
        f.create_dataset("real_cect", shape=(5,2,512,512), dtype="f2")
    with pytest.raises(ValueError,match="Invalid"):
        h5_source.describe(filename)
