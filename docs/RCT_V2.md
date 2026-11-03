# Web4Syn v2 — Three-Arm clinical randomized reading platform

Source branch: feat/three-arm-rw-rct-v2 (based on feat/reader-study-v1).

**Study terminology:** RW-RCT is the project display name. Randomizing archived CT images to doctors is a randomized, parallel-group reader study, not a prospective randomized patient-care trial. A true clinical RCT also needs patient-level intervention, ethics approval, enrollment, safety monitoring, and clinical follow-up.

## Implemented MVP

- 3 arms: NCCT; NCCT + synthetic AP/PVP/DP; NCCT + real AP/PVP/DP.
- Exactly one arm, one doctor, and one valid submitted report per patient (1600 reports, not 4800).
- 6–9 readers, with experience grades junior/intermediate/senior; balanced reader and arm counts.
- Admin: configure target, create readers, import anonymous HDF5 cases, randomize exactly once, publish, monitor progress, lock, export.
- Doctor: independent account, only own tasks, synchronized on-demand 2D CT slice viewer with HU window [-160,240], three-part diagnosis form, automatic next case after server-confirmed submit.
- Server-side group/reader authorization for every image slice, immutable results, idempotent repeated submission requests, audit log.
- Isolated RCT database under WEB4SYN_DATA/rct.sqlite. Original preview data under WEB4SYN_DATA/web4syn.sqlite is untouched.

## Requirements

Linux server with Docker Compose and read-only HDF5 packs. Each pack must contain the datasets:
- ncct: [D,512,512] float16 HU
- real_cect: [D,3,512,512] float16 HU (new v2 format; legacy [3,D,512,512] is also supported)
- syn_cect: [D,3,512,512] float16 HU (new v2 format; legacy [3,D,512,512] is also supported)
- optional liver_mask and tumor_mask: [D,512,512] uint8; liver label 5 is used to select a representative slice, never shown to readers.
- optional root attributes: patient_id, crop_z_start, crop_z_end, spacing_xyz, label_type; diagnostic labels are not exposed to readers.

All 3 phases are ordered AP / PVP / DP. Server indexes the newer format as f[dataset][z,phase,:,:] and the legacy format as f[dataset][phase,z,:,:]. The root directory on the host defaults to /dataset/RCTs/data_h5_v2, mounted read-only into /packs. Only individual 2D slices are read. The viewer assumes anatomical alignment upstream. Each pack file basename and patient code must be pseudonymous and contain no directly identifying information.

## Install

    git clone --branch feat/three-arm-rw-rct-v2 https://github.com/Devil-Jian/web4syn.git web4syn-rct
    cd web4syn-rct
    cp .env.example .env
    # Edit .env: WEB4SYN_RCT_ADMIN_PASSWORD and WEB4SYN_PACKS_PATH
    sudo mkdir -p /var/lib/web4syn
    sudo chown 10001:10001 /var/lib/web4syn
    docker compose config -q
    docker compose up -d --build
    docker compose logs --tail=100

Default local URL: http://127.0.0.1:8000/rct . Container binds localhost only. For remote doctors use SSH forwarding or institutionally approved HTTPS identity-aware access (for instance Cloudflare Access). Never expose clinical images through an unprotected public URL.

Bootstrap login: user admin, password is WEB4SYN_RCT_ADMIN_PASSWORD, **minimum 12 characters**. Password is only used to create a new administrator if no administrator account exists. Changing an environment variable does not rotate an existing hashed password.

When migrating from an existing container on port 8000, stop the old container and back up /var/lib/web4syn first. V2 defaults disable the anonymous V1 demo task API; never set WEB4SYN_ENABLE_LEGACY_DEMO=1 on an externally accessible service.

## Doctor account management

Admin console section **02 · 医生账户管理** supports:
- Create doctor accounts (username, alias, seniority, initial password), before task randomization.
- List **all** reader accounts, including disabled accounts, with status and assigned/completed task counts.
- Edit display name at any time; doctor seniority may be changed only before randomization because it affects study design.
- Enable or disable accounts before any assignment; accounts with tasks cannot be disabled to prevent stranded cases. Disabled accounts cannot log in.
- Reset a doctor password with a new password (minimum 12 characters) and the **current administrator password**. This invalidates all existing sessions of that reader without altering tasks or results.
- Account actions are recorded in the study audit log. No destructive delete operation is exposed.

For replacing a reader with assigned tasks, do not disable the account or silently reassign cases. In a test study use the authorized test-reset workflow; for a clinical study an approved protocol-defined reassignment procedure is required.

## Automated patient dataset management (v2.2)

HDF5 root: `/dataset/RCTs/data_h5_v2` on host, mounted read-only at `/packs` in the container. Set `WEB4SYN_PACKS_PATH` in your existing local `.env` file (changes to `.env.example` do not update `.env`).

