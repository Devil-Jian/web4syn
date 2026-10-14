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


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260000).hex()
    return salt + ":" + digest


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split(":")
        return hmac.compare_digest(password_hash(password, salt).split(":")[1], expected)
    except (ValueError, TypeError):
        return False


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
        count = c.execute("SELECT count(*) FROM users").fetchone()[0]
        if count == 0:
            password = os.getenv("WEB4SYN_ADMIN_PASSWORD")
            if not password or len(password) < 12:
                raise RuntimeError("Set WEB4SYN_ADMIN_PASSWORD (>=12 chars) before first startup.")
            c.execute("INSERT INTO users(username,password_hash,role,display_name) VALUES (?,?,?,?)",
                      ("admin", password_hash(password), "admin", "Administrator"))


init()


def audit(c, user_id, action, detail):
    c.execute("INSERT INTO audit(user_id,action,detail,at) VALUES (?,?,?,?)",
              (user_id, action, json.dumps(detail, ensure_ascii=False), int(time.time())))


class Login(BaseModel):
    username: str
    password: str


class ReaderCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12)


class QualityAnswer(BaseModel):
    guessed_source: str
    quality_score: int = Field(ge=1, le=5)
    guess_confidence: int = Field(ge=1, le=5)


class DiagnosisAnswer(BaseModel):
    lesion_visible: str
    diagnosis: str
    malignancy_score: int = Field(ge=0, le=100)
    confidence: int = Field(ge=1, le=5)
    further_exam: bool
    exam_type: str | None = None
    lesion_points: list[dict] = Field(default_factory=list)


