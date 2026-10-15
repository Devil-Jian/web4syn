# Web4Syn 2D HDF5 preview — Ubuntu Docker

## Source layout
The project contains Dockerfile, compose.yaml, requirements.txt, app/main.py,
app/h5_source.py and static/{index.html,app.js,style.css}.

## Host directories
- Patient HDF5: /home/ch4090/ytwillv2/zhongj/dataset/train_output/syn-final-1013/doctor_reading_packs
- Persistent SQLite/data: /var/lib/web4syn

HDF5 mount is read-only. No 3D data is transferred to the browser.

## Quick start
```bash
cd ~/ytwillv2/zhongj
git clone --branch feat/reader-study-v1 https://github.com/Devil-Jian/web4syn.git web4syn-clean
cd web4syn-clean
cp .env.example .env
# Set WEB4SYN_ADMIN_PASSWORD to 12+ characters.
# WEB4SYN_DEMO_MODE=true for LOCAL testing only.
sudo mkdir -p /var/lib/web4syn
sudo chown 10001:10001 /var/lib/web4syn
docker compose config -q
docker build --pull=false --network=host \
  --build-arg HTTP_PROXY=http://127.0.0.1:27890 \
  --build-arg HTTPS_PROXY=http://127.0.0.1:27890 \
  -t web4syn:reader-v1 .
docker compose up -d --no-build --force-recreate
docker compose ps
docker logs --tail 100 web4syn-reader
docker exec web4syn-reader ls -lh /packs
```

If a previous web4syn-reader container exists from an old checkout, stop it before starting the new one.
A non-empty /var/lib/web4syn is not cleared by recreating the container.
To avoid downtime use the existing checkout path and Git reset after backing up your local modifications.

Visit http://127.0.0.1:8000 through SSH forwarding.
If testing curl from the host, bypass proxies:
`curl --noproxy '*' http://127.0.0.1:8000/`.

## Scope and safety
Demo mode bypasses login; never expose publicly or through Cloudflare while enabled.
Current HDF5 import picks the median liver-mask slice, not a lesion-targeted slice.
Assignments in demo mode are not a valid clinical randomization/washout design.
Only local workflow preview, not formal clinical deployment.
