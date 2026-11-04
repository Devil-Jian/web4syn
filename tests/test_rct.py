"""Integration tests for patient-level three-arm RCT invariants.

Run: pip install -r requirements-dev.txt && pytest -q
Uses temporary SQLite and synthetic HDF5 stubs; no clinical data required.
"""
import collections
import json
import struct
import os
import secrets
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import rct


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(rct, "DATABASE", tmp_path / "rct-test.sqlite")
    monkeypatch.setenv("WEB4SYN_RCT_ADMIN_PASSWORD", "change-this-in-production-12345")
    monkeypatch.setattr(rct, "resolve_h5", lambda filename: Path(filename))
    monkeypatch.setattr(rct, "describe", lambda path: (12, 6, 6))
    monkeypatch.setattr(rct, "render", lambda image_ref, lo=-160, hi=240: b"\x89PNG\r\n\x1a\nstub")
    monkeypatch.setattr(rct,"render_batch",
        lambda filename,dataset,phases,start,end,lo,hi:[
            (phase,z,b"\x89PNG\r\n\x1a\nstub")
            for z in range(start,end) for phase in phases])
    rct.initialize()
    app = FastAPI()
    app.include_router(rct.router)
    with TestClient(app) as testclient:
        yield testclient


def auth(client, username, password):
    response = client.post("/api/rct/auth/login", json={
        "username": username, "password": password,
    })
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def populate(client, headers, count=6):
    response = client.put("/api/rct/admin/study", headers=headers, json={"target": count})
    assert response.status_code == 200, response.text
    for i in range(6):
        response = client.post("/api/rct/admin/readers", headers=headers, json={
            "username": f"R{i+1:02d}",
            "display_name": f"Reader {i+1}",
            "password": "reader-password-12345",
            "level": ("junior", "intermediate", "senior")[i % 3],
        })
        assert response.status_code == 200, response.text
    cases = [
        {"patient_code": f"P{i:04d}", "pack_filename": f"P{i:04d}.h5",
         "center": ("GX", "HX", "SD")[i % 3]}
        for i in range(count)
    ]
    response = client.post("/api/rct/admin/patients/import", headers=headers,
                           json={"patients": cases})
    assert response.status_code == 200, response.text


def test_single_assignment_three_arms_and_submit_once(client):
    admin = auth(client, "admin", "change-this-in-production-12345")
    populate(client, admin)
    response = client.post("/api/rct/admin/randomize", headers=admin, json={"seed": "trial-demo"})
    assert response.status_code == 200, response.text
    assert response.json()["arms"] == {"NCCT": 2, "AI": 2, "REAL": 2}
    assert client.post("/api/rct/admin/randomize", headers=admin, json={}).status_code == 409
    assert client.post("/api/rct/admin/publish", headers=admin).status_code == 200

    all_ids = []
    first_pair = []
    for i in range(6):
        reader = auth(client, f"R{i+1:02d}", "reader-password-12345")
        while True:
            next_response = client.get("/api/rct/tasks/next", headers=reader)
            assert next_response.status_code == 200
            item = next_response.json()["task"]
            if item is None:
                break
            task_id = item["id"]
            all_ids.append(task_id)
            first_pair.append((reader, task_id))
            detail = client.get(f"/api/rct/tasks/{task_id}", headers=reader)
            assert detail.status_code == 200
            phases = detail.json()["phases"]
            assert phases in (["NCCT"], ["NCCT", "AP", "PVP", "DP"])
            assert client.get(f"/api/rct/tasks/{task_id}/images/NCCT/6", headers=reader).status_code == 200
            if phases == ["NCCT"]:
                assert client.get(f"/api/rct/tasks/{task_id}/images/AP/6", headers=reader).status_code == 404
            invalid = client.post(f"/api/rct/tasks/{task_id}/submit", headers=reader, json={
                "submission_key": secrets.token_hex(20), "lesion_visible": "yes",
                "lesion_nature": "malignant", "diagnosis": None, "malignancy_risk": 4,
            })
            assert invalid.status_code == 422
            submission = {
                "submission_key": secrets.token_hex(20), "lesion_visible": "yes",
                "lesion_nature": "malignant", "diagnosis": "HCC", "malignancy_risk": 4, "client_active_seconds": 45.2,
            }
            response = client.post(f"/api/rct/tasks/{task_id}/submit", headers=reader, json=submission)
            assert response.status_code == 200, response.text
            repeat = client.post(f"/api/rct/tasks/{task_id}/submit", headers=reader, json=submission)
            assert repeat.status_code == 200 and repeat.json()["already_saved"]
            altered = {**submission, "submission_key": secrets.token_hex(20)}
            assert client.post(f"/api/rct/tasks/{task_id}/submit", headers=reader, json=altered).status_code == 409

    assert len(all_ids) == len(set(all_ids)) == 6
    if len(first_pair) >= 2:
        wrong_reader, other_task = first_pair[0][0], first_pair[1][1]
        assert client.get(f"/api/rct/tasks/{other_task}", headers=wrong_reader).status_code in (404, 409)
    assert client.post("/api/rct/admin/lock", headers=admin).status_code == 200
    data = client.get("/api/rct/admin/export.csv", headers=admin)
    assert data.status_code == 200
    assert len(data.text.strip().splitlines()) == 7
    assert "lesion_nature" in data.text.splitlines()[0]
    assert "malignant" in data.text


