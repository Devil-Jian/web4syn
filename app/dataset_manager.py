"""Admin-only HDF5 directory index and manual inclusion workflow.

This module performs no image QC, phase validation, voxel analysis or segmentation.
It reads directory entries and minimal HDF5 metadata; all clinical image QC stays upstream.
Official reader assignments are never changed by a rescan.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import h5py
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import h5_source
from app.rct import admin, audit, database, require_draft, study

router = APIRouter(prefix="/api/rct/admin/dataset", tags=["study-dataset"])
_SCAN_LOCK = threading.Lock()


def init_dataset_tables():
    with database() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS rct_source_files (
            id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL UNIQUE,
            patient_code TEXT NOT NULL,
            center TEXT NOT NULL DEFAULT 'GX',
            depth INTEGER,
            label_type TEXT,
            spacing_xyz TEXT,
            file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            included INTEGER NOT NULL DEFAULT 1 CHECK(included IN (0,1)),
            excluded_reason TEXT,
            missing INTEGER NOT NULL DEFAULT 0 CHECK(missing IN (0,1)),
            error TEXT,
            first_seen INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS rct_source_filter
           ON rct_source_files(included,missing,patient_code);
        CREATE TABLE IF NOT EXISTS rct_scan_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            status TEXT NOT NULL DEFAULT 'idle',
            scanned INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            last_started INTEGER,
            last_finished INTEGER,
            last_error TEXT,
            frozen_at INTEGER,
            frozen_count INTEGER,
            frozen_digest TEXT
        );
        INSERT OR IGNORE INTO rct_scan_state(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS rct_dataset_decisions (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL REFERENCES rct_source_files(id),
            included INTEGER NOT NULL,
            reason TEXT,
            actor_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        """)


init_dataset_tables()


def _short_attr(attrs, key):
    if key not in attrs:
        return None
    val = attrs[key]
    if hasattr(val, "tolist"):
        val = val.tolist()
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="replace")
    if isinstance(val, (list, tuple)):
        return json.dumps(val)
    return str(val)[:120]


def _metadata(path):
    """Minimal index metadata only: no pixel reads or image quality checks."""
    try:
        with h5py.File(path, "r") as f:
            if "ncct" not in f or not isinstance(f["ncct"], h5py.Dataset):
                return None, None, None, "NCCT metadata unavailable"
            depth = int(f["ncct"].shape[0]) if f["ncct"].ndim else None
            return depth, _short_attr(f.attrs, "label_type"), _short_attr(f.attrs, "spacing_xyz"), None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, None, None, f"Metadata unreadable: {str(exc)[:120]}"


def run_scan():
    if not _SCAN_LOCK.acquire(blocking=False):
        return False
    try:
        now = int(time.time())
        with database(immediate=True) as c:
            c.execute("UPDATE rct_scan_state SET status='running',last_started=?,last_error=NULL WHERE id=1", (now,))
        root = h5_source.ROOT
        if not root.is_dir():
            raise FileNotFoundError(f"HDF5 root is missing: {root}")
        filenames = h5_source.patient_files()
        discovered = set()
        scanned = 0
        errors = 0
        for path in filenames:
            filename = path.name
            if not filename.endswith(".h5"):
                continue
            code = path.stem
            if len(code) > 80 or not code or not all(x.isalnum() or x in "_-" for x in code):
                continue
            discovered.add(filename)
            stat = path.stat()
            with database() as c:
                old = c.execute("SELECT id,file_size,mtime_ns,error FROM rct_source_files WHERE filename=?",
                                (filename,)).fetchone()
            if old and old["file_size"] == stat.st_size and old["mtime_ns"] == stat.st_mtime_ns and old["error"] is None:
                with database() as c:
                    c.execute("UPDATE rct_source_files SET missing=0,updated_at=? WHERE filename=?", (now, filename))
                scanned += 1
                continue
            depth, label, spacing, error = _metadata(path)
            if error:
                errors += 1
            with database(immediate=True) as c:
                c.execute("""INSERT INTO rct_source_files
                  (filename,patient_code,depth,label_type,spacing_xyz,file_size,mtime_ns,
                   error,first_seen,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(filename) DO UPDATE SET
                   depth=excluded.depth,label_type=excluded.label_type,
                   spacing_xyz=excluded.spacing_xyz,file_size=excluded.file_size,
                   mtime_ns=excluded.mtime_ns,error=excluded.error,
                   missing=0,updated_at=excluded.updated_at""",
                  (filename, code, depth, label, spacing, stat.st_size, stat.st_mtime_ns,
                   error, now, now))
            scanned += 1
        # Never delete excluded decisions or already published/frozen study patients.
        with database(immediate=True) as c:
            rows = c.execute("SELECT id,filename FROM rct_source_files WHERE missing=0").fetchall()
            for row in rows:
                if row["filename"] not in discovered:
                    c.execute("UPDATE rct_source_files SET missing=1,updated_at=? WHERE id=?", (now, row["id"]))
            c.execute("""UPDATE rct_scan_state SET status='idle',scanned=?,errors=?,
                         last_finished=?,last_error=NULL WHERE id=1""", (scanned, errors, int(time.time())))
        return True
    except Exception as exc:
        with database() as c:
            c.execute("""UPDATE rct_scan_state SET status='failed',
                         last_finished=?,last_error=? WHERE id=1""",
                      (int(time.time()), str(exc)[:300]))
        raise
    finally:
        _SCAN_LOCK.release()


