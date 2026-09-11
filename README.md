# mytool — Self-Running, Self-Updating, Self-Healing Terminal AI Agent

Ek terminal AI tool jo **natural language task** leta hai, LLM (Ollama — local
aur free) se baat karke shell commands chalata hai, **khud update** hota hai
(GitHub se), aur **apne bugs khud fix** karta hai (LLM se patch lekar, verify
karke, fail hone par rollback ke saath).

```
┌────────────────────────────────────────────────────────────────────┐
│  you:  mytool do-task "organize ~/Downloads by file type"          │
└──────────────┬─────────────────────────────────────────────────────┘
               ▼
        ┌─────────────┐  JSON plan   ┌────────────────┐
        │  BRAIN      │─────────────▶│  RUNNER        │
        │  (Ollama    │◀─────────────│  safety check  │
        │   LLM API)  │  output      │  bash execute  │
        └─────────────┘  feedback    └───────┬────────┘
                                             ▼
        ┌────────────────────────────────────────────────────┐
        │  DATA: ~/.my_ai_tool/                              │
        │  database.db · config.json · logs/ · backups/      │
        └──────────────┬─────────────────────────────────────┘
                       ▼  har 15 min (cron / daemon / systemd timer)
        ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
        │ UPDATER     │   │ HEALER       │   │ QUEUED TASKS     │
        │ git pull    │   │ crash->LLM   │   │ background exec  │
        │ + restart   │   │ patch+verify │   │                  │
        └─────────────┘   └──────────────┘   └──────────────────┘
```

---

## 1. Data Save & Automatic Run (Base Setup)

- **Persistent Storage:** Data **kabhi code folder mein nahi** jata.
  - Linux/Mac: `~/.my_ai_tool/` → `database.db`, `config.json`, `logs/`, `backups/`, `reports/`, `crashes/`
  - Windows: `%APPDATA%/my_ai_tool/` (code cross-platform hai)
  - Code delete/update hone par bhi data 100% safe rehta hai.
- **Auto-Run:** `install.sh` teen options deta hai:
  - **Cron job (default):** har X minute mein `mytool cron-tick` background mein
  - **Systemd timer:** `./install.sh --systemd`
  - **Always-on daemon:** `./install.sh --daemon-service` (ya manually `mytool daemon`)

## 2. Terminal AI Tool (The Brain)

`brain.py` LLM API se direct baat karta hai (default **Ollama** `localhost:11434`,
koi API key nahi chahiye; `openai` provider bhi built-in hai).

```bash
mytool do-task "check disk usage and write ~/report.txt"   # interactive (confirm mode)
mytool do-task "backup ~/Documents" --queue                # background (cron/daemon chalega)
mytool do-task "..." -y                                    # bina poochhe execute
mytool tasks && mytool results 1                           # history dekho
```

Runner ka loop: prompt → LLM JSON plan `{"commands": [...]}` → safety check →
execute → output wapas LLM ko → max 6 rounds → result DB mein.
**Safety policy:** `rm -rf /`, fork bomb, `mkfs`, `dd of=/dev/`, shutdown,
`curl | sh` jaise commands **auto mode mein bhi refuse** hote hain.

## 3. Self-Update & Build System

`updater.py` ka `check_updates()`:

1. `git fetch origin main` → compare commits
2. Naye commits mile toh `git pull --rebase --autostash`
   (local self-heal commits preserve hote hain)
3. Rebase conflict → **abort**, purana code safe rehta hai (tool kabhi brick nahi hota)
4. Daemon `os.execv` se **khud restart** hota hai naye code ke saath

```bash
mytool update              # abhi update karo
mytool update --check-only # sirf check
```

Update ka source `config` mein hai: `update.repo_url` (default: is GitHub repo
`luffy45k/RDP`) aur `update.branch`. Interval: `update.check_interval_min`.

## 4. Auto Bug-Fix (Self-Healing Logic) — process is tarah kaam karta hai:

1. **CATCH** — har command crash-net se wrapped hai; exception ka
   type/traceback/last-logs capture hote hain.
2. **LOG** — crash `~/.my_ai_tool/database.db` (status=`open`) + markdown
   report `~/.my_ai_tool/crashes/crash-*.md` mein save hota hai. Kuch bhi lost nahi.
3. **NEXT-RUN** — agla `cron-tick`/daemon tick open crashes uthata hai
   (ya turant: `mytool heal`).