- On app startup, a background scan enumerates immediate `.h5` children (non-recursive); admin can manually rescan.
- Scan stores filename, anonymous patient code based on filename stem, optional `label_type` and `spacing_xyz` attributes, NCCT dataset depth, size and mtime.
- No image QC, phase validation, NaN checks, image reading, alignment analysis or mask review is performed; upstream server pipeline owns QC.
- Files are indexed without copying or modifying HDF5. Failed metadata reads are reported as unreadable; removed files are marked missing and never silently dropped.
- Admin searches and pages through the catalog, previews 2D NCCT/real/synthetic phases, and excludes/restores cases (with optional reason).
- Exclusion decisions persist across rescans. Frozen/published studies cannot change inclusion decisions.
- Click **Freeze final cohort** to snapshot currently included/available files into `rct_patients` and automatically set the target count. Existing draft patient identifiers are preserved if still included; excluded draft patients are removed only by this explicit freeze action and only before tasks exist.
- Then create 6–9 doctors, run 1:1:1 randomization once, review allocations and publish.
- Original randomized and submitted tasks remain intact across application upgrades or rescans. New files do not enter an already frozen study.
- Before production, use pseudonymized filenames. The admin catalog contains file stems and clinical labels and is restricted to administrator accounts.

### Important migration behavior

Existing `rct.sqlite` is retained. Do **not** remove or reset the database to enable auto indexing.
A study that already has randomized tasks remains fixed: you can scan/preview but cannot freeze a new cohort or exclude participants within the same study. Create a separate study database for a new experimental cohort if required.

## Reset a test-study round (v2.2)

Use this only in a **test installation**. The reset control is disabled by default.

1. In your existing local `.env` set `WEB4SYN_RCT_ALLOW_TEST_RESET=1`, then rebuild and restart with `docker compose up -d --build --force-recreate`.
2. Sign in as an administrator. If the research round is not formally locked, an extra **重置测试研究并重新选择病例** button appears under the admin progress area.
3. Click the button, type the exact phrase `RESET TEST STUDY` and re-enter the current admin password.
4. The application makes a private backup copy under `/app/data/rct_backups/` (host: `WEB4SYN_DATA_PATH/rct_backups/`) before deletion.
5. It clears the test round's diagnoses, assignments and frozen cohort; restores status to `draft`, resets target to 1600 and signs out reader sessions. It **keeps** reader accounts, file index, manual include/exclude decisions and audit events.
6. Revisit the file index, reselect cases, click **锁定最终纳入队列**, randomize and publish again.
7. Disable reset by setting `WEB4SYN_RCT_ALLOW_TEST_RESET=0` after testing.

A research study with status `locked` cannot be reset, even with the test flag enabled. Do not delete `rct.sqlite` to reset a real clinical study. Note that the SQLite backup includes potentially sensitive study metadata and results; restrict folder permissions and protect backup retention.

## Admin operating procedure

1. Log in as admin and verify automatic directory scan completed; choose **Rescan** if needed.
2. Review the candidate list and optionally exclude cases. The Web application does no medical QC.
3. **Freeze final cohort**. The number of included files becomes the study target.
4. Create/verify 6–9 readers, then randomize the frozen patient cohort once.
5. Publish, let doctors perform one independent read per patient.
6. After all tasks have been submitted, lock and export CSV.

## Diagnostic form validation

- lesion_visible = yes: choose exactly one diagnosis and malignancy risk (1..5).
- lesion_visible = no: diagnosis disabled, malignancy risk optional.
- lesion_visible = unsure: diagnosis optional, malignancy risk disabled.
- Diseases: HCC, ICC, MET, FNH, HEM, CYST, OTHER.

No other doctor opinions, ground-truth labels, or AI-vs-real source labels are sent to physician UI. For a true clinical intervention, clinical staff must not be misled about the nature of AI-generated scans, and a medically appropriate rescue pathway is required.

## Tests

    python -m pip install -r requirements-dev.txt
    python -m compileall -q app tests
    node --check static/rct.js
    python -m pytest -q

Tests use synthetic HDF5 stubs with temporary SQLite, not clinical packs. Before real patient use, validate phase alignment, HU fidelity, image load under real network conditions, de-identification, backup restore, authorization and statistical design.

## Deliberately out of scope for this MVP

- Prespecified statistical non-inferiority testing / pathology-ground-truth management
- Prospective clinical enrollment, real-world interventions and follow-up
- Reader password reset, controlled reassignment and formal electronic approval workflows
- Hospital PACS/EHR connectivity and independently qualified GCP-grade audit system

These require a separate clinical/production implementation and protocol review. Current reader timing is opening-to-submit on server plus an optional client duration; background/inactive-time adjustment remains future work.
