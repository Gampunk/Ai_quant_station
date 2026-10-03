# Version 2 test deployment: step-by-step plan

**For:** an AI assistant guiding a person, one step at a time, through deploying
Version 2 next to the live Version 1 on the same Linux server, plus a second MT5
connector on the Windows machine.

**Goal:** Version 2 runs fully isolated, on its own subdomain, database, port,
service and demo MT5 account, so it can be compared with Version 1. Nothing is
merged and nothing of Version 1 changes.

---

## 0. Rules for the guiding assistant

Follow these in every step.

1. **Never touch Version 1.** That means:
   - `/opt/impulse_analyst` (read only, and only where a step says so)
   - `impulse-analyst.service`
   - port `8002`
   - the `finance_engine` database
   - the live connector on port `5001`
   - the existing nginx site for `autopilot.thefinanceengine.com`

   Never restart, stop, edit or reconfigure any of them. If a step would affect
   one, stop and ask.
2. **One step at a time.** Give one command block, ask the person to paste the
   output, check it against "Expect", then continue. On anything unexpected, stop
   and diagnose before going on.
3. **Never ask for, print or repeat a secret.** Passwords and tokens are generated
   on the server into a file, or typed by the person. When checking a file, show
   variable names only (`cut -d= -f1`).
4. **Check nginx before reloading it.** Always `sudo nginx -t` first, and reload
   (never restart) only if it says "syntax is ok". A reload with a valid config
   does not interrupt Version 1.
5. **Plain, short language.** The person wants the least verbose explanations possible.
6. **Placeholders**, decided with the person in Phase 1: `<V2_DOMAIN>`,
   `<WINDOWS_IP>`, `<LINUX_IP>`. Never invent their values.

## Names used throughout