def test_allocation_balances_workload():
    patients = [{"id": i+1, "center": ("GX","HX","SD")[i % 3]} for i in range(1600)]
    doctors = [{"id": i+1, "level": ("junior","intermediate","senior")[i%3]} for i in range(9)]
    plan, expected = rct.allocation_plan(patients, doctors, "test-seed")
    assert len(plan) == 1600
    assert len({t[0] for t in plan}) == 1600
    assert dict(collections.Counter(t[2] for t in plan)) == expected
    workloads = collections.Counter(t[1] for t in plan)
    assert max(workloads.values()) - min(workloads.values()) <= 3


def test_reader_account_management_before_and_after_randomization(client):
    administrator=auth(client,"admin","change-this-in-production-12345")
    populate(client,administrator,count=6)
    readers=client.get("/api/rct/admin/readers",headers=administrator).json()
    assert len(readers)==6
    assert all(r["enabled"]==1 for r in readers)
    first=readers[0]
    second=readers[1]
    first_id=first["id"]
    second_id=second["id"]
    # Before randomization profile, grade and status can be edited.
    updated=client.patch(f"/api/rct/admin/readers/{first_id}",headers=administrator,json={
        "display_name":"Doctor A","level":"senior"})
    assert updated.status_code==200,updated.text
    turned_off=client.patch(f"/api/rct/admin/readers/{first_id}/status",
                             headers=administrator,json={"enabled":False})
    assert turned_off.status_code==200
    assert client.post("/api/rct/auth/login",json={
        "username":first["username"],"password":"reader-password-12345"}).status_code==401
    assert client.patch(f"/api/rct/admin/readers/{first_id}/status",
                        headers=administrator,json={"enabled":True}).status_code==200
    # Password change needs the administrator's password, invalidates existing tokens.
    reader_session=auth(client,first["username"],"reader-password-12345")
    denied=client.post(f"/api/rct/admin/readers/{first_id}/password",
        headers=administrator,json={"password":"new-reader-password-12345",
                                    "admin_password":"incorrect-admin"})
    assert denied.status_code==403
    assert client.get("/api/rct/me",headers=reader_session).status_code==200
    rotated=client.post(f"/api/rct/admin/readers/{first_id}/password",headers=administrator,
        json={"password":"new-reader-password-12345",
              "admin_password":"change-this-in-production-12345"})
    assert rotated.status_code==200
    assert client.get("/api/rct/me",headers=reader_session).status_code==401
    assert client.post("/api/rct/auth/login",json={
        "username":first["username"],"password":"reader-password-12345"}).status_code==401
    assert client.post("/api/rct/auth/login",json={
        "username":first["username"],"password":"new-reader-password-12345"}).status_code==200
    # Doctors have no access to account management.
    doctor_auth=auth(client,first["username"],"new-reader-password-12345")
    assert client.get("/api/rct/admin/readers",headers=doctor_auth).status_code==403
    assert client.patch(f"/api/rct/admin/readers/{second_id}",headers=doctor_auth,json={
        "display_name":"Invalid","level":"junior"}).status_code==403
    assert client.post(f"/api/rct/admin/readers/{first_id}/password",headers=doctor_auth,
        json={"password":"third-reader-password-12345",
              "admin_password":"change-this-in-production-12345"}).status_code==403
    assert client.patch("/api/rct/admin/readers/99999/status",headers=administrator,
                        json={"enabled":False}).status_code==404
    # Once assigned, disabling accounts and changing grades must not orphan cases.
    randomized=client.post("/api/rct/admin/randomize",headers=administrator,json={"seed":"accounts-test"})
    assert randomized.status_code==200,randomized.text
    assigned=client.get("/api/rct/admin/readers",headers=administrator).json()
    with_tasks=next(x for x in assigned if x["assigned"]>0)
    assert client.patch(f"/api/rct/admin/readers/{with_tasks['id']}/status",
                        headers=administrator,json={"enabled":False}).status_code==409
    alternative="junior" if with_tasks["level"]!="junior" else "senior"
    assert client.patch(f"/api/rct/admin/readers/{with_tasks['id']}",
                        headers=administrator,json={
                            "display_name":"Still assigned","level":alternative}).status_code==409
    # Display name corrections do not alter reader assignment or study grouping.
    assert client.patch(f"/api/rct/admin/readers/{with_tasks['id']}",
                        headers=administrator,json={
                            "display_name":"Corrected alias","level":with_tasks["level"]}).status_code==200
    assert client.get("/api/rct/admin/study",headers=administrator).json()["tasks"]==6