def start_scan():
    if _SCAN_LOCK.locked():
        return False
    threading.Thread(target=_scan_in_background, daemon=True, name="rct-dataset-scan").start()
    return True


def _scan_in_background():
    try:
        run_scan()
    except Exception as exc:
        print(f"RCT dataset scan failed: {exc}", flush=True)


class Selection(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=10000)
    included: bool
    reason: str | None = Field(default=None, max_length=250)


@router.get("/status")
def dataset_status(user=Depends(admin)):
    with database() as c:
        state = dict(c.execute("SELECT * FROM rct_scan_state WHERE id=1").fetchone())
        counts = dict(c.execute("""SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN included=1 AND missing=0 AND error IS NULL THEN 1 ELSE 0 END) AS included,
          SUM(CASE WHEN included=0 THEN 1 ELSE 0 END) AS excluded,
          SUM(CASE WHEN missing=1 THEN 1 ELSE 0 END) AS missing,
          SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS unreadable
          FROM rct_source_files""").fetchone())
        status = study(c)["status"]
    return {"scan": state, "counts": counts, "study_status": status, "root": str(h5_source.ROOT)}


@router.post("/scan")
def trigger_scan(user=Depends(admin)):
    if not start_scan():
        raise HTTPException(409, "Scan already in progress")
    return {"ok": True, "started": True}


@router.get("/files")
def list_files(page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=100),
               query: str = Query("", max_length=100),
               filter: str = Query("all", pattern="^(all|included|excluded|missing|unreadable)$"),
               user=Depends(admin)):
    conditions, args = [], []
    if query:
        conditions.append("(patient_code LIKE ? OR filename LIKE ? OR label_type LIKE ?)")
        q = "%" + query.replace("%", r"\%").replace("_", r"\_") + "%"
        # Escaped wildcards ensure literal patient code searches.
        conditions[-1] = "(patient_code LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\' OR label_type LIKE ? ESCAPE '\\')"
        args.extend([q, q, q])
    if filter == "included":
        conditions.append("included=1 AND missing=0 AND error IS NULL")
    elif filter == "excluded":
        conditions.append("included=0")
    elif filter == "missing":
        conditions.append("missing=1")
    elif filter == "unreadable":
        conditions.append("error IS NOT NULL")
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    with database() as c:
        total = c.execute("SELECT COUNT(*) FROM rct_source_files " + where, args).fetchone()[0]
        rows = [dict(x) for x in c.execute(
            "SELECT id,filename,patient_code,center,depth,label_type,spacing_xyz,"
            "included,excluded_reason,missing,error,file_size FROM rct_source_files "
            + where + " ORDER BY patient_code LIMIT ? OFFSET ?",
            (*args, page_size, (page-1)*page_size)).fetchall()]
    return {"total": total, "page": page, "page_size": page_size, "items": rows}


