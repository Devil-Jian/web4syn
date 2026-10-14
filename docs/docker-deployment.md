# Ubuntu Docker deployment

## Requirements
- Docker Engine + Docker Compose plugin installed on the Ubuntu host.
- This compose file requires **no GPU, CUDA, PyTorch, host IPC or SSH daemon**.
- Only server operator installs software; doctors need a browser.

## Install

```bash
git clone -b feat/reader-study-v1 https://github.com/Devil-Jian/web4syn.git
cd web4syn
cp .env.example .env
# Set a unique admin password (>=12 chars) in .env with your editor.
chmod 600 .env
mkdir -p runtime
# Required only if host directory ownership does not already permit uid 10001 writes:
sudo chown -R 10001:10001 runtime
docker compose up -d --build
docker compose logs -f web
```

From the Ubuntu host, open `http://127.0.0.1:8000` or test locally using curl. Remote access should use a secure tunnel/proxy, NOT a publicly exposed :8000 port.

If port 8000 is already in use, change only the **left side** of the compose port mapping, e.g. `127.0.0.1:18080:8000`.

## Data

Host study packs:
`/home/ch4090/ytwillv2/zhongj/dataset/train_output/syn-final-1013/doctor_reading_packs`

Container read-only mount: `/packs`.

Web4Syn application database and UI uploads persist at `./runtime` on the host (container path `/app/data`). The current V1 does **not automatically index/import arbitrary pack structures**. Upload de-identified PNG/JPEG images through Admin; support for direct mounted-pack import requires inspecting the pack filename/metadata schema to avoid mixing Real/Virtual labels and patient identities.

```bash
# Inspect read-only mount from container
docker compose exec web ls -lah /packs
# Follow logs
docker compose logs -f web
# Stop, preserving data
docker compose down
```

## Access options

- **Local test**: SSH port forwarding: `ssh -L 8000:127.0.0.1:8000 USER@SERVER`, then visit `http://127.0.0.1:8000`.
- **Doctors via browser**: publish via a properly configured authenticated HTTPS reverse proxy or Cloudflare Tunnel. Do not bind port 8000 to `0.0.0.0` without a security layer.
- Cloudflare Tunnel can run on the host and forward to `http://127.0.0.1:8000`. If it runs in another Docker container, both containers need a shared network and the destination should be `http://web:8000` rather than `localhost`.

## Backup
Backup `runtime/` regularly (including SQLite and uploaded images). For SQLite use an online backup or stop the service during filesystem backup to avoid inconsistent database copies.

## Important scope limitations
- Prototype was written without test/build verification.
- Static PNG/JPEG images do not support true HU windowing.
- Pilot assignment generator is not a formal randomized crossover/washout study.
- The app is not yet hardened or clinically validated. Complete privacy, consent/IRB, authentication, auditing, and security review before patient data or external access.