def test_doctor_self_registration_immediately_activates_account(client):
    applicant={
        "username":"NewDoctor123",
        "display_name":"Doctor New",
        "password":"new-doctor-password-1234",
    }
    created=client.post("/api/rct/auth/register",json=applicant)
    assert created.status_code==201,created.text
    assert created.json()["status"]=="approved"
    duplicate=client.post("/api/rct/auth/register",json=applicant)
    assert duplicate.status_code==409
    for invalid in [
        {**applicant,"username":"bad name"},
        {**applicant,"username":"ab"},
        {**applicant,"password":"short"},
    ]:
        assert client.post("/api/rct/auth/register",json=invalid).status_code==422
    administrator=auth(client,"admin","change-this-in-production-12345")
    doctor=next(x for x in client.get("/api/rct/admin/readers",headers=administrator).json()
                if x["username"]==applicant["username"])
    assert doctor["registration_status"]=="approved"
    assert doctor["enabled"]==1 and doctor["level"] is None
    assert doctor["assigned"]==0
    reader_auth=auth(client,applicant["username"],applicant["password"])
    assert client.get("/api/rct/me",headers=reader_auth).status_code==200
    assert client.get("/api/rct/admin/readers",headers=reader_auth).status_code==403
    assert client.get("/api/rct/tasks/next",headers=reader_auth).status_code==409
    assert client.patch(f"/api/rct/admin/readers/{doctor['id']}/status",
        headers=administrator,json={"enabled":False}).status_code==200
    assert client.get("/api/rct/me",headers=reader_auth).status_code==401


def test_registration_during_published_round_cannot_access_others_tasks(client):
    administrator=auth(client,"admin","change-this-in-production-12345")
    with rct.database() as db:
        db.execute("UPDATE rct_studies SET status='published' WHERE id=1")
    applicant={"username":"DoctorNextRound","display_name":"Later Reader",
               "password":"valid-password-12345"}
    registered=client.post("/api/rct/auth/register",json=applicant)
    assert registered.status_code==201,registered.text
    doctor=next(x for x in client.get("/api/rct/admin/readers",headers=administrator).json()
                if x["username"]==applicant["username"])
    assert doctor["enabled"]==1 and doctor["assigned"]==0
    reader_auth=auth(client,applicant["username"],applicant["password"])
    tasks=client.get("/api/rct/tasks/next",headers=reader_auth)
    assert tasks.status_code==200 and tasks.json()["task"] is None
    assert client.get("/api/rct/tasks/12345",headers=reader_auth).status_code==404