def auth(authorization: str | None = Header(default=None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Login required")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        row = c.execute("""SELECT users.id,users.username,users.role,users.display_name
            FROM sessions JOIN users ON users.id=sessions.user_id
            WHERE token_hash=? AND expires_at>?""", (digest, int(time.time()))).fetchone()
    if not row:
        raise HTTPException(401, "Session expired")
    return dict(row)


def admin(user=Depends(auth)):
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator only")
    return user


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/api/auth/login")
def login(payload: Login):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (payload.username,)).fetchone()
        if not u or not verify_password(payload.password, u["password_hash"]):
            raise HTTPException(401, "Invalid credentials")
        token = secrets.token_urlsafe(48)
        c.execute("INSERT INTO sessions VALUES (?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), u["id"], int(time.time()) + SESSION_TTL))
        audit(c, u["id"], "login", {})
        return {"token": token, "role": u["role"], "name": u["display_name"]}


@app.get("/api/me")
def me(user=Depends(auth)):
    return user


@app.post("/api/admin/users")
def add_reader(payload: ReaderCreate, user=Depends(admin)):
    with db() as c:
        try:
            c.execute("INSERT INTO users(username,password_hash,role,display_name) VALUES (?,?,?,?)",
                      (payload.username, password_hash(payload.password), "reader", payload.display_name))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Username already exists")
        audit(c, user["id"], "reader_create", {"username": payload.username})
    return {"ok": True}


@app.get("/api/admin/readers")
def readers(user=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,username,display_name FROM users WHERE role='reader' ORDER BY id")]


@app.post("/api/admin/images")
async def upload_image(patient_code: str = Form(...), module: str = Form(...),
                       source: str = Form(...), phase: str = Form(...),
                       reference_label: str = Form(""), file: UploadFile = File(...),
                       user=Depends(admin)):
    if module not in {"quality", "diagnosis"} or source not in {"real", "virtual"}:
        raise HTTPException(400, "Bad module or source")
    phase = phase.upper()
    if phase not in {"NCCT", "AP", "PVP", "DP"}:
        raise HTTPException(400, "Bad phase")
    if module == "quality" and phase == "NCCT":
        raise HTTPException(400, "Quality study requires CECT")
    patient_code = patient_code.strip()
    if not patient_code or len(patient_code) > 100:
        raise HTTPException(400, "Invalid patient code")
    if reference_label and reference_label not in LABELS:
        raise HTTPException(400, "Unsupported reference label")
    raw = await file.read(15 * 1024 * 1024 + 1)
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(413, "Image exceeds 15 MB")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif raw[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    else:
        raise HTTPException(415, "Only real PNG/JPEG bytes are accepted")
    name = uuid.uuid4().hex + ext
    (IMAGES / name).write_bytes(raw)
    code = "IMG-" + secrets.token_hex(4).upper()
    with db() as c:
        c.execute("""INSERT INTO cases(patient_code,module,source,phase,image_path,public_code,reference_label,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (patient_code, module, source, phase, name, code, reference_label or None, int(time.time())))
        audit(c, user["id"], "image_upload", {"code": code, "module": module, "phase": phase})
    return {"ok": True, "public_code": code}


@app.post("/api/admin/import-packs")
def import_packs(user=Depends(admin)):
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


@app.post("/api/demo/initialize")
def demo_initialize():
    """LOCAL PREVIEW ONLY. Never enable on internet-exposed deployments."""
    if os.getenv("WEB4SYN_DEMO_MODE", "").lower() != "true":
        raise HTTPException(404, "Not enabled")
    with db() as c:
        u = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not u:
            raise HTTPException(500, "Admin user missing")
        uid = u["id"]
        token = secrets.token_urlsafe(48)
        c.execute("INSERT INTO sessions VALUES (?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), uid, int(time.time()) + SESSION_TTL))
    user = {"id": uid, "role": "admin"}
    report = import_packs(user=user)
    assigned = generate_assignments(user=user)
    return {"token": token, "name": "本地预览", "role": "admin", "import": report, "assignments": assigned}


@app.get("/api/admin/cases")
def admin_cases(user=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute("""SELECT id,patient_code,module,source,phase,public_code,reference_label
                                              FROM cases ORDER BY id DESC LIMIT 2000""")]


@app.post("/api/admin/assignments/generate")
def generate_assignments(user=Depends(admin)):
    with db() as c:
        readers = [r["id"] for r in c.execute("SELECT id FROM users WHERE role='reader' ORDER BY id")]
        if not readers:
            raise HTTPException(400, "Create at least one reader")
        case_rows = c.execute("SELECT DISTINCT patient_code,module,source FROM cases").fetchall()
        inserted = 0
        # Balanced order within each reader; no counterpart is shown at the same time.
        for reader in readers:
            for row in case_rows:
                patient, module, source = row
                if module == "diagnosis":
                    check = c.execute("""SELECT count(*) FROM cases WHERE patient_code=? AND module=?
                           AND source=? AND phase='NCCT'""", (patient, module, source)).fetchone()[0]
                    contrast = c.execute("""SELECT count(*) FROM cases WHERE patient_code=? AND module=?
                           AND source=? AND phase IN ('AP','PVP','DP')""", (patient, module, source)).fetchone()[0]
                    if not check or not contrast:
                        continue
                code = "CASE-" + secrets.token_hex(5).upper()
                before = c.total_changes
                c.execute("""INSERT OR IGNORE INTO assignments
                    (user_id,module,case_group,source,public_code,assigned_at)
                    VALUES(?,?,?,?,?,?)""", (reader, module, patient, source, code, int(time.time())))
                inserted += c.total_changes - before
        audit(c, user["id"], "assignments_generate", {"inserted": inserted})
    return {"created": inserted, "note": "Pilot assignment only; enforce washout before formal crossover study."}


@app.get("/api/tasks")
def tasks(user=Depends(auth)):
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
def task_detail(task_id: int, user=Depends(auth)):
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
def task_image(task_id: int, image_id: int, user=Depends(auth)):
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
def submit(task_id: int, payload: dict, user=Depends(auth)):
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


@app.get("/api/admin/export/{module}")
def export(module: str, user=Depends(admin)):
    if module not in {"quality", "diagnosis"}:
        raise HTTPException(400, "Invalid module")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["assignment_id", "reader", "patient_code", "source", "module", "opened_at",
                     "submitted_at", "reference_label", "answer_json"])
    with db() as c:
        rows = c.execute("""SELECT a.id,a.user_id,a.case_group,a.source,a.module,a.opened_at,a.submitted_at,
                 u.username,r.answer_json
                 FROM assignments a JOIN users u ON u.id=a.user_id
                 JOIN reviews r ON r.assignment_id=a.id WHERE a.module=? ORDER BY a.id""", (module,))
        for r in rows:
            ref = c.execute("""SELECT reference_label FROM cases WHERE patient_code=? AND module=? AND source=?
                    AND reference_label IS NOT NULL LIMIT 1""", (r["case_group"], module, r["source"])).fetchone()
            writer.writerow([r["id"], r["username"], r["case_group"], r["source"], module,
                             r["opened_at"], r["submitted_at"], ref["reference_label"] if ref else "", r["answer_json"]])
        audit(c, user["id"], "export", {"module": module})
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{module}_reviews.csv"'})


@app.get("/api/admin/progress")
def progress(user=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute("""SELECT u.username,a.module,a.status,COUNT(*) AS count
                FROM assignments a JOIN users u ON u.id=a.user_id
                GROUP BY u.username,a.module,a.status ORDER BY u.username,a.module,a.status""")]