@router.post("/selection")
def update_selection(payload: Selection, user=Depends(admin)):
    with database(immediate=True) as c:
        require_draft(c)
        state = c.execute("SELECT frozen_at FROM rct_scan_state WHERE id=1").fetchone()
        if state["frozen_at"] is not None or c.execute("SELECT 1 FROM rct_tasks LIMIT 1").fetchone():
            raise HTTPException(409, "Study cohort already frozen or randomized")
        ids = sorted(set(payload.ids))
        rows = c.execute(
            "SELECT id FROM rct_source_files WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids).fetchall()
        if len(rows) != len(ids):
            raise HTTPException(404, "One or more files not in dataset index")
        now = int(time.time())
        for row in rows:
            c.execute("UPDATE rct_source_files SET included=?,excluded_reason=? WHERE id=?",
                      (int(payload.included), None if payload.included else payload.reason, row["id"]))
            c.execute("""INSERT INTO rct_dataset_decisions(file_id,included,reason,actor_id,created_at)
                         VALUES(?,?,?,?,?)""",
                      (row["id"], int(payload.included), payload.reason, user["id"], now))
        audit(c, user["id"], "dataset_selection", {"count":len(ids),"included":payload.included})
    return {"ok": True, "updated": len(ids)}


@router.post("/freeze")
def freeze_cohort(user=Depends(admin)):
    """Explicitly snapshot selected candidates into rct_patients, never after tasks exist."""
    with database(immediate=True) as c:
        require_draft(c)
        state = c.execute("SELECT * FROM rct_scan_state WHERE id=1").fetchone()
        if state["status"] == "running" or state["last_finished"] is None or state["status"] == "failed":
            raise HTTPException(409, "Complete a successful file scan before freezing")
        if state["frozen_at"] is not None:
            raise HTTPException(409, "Study cohort already frozen")
        if c.execute("SELECT 1 FROM rct_tasks LIMIT 1").fetchone():
            raise HTTPException(409, "Cannot change cohort after randomization")
        selected = c.execute("""SELECT filename,patient_code,center,depth,mtime_ns,file_size
           FROM rct_source_files WHERE included=1 AND missing=0 AND error IS NULL
           ORDER BY filename""").fetchall()
        if len(selected) < 3:
            raise HTTPException(422, "At least three available selected files required")
        if len(selected) > 10000:
            raise HTTPException(422, "Selected cohort too large")
        codes = [x["patient_code"] for x in selected]
        if len(set(codes)) != len(codes):
            raise HTTPException(409, "Duplicate patient codes in selected files")
        # Avoid silently destroying historical manually imported patient IDs.
        existing = c.execute("SELECT patient_code,pack_filename FROM rct_patients").fetchall()
        if existing:
            existing_names = {x["patient_code"]: x["pack_filename"] for x in existing}
            selected_names = {x["patient_code"]: x["filename"] for x in selected}
            conflicts = [key for key in existing_names.keys() & selected_names.keys()
                         if existing_names[key] != selected_names[key]]
            if conflicts:
                raise HTTPException(409, "Existing patient identifiers reference different packs")
        # Draft-only migration: selection is explicit and old assignments do not exist.
        # Retain original IDs for included patients; excluded drafts can be removed.
        if existing:
            removed = sorted(set(existing_names) - set(codes))
            for code in removed:
                c.execute("DELETE FROM rct_patients WHERE patient_code=?", (code,))
            if removed:
                audit(c,user["id"],"draft_patients_excluded_on_freeze",{"count":len(removed)})
        now = int(time.time())
        for row in selected:
            c.execute("""INSERT OR IGNORE INTO rct_patients
                     (patient_code,pack_filename,center,depth,created_at)
                     VALUES (?,?,?,?,?)""",
                      (row["patient_code"],row["filename"],row["center"],row["depth"],now))
        digest = hashlib.sha256(json.dumps(
            [(x["filename"],x["mtime_ns"],x["file_size"]) for x in selected],
            separators=(",",":")).encode()).hexdigest()
        c.execute("UPDATE rct_studies SET target=? WHERE id=1", (len(selected),))
        c.execute("""UPDATE rct_scan_state SET frozen_at=?,frozen_count=?,frozen_digest=? WHERE id=1""",
                  (now,len(selected),digest))
        audit(c,user["id"],"dataset_frozen",{"count":len(selected),"digest":digest})
    return {"ok":True,"count":len(selected),"digest":digest}


@router.get("/files/{file_id}/images/{dataset}/{z}")
def admin_preview(file_id: int, dataset: str, z: int, user=Depends(admin)):
    """Admin preview only. It does not expose mask data or HDF5 to doctors."""
    from app.h5_source import encode, render
    mapping = {"NCCT": ("ncct","NCCT"),"SYN_AP":("syn_cect","AP"),
               "SYN_PVP":("syn_cect","PVP"),"SYN_DP":("syn_cect","DP"),
               "REAL_AP":("real_cect","AP"),"REAL_PVP":("real_cect","PVP"),
               "REAL_DP":("real_cect","DP")}
    if dataset not in mapping:
        raise HTTPException(404, "Unknown phase")
    with database() as c:
        row = c.execute("SELECT filename,depth,missing FROM rct_source_files WHERE id=?", (file_id,)).fetchone()
    if not row or row["missing"] or row["depth"] is None or z < 0 or z >= row["depth"]:
        raise HTTPException(404, "File or slice unavailable")
    modality, phase = mapping[dataset]
    try:
        payload = render(encode(row["filename"], modality, z, phase))
    except (ValueError,KeyError,IndexError,OSError,TypeError) as exc:
        raise HTTPException(422, "Preview unavailable")
    return Response(payload,media_type="image/png",headers={"Cache-Control":"private, no-store"})
