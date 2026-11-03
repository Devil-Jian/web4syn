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
        "lesion_nature":"malignant","diagnosis":"HCC","malignancy_risk":4})
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


def test_one_click_launch_freezes_randomizes_and_publishes(setup):
    client, auth, root=setup
    for i in range(8):
        pack(root / f"S{i}.h5", "HCC")
    dataset_manager.run_scan()
    files=client.get("/api/rct/admin/dataset/files",headers=auth).json()["items"]
    excluded=files[0]["id"]
    assert client.post("/api/rct/admin/dataset/selection",headers=auth,json={
        "ids":[excluded],"included":False}).status_code==200
    pre=client.get("/api/rct/admin/dataset/launch/status",headers=auth).json()
    assert not pre["ready"] and pre["included"]==7
    assert "6–9" in str(pre["errors"])
    denied=client.post("/api/rct/admin/dataset/launch",headers=auth)
    assert denied.status_code==409
    for i in range(6):
        res=client.post("/api/rct/admin/readers",headers=auth,json={
            "username":f"AutoDoctor{i}","display_name":f"Doctor {i}",
            "password":"doctor-password-12345","level":"junior"})
        assert res.status_code==200,res.text
    ready=client.get("/api/rct/admin/dataset/launch/status",headers=auth).json()
    assert ready["ready"] and ready["included"]==7
    published=client.post("/api/rct/admin/dataset/launch",headers=auth)
    assert published.status_code==200,published.text
    result=published.json()
    assert result["tasks"]==7 and sum(result["arms"].values())==7
    db=client.get("/api/rct/admin/study",headers=auth).json()
    assert db["status"]=="published" and db["target"]==7 and db["patients"]==7
    counts=client.get("/api/rct/admin/dataset/launch/status",headers=auth).json()
    assert counts["stage"]=="published" and not counts["ready"]
    assert client.post("/api/rct/admin/dataset/launch",headers=auth).status_code==409
    assert client.post("/api/rct/admin/dataset/selection",headers=auth,json={
        "ids":[excluded],"included":True}).status_code==409
    with rct.database() as c:
        assert c.execute("SELECT COUNT(*) FROM rct_tasks").fetchone()[0]==7
        assert c.execute("SELECT COUNT(DISTINCT patient_id) FROM rct_tasks").fetchone()[0]==7


def test_one_click_launch_handles_preexisting_frozen_or_randomized(setup):
    client, auth, root=setup
    for i in range(6):
        pack(root / f"T{i}.h5", "HCC")
    dataset_manager.run_scan()
    for i in range(6):
        res=client.post("/api/rct/admin/readers",headers=auth,json={
            "username":f"ExistingDoctor{i}","display_name":f"Doctor {i}",
            "password":"doctor-password-12345","level":"junior"})
        assert res.status_code==200
    assert client.post("/api/rct/admin/dataset/freeze",headers=auth).status_code==200
    # Previously frozen studies should still publish via one button.
    assert client.get("/api/rct/admin/dataset/launch/status",headers=auth).json()["ready"]
    assert client.post("/api/rct/admin/dataset/launch",headers=auth).status_code==200


def test_one_click_launch_does_not_partial_write_on_failure(setup):
    client, auth, root=setup
    for i in range(2):
        pack(root / f"U{i}.h5", "HCC")
    dataset_manager.run_scan()
    for i in range(6):
        assert client.post("/api/rct/admin/readers",headers=auth,json={
            "username":f"SmallDoctor{i}","display_name":f"Doctor {i}",
            "password":"doctor-password-12345","level":"junior"}).status_code==200
    res=client.post("/api/rct/admin/dataset/launch",headers=auth)
    assert res.status_code==409
    db=client.get("/api/rct/admin/study",headers=auth).json()
    assert db["patients"]==0 and db["tasks"]==0 and db["status"]=="draft"


