# Web4Syn v2 — Three-Arm clinical randomized reading platform

Source branch: feat/three-arm-rw-rct-v2 (based on feat/reader-study-v1).

**Study terminology:** RW-RCT is the project display name. Randomizing archived CT images to doctors is a randomized, parallel-group reader study, not a prospective randomized patient-care trial. A true clinical RCT also needs patient-level intervention, ethics approval, enrollment, safety monitoring, and clinical follow-up.

## Implemented MVP

- 3 arms: NCCT; NCCT + synthetic AP/PVP/DP; NCCT + real AP/PVP/DP.
- Exactly one arm, one doctor, and one valid submitted report per patient (1600 reports, not 4800).
- 6–9 readers, with experience grades junior/intermediate/senior; balanced reader and arm counts.
- Admin: configure target, create readers, import anonymous HDF5 cases, randomize exactly once, publish, monitor progress, lock, export.
- Doctor: independent account, only own tasks, synchronized on-demand 2D CT slice viewer with HU window [-160,240], four-part diagnosis form, automatic next case after server-confirmed submit.
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

## Doctor self-registration (automatic activation)

The login screen has a compact **医生注册账号** entry. A physician submits a username, name/alias, and password (minimum 6 characters, entered twice). Seniority is not requested; it remains unset until an administrator optionally edits the account.

New registrations are automatically stored as **enabled** reader accounts with `registration_status=approved`, without manual approval. The physician can immediately sign in. Signing in does not grant access to cases: task endpoints enforce ownership of assigned patients and publication state. New registrations during a published study receive no existing assignments. Administrator controls can still deactivate accounts without assigned tasks.

Duplicate usernames and malformed submissions remain rejected. Auto-registration should be enabled only on an HTTPS-protected, access-controlled study network; public registration can create untrusted active accounts or interfere with the 6–9-active-readers preflight count. Consider invitations and server-side rate limiting before public deployment.

## Doctor account management

Admin console section **02 · 医生账户管理** supports:
- Add doctor accounts directly (username, alias, seniority, initial password) even while another round is published. New accounts do not inherit existing assignments.
- List **all** reader accounts, including disabled accounts, with status and assigned/completed task counts.
- Edit display name at any time; doctor seniority may be changed only before randomization because it affects study design.
- Enable or disable accounts before any assignment; accounts with tasks cannot be disabled to prevent stranded cases. Disabled accounts cannot log in.
- Reset a doctor password with a new password (minimum 6 characters) and the **current administrator password**. This invalidates all existing sessions of that reader without altering tasks or results.
- Permanently delete unassigned doctor accounts directly in the same list, with a username confirmation. Accounts with active-round assignments cannot be deleted; finish/archive the round first. Deletion revokes sessions and creates an audit event. Admin accounts cannot be deleted through this route.

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

## Ending an unfinished round and starting another

The admin progress panel now has **强制结束本轮并开启下一轮** when the current study is published. This is separate from the destructive test reset.

1. Click the button and confirm that you want to end the currently published round early.
2. Re-enter the administrator password. The operation runs as **one SQLite transaction**: all patient assignments, assigned-arm data and existing reader responses are copied into permanent `rct_round_archives` / `rct_round_archive_tasks` tables, and only then are active tasks cleared.
3. An archive records both **completed and incomplete** task statuses; no missing reader answer is imputed. All existing doctor sessions are revoked, so any open reader window must log in again.
4. The active study returns to `draft` with no assigned patients. Doctor accounts, HDF5 file index, inclusion decisions and historical archives are retained.
5. Reselect candidate cases as needed, then press **一键分配并发布任务** to start the next round.
6. Historical rounds remain listed under the admin progress area, with an authorized CSV download containing both completed and unfinished cases.

Important: This feature ends an active `published` round even when the task count is incomplete. It does not operate on formally `locked` studies. A new round may reuse the same cases or doctors: maintain distinct archived round identifiers in analysis. Archive tables contain sensitive case metadata and diagnosis results; limit server and account access. This is an administrative early-stop operation, not an approved clinical trial stopping rule.

## Simplified one-click study launch

The administrator no longer needs to configure target sample size, set a randomization seed, freeze the cohort separately, or press a second Publish button. The main admin panel now shows **一键分配并发布任务** with readiness checks.

