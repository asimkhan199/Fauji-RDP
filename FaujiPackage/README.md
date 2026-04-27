# FaujiBot — Plug-and-Play Web UI + Installer

A turnkey wrapper around `FaujiBot.py` (the original is **untouched**, byte-identical).

- **Phone-friendly web dashboard** (HTTPS, password-protected) — Play / Pause / Stop, live equity, level, positions, logs.
- **Settings page** — edit `config.json` from the phone.
- **Single `FaujiSetup.exe`** — installs Python, MT5 (vanilla MetaQuotes), all packages, registers an auto-start Scheduled Task, opens firewall, drops a desktop icon. ~5 minutes on a fresh AWS RDP.

## Folder layout

```
FaujiPackage/
├── bot/FaujiBot.py            # original bot, untouched (copied from D:\UNIVERSAL\FaujiBot.py)
├── supervisor/                # the wrapper (FastAPI + bot lifecycle)
│   ├── main.py                # entry: starts uvicorn (HTTPS on 8443)
│   ├── api.py                 # routes
│   ├── bot_manager.py         # imports MartingaleBot, runs main_loop in a thread
│   ├── config_store.py        # data/config.json with pydantic validation
│   ├── auth.py                # bcrypt password + JWT cookie
│   ├── cert_gen.py            # self-signed HTTPS cert
│   ├── aws.py                 # EC2 public-IP detection
│   ├── paths.py               # install-root resolver
│   └── ui/                    # mobile-first dashboard, login, wizard, settings
├── installer/
│   ├── build.ps1              # downloads Python + MT5 + wheels, compiles Inno Setup
│   ├── fauji.iss              # Inno Setup script → FaujiSetup.exe
│   ├── FaujiBot.cmd           # launcher used by Scheduled Task
│   └── FaujiOpenMT5.cmd
├── data/                      # runtime state (created on first run)
├── requirements.txt
├── run_dev.bat                # local dev (your own Windows, MT5 already installed)
└── README.md
```

## Local dev

You already have MT5 installed and logged in. From `D:\UNIVERSAL\FaujiPackage\`:

```bat
run_dev.bat
```

Open https://localhost:8443 — accept the self-signed warning — first-run wizard:
1. Set a dashboard password
2. Enter `magic_number`, symbol, lot sizes
3. Confirm MT5 is logged in → **Start bot**

## Building `FaujiSetup.exe`

Prereq: install **Inno Setup 6** (https://jrsoftware.org/isinfo.php). One-time.

From `D:\UNIVERSAL\FaujiPackage\`:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

`build.ps1` will:
1. Download embeddable Python 3.12 → `installer\python\`
2. Download MetaQuotes MT5 installer → `installer\mt5setup.exe`
3. Download all wheels (offline) → `installer\wheels\`
4. Compile `installer\fauji.iss` with ISCC

Output: `installer\Output\FaujiSetup.exe` (~250 MB).

## Deploying to an AWS Windows RDP

1. Copy `FaujiSetup.exe` to the RDP (RDP file-paste or S3).
2. Right-click → **Run as administrator**. ~5 min: silent MT5 install, deps install, Scheduled Task created, firewall rule added.
3. Browser auto-opens https://localhost:8443. Complete the 3-step wizard.
4. **AWS Console → EC2 → Security Groups → Inbound rules** → add `Custom TCP, 8443, source = your phone IP/32`. (Skip this if you only use it from the RDP itself.)
5. Phone URL appears in the banner on the dashboard. Bookmark it.

## How control works

The supervisor imports `MartingaleBot` from `bot/FaujiBot.py` and runs `bot.main_loop()` in a daemon thread.

- **Play** — instantiates the bot, calls `bot.start()` then `bot.main_loop()`.
- **Pause** — sets `bot.is_running = False` (loop exits cleanly; positions stay open at the broker).
- **Stop** — same flag, plus drops the bot instance.

State shown on the dashboard is read directly from the bot's own attributes (`bot_peak_equity`, `market_behavior`, `hedge_state`, etc.) and from `MetaTrader5.account_info()` / `positions_get()` — no IPC, no polling files.

The bot's hedge JSON state file (`bot-{symbol}-{magic}-{code}-hedges.json`) is written to `data/` because the supervisor `chdir()`s there before importing — no path code in `FaujiBot.py` had to change.

## Things that intentionally aren't done

- ❌ `FaujiBot.py` is **never modified**. Even the commented-out `_load_hedges()` call at line 109 stays as the user wrote it; the supervisor calls the method itself after `bot.start()` (the method is still defined at line 1856 and safe to call).
- ❌ Broker login is **not** automated — brokers block credential automation. The wizard tells you to log into MT5 once.
- ❌ No auto Let's Encrypt; HTTPS uses a self-signed cert (red warning the first time you visit on the phone — accept once).

## Operational notes

- **Memory:** comfortably fits in 4 GB RAM (MT5 ~250 MB + supervisor ~120 MB + Windows).
- **Logs:** `data\supervisor.log` (FastAPI), `data\bot.log` (everything the bot prints). Last 200 lines streamed to the dashboard.
- **Restart safety:** Scheduled Task launches the supervisor on every login/reboot; supervisor calls `bot._load_hedges()` after start, so hedge baskets survive RDP reboots.
- **Updating the bot:** drop a new `FaujiBot.py` into `C:\FaujiBot\bot\` and click Stop → Play. No reinstall needed.
- **Uninstall:** Control Panel → Apps → FaujiBot → Uninstall. Removes the Scheduled Task and firewall rule. `data\` is preserved.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `RuntimeError: No config.json` on Play | Re-open the wizard at `/setup` and re-enter magic number. |
| Bot crashes immediately with `mt5.initialize() failed` | MT5 isn't running or not logged in. Open MT5 from the Start menu, log in, then click Play. |
| Phone gets `ERR_CONNECTION_TIMED_OUT` | AWS Security Group hasn't allowed port 8443 from your phone IP yet. |
| Phone shows scary red warning | Self-signed cert. Tap **Advanced → Proceed**. One-time per device. |
| `_load_hedges` log warning | Non-fatal. Means there's no prior hedge state file, or its format is unexpected. The bot will create a fresh one. |
