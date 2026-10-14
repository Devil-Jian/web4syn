# Three-patient local functional preview

Enable only on a trusted localhost development machine. This mode **bypasses login**, creates an admin session and automatically imports the patient HDF5 files from /packs. Never expose it through a public tunnel.

Set `WEB4SYN_DEMO_MODE=true` in your existing .env (retain WEB4SYN_ADMIN_PASSWORD). Build the image and recreate with `docker compose up -d --no-build --force-recreate`.

Browse locally: http://127.0.0.1:8000

Quality tasks: 3 patients x 3 phases x 2 sources = 18 (if all valid). Diagnosis tasks: 3 patients x 2 sources = 6. Existing assignments/submissions are retained, not reset.

Only one fixed, liver-middle 2D slice is selected; this is not yet lesion-targeted. No ground truth is created automatically. Formal non-inferiority research requires proper slice selection, blinded study randomization and washout.

Disable preview (`WEB4SYN_DEMO_MODE=false`) before exposing service to any remote reader. Do not clear an existing research database to reset test answers.