def test_force_end_partial_round_and_publish_second_round(setup):
    client, administrator, root=setup
    for i in range(8):
        pack(root/f"ROUND{i}.h5","HCC")
    assert dataset_manager.run_scan()
    for i in range(6):
        res=client.post("/api/rct/admin/readers",headers=administrator,json={
            "username":f"RoundReader{i}","display_name":f"Doctor {i}",
            "password":"doctor-password-12345","level":"junior"})
        assert res.status_code==200,res.text
    launch=client.post("/api/rct/admin/dataset/launch",headers=administrator)
    assert launch.status_code==200,launch.text
    with rct.database() as db:
        task=db.execute("SELECT id,reader_id FROM rct_tasks LIMIT 1").fetchone()
        reader_name=db.execute("SELECT username FROM rct_users WHERE id=?",
                               (task["reader_id"],)).fetchone()[0]
    token=client.post("/api/rct/auth/login",json={
        "username":reader_name,"password":"doctor-password-12345"})
    assert token.status_code==200
    doctor_auth={"Authorization":"Bearer "+token.json()["token"]}
    assert client.get(f"/api/rct/tasks/{task['id']}",headers=doctor_auth).status_code==200
    submit=client.post(f"/api/rct/tasks/{task['id']}/submit",headers=doctor_auth,json={
        "submission_key":"partial-round-submission-1234",
        "lesion_visible":"yes","lesion_nature":"malignant","diagnosis":"HCC","malignancy_risk":4})
    assert submit.status_code==200,submit.text
    req={"confirm":"END CURRENT ROUND","admin_password":"test-admin-password-1234",
         "reason":"First test complete"}
    assert client.post("/api/rct/admin/end-round",headers=doctor_auth,json=req).status_code==403
    assert client.post("/api/rct/admin/end-round",headers=administrator,json={
        **req,"admin_password":"incorrect"}).status_code==403
    assert client.post("/api/rct/admin/end-round",headers=administrator,json={
        **req,"confirm":"not-confirmed"}).status_code==422
    ended=client.post("/api/rct/admin/end-round",headers=administrator,json=req)
    assert ended.status_code==200,ended.text
    data=ended.json()
    assert data["assigned"]==8 and data["completed"]==1 and data["incomplete"]==7
    assert client.get("/api/rct/me",headers=doctor_auth).status_code==401
    state=client.get("/api/rct/admin/study",headers=administrator).json()
    assert state["status"]=="draft" and state["patients"]==0 and state["tasks"]==0
    history=client.get("/api/rct/admin/round-archives",headers=administrator)
    assert history.status_code==200 and len(history.json())==1
    assert history.json()[0]["ended_early"]==1
    archive_id=data["archive_id"]
    exported=client.get(f"/api/rct/admin/round-archives/{archive_id}/export.csv",
                        headers=administrator)
    assert exported.status_code==200
    assert len(exported.text.strip().splitlines())==9
    assert "HCC" in exported.text and "submitted" in exported.text
    assert "lesion_nature" in exported.text.splitlines()[0]
    assert "malignant" in exported.text
    assert "assigned" in exported.text or "opened" in exported.text
    assert client.post("/api/rct/admin/end-round",headers=administrator,json=req).status_code==409
    # Second round can use the existing index and readers without resetting the database.
    result=client.post("/api/rct/admin/dataset/launch",headers=administrator)
    assert result.status_code==200,result.text
    assert result.json()["tasks"]==8
    assert client.get("/api/rct/admin/study",headers=administrator).json()["status"]=="published"
    assert len(client.get("/api/rct/admin/round-archives",headers=administrator).json())==1
    assert client.get(f"/api/rct/admin/round-archives/{archive_id}/export.csv",
                      headers=administrator).status_code==200


def test_force_end_requires_published_tasks(setup):
    client, auth, root=setup
    req={"confirm":"END CURRENT ROUND","admin_password":"test-admin-password-1234"}
    assert client.post("/api/rct/admin/end-round",headers=auth,json=req).status_code==409
    with rct.database() as db:
        db.execute("UPDATE rct_studies SET status='locked' WHERE id=1")
    assert client.post("/api/rct/admin/end-round",headers=auth,json=req).status_code==409
