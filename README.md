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