| Thing | Version 1 (live, do not touch) | Version 2 (test, new) |
|---|---|---|
| Code | `/opt/impulse_analyst` | `/opt/quant_station_compare` (already cloned) |
| Branch | Gautam-813 `master` | Gampunk `v2/merge` |
| Service | `impulse-analyst.service` | `quant-station-v2.service` |
| Monitor | (none or upstream's) | `quant-station-v2-monitor.service` + `.timer` |
| Runs as | root | `quantv2` (new system user) |
| Backend port | 8002 | **8004**, on 127.0.0.1 only (8003 is taken) |
| Python | system 3.10 | 3.11, private to Version 2 (via `uv`) |
| Database | `finance_engine` | `finance_engine_v2`, owner `quant_v2` |
| Website | `autopilot.thefinanceengine.com` | `<V2_DOMAIN>`, e.g. `autopilot-v2.thefinanceengine.com` |
| MT5 connector | Windows port 5001, live setup | Windows port **5002**, Version 2 connector code, second terminal, **demo account** |
| Label in the app | (title says v2) | Sidebar and `/health` say **Version 2** |

---

## Phase 1. Decide and check (read only)

**1.1 Decide the three placeholders with the person:**
- `<V2_DOMAIN>`: the test subdomain. A DNS **A record** for it must point to this server.
- `<WINDOWS_IP>`: the Windows connector machine's address.
- `<LINUX_IP>`: this server's public address, as seen by the Windows machine.

**1.2 Check that nothing Version 2 needs is taken:**
```bash
sudo ss -ltnp | grep -E ':(8004)\b' || echo "port 8004 free"
id quantv2 2>/dev/null || echo "user quantv2 free"
systemctl list-unit-files | grep -E 'quant-station-v2' || echo "service names free"
sudo -u postgres psql -tAc "select 1 from pg_database where datname='finance_engine_v2'" | grep -q 1 && echo "DB EXISTS" || echo "database name free"
ls /etc/nginx/sites-enabled/ ; which certbot || echo "certbot missing"
dig +short <V2_DOMAIN>
```
**Expect:** every line says free, certbot is present (the live site uses HTTPS), and `dig` prints this server's IP. If DNS is not set up yet, the person creates the A record now; continue once `dig` shows the IP.

**1.3 Bring the clone up to date** (the plan needs commit `e5c44e4` or later):
```bash
cd /opt/quant_station_compare && sudo git fetch origin && sudo git checkout v2/merge && sudo git pull --ff-only
git log --oneline -1 && git status -sb | head -1
```
**Expect:** `v2/merge...origin/v2/merge` with no local changes, and the latest commit mentioning the instance label in Telegram messages.

---

## Phase 2. A user and a private Python 3.11

**2.1 System user, owning only Version 2's folder:**
```bash
sudo useradd --system --home-dir /opt/quant_station_compare --shell /usr/sbin/nologin quantv2
sudo chown -R quantv2:quantv2 /opt/quant_station_compare
```

**2.2 Install `uv`.** It's a single binary in `/usr/local/bin` and changes no system Python:
```bash
which uv || curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
uv --version
```

**2.3 Python 3.11 and the backend packages, all inside Version 2's folder:**
```bash
cd /opt/quant_station_compare/backend
sudo -u quantv2 env UV_PYTHON_INSTALL_DIR=/opt/quant_station_compare/.python UV_CACHE_DIR=/opt/quant_station_compare/.uv-cache \
  uv venv --python 3.11 .venv
sudo -u quantv2 env UV_PYTHON_INSTALL_DIR=/opt/quant_station_compare/.python UV_CACHE_DIR=/opt/quant_station_compare/.uv-cache \
  uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python --version
```
**Expect:**
- Python 3.11.x.
- The install ends with no errors. It downloads about 1 GB, mostly CPU torch, and takes several minutes.
- The command must run from `backend/`, so uv reads `uv.toml`.

---

## Phase 3. Database

**3.1 Create the role and database.** The password is generated into a root-only file and never shown:
```bash
sudo install -m 600 /dev/null /root/quant_v2_db_password
openssl rand -hex 24 | sudo tee /root/quant_v2_db_password >/dev/null
sudo -u postgres psql -v pw="$(sudo cat /root/quant_v2_db_password)" <<'SQL'
CREATE ROLE quant_v2 LOGIN PASSWORD :'pw';
CREATE DATABASE finance_engine_v2 OWNER quant_v2;
SQL
sudo -u postgres psql -tAc "select datname, pg_get_userbyid(datdba) from pg_database where datname='finance_engine_v2'"
```
**Expect:** `finance_engine_v2|quant_v2`. `finance_engine` is untouched.

---

## Phase 4. Configuration (`backend/.env`)

**4.1 Generate the file, with every secret new and none printed:**
```bash
cd /opt/quant_station_compare/backend
DBPW=$(sudo cat /root/quant_v2_db_password)
sudo -u quantv2 install -m 600 /dev/null .env
sudo -u quantv2 tee .env >/dev/null <<EOF
APP_ENV=production
INSTANCE_LABEL=Version 2
SECRET_KEY=$(openssl rand -hex 32)
DATABASE_URL=postgresql+asyncpg://quant_v2:${DBPW}@127.0.0.1:5432/finance_engine_v2
CORS_ORIGINS=https://<V2_DOMAIN>
FORWARDED_ALLOW_IPS=127.0.0.1
# Second connector, Version 2 code, demo account (Phase 7). Filled in there.
MT5_CONNECTOR_URL=http://<WINDOWS_IP>:5002
MT5_API_TOKEN=$(openssl rand -hex 32)
# The connector is on a public address until a private tunnel exists (Phase 10).
ALLOW_REMOTE_CONNECTOR=true
# Leave empty: Version 2 must not upload price files to Version 1's Hugging Face dataset.
HUGGINGFACE_API_KEY=
EOF
unset DBPW
```
**4.2 The person adds the rest by hand,** with `sudo -u quantv2 nano .env`:
- `DEFAULT_ADMIN_PASSWORD=`: a new password, 12+ characters, not Version 1's. It creates the Version 2 admin on first start.
- AI keys (`NVIDIA_API_KEY=`, `GROQ_API_KEY=` and so on): the same as Version 1 or separate.
- `TELEGRAM_BOT_TOKEN=` and `TELEGRAM_CHAT_ID=`: **preferably a separate chat** from Version 1. Every Version 2 message says "Version 2" either way.

**4.3 Check names only:**
```bash
sudo -u quantv2 cut -d= -f1 .env | grep -v '^#' | grep -v '^$'
```

**4.4 Copy the connector token for Phase 7,** shown once in the terminal, to type into the Windows `.env`:
```bash
sudo -u quantv2 grep '^MT5_API_TOKEN=' .env | cut -d= -f2-
```
The person copies it directly; the assistant must not repeat it.

---

## Phase 5. Frontend build

```bash
cd /opt/quant_station_compare/frontend
sudo -u quantv2 env HOME=/opt/quant_station_compare npm ci
sudo -u quantv2 env HOME=/opt/quant_station_compare npm run build
ls dist/index.html
```
**Expect:** `dist/index.html` exists. If the build fails on Node 20, install Node 24 for `quantv2` only with nvm; don't change the system Node that Version 1 uses.

**Optional, price history for the Historical Lab.** A read-only copy from Version 1, only if it exists:
```bash
ls /opt/impulse_analyst/data_archive/parquet_storage 2>/dev/null && \
  sudo -u quantv2 mkdir -p /opt/quant_station_compare/data_archive/parquet_storage && \
  sudo cp -a /opt/impulse_analyst/data_archive/parquet_storage/. /opt/quant_station_compare/data_archive/parquet_storage/ && \
  sudo chown -R quantv2:quantv2 /opt/quant_station_compare/data_archive
```

---

## Phase 6. The backend service

**6.1 Write the unit:**
```bash
sudo tee /etc/systemd/system/quant-station-v2.service >/dev/null <<'EOF'
[Unit]
Description=AI Quant Station Version 2 (test)
After=network.target postgresql.service

[Service]
Type=simple
User=quantv2
Group=quantv2
WorkingDirectory=/opt/quant_station_compare/backend
Environment="APP_ENV=production"
Environment="HOST=127.0.0.1"
Environment="PORT=8004"
Environment="FORWARDED_ALLOW_IPS=127.0.0.1"
# One worker only: autopilot state and login limits live in this process.
ExecStart=/opt/quant_station_compare/backend/.venv/bin/python /opt/quant_station_compare/backend/run.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
```

**6.2 First start, watching the log:**
```bash
sudo systemctl start quant-station-v2
sleep 20; sudo journalctl -u quant-station-v2 -n 40 --no-pager
```
**Expect:**
- `Database schema created, at revision f3b8d1e6a2c9` (logs are JSON lines in production).
- `Admin account created from DEFAULT_ADMIN_PASSWORD`.
- `Application startup complete`.
- A connector warning is normal until Phase 7.

If startup fails, read the error. The common causes are a weak `SECRET_KEY`, a missing `DEFAULT_ADMIN_PASSWORD`, or a missing `MT5_API_TOKEN`. Fix `.env` and restart Version 2 only.

**6.3 Check that it answers locally and not from outside:**
```bash
curl -s 127.0.0.1:8004/health; echo
sudo ss -ltnp | grep 8004
```
**Expect:** `{"status":"healthy","instance":"Version 2"}`, listening on `127.0.0.1:8004` only.

**6.4 Start at boot:**
```bash
sudo systemctl enable quant-station-v2
```

---

## Phase 7. The second MT5 connector (Windows)

All of this is on the Windows machine. **The live connector on port 5001 keeps running untouched.**

**7.1 A second MT5 terminal in its own folder,** for example `C:\MT5-V2\`:
- Install it again from the broker's installer into that folder, or copy the existing terminal folder and start it with `terminal64.exe /portable`.
- Log it into the **demo account**.
- In Tools, Options, Expert Advisors, tick **Allow algorithmic trading**.

**7.2 Version 2's connector code,** in its own folder, for example `C:\quant-v2\`:
```powershell
git clone -b v2/merge https://github.com/Gampunk/Ai_quant_station C:\quant-v2
py -3.11 -m venv C:\quant-v2\venv
C:\quant-v2\venv\Scripts\python.exe -m pip install -r C:\quant-v2\mt5_connector\requirements.txt
```
(Python 3.11 to 3.14 all work. If git isn't installed, download the branch as a ZIP from GitHub.)

**7.3 `C:\quant-v2\mt5_connector\.env`,** created with Notepad:
```
MT5_CONNECTOR_PORT=5002
MT5_CONNECTOR_HOST=0.0.0.0
MT5_TERMINAL_PATH=C:\MT5-V2\terminal64.exe
MT5_API_TOKEN=<the token copied in step 4.4>
MT5_REQUIRE_DEMO=true
MT5_MAX_VOLUME=0.10
```
- `MT5_REQUIRE_DEMO=true` makes the connector refuse to trade on anything but a demo account.
- `MT5_MAX_VOLUME` caps every order, here at 0.10 lot, whatever the website sends.

**7.4 Let only the Linux server in,** in PowerShell as Administrator:
```powershell
New-NetFirewallRule -DisplayName "Quant V2 connector" -Direction Inbound -Protocol TCP -LocalPort 5002 -RemoteAddress <LINUX_IP> -Action Allow
```
Also allow port 5002 from `<LINUX_IP>` only in any cloud or VPS firewall in front of the Windows machine.

**7.5 Start it:**
```powershell
cd C:\quant-v2\mt5_connector
C:\quant-v2\venv\Scripts\python.exe connector.py
```
Leave this window open for now. Making it start by itself (Task Scheduler or NSSM) can come after the test works.

**7.6 From the Linux server,** check that it answers and holds the demo account:
```bash
cd /opt/quant_station_compare/backend
T=$(sudo -u quantv2 grep '^MT5_API_TOKEN=' .env | cut -d= -f2-)
curl -s http://<WINDOWS_IP>:5002/health -H "Authorization: Bearer $T"; echo
curl -s -X POST http://<WINDOWS_IP>:5002/initialize -H "Authorization: Bearer $T" | python3 -c "
import sys, json; r = json.load(sys.stdin); a = r.get('account', {})
print('login', a.get('login'), '| server', a.get('server'), '| trade_mode', a.get('trade_mode'), '| demo guard', r.get('require_demo'))"
unset T
```
**Expect:** health answers, and initialize prints the **demo** login and server with `trade_mode demo` and `demo guard True`. If it says `real`, stop and fix the terminal login before anything else. (With the demo guard on, the connector would refuse to trade on a real account anyway.)

**7.7 Restart Version 2** so it connects:
```bash
sudo systemctl restart quant-station-v2; sleep 20
sudo journalctl -u quant-station-v2 -n 20 --no-pager | grep -iE "connector|clock|error" || true
```

---

## Phase 8. The website (nginx and HTTPS)

**8.1 A new site file.** It doesn't touch the live site's file:
```bash
sudo tee /etc/nginx/sites-available/quant-station-v2 >/dev/null <<'EOF'
server {
    listen 80;
    server_name <V2_DOMAIN>;

    root /opt/quant_station_compare/frontend/dist;
    index index.html;
    client_max_body_size 10M;
    server_tokens off;

    location /api/ {
        proxy_pass http://127.0.0.1:8004;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }

    location = /health {
        proxy_pass http://127.0.0.1:8004;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/quant-station-v2 /etc/nginx/sites-enabled/quant-station-v2
sudo nginx -t
```
**Expect:** `syntax is ok` and `test is successful`. Only then:
```bash
sudo systemctl reload nginx
```

**8.2 The HTTPS certificate,** for the new name only:
```bash
sudo certbot --nginx -d <V2_DOMAIN>
sudo nginx -t && sudo systemctl reload nginx
curl -s https://<V2_DOMAIN>/health; echo
curl -s -o /dev/null -w "live site: %{http_code}\n" https://autopilot.thefinanceengine.com/
```
**Expect:** Version 2's health says `"instance":"Version 2"`, and the live site still answers 200.

**8.3 Optional:** hide the test site behind a browser password with nginx `auth_basic`, so only the two of you can reach its login page.

---

## Phase 9. Version 2's monitor

```bash
sudo tee /etc/systemd/system/quant-station-v2-monitor.service >/dev/null <<'EOF'
[Unit]
Description=AI Quant Station Version 2 health monitor
After=network.target quant-station-v2.service

[Service]
Type=oneshot
User=quantv2
Group=quantv2
WorkingDirectory=/opt/quant_station_compare/backend
EnvironmentFile=/opt/quant_station_compare/backend/.env
StateDirectory=quant-station-v2-monitor
Environment="MONITOR_STATE_FILE=/var/lib/quant-station-v2-monitor/state.json"
Environment="MONITOR_BACKEND_URL=http://127.0.0.1:8004"
Environment="MONITOR_SERVICE_NAME=quant-station-v2"
ExecStart=/opt/quant_station_compare/backend/.venv/bin/python /opt/quant_station_compare/backend/scripts/monitor.py
EOF
sudo tee /etc/systemd/system/quant-station-v2-monitor.timer >/dev/null <<'EOF'
[Unit]
Description=AI Quant Station Version 2 monitor, every 5 minutes

[Timer]
OnBootSec=90
OnUnitActiveSec=300
AccuracySec=30

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl start quant-station-v2-monitor.service
sudo journalctl -u quant-station-v2-monitor -n 5 --no-pager
sudo systemctl enable --now quant-station-v2-monitor.timer
```
**Expect:** `service=ok backend=ok connector=ok`.

---

## Phase 10. Checks in the browser

Open `https://<V2_DOMAIN>` and log in as `admin` with Version 2's own password.

1. The sidebar says **Version 2**.
2. Dashboard shows the **demo** account balance.
3. Settings, Risk Limits: the limits, Today, the kill switch, Recent alerts.
4. Terminal: a 0.01 BUY on XAUUSD **with** a stop loss is sent. One **without** a stop is refused, "Every order needs a stop loss". Close the trade.
5. Autopilot: start it. The cycle history fills, and the MT5 badge says Connected.
6. Kill switch: Stop all trading. The red banner appears, and a Terminal order is refused. Resume, as admin, with a reason.

**Recommended soon:** a private tunnel (WireGuard or Tailscale) between the Linux server and the Windows machine. Then set `MT5_CONNECTOR_URL` to the tunnel address, `ALLOW_REMOTE_CONNECTOR=false`, and `MT5_CONNECTOR_HOST` on Windows to the tunnel address. Until then the token travels over plain HTTP, protected only by the firewall rule.

---

## Phase 11. Running the comparison

Configure both versions' autopilots the same way, so differences come from the code:
- **Symbol:** XAUUSD on both.
- **Settings:** the same interval, AI provider and model, and selected prompts.
- **Position size:** Version 1 trades a fixed lot; Version 2 sizes from equity and the stop (Risk Limits, 1% by default). Note this when comparing profit; compare risk per trade too.

Agree a period, for example two weeks, and compare:

| What | Where |
|---|---|
| Trades, win rate, profit, drawdown | Reports page on each site; Telegram daily reports, each named by instance |
| Why cycles didn't trade | Autopilot cycle history on both |
| Orders refused and why | Version 2 only: Risk Limits, Recently refused |
| Fill vs requested price (slippage) | Version 2 trade records |
| Uptime and incidents | Telegram alerts, each named by instance |

---

## Updating Version 2 later

```bash
cd /opt/quant_station_compare
sudo -u quantv2 git pull --ff-only
cd backend && sudo -u quantv2 env UV_PYTHON_INSTALL_DIR=/opt/quant_station_compare/.python UV_CACHE_DIR=/opt/quant_station_compare/.uv-cache uv pip install --python .venv/bin/python -r requirements.txt
cd ../frontend && sudo -u quantv2 env HOME=/opt/quant_station_compare npm ci && sudo -u quantv2 env HOME=/opt/quant_station_compare npm run build
sudo systemctl restart quant-station-v2
```
Database changes apply by themselves at start.

## Removing Version 2 completely

Version 1 is untouched throughout.
```bash
sudo systemctl disable --now quant-station-v2-monitor.timer quant-station-v2
sudo rm /etc/systemd/system/quant-station-v2.service /etc/systemd/system/quant-station-v2-monitor.*
sudo systemctl daemon-reload
sudo rm /etc/nginx/sites-enabled/quant-station-v2 && sudo nginx -t && sudo systemctl reload nginx
# Only if the data is no longer wanted:
# sudo -u postgres psql -c "DROP DATABASE finance_engine_v2" -c "DROP ROLE quant_v2"
```
On Windows: close the Version 2 connector window, delete the firewall rule, and close the second terminal.
