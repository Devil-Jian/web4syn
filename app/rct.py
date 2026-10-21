"""Isolated, authenticated three-arm single-read study for Web4Syn.

No legacy public reader endpoints are reused. All images are served only after
checking the authenticated reader's immutable patient-level assignment.
"""
import csv
import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.h5_source import describe, encode, patient_files, render, resolve_h5

router = APIRouter(prefix="/api/rct", tags=["three-arm-study"])
ROOT = Path(os.getenv("WEB4SYN_DATA", Path(__file__).resolve().parents[1] / "data"))
ROOT.mkdir(parents=True, exist_ok=True)
DATABASE = ROOT / "rct.sqlite"
ARMS = ("NCCT", "AI", "REAL")
LABELS = {"HCC", "ICC", "MET", "FNH", "HEM", "CYST", "OTHER"}
PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SESSION_SECONDS = 12 * 3600
ITERATIONS = 260000


@contextmanager
def database(immediate=False):
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return "pbkdf2_sha256:" + salt + ":" + digest.hex()


def check_password(password, stored):
    try:
        _, salt, _ = stored.split(":")
        return hmac.compare_digest(password_hash(password, salt), stored)
    except (ValueError, TypeError):
        return False


def audit(c, who, action, detail):
    c.execute("INSERT INTO rct_audit(actor_id,action,detail,created_at) VALUES(?,?,?,?)",
              (who, action, json.dumps(detail, ensure_ascii=False), int(time.time())))


