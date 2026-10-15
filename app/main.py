import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, Response
from app.h5_source import patient_files, describe, encode, render
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("WEB4SYN_DATA", ROOT / "data"))
IMAGES = DATA / "images"
DATA.mkdir(parents=True, exist_ok=True)
IMAGES.mkdir(parents=True, exist_ok=True)
DB = DATA / "web4syn.sqlite"
SESSION_TTL = 12 * 3600
LABELS = {"HCC", "ICC", "MET", "FNH", "HEM", "CYST", "OTHER", "NONE", "UNKNOWN"}
app = FastAPI(title="web4syn Reader Study")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

@app.middleware("http")
async def disable_reader_cache(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/") or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response



@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','reader')), display_name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS cases(id INTEGER PRIMARY KEY, patient_code TEXT NOT NULL,
            module TEXT NOT NULL CHECK(module IN ('quality','diagnosis')), source TEXT NOT NULL
            CHECK(source IN ('real','virtual')), phase TEXT NOT NULL,
            image_path TEXT NOT NULL, public_code TEXT NOT NULL,
            reference_label TEXT, created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_cases ON cases(patient_code,module,source);
        CREATE TABLE IF NOT EXISTS assignments(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
            module TEXT NOT NULL, case_group TEXT NOT NULL, source TEXT NOT NULL,
            public_code TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            assigned_at INTEGER NOT NULL, opened_at INTEGER, submitted_at INTEGER,
            UNIQUE(user_id,module,case_group,source),
            FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, assignment_id INTEGER UNIQUE NOT NULL,
            answer_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            FOREIGN KEY(assignment_id) REFERENCES assignments(id));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT NOT NULL,
            detail TEXT NOT NULL, at INTEGER NOT NULL);
        """)
        # Internal non-login review identity: retained only for existing SQLite foreign keys.
        c.execute("INSERT OR IGNORE INTO users(username,password_hash,role,display_name) VALUES (?,?,?,?)",
                  ("local-reviewer", "disabled", "reader", "Local review"))


init()


def audit(c, user_id, action, detail):
    c.execute("INSERT INTO audit(user_id,action,detail,at) VALUES (?,?,?,?)",
              (user_id, action, json.dumps(detail, ensure_ascii=False), int(time.time())))


class QualityAnswer(BaseModel):
    guessed_source: str
    quality_score: int = Field(ge=1, le=5)


class DiagnosisAnswer(BaseModel):
    lesion_visible: str
    diagnosis: str
    malignancy_score: int = Field(ge=1, le=5)
    confidence: int = Field(ge=1, le=5)
    lesion_points: list[dict] = Field(default_factory=list)


def local_reviewer():
    with db() as c:
        row = c.execute("SELECT id FROM users WHERE username='local-reviewer'").fetchone()
    return {"id": row["id"]}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


def import_packs(user):
    """Create stable one-slice blinded cases from all mounted H5 files."""
    report = {"files": 0, "created": 0, "skipped": 0, "errors": []}
    for path in patient_files():
        report["files"] += 1
        try:
            d, z, original_z = describe(path)
            patient = path.stem
            if not patient or len(patient) > 90:
                raise ValueError("Invalid patient file ID")
            with db() as c:
                # Import both arms at the SAME predetermined z and phase.
                for source, dataset in (("real", "real_cect"), ("virtual", "syn_cect")):
                    for module in ("diagnosis", "quality"):
                        for phase in ("AP", "PVP", "DP"):
                            group = patient if module == "diagnosis" else patient + "@@" + phase
                            exists = c.execute("""SELECT 1 FROM cases WHERE patient_code=? AND module=? AND source=? AND phase=?""",
                                               (group, module, source, phase)).fetchone()
                            if exists:
                                report["skipped"] += 1
                                continue
                            c.execute("""INSERT INTO cases(patient_code,module,source,phase,image_path,public_code,reference_label,created_at)
                                        VALUES(?,?,?,?,?,?,?,?)""",
                                      (group, module, source, phase, encode(path.name, dataset, z, phase),
                                       "IMG-" + secrets.token_hex(4).upper(), None, int(time.time())))
                            report["created"] += 1
                    group = patient
                    exists = c.execute("""SELECT 1 FROM cases WHERE patient_code=? AND module='diagnosis' AND source=? AND phase='NCCT'""",
                                       (group, source)).fetchone()
                    if not exists:
                        c.execute("""INSERT INTO cases(patient_code,module,source,phase,image_path,public_code,created_at)
                                    VALUES(?,?,?,?,?,?,?)""", (group, "diagnosis", source, "NCCT",
                                    encode(path.name, "ncct", z, "NCCT"),
                                    "IMG-" + secrets.token_hex(4).upper(), int(time.time())))
                        report["created"] += 1
                audit(c, user["id"], "h5_import", {"filename": path.name, "slice_position": z, "original_z": original_z, "D": d})
        except Exception as exc:
            report["errors"].append({"file": path.name, "error": str(exc)[:200]})
    return report


def prepare_demo_cases(uid):
    report = import_packs(user={"id": uid, "role": "admin"})
    with db() as c:
        rows = c.execute("SELECT DISTINCT patient_code,module,source FROM cases ORDER BY patient_code,module,source").fetchall()
        inserted = 0
        for row in rows:
            patient, module, source = row
            if module == "diagnosis":
                phases = {x[0] for x in c.execute(
                    "SELECT phase FROM cases WHERE patient_code=? AND module=? AND source=?",
                    (patient, module, source))}
                if "NCCT" not in phases or not (phases & {"AP","PVP","DP"}):
                    continue
            before = c.total_changes
            c.execute("""INSERT OR IGNORE INTO assignments
                       (user_id,module,case_group,source,public_code,assigned_at)
                       VALUES(?,?,?,?,?,?)""",
                      (uid,module,patient,source,"DEMO-" + secrets.token_hex(5).upper(),int(time.time())))
            inserted += c.total_changes - before
        audit(c, uid, "demo_prepare", {"created": inserted})
    return {"import": report, "assignments": {"created": inserted}}


@app.on_event("startup")
def load_local_tasks():
    uid = local_reviewer()["id"]
    report = prepare_demo_cases(uid)
    if report["import"]["errors"]:
        print("HDF5 import warnings:", report["import"]["errors"], flush=True)


@app.get("/api/tasks")
def tasks(user=Depends(local_reviewer)):
    with db() as c:
        return [dict(r) for r in c.execute("""SELECT id,module,public_code,status,assigned_at
               FROM assignments WHERE user_id=? ORDER BY
               CASE status WHEN 'pending' THEN 0 WHEN 'opened' THEN 1 ELSE 2 END,
               assigned_at,id""", (user["id"],))]


def get_owned_task(c, task_id, user):
    row = c.execute("SELECT * FROM assignments WHERE id=? AND user_id=?", (task_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Task not found")
    return row


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: int, user=Depends(local_reviewer)):
    with db() as c:
        task = get_owned_task(c, task_id, user)
        if task["status"] == "submitted":
            return {"id": task_id, "module": task["module"], "status": "submitted"}
        if task["status"] == "pending":
            c.execute("UPDATE assignments SET status='opened', opened_at=? WHERE id=?",
                      (int(time.time()), task_id))
        images = c.execute("""SELECT id, phase FROM cases WHERE patient_code=? AND module=? AND source=?
             ORDER BY CASE phase WHEN 'NCCT' THEN 0 WHEN 'AP' THEN 1 WHEN 'PVP' THEN 2 ELSE 3 END""",
                           (task["case_group"], task["module"], task["source"])).fetchall()
        if task["module"] == "quality":
            # Each quality assignment assesses exactly one 2D CECT image.
            images = images[:1]
        return {"id": task_id, "module": task["module"], "status": "opened",
                "public_code": task["public_code"],
                "images": [{"id": r["id"], "phase": r["phase"],
                            "url": f"/api/tasks/{task_id}/images/{r['id']}"} for r in images]}


@app.get("/api/tasks/{task_id}/images/{image_id}")
def task_image(task_id: int, image_id: int, user=Depends(local_reviewer)):
    with db() as c:
        t = get_owned_task(c, task_id, user)
        if t["status"] == "submitted":
            raise HTTPException(403, "Task closed")
        if t["module"] == "quality":
            first = c.execute("""SELECT id FROM cases WHERE patient_code=? AND module=? AND source=?
                                 ORDER BY CASE phase WHEN 'AP' THEN 1 WHEN 'PVP' THEN 2 ELSE 3 END,id LIMIT 1""",
                              (t["case_group"], t["module"], t["source"])).fetchone()
            if not first or image_id != first["id"]:
                raise HTTPException(404, "Image not assigned")
        row = c.execute("""SELECT image_path FROM cases WHERE id=? AND patient_code=? AND module=? AND source=?""",
                        (image_id, t["case_group"], t["module"], t["source"])).fetchone()
        if not row:
            raise HTTPException(404, "Image not assigned")
        if row["image_path"].startswith("h5:"):
            try:
                raw = render(row["image_path"])
            except (ValueError, KeyError, IndexError, OSError) as exc:
                raise HTTPException(422, f"Cannot render selected H5 image: {exc}")
            return Response(raw, media_type="image/png", headers={"Cache-Control": "no-store"})
        path = IMAGES / row["image_path"]
        return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "image/jpeg",
                            headers={"Cache-Control": "no-store"})


@app.post("/api/tasks/{task_id}/submit")
def submit(task_id: int, payload: dict, user=Depends(local_reviewer)):
    with db() as c:
        t = get_owned_task(c, task_id, user)
        if t["status"] == "submitted":
            raise HTTPException(409, "Already submitted")
        if t["module"] == "quality":
            answer = QualityAnswer.model_validate(payload).model_dump()
            if answer["guessed_source"] not in {"real", "virtual", "unsure"}:
                raise HTTPException(422, "Invalid guess")
        else:
            answer = DiagnosisAnswer.model_validate(payload).model_dump()
            if answer["diagnosis"] not in LABELS:
                raise HTTPException(422, "Invalid diagnosis")
            if answer["lesion_visible"] not in {"yes", "no", "unsure"}:
                raise HTTPException(422, "Invalid lesion visibility")
            if len(answer["lesion_points"]) > 20:
                raise HTTPException(422, "Too many lesion points")
            for point in answer["lesion_points"]:
                if not isinstance(point, dict) or set(point) != {"image_id", "x", "y"}:
                    raise HTTPException(422, "Invalid point")
                if not all(isinstance(point[k], (int, float)) and 0 <= point[k] <= 1 for k in ("x", "y")):
                    raise HTTPException(422, "Invalid coordinate")
                legal = c.execute("""SELECT 1 FROM cases WHERE id=? AND patient_code=? AND module=? AND source=?""",
                                  (point["image_id"], t["case_group"], t["module"], t["source"])).fetchone()
                if not legal:
                    raise HTTPException(422, "Invalid image for point")
        now = int(time.time())
        c.execute("INSERT INTO reviews(assignment_id,answer_json,created_at) VALUES (?,?,?)",
                  (task_id, json.dumps(answer, ensure_ascii=False), now))
        c.execute("UPDATE assignments SET status='submitted', submitted_at=? WHERE id=?", (now, task_id))
        audit(c, user["id"], "task_submit", {"task_id": task_id})
    return {"ok": True}