4. **DIAGNOSE** — traceback + suspect file ka poora code LLM ko jata hai;
   LLM `{"analysis", "file_to_fix", "full_corrected_code"}` return karta hai.
5. **APPLY** — original file `~/.my_ai_tool/backups/` mein backup, phir patch write.
6. **VERIFY** — `py_compile` + file ka `--heal-verify` self-check + core
   selftest — **teeno pass hone chahiye**.
7. **COMMIT / ROLLBACK** — pass → git autocommit `self-heal(crash-N)` + daemon
   restart; **fail → instant rollback** from backup, crash open rehta hai.
8. **REPORT-ONLY** — `mytool config set heal.mode report` → sirf analysis
   report banti hai, code touch nahi hota.
9. **GUARDS** — max 3 attempts per crash, sirf repo ke andar `.py` files,
   path-escape protection.

### Demo (2 minute):

```bash
mytool config set provider mock     # bina Ollama ke bhi pipeline chalega
mytool crash-test --demo            # ZeroDivisionError -> crash -> auto-fix
mytool crashes                      # crash #1 [fixed] dekho
git checkout examples/broken.py     # bug wapas lao, dobara demo karo
```

Real Ollama ke saath: `mytool config set provider ollama` — koi bhi Python
bug traceback ke saath fix hoga.

---

## Install (Linux)

```bash
git clone https://github.com/luffy45k/RDP.git ~/my_ai_tool_app   # ya is clone mein:
cd ~/my_ai_tool_app
./install.sh                  # cron default, har 15 min
MYTOOL_INTERVAL_MIN=30 ./install.sh --interval 30
./install.sh --systemd        # systemd timer (cron na ho toh)
./install.sh --daemon-service # hamesha chalta rehne wala daemon
```

Installer kya karta hai: code git repo mein install → `~/.my_ai_tool/venv`
(isolated python, **zero pip dependencies** — pure stdlib) → `~/.local/bin/mytool`
symlink → cron/timer → Ollama check.

Requirements: `python3`, `git`, aur AI ke liye [Ollama](https://ollama.com):

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2
mytool config set provider ollama
```

## Commands

| Command | Kaam |
|---|---|
| `mytool do-task "..."` | AI se task karwao (`--queue`, `-y`) |
| `mytool tasks` / `mytool results ID` | task history / step-by-step output |
| `mytool status` | version, provider, crashes, cron sab kuch |
| `mytool heal [--id N]` | open crashes abhi fix karo |
| `mytool crashes [ID]` | crash list / detail |
| `mytool update [--check-only]` | GitHub se code update + restart |
| `mytool daemon --interval 15` | always-on background mode |
| `mytool cron-tick` | ek background cycle (cron yehi call karta hai) |
| `mytool selftest [--core]` | health check (core = LLM skip) |
| `mytool config list/get/set` | config (`heal.mode`, `provider`, ...) |
| `mytool init` | data dir + config + db banao |

## Important Config Keys

```bash
mytool config set provider ollama          # ollama | openai | mock
mytool config set ollama.model llama3.2    # khali = auto-detect
mytool config set runner.confirm false     # interactive confirm band
mytool config set runner.background_auto true
mytool config set heal.mode report         # apply | report
mytool config set heal.auto false          # crash par auto-heal band
mytool config set update.auto false        # auto-update band
mytool config set schedule.interval_min 15
```

## Windows note

Code cross-platform hai (data `%APPDATA%/my_ai_tool/` mein jayega). Auto-run
ke liye Task Scheduler:

```
schtasks /create /tn "mytool" /tr "C:\path\to\python -m my_ai_tool cron-tick" /sc minute /mo 15
```

## Security notes (padho zaroor)

- `do-task` LLM-generated commands chalata hai. `runner.confirm true`
  (default) mein har command dikhti hai aur poochti hai; `-y`/background
  mode mein **safety blocklist** hi guard hai (best-effort — koi blocklist
  perfect nahi hoti).
- Self-heal sirf repo ke `.py` files ko touch karta hai, har patch se pehle
  backup + verify + rollback built-in hai.
- Auto-update (`update.auto`) band karna ho toh config se band karo.
- Har fix `self-heal(crash-N)` message ke saath git commit hota hai —
  `git log` mein sab transparent hai.