def test_six_character_doctor_password_is_accepted(client):
    applicant={"username":"SixPassword","display_name":"Six Reader",
               "password":"abc123"}
    registered=client.post("/api/rct/auth/register",json=applicant)
    assert registered.status_code==201,registered.text
    too_short=client.post("/api/rct/auth/register",json={
        "username":"ShortPassword","display_name":"Short Reader","password":"abc12"})
    assert too_short.status_code==422

    administrator=auth(client,"admin","change-this-in-production-12345")
    doctor=next(r for r in client.get("/api/rct/admin/readers",headers=administrator).json()
                if r["username"]=="SixPassword")
    assert doctor["enabled"]==1 and doctor["registration_status"]=="approved"
    assert client.post("/api/rct/auth/login",json={
        "username":"SixPassword","password":"abc123"}).status_code==200

    created=client.post("/api/rct/admin/readers",headers=administrator,json={
        "username":"SixCreated","display_name":"Created Reader",
        "level":"junior","password":"abc123"})
    assert created.status_code==200,created.text
    doctor_id=next(r["id"] for r in client.get("/api/rct/admin/readers",headers=administrator).json()
                   if r["username"]=="SixCreated")
    reset=client.post(f"/api/rct/admin/readers/{doctor_id}/password",
        headers=administrator,json={"password":"xyz789",
                                  "admin_password":"change-this-in-production-12345"})
    assert reset.status_code==200,reset.text
    rejected=client.post(f"/api/rct/admin/readers/{doctor_id}/password",
        headers=administrator,json={"password":"12345",
                                  "admin_password":"change-this-in-production-12345"})
    assert rejected.status_code==422
    assert client.post("/api/rct/auth/login",json={
        "username":"SixCreated","password":"xyz789"}).status_code==200


def test_admin_can_create_and_delete_unassigned_reader_accounts(client):
    administrator=auth(client,"admin","change-this-in-production-12345")
    created=client.post("/api/rct/admin/readers",headers=administrator,json={
        "username":"ExtraReader","display_name":"Extra Doctor",
        "level":"junior","password":"123456"})
    assert created.status_code==200,created.text
    login_token=auth(client,"ExtraReader","123456")
    record=next(x for x in client.get("/api/rct/admin/readers",headers=administrator).json()
                if x["username"]=="ExtraReader")
    assert record["assigned"]==0
    reader_id=record["id"]
    # Reader cannot remove anyone; admin cannot be deleted through the reader route.
    assert client.delete(f"/api/rct/admin/readers/{reader_id}",headers=login_token).status_code==403
    with rct.database() as c:
        admin_id=c.execute("SELECT id FROM rct_users WHERE role='admin'").fetchone()["id"]
    assert client.delete(f"/api/rct/admin/readers/{admin_id}",headers=administrator).status_code==404
    deleted=client.delete(f"/api/rct/admin/readers/{reader_id}",headers=administrator)
    assert deleted.status_code==200,deleted.text
    assert deleted.json()["username"]=="ExtraReader"
    assert client.get("/api/rct/me",headers=login_token).status_code==401
    assert client.delete(f"/api/rct/admin/readers/{reader_id}",headers=administrator).status_code==404
    with rct.database() as c:
        assert c.execute("SELECT COUNT(*) FROM rct_audit WHERE action='reader_deleted'").fetchone()[0]==1
    # Creating users while another round is published is fine; they receive no old assignments.
    with rct.database() as c:
        c.execute("UPDATE rct_studies SET status='published' WHERE id=1")
    added=client.post("/api/rct/admin/readers",headers=administrator,json={
        "username":"NextReader","display_name":"Next Doctor",
        "level":"senior","password":"654321"})
    assert added.status_code==200,added.text
    user=next(x for x in client.get("/api/rct/admin/readers",headers=administrator).json()
              if x["username"]=="NextReader")
    assert user["assigned"]==0
    assert client.delete(f"/api/rct/admin/readers/{user['id']}",
                         headers=administrator).status_code==200


