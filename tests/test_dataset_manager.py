"""Candidate catalog tests with HDF5 headers only, no image quality checks."""
import os
from pathlib import Path
import h5py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import rct, dataset_manager, h5_source


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(rct, "DATABASE", tmp_path / "study.sqlite")
    monkeypatch.setattr(h5_source, "ROOT", tmp_path / "packs")
    h5_source.ROOT.mkdir()
    monkeypatch.setenv("WEB4SYN_RCT_ADMIN_PASSWORD", "test-admin-password-1234")
    rct.initialize()
    dataset_manager.init_dataset_tables()
    app = FastAPI()
    app.include_router(rct.router)
    app.include_router(dataset_manager.router)
    with TestClient(app) as client:
        login = client.post("/api/rct/auth/login",json={
            "username":"admin","password":"test-admin-password-1234"})
        token = login.json()["token"]
        yield client, {"Authorization":"Bearer "+token}, h5_source.ROOT


def pack(path,label,depth=4):
    with h5py.File(path,"w") as f:
        f.attrs["label_type"] = label
        f.attrs["spacing_xyz"] = [0.8,0.8,1.0]
        f.create_dataset("ncct", shape=(depth,512,512),dtype="f2")


def test_scan_filter_exclude_restore_and_freeze(setup):
    client, auth, root = setup
    for i in range(4):
        pack(root / f"P{i:03d}.h5", "HCC" if i<2 else "FNH")
    assert dataset_manager.run_scan()
    stats=client.get("/api/rct/admin/dataset/status",headers=auth).json()
    assert stats["counts"]["total"] == 4
    listing=client.get("/api/rct/admin/dataset/files?page=1&page_size=2",headers=auth).json()
    assert len(listing["items"])==2 and listing["total"]==4
    first=listing["items"][0]["id"]
    excluded=client.post("/api/rct/admin/dataset/selection",headers=auth,
                         json={"ids":[first],"included":False,"reason":"manual review"})
    assert excluded.status_code==200
    dataset_manager.run_scan()
    assert client.get("/api/rct/admin/dataset/status",headers=auth).json()["counts"]["excluded"] == 1
    # The final cohort count is updated from the selected catalog, with no hand-entered CSV.
    freeze=client.post("/api/rct/admin/dataset/freeze",headers=auth)
    assert freeze.status_code==200,freeze.text
    assert freeze.json()["count"]==3
    assert client.get("/api/rct/admin/study",headers=auth).json()["target"]==3
    assert client.post("/api/rct/admin/dataset/selection",headers=auth,json={
        "ids":[first],"included":True}).status_code==409
    assert client.post("/api/rct/admin/dataset/freeze",headers=auth).status_code==409
    # Source changes never mutate the frozen patient cohort.
    pack(root/"P999.h5","ICC")
    dataset_manager.run_scan()
    assert client.get("/api/rct/admin/study",headers=auth).json()["patients"]==3


def test_scan_preserves_exclusions_and_missing_rows(setup):
    client, auth, root=setup
    for i in range(3):
        pack(root/f"Q{i}.h5","HCC")
    dataset_manager.run_scan()
    rows=client.get("/api/rct/admin/dataset/files",headers=auth).json()["items"]
    selected=rows[0]["id"]
    client.post("/api/rct/admin/dataset/selection",headers=auth,
                json={"ids":[selected],"included":False})
    (root/rows[0]["filename"]).unlink()
    dataset_manager.run_scan()
    row=client.get("/api/rct/admin/dataset/files?filter=missing",headers=auth).json()["items"][0]
    assert row["id"]==selected and row["included"]==0 and row["missing"]==1
    assert client.get("/api/rct/admin/dataset/status",headers=auth).json()["counts"]["total"]==3
