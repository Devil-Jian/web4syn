"""Integration tests for patient-level three-arm RCT invariants.

Run: pip install -r requirements-dev.txt && pytest -q
Uses temporary SQLite and synthetic HDF5 stubs; no clinical data required.
"""
import collections
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
                "diagnosis": None, "malignancy_risk": 4,
            })
            assert invalid.status_code == 422
            submission = {
                "submission_key": secrets.token_hex(20), "lesion_visible": "yes",
                "diagnosis": "HCC", "malignancy_risk": 4, "client_active_seconds": 45.2,
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


def test_allocation_balances_workload():
    patients = [{"id": i+1, "center": ("GX","HX","SD")[i % 3]} for i in range(1600)]
    doctors = [{"id": i+1, "level": ("junior","intermediate","senior")[i%3]} for i in range(9)]
    plan, expected = rct.allocation_plan(patients, doctors, "test-seed")
    assert len(plan) == 1600
    assert len({t[0] for t in plan}) == 1600
    assert dict(collections.Counter(t[2] for t in plan)) == expected
    workloads = collections.Counter(t[1] for t in plan)
    assert max(workloads.values()) - min(workloads.values()) <= 3