def test_admin_cannot_delete_reader_with_active_assignment(client):
    administrator=auth(client,"admin","change-this-in-production-12345")
    populate(client,administrator,count=6)
    assert client.post("/api/rct/admin/randomize",
                       headers=administrator,json={"seed":"reader-delete-protection"}).status_code==200
    doctors=client.get("/api/rct/admin/readers",headers=administrator).json()
    assigned=next(x for x in doctors if x["assigned"]>0)
    rejected=client.delete(f"/api/rct/admin/readers/{assigned['id']}",
                           headers=administrator)
    assert rejected.status_code==409,rejected.text
    assert client.post("/api/rct/auth/login",json={
        "username":assigned["username"],"password":"reader-password-12345"}).status_code==200
    with rct.database() as c:
        assert c.execute("SELECT COUNT(*) FROM rct_tasks").fetchone()[0]==6
        assert c.execute("SELECT COUNT(*) FROM rct_audit WHERE action='reader_deleted'").fetchone()[0]==0


def test_lesion_nature_required_and_independent_for_visible_lesion(client):
    administrator=auth(client,"admin","change-this-in-production-12345")
    populate(client,administrator,count=6)
    assert client.post("/api/rct/admin/randomize",headers=administrator,
                       json={"seed":"lesion-nature"}).status_code==200
    assert client.post("/api/rct/admin/publish",headers=administrator).status_code==200
    doctor=auth(client,"R01","reader-password-12345")
    next_item=client.get("/api/rct/tasks/next",headers=doctor).json()["task"]
    assert next_item is not None
    task_id=next_item["id"]
    assert client.get(f"/api/rct/tasks/{task_id}",headers=doctor).status_code==200
    base={"submission_key":secrets.token_hex(20),"lesion_visible":"yes",
          "diagnosis":"OTHER","malignancy_risk":3}
    assert client.post(f"/api/rct/tasks/{task_id}/submit",headers=doctor,
                       json=base).status_code==422
    assert client.post(f"/api/rct/tasks/{task_id}/submit",headers=doctor,
                       json={**base,"lesion_nature":"invalid"}).status_code==422
    assert client.post(f"/api/rct/tasks/{task_id}/submit",headers=doctor,
                       json={**base,"lesion_nature":"benign","malignancy_risk":None}).status_code==422
    for nature in ["benign",None]:
        assert client.post(f"/api/rct/tasks/{task_id}/submit",headers=doctor,
            json={**base,"lesion_visible":"no","lesion_nature":nature}).status_code==422
    # Nature and 1-5 risk are saved independently, without auto-inference.
    saved=client.post(f"/api/rct/tasks/{task_id}/submit",headers=doctor,
       json={**base,"lesion_nature":"unsure"})
    assert saved.status_code==200,saved.text
    with rct.database() as db:
        row=db.execute("SELECT lesion_visible,lesion_nature,diagnosis,malignancy_risk "
                       "FROM rct_results WHERE task_id=?",(task_id,)).fetchone()
        assert dict(row)=={"lesion_visible":"yes","lesion_nature":"unsure",
                           "diagnosis":"OTHER","malignancy_risk":3}


def test_absent_lesion_has_no_nature_diagnosis_or_risk(client):
    administrator=auth(client,"admin","change-this-in-production-12345")
    populate(client,administrator,count=6)
    assert client.post("/api/rct/admin/randomize",headers=administrator,
                       json={"seed":"no-lesion"}).status_code==200
    assert client.post("/api/rct/admin/publish",headers=administrator).status_code==200
    doctor=auth(client,"R01","reader-password-12345")
    task=client.get("/api/rct/tasks/next",headers=doctor).json()["task"]
    assert client.get(f"/api/rct/tasks/{task['id']}",headers=doctor).status_code==200
    response=client.post(f"/api/rct/tasks/{task['id']}/submit",headers=doctor,json={
        "submission_key":secrets.token_hex(20),"lesion_visible":"no"})
    assert response.status_code==200,response.text
    with rct.database() as db:
        result=db.execute("SELECT lesion_nature,diagnosis,malignancy_risk "
                          "FROM rct_results WHERE task_id=?",(task["id"],)).fetchone()
        assert all(x is None for x in result)


