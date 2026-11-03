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

def test_gated_admin_reset_preserves_index_and_backups(setup, monkeypatch):
    client, admin_auth, root = setup
    for i in range(4):
        pack(root / f"R{i}.h5", "HCC")
    dataset_manager.run_scan()
    listing = client.get("/api/rct/admin/dataset/files",headers=admin_auth).json()["items"]
    excluded_id = listing[0]["id"]
    response=client.post("/api/rct/admin/dataset/selection",headers=admin_auth,
        json={"ids":[excluded_id],"included":False,"reason":"manual exclusion"})
    assert response.status_code==200
    assert client.post("/api/rct/admin/dataset/freeze",headers=admin_auth).status_code==200
    for i in range(6):
        created=client.post("/api/rct/admin/readers",headers=admin_auth,json={
            "username":f"ResetReader{i}","display_name":f"Reader{i}",
            "password":"reader-test-password-1234","level":"junior"})
        assert created.status_code==200,created.text
    assigned=client.post("/api/rct/admin/randomize",headers=admin_auth,json={"seed":"reset-test"})
    assert assigned.status_code==200,assigned.text
    assert client.post("/api/rct/admin/publish",headers=admin_auth).status_code==200
    with rct.database() as db:
        a=db.execute("SELECT id,reader_id FROM rct_tasks LIMIT 1").fetchone()
        username=db.execute("SELECT username FROM rct_users WHERE id=?",(a["reader_id"],)).fetchone()[0]
    logged=client.post("/api/rct/auth/login",json={"username":username,"password":"reader-test-password-1234"})
    reader_auth={"Authorization":"Bearer "+logged.json()["token"]}
    assert client.get(f"/api/rct/tasks/{a['id']}",headers=reader_auth).status_code==200
    submitted=client.post(f"/api/rct/tasks/{a['id']}/submit",headers=reader_auth,json={
        "submission_key":"reset-submission-123456","lesion_visible":"yes",
        "diagnosis":"HCC","malignancy_risk":4})
    assert submitted.status_code==200,submitted.text
    payload={"confirm":"RESET TEST STUDY","password":"test-admin-password-1234"}
    assert client.post("/api/rct/admin/reset-test-study",headers=admin_auth,json=payload).status_code==403
    monkeypatch.setenv("WEB4SYN_RCT_ALLOW_TEST_RESET","1")
    bad=client.post("/api/rct/admin/reset-test-study",headers=admin_auth,
        json={"confirm":"RESET TEST STUDY","password":"wrong"})
    assert bad.status_code==403
    result=client.post("/api/rct/admin/reset-test-study",headers=admin_auth,json=payload)
    assert result.status_code==200,result.text
    assert result.json()["removed_tasks"]==3
    assert result.json()["removed_results"]==1
    assert (rct.DATABASE.parent/"rct_backups"/result.json()["backup_name"]).is_file()
    with rct.database() as db:
        assert db.execute("SELECT COUNT(*) FROM rct_tasks").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM rct_results").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM rct_patients").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM rct_users WHERE role='reader'").fetchone()[0]==6
        assert db.execute("SELECT status,target FROM rct_studies").fetchone()["status"]=="draft"
        assert db.execute("SELECT COUNT(*) FROM rct_audit WHERE action='test_study_reset'").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM rct_source_files").fetchone()[0]==4
        assert db.execute("SELECT frozen_at FROM rct_scan_state").fetchone()[0] is None
    assert client.get("/api/rct/admin/dataset/status",headers=admin_auth).json()["counts"]["excluded"]==1
    assert client.get("/api/rct/me",headers=reader_auth).status_code==401
    # Reselect previously excluded patient and freeze new cohort.
    assert client.post("/api/rct/admin/dataset/selection",headers=admin_auth,
        json={"ids":[excluded_id],"included":True}).status_code==200
    fresh=client.post("/api/rct/admin/dataset/freeze",headers=admin_auth)
    assert fresh.status_code==200 and fresh.json()["count"]==4


def test_locked_study_cannot_be_reset(setup, monkeypatch):
    client, auth, root=setup
    monkeypatch.setenv("WEB4SYN_RCT_ALLOW_TEST_RESET","1")
    with rct.database() as db:
        db.execute("UPDATE rct_studies SET status='locked' WHERE id=1")
    res=client.post("/api/rct/admin/reset-test-study",headers=auth,json={
        "confirm":"RESET TEST STUDY","password":"test-admin-password-1234"})
    assert res.status_code==409