1. Automatic HDF5 indexing populates the candidate list from `/dataset/RCTs/data_h5_v2`; exclude only unsuitable files.
2. Ensure 6–9 readers are enabled. The panel reports the included-case and enabled-doctor totals and lists any blocking issues.
3. Click **一键分配并发布任务**, then confirm the exact counts once. The server performs cohort freeze, 1:1:1 allocation, and publication **in a single SQLite transaction**. It automatically sets the research target to the included cohort size.
4. Existing **frozen** studies can also use this button; a prior randomized-but-unpublished study may publish its existing tasks without rerandomizing.
5. The same published study cannot be published again. To restart a test round, use the separately gated test-reset function; locked studies remain immutable.

Existing legacy APIs remain for backwards compatibility, but the admin UI presents only the guided launch action. Neither the web indexer nor the launcher performs medical image QC.

## Admin operating procedure

1. Log in and confirm directory scanning has finished.
2. Exclude unwanted candidate cases in the patient list and create/enable 6–9 readers.
3. Click **一键分配并发布任务**, check the displayed patient/doctor counts, and confirm.
4. Let readers complete their tasks. Lock and export results after all reports are submitted.

## Diagnostic form validation

- `lesion_visible = yes`: explicitly choose an independent `lesion_nature` (`benign`, `malignant`, or `unsure`), the most likely diagnosis type, and a malignancy risk score (1–5). These fields cannot be inferred from one another.
- `lesion_visible = no`: nature, diagnosis and risk are all disabled and saved as NULL.
- `lesion_visible = unsure`: existence is uncertain; nature, diagnosis and risk remain enabled but optional. Selected values are saved as entered; unselected fields stay NULL.
- Risk scale is ordinal: 1 extremely unlikely malignant; 2 unlikely; 3 indeterminate; 4 likely; 5 extremely likely. It is **not** pathology grade or tumor staging.
- Specific subtype `OTHER` should be interpreted alongside the **independently recorded** nature. Findings such as cirrhosis or biliary stones are not necessarily focal hepatic lesions; protocol inclusion and classification must be specified prospectively.
- `rct_results.lesion_nature` is added to existing SQLite databases without losing legacy submissions. Prior diagnoses are left NULL, not guessed from risk or subtype. Current locked CSV exports and early-end archives include the field.
- Diseases: HCC, ICC, MET, FNH, HEM, CYST, OTHER, UNSURE (uncertain lesion subtype). The nature choices appear as malignant, benign, unsure; risk 1–5 remains available without a visible section heading.

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


## Remote reading performance: per-CASE progressive prefetch

The reader viewer downloads the requested z/phase immediately, then prefetches
the surrounding ~20 slices before loading the rest of the current CASE.
Background batches use `GET /api/rct/tasks/{task_id}/images/batch?start=...&end=...`
with a maximum of 12 contiguous slices and up to two batch HTTP requests
in flight. The server validates task ownership, opened status, study publication
and arm-specific access before rendering, and opens one HDF5 handle per batch.
The legacy single-slice route remains a fallback.

Batch wire format (application/octet-stream, no-store):
`uint32_le(manifest_size) + UTF-8 JSON manifest + concatenated PNG images`.
The manifest contains `version: 1` and `images: [{phase,z,offset,length}]`.
Offsets are relative to the first PNG byte. No source HDF5 path is returned.

Browser memory uses separate per-CASE budgets: ~128 MiB of decoded image
data and ~256 MiB of compressed PNG blobs (approximate accounting, not an
absolute browser RSS limit). The current frame, target frame and in-flight
images are protected against eviction. The priority queue is rebuilt when
scrolling direction changes; target images always bypass background batches.
If batch requests are unsupported, the viewer switches to bounded single-image
prefetching. On case completion or switching, in-flight prefetch requests are
aborted and all patient image blobs/URLs are discarded.

Security: prefetch never fetches other readers' cases or a forbidden REAL/AI
arm. A doctor does not obtain the next CASE until the current diagnosis has
been submitted through the existing task workflow. Measure p50/p95 slice
latency and memory in the actual remote network before interpreting
performance targets as achieved.
