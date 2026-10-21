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
- ncct: [D,512,512]
- real_cect: [3,D,512,512]
- syn_cect: [3,D,512,512]

All 3 phases are ordered AP / PVP / DP. The viewer assumes anatomical alignment upstream. Each pack file basename and patient code must be pseudonymous and contain no directly identifying information.

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

## Admin operating procedure

1. Set the target to 1600 and register 6–9 doctors, with correct reader levels.
2. Prepare anonymized manifest CSV rows in the format: patient_code,pack_filename,center .
3. Import all HDF5 packs and verify they have both real and synthetic contrast data.
4. Run randomization once, obtaining precisely NCCT=534, AI=533, REAL=533 when 1600 subjects, because remainder is assigned in fixed arm order (actual API returns counts). The SHA256 commitment of the random seed is logged. To reproduce allocations, supply and privately retain a known seed.
5. Verify doctor workloads and publish task assignments.
6. Doctors complete their own queues. Cases cannot be reopened after confirmed submission.
7. Once all submitted, lock the study and export CSV for independent statistical analysis.

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
