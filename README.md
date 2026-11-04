# Web4Syn v2 — Three-Arm RW-RCT Reading Platform

This branch adds an authenticated, randomized three-arm patient-level reader workflow **without modifying the historical V1 review database**.

- Study arms: NCCT (533) / NCCT + synthetic CECT (533) / NCCT + real CECT (534), for 1600 participants.
- 6–9 participating readers, exactly one patient-to-reader allocation and one official read per patient.
- Admin control panel, strict image authorization, 2D HDF5 slice viewer, single-submit diagnostic form, locked CSV export.
- Start here: [docs/RCT_V2.md](docs/RCT_V2.md). At first boot configure the required administrator password and HDF5 mount path from [.env.example](.env.example).
- Browser entry: /rct (also the default home page in this branch).
- Test command: python -m pytest -q (after installing requirements-dev.txt).

**Important:** The project display name does not imply that retrospective randomized reader assessments qualify as a prospective real-world clinical RCT. Prospective interventions require their own ethics, patient safety, trial registration and outcome workflows.

---

## Historical V1 documentation

# web4syn — 2D Virtual CECT Clinical Reader Study

A lightweight web application for **blinded two-dimensional clinical reader studies**.

## Modules
1. **Image realism and quality** — blinded Real/Virtual CECT recognition, 1–5 quality score and recognition confidence.
2. **Diagnostic assessment** — NCCT + Real/Virtual CECT phase panels, lesion visibility, disease classification, malignancy likelihood, confidence, further-exam recommendation.
3. **Administrator** — case registration, image upload, balanced assignment, review progress, data export, audit trail.

### Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export WEB4SYN_ADMIN_PASSWORD='replace-with-a-strong-password'
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open http://127.0.0.1:8000. Default username: `admin`. Set `WEB4SYN_ADMIN_PASSWORD` **before the first launch**. For remote use deploy behind TLS, controlled network access, backups and institutional approvals.

### Study design and scope
- Every case consists of selected **2D** image(s), not a 3D volume or stack.
- Real and virtual images are **separate blinded tasks**. Never expose source labels, ground truth, or the same patient's counterpart in the reader API.
- Case/task identifiers are pseudonyms; no patient names or identifiers should enter the application.
- Only JPEG/PNG upload is implemented in V1. It does **not** preserve HU or clinical-grade windowing; original image arrays should be retained separately. Use images with consistent pre-rendered windowing and resolution across arms.
- The study can support claims about selected 2D images, **not** full-volume lesion detection or full 3D CECT clinical interchangeability.
- Admin can register new cases by JSON multipart upload; list quality and diagnostic tasks and assign readers.
- For formal studies, add prespecified randomization, washout scheduling and audit signoff; set a non-inferiority endpoint, margin and statistical plan separately.

### Example administrator workflow
1. Login as admin; create a reader account.
2. Upload a JPEG/PNG case with `patient_code`, `phase`, `source` and `module`. For diagnostic cases, upload NCCT and contrast phases with the **same** `patient_code`, `source`, `module=diagnosis`.
3. Click **Generate blinded assignments**; readers log in and open their assigned cases.
4. Export CSV after review.

### API
- `POST /api/auth/login`
- `GET /api/me`
- `POST /api/admin/users`
- `POST /api/admin/images` (multipart)
- `POST /api/admin/assignments/generate`
- `GET /api/tasks` and `GET /api/tasks/{id}`
- `POST /api/tasks/{id}/submit`
- `GET /api/admin/export/{module}`

Architecture: **FastAPI / SQLite / static HTML + CSS + JavaScript**. Database and uploads are stored in `data/`, excluded from Git.