def test_case_batch_has_authorized_phases_bounded_range_and_index(client):
    admin=auth(client,"admin","change-this-in-production-12345")
    populate(client,admin)
    assert client.post("/api/rct/admin/randomize",headers=admin,
                       json={"seed":"batch-security"}).status_code==200
    assert client.post("/api/rct/admin/publish",headers=admin).status_code==200
    reader=auth(client,"R01","reader-password-12345")
    selected=client.get("/api/rct/tasks/next",headers=reader).json()["task"]
    task_id=selected["id"]
    url=f"/api/rct/tasks/{task_id}/images/batch?start=0&end=12"
    assert client.get(url,headers=reader).status_code==403
    assert client.get(url).status_code==401
    task=client.get(f"/api/rct/tasks/{task_id}",headers=reader).json()
    for query in ["start=-1&end=4","start=0&end=13",
                  "start=6&end=6","start=0&end=100000"]:
        assert client.get(f"/api/rct/tasks/{task_id}/images/batch?{query}",
                          headers=reader).status_code==422
    response=client.get(url,headers=reader)
    assert response.status_code==200,response.text if response.status_code!=200 else ""
    assert response.headers["cache-control"]=="private, no-store"
    assert response.headers["content-type"].startswith("application/octet-stream")
    payload=response.content
    size=struct.unpack_from("<I",payload)[0]
    manifest=json.loads(payload[4:4+size])
    assert manifest["version"]==1
    assert len(manifest["images"])==12*len(task["phases"])
    for row in manifest["images"]:
        assert row["phase"] in task["phases"]
        assert 0<=row["z"]<12
        start=4+size+row["offset"]
        assert payload[start:start+8]==b"\x89PNG\r\n\x1a\n"
        assert row["length"]>8
    # An opened task remains private to the assigned doctor.
    other=auth(client,"R02","reader-password-12345")
    assert client.get(url,headers=other).status_code==404
    with rct.database() as c:
        task_row=c.execute("SELECT arm FROM rct_tasks WHERE id=?",(task_id,)).fetchone()
    if task_row["arm"]=="NCCT":
        assert {row["phase"] for row in manifest["images"]}=={"NCCT"}


def test_uncertain_lesion_presence_allows_optional_followup_judgments(client):
    administrator=auth(client,"admin","change-this-in-production-12345")
    populate(client,administrator,count=6)
    assert client.post("/api/rct/admin/randomize",headers=administrator,
                       json={"seed":"uncertain-branch"}).status_code==200
    assert client.post("/api/rct/admin/publish",headers=administrator).status_code==200
    doctor=auth(client,"R01","reader-password-12345")
    task=client.get("/api/rct/tasks/next",headers=doctor).json()["task"]
    assert task is not None
    assert client.get(f"/api/rct/tasks/{task['id']}",headers=doctor).status_code==200
    result=client.post(f"/api/rct/tasks/{task['id']}/submit",headers=doctor,json={
        "submission_key":secrets.token_hex(20),"lesion_visible":"unsure",
        "lesion_nature":"unsure","diagnosis":"UNSURE","malignancy_risk":3})
    assert result.status_code==200,result.text
    with rct.database() as db:
        record=db.execute("SELECT lesion_visible,lesion_nature,diagnosis,malignancy_risk "
                          "FROM rct_results WHERE task_id=?",(task["id"],)).fetchone()
    assert dict(record)=={"lesion_visible":"unsure","lesion_nature":"unsure",
                          "diagnosis":"UNSURE","malignancy_risk":3}


def test_uncertain_lesion_presence_can_submit_without_followup_judgments(client):
    administrator=auth(client,"admin","change-this-in-production-12345")
    populate(client,administrator,count=6)
    assert client.post("/api/rct/admin/randomize",headers=administrator,
                       json={"seed":"uncertain-empty-branch"}).status_code==200
    assert client.post("/api/rct/admin/publish",headers=administrator).status_code==200
    doctor=auth(client,"R01","reader-password-12345")
    task=client.get("/api/rct/tasks/next",headers=doctor).json()["task"]
    assert task is not None
    assert client.get(f"/api/rct/tasks/{task['id']}",headers=doctor).status_code==200
    result=client.post(f"/api/rct/tasks/{task['id']}/submit",headers=doctor,json={
        "submission_key":secrets.token_hex(20),"lesion_visible":"unsure"})
    assert result.status_code==200,result.text
