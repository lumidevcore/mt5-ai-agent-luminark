# MT5 AI Agent Luminark

Sistem trading AI multi-agent lokal untuk MetaTrader 5 (MT5) menggunakan Ollama, pemilihan timeframe dinamis, adaptive risk management, market intelligence, learning memory, dan beberapa lapisan pengaman eksekusi.

> **Peringatan:** Selalu uji project ini menggunakan akun MT5 **demo terlebih dahulu** sebelum mempertimbangkan penggunaan uang asli.

## Pilih Bahasa / Choose Language

- 🇮🇩 [Bahasa Indonesia](#-bahasa-indonesia)
- 🇬🇧 [English](#-english)

---

# 🇮🇩 Bahasa Indonesia

## Fitur Utama

- Koneksi MT5 dan pencarian simbol
- Dynamic timeframe: `M1`, `M5`, `M15`, `M30`, `H1`, `H4`
- Mode AUTO dengan adaptasi Scalping / Intraday / Swing
- Local AI Council: Scout, Technical, Critic, Chief
- Council consensus, calibration, smart routing, circuit breaker, dan fast adjudication
- Analisis teknikal, market structure, session intelligence, Fibonacci
- Market context dari ZPI / TradingView / Binance
- Dynamic risk dan expectancy-based learning
- Adaptasi market regime
- Drawdown dan exposure controller
- Portfolio dan rolling-correlation guard
- Multi-entry dengan MarginGuard
- Dynamic SL/TP, break-even, profit lock, ATR/structure trailing
- Partial close / scale-out
- Paper / Shadow mode
- Historical Replay / Backtest
- Live Readiness Guard

---

## Persyaratan

Sebelum menjalankan project, instal:

1. **Windows 10 / 11**
2. **Python 3.10+**
3. **Git**
4. **MetaTrader 5 Desktop**
5. **Ollama**
6. Akun MT5 — gunakan akun demo untuk pengujian awal
7. Opsional: ZPI API key

MT5 harus tetap terbuka selama bot terhubung.

---

## Cara Clone Repository

Buka CMD, PowerShell, Git Bash, atau Windows Terminal:

```bash
git clone https://github.com/lumidevcore/mt5-ai-agent-luminark.git
cd mt5-ai-agent-luminark
```

Setelah itu lanjutkan ke bagian instalasi.

---

## Cara Fork Repository

Kalau ingin memiliki salinan repository sendiri untuk dikembangkan:

1. Buka repository ini di GitHub.
2. Klik **Fork**.
3. Pilih akun GitHub kamu.
4. Clone repository hasil fork:

```bash
git clone https://github.com/YOUR_USERNAME/mt5-ai-agent-luminark.git
cd mt5-ai-agent-luminark
```

Ganti `YOUR_USERNAME` dengan username GitHub kamu.

Tambahkan repository asli sebagai `upstream`:

```bash
git remote add upstream https://github.com/lumidevcore/mt5-ai-agent-luminark.git
git remote -v
```

Contoh hasil:

```text
origin    https://github.com/YOUR_USERNAME/mt5-ai-agent-luminark.git
upstream  https://github.com/lumidevcore/mt5-ai-agent-luminark.git
```

---

## Instalasi

### 1. Buat Virtual Environment

```bash
python -m venv .venv
```

Aktifkan di Windows CMD:

```cmd
.venv\Scripts\activate
```

PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### 2. Instal Dependency

```bash
pip install -r requirements.txt
```

Repository juga menyediakan:

```text
setup.bat
```

untuk membantu proses setup Windows yang tersedia pada project.

---

## Konfigurasi `.env`

File `.env` asli sengaja **tidak disimpan di GitHub** karena dapat berisi konfigurasi privat.

Buat `.env` dari template.

CMD:

```cmd
copy .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

Kemudian buka `.env` dan isi konfigurasi milikmu sendiri.

Contoh:

```env
ZPI_API_KEY=
OLLAMA_URL=http://127.0.0.1:11434
```

Gunakan nama variabel yang benar-benar tersedia pada `.env.example` versi terbaru.

> Jangan pernah commit `.env`, API key, password, token, atau kredensial broker ke GitHub.

---

## Instal Model Ollama

Periksa Ollama:

```bash
ollama list
```

Instal model ringan default:

```bash
ollama pull deepseek-r1:1.5b
```

Model tambahan AI Council dikonfigurasi melalui:

```text
ai_model.json
ai_council.json
```

Pull model sesuai nama yang tercantum pada konfigurasi:

```bash
ollama pull MODEL_NAME
```

Model lokal besar dapat berjalan lambat pada komputer dengan RAM, CPU, atau GPU terbatas.

---

## Setup MetaTrader 5

1. Buka MetaTrader 5 Desktop.
2. Login ke akun MT5.
3. Untuk pengujian awal, gunakan **akun demo**.
4. Aktifkan izin algorithmic / Expert Advisor trading.
5. Biarkan MT5 tetap terbuka.

Tergantung versi MT5:

```text
Tools
→ Options
→ Expert Advisors
```

Bot akan terhubung ke akun yang sedang aktif pada terminal MT5 lokal.

---

## Menjalankan Project

Jalankan:

```cmd
start.bat
```

atau secara manual:

```bash
python main.py
```

Jika berhasil, log kurang lebih akan menampilkan:

```text
Ollama OK: ...
MT5 connected: ...
Symbols loaded: ...
```

---

## Urutan Penggunaan Pertama yang Disarankan

```text
1. Buka MT5
2. Login ke akun DEMO
3. Jalankan Ollama
4. Jalankan MT5 AI Agent
5. Connect MT5
6. Scan simbol yang tersedia
7. Pilih simbol
8. Aktifkan AI Council jika diperlukan
9. Pilih mode trading AUTO
10. Mulai trading
11. Pantau Activity / Learning Log
```

---

## AUTO Trading Mode

AUTO dapat memilih timeframe secara dinamis:

```text
M1
M5
M15
M30
H1
H4
```

dan menyesuaikan gaya trading menjadi:

```text
SCALPING
INTRADAY
SWING
```

Contoh:

```text
AUTO TF SWITCH: M15 -> M5
effective mode=SCALPING
```

atau:

```text
AUTO TF SWITCH: M15 -> H4
effective mode=SWING
```

---

## AI Council

AI Council lokal menggunakan beberapa AI dengan peran berbeda:

```text
Market Data
   ↓
SCOUT
   ↓
TECHNICAL
   ↓
CRITIC
   ↓
CHIEF
   ↓
Final AI Decision
   ↓
Risk / Regime / Correlation / Margin Guards
   ↓
Order
```

Council dapat berhenti lebih awal apabila setup lemah, Technical tidak mengonfirmasi, Critic memberikan rejection valid, model timeout, circuit breaker terbuka, atau fast adjudication tidak dapat mengonfirmasi setup.

Valid rejection dari Critic tetap diperlakukan sebagai safety veto.

---

## ZPI Market Intelligence

Jika dikonfigurasi, ZPI dapat menyediakan:

- TradingView technicals
- TradingView news
- economic calendar
- Binance ticker
- Binance depth / order book
- Binance klines
- Fear & Greed

Jika satu endpoint bermasalah, endpoint tersebut dapat diisolasi sementara sehingga endpoint sehat lainnya masih dapat digunakan. Bot juga dapat memakai cache atau menandai endpoint sebagai degraded.

---

## Risk & Safety

Sinyal BUY atau SELL **tidak otomatis berarti order akan dikirim**.

Project memiliki beberapa lapisan pengaman:

```text
AI Council
Consensus / Calibration
Expectancy
Market Regime Adaptation
Account Stress / Drawdown
Correlation Guard
Real Rolling Correlation
MarginGuard
Spread Guard
Live Readiness
Broker Preflight
```

Guard yang bersifat authoritative dapat memblokir entry.

---

## Dynamic Position Management

Posisi terbuka dapat dikelola menggunakan:

- broker-side SL / TP
- break-even
- profit lock
- ATR trailing
- market-structure trailing
- selective TP extension
- partial close / scale-out

Broker-side SL/TP penting karena masih dapat melindungi posisi jika aplikasi Python berhenti.

---

## Paper / Shadow Mode

Paper / Shadow mode digunakan untuk mengevaluasi setup **tanpa mengirim order ke MT5**.

Sistem dapat mencatat simulasi:

- Entry
- SL
- TP
- RR
- timeframe
- regime
- structure
- result

Hasil Shadow dipisahkan dari data pembelajaran trade MT5 sebenarnya.

---

## Historical Replay / Backtest

Project memiliki historical replay engine:

```text
FAST
→ deterministic historical replay
→ tidak memerlukan panggilan Ollama

FULL_AI
→ mendukung AI decision callback
→ lebih lambat
```

Replay dapat menghasilkan statistik seperti trades, win rate, profit factor, expectancy dalam R, return, max drawdown, longest losing streak, performa per regime, dan equity curve.

Hasil historis tidak menjamin performa di masa depan.

---

## Stop Controls

### STOP SAFE

Menonaktifkan entry baru, tetapi posisi yang sudah terbuka tetap berjalan dan broker-side SL/TP tetap aktif.

```text
NEW ENTRY = DISABLED
EXISTING POSITIONS = REMAIN OPEN
SL / TP = REMAIN ACTIVE
```

### CLOSE ALL & STOP

Mencoba menutup posisi bot dan menghentikan eksekusi.

Selalu periksa terminal MT5 setelah menggunakan fungsi emergency close.

---

## Update Repository Hasil Clone

Jika clone langsung dari repository asli:

```bash
git pull origin main
```

Jika ada perubahan lokal, commit atau stash terlebih dahulu.

---

## Update Repository Hasil Fork

Jika `upstream` sudah dikonfigurasi:

```bash
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

Alternatif menggunakan rebase:

```bash
git fetch upstream
git checkout main
git rebase upstream/main
git push origin main
```

---

## Workflow Pengembangan untuk Fork

Buat branch baru:

```bash
git checkout -b feature/my-feature
```

Setelah melakukan perubahan:

```bash
git add .
git commit -m "Add my feature"
git push -u origin feature/my-feature
```

Kemudian buat Pull Request di GitHub.

---

## File yang Jangan Di-commit

Jaga file lokal/private agar tidak masuk Git:

```text
.env
.venv/
venv/
__pycache__/
*.pyc
*.db
*.sqlite
*.sqlite3
*.log
local runtime data
AI Council local metrics
API keys
MT5 credentials
tokens
passwords
```

Sebelum push selalu cek:

```bash
git status
```

Kalau `.env` muncul di staged files, **jangan push**.

---

## Troubleshooting

### `ModuleNotFoundError`

Aktifkan virtual environment lalu:

```bash
pip install -r requirements.txt
```

### MT5 Tidak Bisa Connect

Pastikan MT5 Desktop berjalan, akun sudah login, izin algorithmic trading aktif, dan package Python `MetaTrader5` dapat mendeteksi terminal lokal.

### Ollama Bermasalah

Periksa:

```bash
ollama list
```

Alamat lokal yang umum digunakan:

```text
http://127.0.0.1:11434
```

### AI Council Timeout

Model lokal besar dapat lambat. Project memiliki timeout handling, smart routing, circuit breaker, dan fail-safe HOLD.

### ZPI Timeout / HTTP 503

Endpoint ZPI yang bermasalah dapat diisolasi sementara sementara endpoint sehat tetap digunakan.

---

## Security

Jangan pernah mempublikasikan:

- `.env`
- API key
- password MT5
- kredensial broker
- recovery code
- private token

Jika secret tidak sengaja ter-push ke GitHub, menghapus file yang terlihat saja tidak cukup karena secret dapat tetap tersimpan di Git history.

Segera revoke atau rotate credential tersebut.

---

## Disclaimer

Project ini merupakan software eksperimental untuk penelitian, pengembangan, dan pembelajaran.

Trading memiliki risiko finansial. Sistem otomatis dapat gagal akibat bug software, kesalahan konfigurasi, kesalahan model, latency, perilaku broker, API outage, market gap, volatilitas tidak terduga, dan kondisi lainnya.

**Tidak ada jaminan profit.**

Lakukan pengujian secara menyeluruh menggunakan akun MT5 demo sebelum mempertimbangkan penggunaan uang asli.

Pengguna bertanggung jawab untuk memeriksa source code, konfigurasi, aturan broker, risk setting, dan trade yang dihasilkan.

---

# 🇬🇧 English

## Main Features

- MT5 connection and symbol discovery
- Dynamic timeframe: `M1`, `M5`, `M15`, `M30`, `H1`, `H4`
- AUTO mode with Scalping / Intraday / Swing adaptation
- Local AI Council: Scout, Technical, Critic, Chief
- Council consensus, calibration, smart routing, circuit breaker, fast adjudication
- Technical analysis, market structure, session intelligence, Fibonacci
- ZPI / TradingView / Binance market context
- Dynamic risk and expectancy-based learning
- Market-regime adaptation
- Drawdown and exposure controller
- Portfolio and rolling-correlation guards
- Multi-entry with MarginGuard
- Dynamic SL/TP, break-even, profit lock, ATR/structure trailing
- Partial close / scale-out
- Paper / Shadow mode
- Historical Replay / Backtest
- Live Readiness Guard

---

# Requirements

Install these before running the project:

1. **Windows 10 / 11**
2. **Python 3.10+**
3. **Git**
4. **MetaTrader 5 Desktop**
5. **Ollama**
6. An MT5 account — use a demo account for first testing
7. Optional: ZPI API key

MT5 must stay open while the bot is connected.

---

# Clone the Repository

Open CMD, PowerShell, Git Bash, or Windows Terminal:

```bash
git clone https://github.com/lumidevcore/mt5-ai-agent-luminark.git
cd mt5-ai-agent-luminark
```

Then follow the installation steps below.

---

# Fork the Repository

If you want your own copy for development:

1. Open this repository on GitHub.
2. Click **Fork**.
3. Choose your GitHub account.
4. Clone your fork:

```bash
git clone https://github.com/YOUR_USERNAME/mt5-ai-agent-luminark.git
cd mt5-ai-agent-luminark
```

Replace `YOUR_USERNAME` with your GitHub username.

Add the original repository as `upstream`:

```bash
git remote add upstream https://github.com/lumidevcore/mt5-ai-agent-luminark.git
git remote -v
```

Typical result:

```text
origin    https://github.com/YOUR_USERNAME/mt5-ai-agent-luminark.git
upstream  https://github.com/lumidevcore/mt5-ai-agent-luminark.git
```

---

# Installation

## 1. Create a Virtual Environment

```bash
python -m venv .venv
```

Activate it on Windows CMD:

```cmd
.venv\Scripts\activate
```

PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

## 2. Install Dependencies

```bash
pip install -r requirements.txt
```

You can also review/use:

```text
setup.bat
```

for the Windows setup flow included in the repository.

---

# Configure `.env`

The real `.env` is intentionally **not included in GitHub**.

Create it from the template.

CMD:

```cmd
copy .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

Then open `.env` and fill in your own configuration.

Example:

```env
ZPI_API_KEY=
OLLAMA_URL=http://127.0.0.1:11434
```

Use the exact variables provided by the current `.env.example`.

> Never commit your real `.env`, API keys, passwords, tokens, or broker credentials.

---

# Install Ollama Models

Check Ollama:

```bash
ollama list
```

Install the lightweight default model:

```bash
ollama pull deepseek-r1:1.5b
```

Additional Council models are configured in:

```text
ai_model.json
ai_council.json
```

Pull the exact model names used by those files:

```bash
ollama pull MODEL_NAME
```

Large local models may run slowly on hardware with limited RAM, CPU, or GPU.

---

# MetaTrader 5 Setup

1. Open MetaTrader 5 Desktop.
2. Log in to an MT5 account.
3. Use a **demo account** for first testing.
4. Enable algorithmic / Expert Advisor trading permission.
5. Keep MT5 open.

Depending on your MT5 version:

```text
Tools
→ Options
→ Expert Advisors
```

The bot connects to the MT5 account currently active in the local terminal.

---

# Run the Project

Start with:

```cmd
start.bat
```

or manually:

```bash
python main.py
```

A successful startup should show logs similar to:

```text
Ollama OK: ...
MT5 connected: ...
Symbols loaded: ...
```

---

# Recommended First Run

```text
1. Open MT5
2. Log in to a DEMO account
3. Start Ollama
4. Run MT5 AI Agent
5. Connect MT5
6. Scan available symbols
7. Select a symbol
8. Enable AI Council if required
9. Select AUTO trading mode
10. Start trading
11. Monitor Activity / Learning Log
```

---

# AUTO Trading Mode

AUTO can dynamically choose:

```text
M1
M5
M15
M30
H1
H4
```

and switch between:

```text
SCALPING
INTRADAY
SWING
```

Example:

```text
AUTO TF SWITCH: M15 -> M5
effective mode=SCALPING
```

or:

```text
AUTO TF SWITCH: M15 -> H4
effective mode=SWING
```

---

# AI Council

The local AI Council uses several roles:

```text
Market Data
   ↓
SCOUT
   ↓
TECHNICAL
   ↓
CRITIC
   ↓
CHIEF
   ↓
Final AI Decision
   ↓
Risk / Regime / Correlation / Margin Guards
   ↓
Order
```

The Council can stop early when:

- the setup is weak,
- Technical does not confirm,
- Critic produces a valid rejection,
- a model times out,
- a model circuit breaker is open,
- fast adjudication cannot confirm the setup.

A valid Critic rejection remains a safety veto.

---

# ZPI Market Intelligence

If configured, ZPI can provide:

- TradingView technicals
- TradingView news
- economic calendar
- Binance ticker
- Binance depth / order book
- Binance klines
- Fear & Greed

A failing endpoint can be isolated while healthy endpoints continue working.

The bot may use cached data or temporarily mark an endpoint as degraded.

---

# Risk & Safety

A BUY or SELL signal does **not** automatically create an order.

The project contains multiple safety layers:

```text
AI Council
Consensus / Calibration
Expectancy
Market Regime Adaptation
Account Stress / Drawdown
Correlation Guard
Real Rolling Correlation
MarginGuard
Spread Guard
Live Readiness
Broker Preflight
```

Any authoritative guard may block an entry.

---

# Dynamic Position Management

Open positions can be managed with:

- broker-side SL / TP
- break-even
- profit lock
- ATR trailing
- market-structure trailing
- selective TP extension
- partial close / scale-out

Broker-side SL/TP remain important because they can continue protecting a position if Python stops running.

---

# Paper / Shadow Mode

Paper / Shadow mode can evaluate qualifying setups **without sending an MT5 order**.

It can record hypothetical:

- Entry
- SL
- TP
- RR
- timeframe
- regime
- structure
- result

Shadow results are stored separately from actual MT5 trade-learning data.

---

# Historical Replay / Backtest

The project contains a historical replay engine.

```text
FAST
→ deterministic historical replay
→ no Ollama call required

FULL_AI
→ supports an AI decision callback
→ slower
```

Replay can report:

- trades
- win rate
- profit factor
- expectancy in R
- return
- max drawdown
- longest losing streak
- performance by regime
- equity curve

Historical performance does not guarantee future results.

---

# Stop Controls

## STOP SAFE

Stops new entries but leaves existing positions open with their broker-side SL/TP active.

```text
NEW ENTRY = DISABLED
EXISTING POSITIONS = REMAIN OPEN
SL / TP = REMAIN ACTIVE
```

## CLOSE ALL & STOP

Attempts to close bot positions and stop execution.

Always verify the MT5 terminal after using an emergency close action.

---

# Project Structure

```text
mt5-ai-agent-luminark/
│
├── agent/
│   ├── __init__.py
│   ├── config.py
│   └── core.py
│
├── assets/
├── data/
│
├── .env.example
├── .gitignore
├── ai_council.json
├── ai_model.json
├── main.py
├── requirements.txt
├── setup.bat
├── start.bat
└── README.md
```

---

# Update a Clone

If you cloned the original repository:

```bash
git pull origin main
```

If you have local changes, commit or stash them first.

---

# Keep a Fork Updated

If `upstream` is configured:

```bash
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

Alternative with rebase:

```bash
git fetch upstream
git checkout main
git rebase upstream/main
git push origin main
```

---

# Development Workflow for Forks

Create a feature branch:

```bash
git checkout -b feature/my-feature
```

Make your changes, then:

```bash
git add .
git commit -m "Add my feature"
git push -u origin feature/my-feature
```

Then open a Pull Request on GitHub.

---

# Files You Should NOT Commit

Keep local/private files out of Git:

```text
.env
.venv/
venv/
__pycache__/
*.pyc
*.db
*.sqlite
*.sqlite3
*.log
local runtime data
AI Council local metrics
API keys
MT5 credentials
tokens
passwords
```

Before every push:

```bash
git status
```

If `.env` appears in staged files, **do not push**.

---

# Troubleshooting

## `ModuleNotFoundError`

Activate the virtual environment and reinstall:

```bash
pip install -r requirements.txt
```

## MT5 does not connect

Check:

- MetaTrader 5 Desktop is running.
- You are logged in.
- Algorithmic trading permission is enabled.
- The Python `MetaTrader5` package can detect the local terminal.

## Ollama connection problem

Check:

```bash
ollama list
```

A common local Ollama URL is:

```text
http://127.0.0.1:11434
```

## AI Council timeout

Large local models can be slow.

The project includes timeout handling, smart routing, circuit breaker, and fail-safe HOLD behavior.

## ZPI timeout / HTTP 503

A degraded ZPI endpoint can be temporarily isolated while healthy endpoints continue.

---

# Security

Never publish:

- `.env`
- API keys
- MT5 passwords
- broker credentials
- recovery codes
- private tokens

If a secret is accidentally pushed to GitHub, deleting the visible file is not enough because it can remain in Git history.

Revoke or rotate the exposed credential immediately.

---

# Disclaimer

This project is experimental software for research, development, and educational use.

Trading involves financial risk. Automated systems can fail because of software bugs, configuration mistakes, model errors, latency, broker behavior, API outages, market gaps, unexpected volatility, or other conditions.

There is **no guarantee of profitability**.

Test extensively on an MT5 demo account before considering real-money trading.

You are responsible for reviewing the code, configuration, broker rules, risk settings, and resulting trades.

---

# Repository

Original repository:

```text
https://github.com/lumidevcore/mt5-ai-agent-luminark
```

Clone:

```bash
git clone https://github.com/lumidevcore/mt5-ai-agent-luminark.git
```

Fork the repository if you want your own development copy, and use Pull Requests if you want to contribute improvements.