def initialize():
    with database() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS rct_users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','reader')),
            display_name TEXT NOT NULL,
            level TEXT,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS rct_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES rct_users(id),
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rct_studies (
            id INTEGER PRIMARY KEY CHECK(id=1),
            name TEXT NOT NULL,
            target INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft','published','locked')),
            randomization_digest TEXT,
            created_at INTEGER NOT NULL,
            locked_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS rct_patients (
            id INTEGER PRIMARY KEY,
            patient_code TEXT NOT NULL UNIQUE,
            pack_filename TEXT NOT NULL UNIQUE,
            center TEXT NOT NULL,
            depth INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rct_tasks (
            id INTEGER PRIMARY KEY,
            patient_id INTEGER NOT NULL UNIQUE REFERENCES rct_patients(id),
            reader_id INTEGER NOT NULL REFERENCES rct_users(id),
            arm TEXT NOT NULL CHECK(arm IN ('NCCT','AI','REAL')),
            public_code TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'assigned' CHECK(status IN ('assigned','opened','submitted')),
            assigned_at INTEGER NOT NULL,
            opened_at INTEGER,
            submitted_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_rct_reader ON rct_tasks(reader_id,status,sort_order);
        CREATE TABLE IF NOT EXISTS rct_results (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL UNIQUE REFERENCES rct_tasks(id),
            submission_key TEXT NOT NULL UNIQUE,
            lesion_visible TEXT NOT NULL,
            diagnosis TEXT,
            malignancy_risk INTEGER,
            client_active_seconds REAL,
            server_elapsed_seconds INTEGER,
            submitted_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rct_audit (
            id INTEGER PRIMARY KEY,
            actor_id INTEGER,
            action TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO rct_studies(id,name,target,status,created_at)
        VALUES(1,'Three-Arm RW-RCT',1600,'draft',strftime('%s','now'));
        """)
        exists = c.execute("SELECT 1 FROM rct_users WHERE role='admin' LIMIT 1").fetchone()
        if not exists:
            bootstrap = os.getenv("WEB4SYN_RCT_ADMIN_PASSWORD", "")
            if len(bootstrap) >= 12:
                c.execute("""INSERT INTO rct_users(username,password_hash,role,display_name)
                             VALUES (?,?,?,?)""",
                          ("admin", password_hash(bootstrap), "admin", "Study administrator"))
            else:
                # Never create a known/default admin password.
                print("RCT admin disabled until WEB4SYN_RCT_ADMIN_PASSWORD (>=12 chars) is configured.", flush=True)


initialize()


class Credentials(BaseModel):
    username: str
    password: str


class ReaderCreate(BaseModel):
    username: str
    password: str = Field(min_length=12)
    display_name: str = Field(min_length=1, max_length=80)
    level: Literal["junior", "intermediate", "senior"]


class PatientRow(BaseModel):
    patient_code: str
    pack_filename: str
    center: str = Field(min_length=1, max_length=40)


class PatientImport(BaseModel):
    patients: list[PatientRow] = Field(min_length=1, max_length=10000)


class StudyConfig(BaseModel):
    target: int = Field(ge=3, le=10000)


class RandomizationRequest(BaseModel):
    seed: str | None = Field(default=None, max_length=256)


class DiagnosisSubmission(BaseModel):
    submission_key: str = Field(min_length=12, max_length=120)
    lesion_visible: Literal["yes", "no", "unsure"]
    diagnosis: str | None = None
    malignancy_risk: int | None = Field(default=None, ge=1, le=5)
    client_active_seconds: float | None = Field(default=None, ge=0, le=86400)


def current_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Login required")
    token = authorization[7:].strip()
    if len(token) != 64:
        raise HTTPException(401, "Invalid session")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with database() as c:
        row = c.execute("""SELECT u.id,u.username,u.role,u.display_name,u.level FROM rct_sessions s
             JOIN rct_users u ON u.id=s.user_id
             WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1""",
                        (digest, int(time.time()))).fetchone()
    if not row:
        raise HTTPException(401, "Session expired")
    return dict(row)


def admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin required")
    return user


def reader(user=Depends(current_user)):
    if user["role"] != "reader":
        raise HTTPException(403, "Reader required")
    return user


def study(c):
    return c.execute("SELECT * FROM rct_studies WHERE id=1").fetchone()


def require_draft(c):
    if study(c)["status"] != "draft":
        raise HTTPException(409, "Study is already published")


@router.post("/auth/login")
def login(payload: Credentials):
    with database(immediate=True) as c:
        row = c.execute("SELECT * FROM rct_users WHERE username=? AND enabled=1",
                        (payload.username,)).fetchone()
        if not row or not check_password(payload.password, row["password_hash"]):
            raise HTTPException(401, "Invalid credentials")
        token = secrets.token_hex(32)
        c.execute("INSERT INTO rct_sessions(token_hash,user_id,expires_at) VALUES (?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), row["id"],
                   int(time.time()) + SESSION_SECONDS))
        audit(c, row["id"], "login", {})
    return {"token": token, "role": row["role"], "display_name": row["display_name"]}


@router.post("/auth/logout")
def logout(authorization: str | None = Header(default=None), user=Depends(current_user)):
    with database() as c:
        c.execute("DELETE FROM rct_sessions WHERE token_hash=?",
                  (hashlib.sha256(authorization[7:].strip().encode()).hexdigest(),))
    return {"ok": True}


@router.get("/me")
def me(user=Depends(current_user)):
    with database() as c:
        s = study(c)
    return {**user, "study": {"name": s["name"], "target": s["target"], "status": s["status"]}}


@router.get("/admin/study")
def admin_study(user=Depends(admin)):
    with database() as c:
        s = dict(study(c))
        counts = {
            "patients": c.execute("SELECT COUNT(*) FROM rct_patients").fetchone()[0],
            "readers": c.execute("SELECT COUNT(*) FROM rct_users WHERE role='reader' AND enabled=1").fetchone()[0],
            "tasks": c.execute("SELECT COUNT(*) FROM rct_tasks").fetchone()[0],
        }
    return {**s, **counts}


@router.put("/admin/study")
def configure_study(payload: StudyConfig, user=Depends(admin)):
    with database(immediate=True) as c:
        require_draft(c)
        if c.execute("SELECT 1 FROM rct_tasks LIMIT 1").fetchone():
            raise HTTPException(409, "Tasks already randomized")
        c.execute("UPDATE rct_studies SET target=? WHERE id=1", (payload.target,))
        audit(c, user["id"], "study_target", {"target": payload.target})
    return {"ok": True, "target": payload.target}


@router.post("/admin/readers")
def create_reader(payload: ReaderCreate, user=Depends(admin)):
    if not PATTERN.fullmatch(payload.username):
        raise HTTPException(422, "Invalid reader username")
    with database(immediate=True) as c:
        require_draft(c)
        if c.execute("SELECT 1 FROM rct_tasks LIMIT 1").fetchone():
            raise HTTPException(409, "Assignments already exist")
        try:
            c.execute("""INSERT INTO rct_users(username,password_hash,role,display_name,level)
                         VALUES(?,?,'reader',?,?)""",
                      (payload.username, password_hash(payload.password),
                       payload.display_name, payload.level))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Username already exists")
        audit(c, user["id"], "reader_created", {"username": payload.username, "level": payload.level})
    return {"ok": True}


@router.get("/admin/readers")
def list_readers(user=Depends(admin)):
    with database() as c:
        rows = c.execute("""SELECT u.id,u.username,u.display_name,u.level,
                COUNT(t.id) assigned, SUM(CASE WHEN t.status='submitted' THEN 1 ELSE 0 END) completed
                FROM rct_users u LEFT JOIN rct_tasks t ON t.reader_id=u.id
                WHERE u.role='reader' AND u.enabled=1
                GROUP BY u.id ORDER BY u.level,u.id""").fetchall()
    return [dict(x) for x in rows]


@router.get("/admin/packs")
def list_packs(user=Depends(admin)):
    return {"packs": [p.name for p in patient_files()[:10000]]}


@router.post("/admin/patients/import")
def import_patients(payload: PatientImport, user=Depends(admin)):
    rows = []
    seen_codes = set()
    seen_files = set()
    for item in payload.patients:
        code, filename = item.patient_code, item.pack_filename
        if not PATTERN.fullmatch(code) or Path(filename).name != filename or not filename.endswith(".h5"):
            raise HTTPException(422, "Invalid patient code or HDF5 filename")
        if code in seen_codes or filename in seen_files:
            raise HTTPException(422, "Duplicate patient or pack in batch")
        seen_codes.add(code)
        seen_files.add(filename)
        try:
            depth, _, _ = describe(resolve_h5(filename))
        except (ValueError, KeyError, OSError, IndexError) as exc:
            raise HTTPException(422, f"Invalid HDF5 pack for {code}: {str(exc)[:100]}")
        rows.append((code, filename, item.center.strip(), depth, int(time.time())))
    with database(immediate=True) as c:
        require_draft(c)
        if c.execute("SELECT 1 FROM rct_tasks LIMIT 1").fetchone():
            raise HTTPException(409, "Cannot import after randomization")
        old_count = c.execute("SELECT COUNT(*) FROM rct_patients").fetchone()[0]
        if old_count + len(rows) > study(c)["target"]:
            raise HTTPException(422, "Import exceeds configured target")
        try:
            c.executemany("""INSERT INTO rct_patients(patient_code,pack_filename,center,depth,created_at)
                             VALUES(?,?,?,?,?)""", rows)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Patient or pack already imported")
        audit(c, user["id"], "patients_imported", {"count": len(rows)})
    return {"ok": True, "imported": len(rows)}


def allocation_plan(patients, doctors, seed):
    """Balance each center across arms, keep exact global arm totals, and balance doctors."""
    rng = random.Random(seed)
    n = len(patients)
    base, extra = divmod(n, 3)
    targets = {arm: base + (i < extra) for i, arm in enumerate(ARMS)}
    remaining = targets.copy()
    by_center = defaultdict(list)
    for p in patients:
        by_center[p["center"]].append(p)
    for bucket in by_center.values():
        rng.shuffle(bucket)
    centers = list(by_center)
    rng.shuffle(centers)
    order = []
    while any(by_center.values()):
        for center in centers:
            if by_center[center]:
                order.append(by_center[center].pop())
    local_counts = defaultdict(Counter)
    reader_counts = Counter()
    reader_arm = defaultdict(Counter)
    results = []
    for index, patient in enumerate(order):
        center = patient["center"]
        available = [arm for arm in ARMS if remaining[arm] > 0]
        rng.shuffle(available)
        # Minimize imbalance inside each center, then use global remaining quota.
        arm = min(available, key=lambda a: (local_counts[center][a], -remaining[a]))
        remaining[arm] -= 1
        local_counts[center][arm] += 1
        choices = list(doctors)
        rng.shuffle(choices)
        person = min(choices, key=lambda r: (
            reader_arm[r["id"]][arm], reader_counts[r["id"]]))
        reader_counts[person["id"]] += 1
        reader_arm[person["id"]][arm] += 1
        results.append((patient["id"], person["id"], arm,
                        "CASE-" + secrets.token_hex(5).upper(), index, int(time.time())))
    assert not any(remaining.values())
    return results, dict(targets)


@router.post("/admin/randomize")
def randomize(payload: RandomizationRequest, user=Depends(admin)):
    with database(immediate=True) as c:
        require_draft(c)
        if c.execute("SELECT 1 FROM rct_tasks LIMIT 1").fetchone():
            raise HTTPException(409, "Randomization is immutable and already exists")
        patients = [dict(x) for x in c.execute("SELECT id,center FROM rct_patients ORDER BY id")]
        doctors = [dict(x) for x in c.execute(
            "SELECT id,level FROM rct_users WHERE role='reader' AND enabled=1 ORDER BY id")]
        if len(patients) != study(c)["target"]:
            raise HTTPException(422, f"Expected {study(c)['target']} eligible patients; got {len(patients)}")
        if not 6 <= len(doctors) <= 9:
            raise HTTPException(422, "Six to nine active readers required")
        seed = payload.seed if payload.seed is not None else secrets.token_hex(32)
        plan, arm_counts = allocation_plan(patients, doctors, seed)
        c.executemany("""INSERT INTO rct_tasks(patient_id,reader_id,arm,public_code,sort_order,assigned_at)
                         VALUES(?,?,?,?,?,?)""", plan)
        # Store only a commitment, not the secret randomization seed.
        commitment = hashlib.sha256(seed.encode()).hexdigest()
        c.execute("UPDATE rct_studies SET randomization_digest=? WHERE id=1", (commitment,))
        audit(c, user["id"], "randomized", {"arms": arm_counts, "sha256_seed": commitment})
    return {"ok": True, "tasks": len(plan), "arms": arm_counts, "seed_sha256": commitment}


@router.post("/admin/publish")
def publish(user=Depends(admin)):
    with database(immediate=True) as c:
        require_draft(c)
        if c.execute("SELECT COUNT(*) FROM rct_tasks").fetchone()[0] != study(c)["target"]:
            raise HTTPException(409, "Complete randomization first")
        c.execute("UPDATE rct_studies SET status='published' WHERE id=1")
        audit(c, user["id"], "published", {})
    return {"ok": True}


@router.get("/admin/progress")
def progress(user=Depends(admin)):
    with database() as c:
        s = dict(study(c))
        by_arm = [dict(r) for r in c.execute(
            "SELECT arm,status,COUNT(*) AS total FROM rct_tasks GROUP BY arm,status ORDER BY arm,status")]
        by_reader = [dict(r) for r in c.execute("""SELECT u.username,u.level,t.arm,t.status,COUNT(*) total
                FROM rct_tasks t JOIN rct_users u ON u.id=t.reader_id
                GROUP BY t.reader_id,t.arm,t.status ORDER BY u.username,t.arm""")]
    return {"study": s, "by_arm": by_arm, "by_reader": by_reader}


@router.post("/admin/lock")
def lock_study(user=Depends(admin)):
    with database(immediate=True) as c:
        if study(c)["status"] != "published":
            raise HTTPException(409, "Study must be published")
        incomplete = c.execute("SELECT COUNT(*) FROM rct_tasks WHERE status!='submitted'").fetchone()[0]
        if incomplete:
            raise HTTPException(409, f"{incomplete} unfinished tasks")
        if c.execute("SELECT COUNT(*) FROM rct_results").fetchone()[0] != study(c)["target"]:
            raise HTTPException(409, "Result count mismatch")
        now = int(time.time())
        c.execute("UPDATE rct_studies SET status='locked',locked_at=? WHERE id=1", (now,))
        audit(c, user["id"], "locked", {"at": now})
    return {"ok": True}


@router.get("/admin/export.csv")
def export_csv(user=Depends(admin)):
    with database() as c:
        if study(c)["status"] != "locked":
            raise HTTPException(403, "Export requires a locked study")
        rows = c.execute("""SELECT p.patient_code,p.center,t.public_code,t.arm,
                u.username reader,u.level reader_level,r.lesion_visible,r.diagnosis,
                r.malignancy_risk,r.client_active_seconds,r.server_elapsed_seconds,
                t.opened_at,r.submitted_at
                FROM rct_tasks t JOIN rct_patients p ON p.id=t.patient_id
                JOIN rct_users u ON u.id=t.reader_id
                JOIN rct_results r ON r.task_id=t.id
                ORDER BY t.id""").fetchall()
        audit(c, user["id"], "export", {"rows": len(rows)})
    columns = ["patient_code", "center", "public_code", "arm", "reader", "reader_level",
               "lesion_visible", "diagnosis", "malignancy_risk", "client_active_seconds",
               "server_elapsed_seconds", "opened_at", "submitted_at"]
    file = io.StringIO()
    writer = csv.writer(file)
    writer.writerow(columns)
    for row in rows:
        # Prevent formula injection when viewing CSV with spreadsheet software.
        writer.writerow(["'" + str(v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@"))
                         else v for v in row])
    return Response(file.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="rct_results.csv"'})


def owned_task(c, task_id, user):
    if study(c)["status"] != "published":
        raise HTTPException(409, "Study is not accepting readings")
    task = c.execute("""SELECT t.*,p.depth,p.pack_filename FROM rct_tasks t
                        JOIN rct_patients p ON p.id=t.patient_id
                        WHERE t.id=? AND t.reader_id=?""", (task_id, user["id"])).fetchone()
    if not task:
        raise HTTPException(404, "Task not assigned")
    return task


@router.get("/tasks/summary")
def my_summary(user=Depends(reader)):
    with database() as c:
        rows = c.execute("SELECT status,COUNT(*) n FROM rct_tasks WHERE reader_id=? GROUP BY status",
                         (user["id"],)).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return {"total": sum(counts.values()), "completed": counts.get("submitted", 0),
            "remaining": counts.get("assigned", 0) + counts.get("opened", 0)}


@router.get("/tasks/next")
def next_task(user=Depends(reader)):
    with database() as c:
        if study(c)["status"] != "published":
            raise HTTPException(409, "Study not published")
        item = c.execute("""SELECT id,public_code,status FROM rct_tasks
             WHERE reader_id=? AND status!='submitted'
             ORDER BY CASE status WHEN 'opened' THEN 0 ELSE 1 END,sort_order LIMIT 1""",
                         (user["id"],)).fetchone()
    return {"task": dict(item) if item else None}


@router.get("/tasks/{task_id}")
def task_detail(task_id: int, user=Depends(reader)):
    with database(immediate=True) as c:
        task = owned_task(c, task_id, user)
        if task["status"] == "submitted":
            raise HTTPException(409, "Reading already submitted")
        if task["status"] == "assigned":
            c.execute("UPDATE rct_tasks SET status='opened',opened_at=? WHERE id=?",
                      (int(time.time()), task_id))
            audit(c, user["id"], "opened", {"task_id": task_id})
        phases = ["NCCT"] if task["arm"] == "NCCT" else ["NCCT", "AP", "PVP", "DP"]
        return {"id": task["id"], "public_code": task["public_code"],
                "depth": task["depth"], "phases": phases, "status": "opened"}


@router.get("/tasks/{task_id}/images/{phase}/{z}")
def image_slice(task_id: int, phase: str, z: int, lo: float = -160, hi: float = 240,
                user=Depends(reader)):
    with database() as c:
        task = owned_task(c, task_id, user)
        if task["status"] != "opened":
            raise HTTPException(403, "Open assigned task before viewing images")
        phases = ("NCCT",) if task["arm"] == "NCCT" else ("NCCT", "AP", "PVP", "DP")
        if phase not in phases or not 0 <= z < task["depth"]:
            raise HTTPException(404, "Image not in assigned task")
        modality = "ncct" if phase == "NCCT" else (
            "syn_cect" if task["arm"] == "AI" else "real_cect")
        filename = task["pack_filename"]
    try:
        pixels = render(encode(filename, modality, z, phase), lo, hi)
    except (ValueError, KeyError, IndexError, OSError, TypeError) as exc:
        raise HTTPException(422, f"Unable to render assigned image: {str(exc)[:120]}")
    return Response(pixels, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.post("/tasks/{task_id}/submit")
def submit(task_id: int, payload: DiagnosisSubmission, user=Depends(reader)):
    if payload.diagnosis not in LABELS | {None}:
        raise HTTPException(422, "Invalid diagnosis category")
    if payload.lesion_visible == "yes" and (
        payload.diagnosis is None or payload.malignancy_risk is None):
        raise HTTPException(422, "Diagnosis and risk required for visible lesion")
    if payload.lesion_visible == "no" and payload.diagnosis is not None:
        raise HTTPException(422, "Diagnosis forbidden for absent lesion")
    if payload.lesion_visible == "unsure" and payload.malignancy_risk is not None:
        raise HTTPException(422, "Risk forbidden for uncertain lesion")
    with database(immediate=True) as c:
        task = owned_task(c, task_id, user)
        previous = c.execute("SELECT submission_key FROM rct_results WHERE task_id=?",
                             (task_id,)).fetchone()
        if previous:
            if hmac.compare_digest(previous["submission_key"], payload.submission_key):
                return {"ok": True, "already_saved": True}
            raise HTTPException(409, "Task already submitted")
        if task["status"] != "opened":
            raise HTTPException(409, "Task must be opened before submission")
        now = int(time.time())
        try:
            c.execute("""INSERT INTO rct_results(task_id,submission_key,lesion_visible,
                        diagnosis,malignancy_risk,client_active_seconds,server_elapsed_seconds,submitted_at)
                        VALUES(?,?,?,?,?,?,?,?)""",
                      (task_id, payload.submission_key, payload.lesion_visible, payload.diagnosis,
                       payload.malignancy_risk, payload.client_active_seconds,
                       max(0, now - (task["opened_at"] or now)), now))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Duplicate submission key")
        c.execute("UPDATE rct_tasks SET status='submitted',submitted_at=? WHERE id=?",
                  (now, task_id))
        audit(c, user["id"], "submitted", {"task_id": task_id})
    return {"ok": True, "already_saved": False}
