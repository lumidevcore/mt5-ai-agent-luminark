import threading
import time
import json
import tkinter as tk
import webbrowser
import MetaTrader5 as mt5
from pathlib import Path
from tkinter import ttk, messagebox
from queue import Queue, Empty
import re
import subprocess

from agent.config import Settings
from agent.core import (
    MT5Client, Memory, LocalLLM, RiskGuard, TradingEngine,
    classify_symbol, trade_mode_name, snapshot, technical_score
)

MODEL_CATALOG = [
    {"label":"DeepSeek-R1 1.5B • lightweight","model":"deepseek-r1:1.5b","kind":"Official/library"},
    {"label":"Qwen3.5 4B • recommended light","model":"qwen3.5:4b","kind":"Official Qwen"},
    {
        "label":"Qwen3.5 Claude-distill 4B • community",
        "model":"kwangsuklee/Qwen3.5-4B.Q4_K_M-Claude-4.6-Opus-Reasoning-Distilled-v2",
        "kind":"Community Qwen distill"
    },
    {
        "label":"Qwen3.5 Claude-distill 9B • community / strongest",
        "model":"kwangsuklee/Qwen3.5-9B.Q4_K_M-Claude-4.6-Opus-Reasoning-Distilled-v2",
        "kind":"Community Qwen distill"
    },
]
MODEL_BY_LABEL={x["label"]:x for x in MODEL_CATALOG}
MODEL_LABEL_BY_NAME={x["model"]:x["label"] for x in MODEL_CATALOG}

AI_COUNCIL_MODELS={
    "SCOUT":"deepseek-r1:1.5b",
    "TECHNICAL":"qwen3.5:4b",
    "CRITIC":"kwangsuklee/Qwen3.5-4B.Q4_K_M-Claude-4.6-Opus-Reasoning-Distilled-v2",
    "CHIEF":"kwangsuklee/Qwen3.5-9B.Q4_K_M-Claude-4.6-Opus-Reasoning-Distilled-v2",
}
AI_COUNCIL_LABELS={"SCOUT":"DeepSeek-R1 1.5B","TECHNICAL":"Qwen3.5 4B","CRITIC":"Claude-distill 4B","CHIEF":"Claude-distill 9B"}

s=Settings()
_model_pref_path=Path(__file__).resolve().parent / "ai_model.json"
_council_pref_path=Path(__file__).resolve().parent / "ai_council.json"
# Council is session-scoped: every fresh launch starts OFF.
s.ai_council_enabled=False
try:
    if _model_pref_path.exists():
        _pref=json.loads(_model_pref_path.read_text(encoding="utf-8"))
        _saved=str(_pref.get("ollama_model","") or "").strip()
        if _saved:
            s.ollama_model=_saved
except Exception:
    pass

mt=MT5Client(s)
mem=Memory()
llm=LocalLLM(s)
risk=RiskGuard(s,mt)
q=Queue()

def log(x): q.put(("log",x))
def state(x): q.put(("state",x))
engine=TradingEngine(s,mt,llm,mem,risk,log,state)

_ui_connected={"value":False}

app=tk.Tk()
app.title("MT5 AI Agent V3.10.28")
app.geometry("1380x860")
app.minsize(980, 620)

# ---------- Theme ----------
COLORS={
    "bg":"#0A0F1C",
    "panel":"#121A2A",
    "panel2":"#1A2438",
    "border":"#2A3852",
    "text":"#EEF3FA",
    "muted":"#9BA9BE",
    "accent":"#5EEAD4",
    "accent2":"#60A5FA",
    "success":"#22C55E",
    "warning":"#F59E0B",
    "danger":"#EF4444",
    "danger_dark":"#991B1B",
    "purple":"#A78BFA",
    "cyan":"#5EEAD4",
}
app.configure(bg=COLORS["bg"])

style=ttk.Style()
try:
    style.theme_use("clam")
except Exception:
    pass

style.configure(".",font=("Segoe UI",10))
style.configure("TFrame",background=COLORS["panel"])
style.configure("Panel.TFrame",background=COLORS["panel"])
style.configure("Header.TFrame",background=COLORS["bg"])
style.configure("TLabel",background=COLORS["panel"],foreground=COLORS["text"])
style.configure("Panel.TLabel",background=COLORS["panel"],foreground=COLORS["text"])
style.configure("Title.TLabel",background=COLORS["bg"],foreground="#F8FAFC",font=("Segoe UI Semibold",20))
style.configure("Sub.TLabel",background=COLORS["bg"],foreground=COLORS["muted"],font=("Segoe UI",9))
style.configure("CardTitle.TLabel",background=COLORS["bg"],foreground=COLORS["muted"],font=("Segoe UI Semibold",9))
style.configure(
    "Status.TLabel",
    background=COLORS["panel2"],foreground=COLORS["cyan"],
    font=("Segoe UI Semibold",9),padding=(7,3)
)
style.configure("Metric.TLabel",background=COLORS["panel"],foreground="#F8FAFC",font=("Segoe UI Semibold",14))
style.configure("BigMetric.TLabel",background=COLORS["panel"],foreground=COLORS["accent"],font=("Segoe UI Semibold",18))

style.configure(
    "TLabelframe",
    background=COLORS["panel"],
    bordercolor=COLORS["border"],
    lightcolor=COLORS["border"],
    darkcolor=COLORS["border"],
    relief="solid",
    borderwidth=1
)
style.configure(
    "TLabelframe.Label",
    background=COLORS["panel"],
    foreground=COLORS["accent"],
    font=("Segoe UI Semibold",10)
)

style.configure(
    "TEntry",fieldbackground=COLORS["panel2"],foreground=COLORS["text"],
    bordercolor=COLORS["border"],insertcolor=COLORS["text"],padding=6
)
style.configure(
    "TCombobox",fieldbackground=COLORS["panel2"],background=COLORS["panel2"],
    foreground=COLORS["text"],arrowcolor=COLORS["accent"],padding=5
)
style.map(
    "TCombobox",
    fieldbackground=[("readonly",COLORS["panel2"])],
    foreground=[("readonly",COLORS["text"])],
    selectbackground=[("readonly",COLORS["panel2"])],
    selectforeground=[("readonly",COLORS["text"])]
)
style.configure(
    "TCheckbutton",background=COLORS["panel"],foreground=COLORS["text"]
)
style.map("TCheckbutton",background=[("active",COLORS["panel"])])

style.configure(
    "TButton",background=COLORS["panel2"],foreground=COLORS["text"],
    borderwidth=0,padding=(10,7),font=("Segoe UI Semibold",9)
)
style.map(
    "TButton",
    background=[("active","#22304A"),("disabled","#131B2B")],
    foreground=[("disabled","#53627A")]
)
style.configure("Accent.TButton",background=COLORS["accent2"],foreground="white",padding=(12,8),font=("Segoe UI Semibold",10))
style.map("Accent.TButton",background=[("active","#3B82F6")])
style.configure("Success.TButton",background=COLORS["success"],foreground="#06130A",padding=(12,8),font=("Segoe UI Semibold",10))
style.map("Success.TButton",background=[("active","#16A34A")])
style.configure("Danger.TButton",background=COLORS["danger"],foreground="white",padding=(12,8),font=("Segoe UI Semibold",10))
style.map("Danger.TButton",background=[("active","#DC2626")])
style.configure("DarkDanger.TButton",background=COLORS["danger_dark"],foreground="white",padding=(12,8),font=("Segoe UI Semibold",10))
style.map("DarkDanger.TButton",background=[("active","#7F1D1D")])
style.configure("Secondary.TButton",background="#334155",foreground="white",padding=(12,8),font=("Segoe UI Semibold",10))
style.map("Secondary.TButton",background=[("active","#475569")])

style.configure(
    "Treeview",background=COLORS["panel"],fieldbackground=COLORS["panel"],
    foreground=COLORS["text"],rowheight=27,bordercolor=COLORS["border"],borderwidth=0
)
style.configure(
    "Treeview.Heading",background=COLORS["panel2"],foreground=COLORS["muted"],
    font=("Segoe UI Semibold",9),relief="flat"
)
style.map("Treeview",background=[("selected","#1D4ED8")],foreground=[("selected","white")])

style.configure(
    "Vertical.TScrollbar",background=COLORS["panel2"],troughcolor=COLORS["bg"],
    bordercolor=COLORS["bg"],arrowcolor=COLORS["muted"]
)
style.configure(
    "Horizontal.TScrollbar",background=COLORS["panel2"],troughcolor=COLORS["bg"],
    bordercolor=COLORS["bg"],arrowcolor=COLORS["muted"]
)

# ---------- State ----------
vars = {
    "status": tk.StringVar(value="DISCONNECTED"),
    "account": tk.StringVar(value="-"),
    "server": tk.StringVar(value="-"),
    "balance": tk.StringVar(value="-"),
    "equity": tk.StringVar(value="-"),
    "pnl": tk.StringVar(value="+0.00"),
    "realized": tk.StringVar(value="+0.00"),
    "floating": tk.StringVar(value="+0.00"),
    "positions": tk.StringVar(value="0"),
    "total_trades": tk.StringVar(value="0"),
    "win_rate": tk.StringVar(value="0.0%"),
    "profit_factor": tk.StringVar(value="0.00"),
    "consecutive_losses": tk.StringVar(value="0"),
    "cooldown": tk.StringVar(value="0"),
    "search_status": tk.StringVar(value="IDLE"),
    "position_side": tk.StringVar(value="-"),
    "position_symbol": tk.StringVar(value="-"),
    "position_ticket": tk.StringVar(value="-"),
    "position_volume": tk.StringVar(value="-"),
    "position_entry": tk.StringVar(value="-"),
    "position_pnl": tk.StringVar(value="-"),
    "position_sl": tk.StringVar(value="-"),
    "position_tp": tk.StringVar(value="-"),
    "mt5_status": tk.StringVar(value="OFFLINE"),
    "ollama_status": tk.StringVar(value="UNKNOWN"),
    "ai_status": tk.StringVar(value="IDLE"),
    "market_status": tk.StringVar(value="-"),
    "candidate_count": tk.StringVar(value="-"),
    "market_permission": tk.StringVar(value="-"),
    "market_session": tk.StringVar(value="-"),
    "candidate_status": tk.StringVar(value="NOT SCANNED"),
    "market_source": tk.StringVar(value="-"),
    "quote_status": tk.StringVar(value="-"),
    "market_overall": tk.StringVar(value="-"),
    "macro_status": tk.StringVar(value="-"),
    "macro_detail": tk.StringVar(value="-"),
    "micro_status": tk.StringVar(value="-"),
    "micro_detail": tk.StringVar(value="-"),
    "news_status": tk.StringVar(value="-"),
    "news_detail": tk.StringVar(value="-"),
    "tvtech_status": tk.StringVar(value="-"),
    "tvtech_detail": tk.StringVar(value="-"),
    "fear_detail": tk.StringVar(value="-"),
    "session_intel": tk.StringVar(value="-"),
    "fib_detail": tk.StringVar(value="-"),
    "active_ai_model": tk.StringVar(value="-"),
    "ai_council": tk.StringVar(value="SCOUT - | TECH - | CRITIC - | CHIEF -"),
    "margin_level": tk.StringVar(value="-"),
    "free_margin": tk.StringVar(value="-"),
    "portfolio_exposure": tk.StringVar(value="-"),
    "dynamic_quality": tk.StringVar(value="-"),
    "dynamic_risk": tk.StringVar(value="-"),
    "dynamic_rr": tk.StringVar(value="-"),
    "dynamic_entries": tk.StringVar(value="-"),
    "dynamic_session": tk.StringVar(value="-"),
    "entry_snapshot": tk.StringVar(value="-"),
}

symbol=tk.StringVar(value=s.symbol)
tf=tk.StringVar(value=s.timeframe)
tf_display=tk.StringVar(value=s.timeframe)
trading_mode=tk.StringVar(value=getattr(s,"trading_mode","AUTO"))
ack=tk.BooleanVar(value=False)
account_mode=tk.StringVar(value=("CENT (x100)" if s.account_unit_mode=="CENT" else "STANDARD"))

_all_symbols = []
_symbol_meta = {}

def display_scale():
    return 100.0 if account_mode.get().startswith("CENT") else 1.0

def refresh_account(i):
    scale=display_scale()
    vars["account"].set(str(i.login))
    vars["server"].set(str(i.server))
    vars["balance"].set(f"{i.balance/scale:,.2f} {i.currency}")
    vars["equity"].set(f"{i.equity/scale:,.2f} {i.currency}")
    try:
        ml=float(getattr(i,"margin_level",0.0) or 0.0)
        fm=float(getattr(i,"margin_free",0.0) or 0.0)
        vars["margin_level"].set(f"{ml:,.1f}%")
        vars["free_margin"].set(f"{engine.to_display(fm):,.2f} {i.currency}")
    except Exception:
        vars["margin_level"].set("-")
        vars["free_margin"].set("-")

# ---------- Helpers ----------
def classify_symbol(name, info=None):
    n=(name or "").upper()
    path=(getattr(info, "path", "") or "").upper()
    desc=(getattr(info, "description", "") or "").upper()
    blob=f"{n} {path} {desc}"

    if any(x in blob for x in ["BTC","ETH","LTC","XRP","SOL","DOGE","ADA","CRYPTO"]):
        return "Crypto"
    if any(x in blob for x in ["XAU","XAG","GOLD","SILVER","PLATINUM","PALLADIUM","METAL"]):
        return "Metals"
    if any(x in blob for x in ["USOIL","UKOIL","WTI","BRENT","NATGAS","OIL","ENERG"]):
        return "Energies"
    if any(x in path for x in ["STOCK","SHARE","EQUITIES"]) or any(x in desc for x in [" INC"," CORP"," PLC"," LTD"]):
        return "Stocks"
    if any(x in blob for x in ["INDEX","INDICES","US30","US500","SPX","NAS100","USTEC","GER40","DE40","UK100","JP225"]):
        return "Indexes"
    if any(x in path for x in ["SYNTH","VOLATILITY","BOOM","CRASH"]):
        return "Synthetics"
    if any(x in path for x in ["LOCAL"]):
        return "Local"

    # Typical 6-letter FX symbols, optionally with broker suffix/prefix.
    core=re.sub(r"[^A-Z]", "", n)
    majors=["USD","EUR","GBP","JPY","CHF","AUD","NZD","CAD","CNH","SGD","HKD","ZAR","TRY","MXN","NOK","SEK","PLN"]
    if len(core) >= 6 and core[:3] in majors and core[3:6] in majors:
        return "Forex"
    if "FOREX" in path or "FX" in path:
        return "Forex"
    return "Other"

def reload_symbol_cache():
    global _all_symbols, _symbol_meta
    _all_symbols=[]
    _symbol_meta={}
    try:
        items=mt5.symbols_get() or []
        for info in items:
            name=info.name
            _all_symbols.append(name)
            _symbol_meta[name]=info
    except Exception:
        _all_symbols=mt.symbols()
    _all_symbols=sorted(set(_all_symbols))

# ---------- Symbol Browser ----------
def open_symbol_browser():
    if not _all_symbols:
        try:
            reload_symbol_cache()
        except Exception as e:
            messagebox.showerror("Symbols", str(e))
            return

    win=tk.Toplevel(app)
    win.title("Search Symbol")
    win.geometry("760x650")
    win.minsize(650, 520)
    win.transient(app)

    outer=ttk.Frame(win,padding=14)
    outer.pack(fill="both",expand=True)

    ttk.Label(outer,text="Pilih Simbol",font=("Segoe UI",16,"bold")).pack(anchor="w")
    ttk.Label(outer,text="Cari berdasarkan nama atau deskripsi simbol MT5.",style="Sub.TLabel").pack(anchor="w",pady=(2,10))

    search_var=tk.StringVar()
    search_wrap=ttk.Frame(outer)
    search_wrap.pack(fill="x",pady=(0,10))

    entry=ttk.Entry(search_wrap,textvariable=search_var,font=("Segoe UI",11))
    entry.pack(side="left",fill="x",expand=True,ipady=5)

    def clear_search():
        search_var.set("")
        entry.focus_set()

    ttk.Button(search_wrap,text="✕",width=4,command=clear_search).pack(side="left",padx=(6,0))

    tree=ttk.Treeview(outer,columns=("symbol","description"),show="tree headings",selectmode="browse")
    tree.heading("#0",text="Kategori")
    tree.heading("symbol",text="Symbol")
    tree.heading("description",text="Description")
    tree.column("#0",width=160,anchor="w")
    tree.column("symbol",width=150,anchor="w")
    tree.column("description",width=370,anchor="w")

    scroll=ttk.Scrollbar(outer,orient="vertical",command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left",fill="both",expand=True)
    scroll.pack(side="right",fill="y")

    categories=["Forex","Metals","Crypto","Stocks","Indexes","Energies","Local","Synthetics","Other"]

    def rebuild(*_):
        query=search_var.get().strip().lower()
        tree.delete(*tree.get_children())

        grouped={c:[] for c in categories}
        for name in _all_symbols:
            info=_symbol_meta.get(name)
            desc=getattr(info,"description","") if info else ""
            hay=f"{name} {desc}".lower()
            if query and query not in hay:
                continue
            cat=classify_symbol(name,info)
            grouped.setdefault(cat,[]).append((name,desc))

        for cat in categories:
            rows=grouped.get(cat,[])
            if not rows:
                continue
            parent=tree.insert("", "end", text=f"{cat}  ({len(rows)})", open=True)
            for name,desc in rows:
                tree.insert(parent,"end",text="",values=(name,desc))

    def choose(_event=None):
        sel=tree.selection()
        if not sel:
            return
        item=tree.item(sel[0])
        vals=item.get("values",[])
        if not vals:
            return
        symbol.set(str(vals[0]))
        win.destroy()

    tree.bind("<Double-1>",choose)
    tree.bind("<Return>",choose)
    search_var.trace_add("write",rebuild)
    rebuild()
    entry.focus_set()

# ---------- Layout ----------
root=ttk.Frame(app,padding=12,style="Header.TFrame")
root.pack(fill="both",expand=True)
root.columnconfigure(0,weight=1)
root.rowconfigure(0,weight=1)

# Responsive horizontal split:
# left  = dashboard / controls
# right = realtime log sidebar
main_pane=tk.PanedWindow(
    root,
    orient="horizontal",
    sashwidth=8,
    sashrelief="flat",
    bd=0,
    relief="flat",
    bg=COLORS["border"],
    sashpad=2
)
main_pane.grid(row=0,column=0,sticky="nsew")

left_host=ttk.Frame(main_pane,style="Header.TFrame")
right=ttk.Frame(main_pane,padding=(10,0,0,0),style="Header.TFrame")

main_pane.add(left_host,minsize=650,stretch="always")
main_pane.add(right,minsize=300,stretch="always")

left_host.columnconfigure(0,weight=1)
left_host.rowconfigure(1,weight=1)
right.columnconfigure(0,weight=1)
right.rowconfigure(0,weight=1)

left_canvas=tk.Canvas(left_host,highlightthickness=0,borderwidth=0,bg=COLORS["bg"])
left_scroll=ttk.Scrollbar(left_host,orient="vertical",command=left_canvas.yview)
left_canvas.configure(yscrollcommand=left_scroll.set)
fixed_header=ttk.Frame(left_host,style="Header.TFrame",padding=(0,0,8,8))
fixed_header.grid(row=0,column=0,columnspan=2,sticky="ew")
fixed_header.columnconfigure(0,weight=1)
left_canvas.grid(row=1,column=0,sticky="nsew")
left_scroll.grid(row=1,column=1,sticky="ns")

left=ttk.Frame(left_canvas,padding=(0,0,8,0),style="Header.TFrame")
left_window=left_canvas.create_window((0,0),window=left,anchor="nw")

def _sync_left_scrollregion(_event=None):
    left_canvas.configure(scrollregion=left_canvas.bbox("all"))

def _sync_left_width(event):
    left_canvas.itemconfigure(left_window,width=event.width)

left.bind("<Configure>",_sync_left_scrollregion)
left_canvas.bind("<Configure>",_sync_left_width)

def _left_mousewheel(event):
    try:
        left_canvas.yview_scroll(int(-1*(event.delta/120)),"units")
    except Exception:
        pass

left_canvas.bind("<MouseWheel>",_left_mousewheel)
left.bind("<MouseWheel>",_left_mousewheel)

left.columnconfigure(0,weight=1)

# Header
header=ttk.Frame(fixed_header,style="Panel.TFrame",padding=(12,10))
header.grid(row=0,column=0,sticky="ew")
header.columnconfigure(0,weight=1)

title_box=ttk.Frame(header,style="Panel.TFrame")
title_box.grid(row=0,column=0,sticky="w")
try:
    _header_logo_raw=tk.PhotoImage(file=str(Path(__file__).resolve().parent / "assets" / "ai_trading_logo.png"))
    _header_logo=_header_logo_raw.subsample(max(1,_header_logo_raw.width()//52),max(1,_header_logo_raw.height()//52))
    _header_logo_label=ttk.Label(title_box,image=_header_logo,style="Panel.TLabel")
    _header_logo_label.image=_header_logo
    _header_logo_label.pack(side="left",padx=(0,10))
except Exception:
    pass
_header_text=ttk.Frame(title_box,style="Panel.TFrame")
_header_text.pack(side="left")
ttk.Label(_header_text,text="MT5 AI AGENT V3.10.28",style="Metric.TLabel").pack(anchor="w")
ttk.Label(_header_text,text="AI execution • Multi-timeframe • ZPI intelligence • Dynamic risk",style="Panel.TLabel").pack(anchor="w")

status_box=ttk.Frame(header,style="Panel.TFrame")
status_box.grid(row=0,column=1,sticky="e")
ttk.Label(status_box,text="STATUS",style="CardTitle.TLabel").pack(anchor="e")
ttk.Label(status_box,textvariable=vars["status"],font=("Segoe UI",12,"bold")).pack(anchor="e")
menu_btn=ttk.Button(status_box,text="☰  MENU",style="Accent.TButton")
menu_btn.pack(anchor="e",pady=(7,0))


# System status strip
sysbar=ttk.LabelFrame(left,text="System Status",padding=(10,8))
sysbar.grid(row=0,column=0,sticky="ew",pady=(0,6))
for c in range(8): sysbar.columnconfigure(c,weight=1 if c%2 else 0)

ttk.Label(sysbar,text="MT5").grid(row=0,column=0,sticky="w")
ttk.Label(sysbar,textvariable=vars["mt5_status"],style="Status.TLabel").grid(row=0,column=1,sticky="w",padx=(6,18))
ttk.Label(sysbar,text="Ollama").grid(row=0,column=2,sticky="w")
ttk.Label(sysbar,textvariable=vars["ollama_status"],style="Status.TLabel").grid(row=0,column=3,sticky="w",padx=(6,18))
ttk.Label(sysbar,text="AI").grid(row=0,column=4,sticky="w")
ttk.Label(sysbar,textvariable=vars["ai_status"],style="Status.TLabel").grid(row=0,column=5,sticky="w",padx=(6,18))
ttk.Label(sysbar,text="Permission").grid(row=0,column=6,sticky="w")
ttk.Label(sysbar,textvariable=vars["market_permission"],style="Status.TLabel").grid(row=0,column=7,sticky="w",padx=(6,18))

ttk.Label(sysbar,text="Session").grid(row=1,column=0,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["market_session"],style="Status.TLabel").grid(row=1,column=1,sticky="w",padx=(6,18),pady=(4,0))

ttk.Label(sysbar,text="Source").grid(row=1,column=2,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["market_source"],style="Status.TLabel").grid(row=1,column=3,sticky="w",padx=(6,18),pady=(4,0))

ttk.Label(sysbar,text="Quote").grid(row=1,column=4,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["quote_status"],style="Status.TLabel").grid(row=1,column=5,sticky="w",padx=(6,18),pady=(4,0))

ttk.Label(sysbar,text="Overall").grid(row=1,column=6,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["market_overall"],style="Status.TLabel").grid(row=1,column=7,sticky="w",padx=(6,0),pady=(4,0))

ttk.Label(sysbar,text="Macro").grid(row=2,column=0,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["macro_status"],style="Status.TLabel").grid(
    row=2,column=1,sticky="w",padx=(6,18),pady=(4,0)
)
ttk.Label(sysbar,textvariable=vars["macro_detail"],font=("Segoe UI",9)).grid(
    row=2,column=2,columnspan=2,sticky="w",padx=(0,18),pady=(4,0)
)
ttk.Label(sysbar,text="Micro").grid(row=2,column=4,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["micro_status"],style="Status.TLabel").grid(
    row=2,column=5,sticky="w",padx=(6,18),pady=(4,0)
)
ttk.Label(sysbar,textvariable=vars["micro_detail"],font=("Segoe UI",9)).grid(
    row=2,column=6,columnspan=2,sticky="w",padx=(0,0),pady=(4,0)
)

ttk.Label(sysbar,text="News").grid(row=3,column=0,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["news_status"],style="Status.TLabel").grid(
    row=3,column=1,sticky="w",padx=(6,18),pady=(4,0)
)
ttk.Label(sysbar,textvariable=vars["news_detail"],font=("Segoe UI",9)).grid(
    row=3,column=2,columnspan=6,sticky="w",padx=(0,0),pady=(4,0)
)

ttk.Label(sysbar,text="TV Tech").grid(row=4,column=0,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["tvtech_status"],style="Status.TLabel").grid(
    row=4,column=1,sticky="w",padx=(6,18),pady=(4,0)
)
ttk.Label(sysbar,textvariable=vars["tvtech_detail"],font=("Segoe UI",9)).grid(
    row=4,column=2,columnspan=3,sticky="w",padx=(0,18),pady=(4,0)
)
ttk.Label(sysbar,text="Fear/Greed").grid(row=4,column=5,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["fear_detail"],style="Status.TLabel").grid(
    row=4,column=6,columnspan=2,sticky="w",padx=(6,0),pady=(4,0)
)

ttk.Label(sysbar,text="Session Intel").grid(row=5,column=0,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["session_intel"],style="Status.TLabel").grid(
    row=5,column=1,columnspan=3,sticky="w",padx=(6,18),pady=(4,0)
)
ttk.Label(sysbar,text="Fibonacci").grid(row=5,column=4,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["fib_detail"],style="Status.TLabel").grid(
    row=5,column=5,columnspan=3,sticky="w",padx=(6,0),pady=(4,0)
)

ttk.Label(sysbar,text="AI Model").grid(row=6,column=0,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["active_ai_model"],style="Status.TLabel").grid(
    row=6,column=1,columnspan=7,sticky="w",padx=(6,0),pady=(4,0)
)
vars["active_ai_model"].set("AI COUNCIL • 4 agents • ON" if bool(getattr(s,"ai_council_enabled",False)) else "AI COUNCIL • OFF")
ttk.Label(sysbar,text="AI Council").grid(row=7,column=0,sticky="w",pady=(4,0))
ttk.Label(sysbar,textvariable=vars["ai_council"],style="Status.TLabel").grid(row=7,column=1,columnspan=7,sticky="w",padx=(6,0),pady=(4,0))

# Account + session cards
top=ttk.Frame(left)
top.grid(row=1,column=0,sticky="ew")
for c in range(3): top.columnconfigure(c,weight=1)

account_card=ttk.LabelFrame(top,text="Account",padding=(10,9))
session_card=ttk.LabelFrame(top,text="Session",padding=(10,9))
stats_card=ttk.LabelFrame(top,text="Learning Stats",padding=(10,9))
account_card.grid(row=0,column=0,sticky="nsew",padx=(0,6))
session_card.grid(row=0,column=1,sticky="nsew",padx=6)
stats_card.grid(row=0,column=2,sticky="nsew",padx=(6,0))

def pair(frame,row,label,key,big=False):
    ttk.Label(frame,text=label).grid(row=row,column=0,sticky="w",pady=1)
    ttk.Label(frame,textvariable=vars[key],style=("Metric.TLabel" if big else "TLabel")).grid(row=row,column=1,sticky="e",pady=1,padx=(10,0))
    frame.columnconfigure(1,weight=1)

pair(account_card,0,"Account","account")
pair(account_card,1,"Server","server")
pair(account_card,2,"Balance","balance",True)
pair(account_card,3,"Equity","equity")

pair(session_card,0,"Session PnL","pnl",True)
pair(session_card,1,"Realized","realized")
pair(session_card,2,"Floating","floating")
pair(session_card,3,"Bot Positions","positions")

pair(stats_card,0,"Trades","total_trades")
pair(stats_card,1,"Win Rate","win_rate")
pair(stats_card,2,"Profit Factor","profit_factor")
pair(stats_card,3,"Loss Streak","consecutive_losses")
pair(stats_card,4,"Cooldown","cooldown")


# Position / scanner card
scanner=ttk.LabelFrame(left,text="Position Scanner",padding=(10,9))
scanner.grid(row=2,column=0,sticky="ew",pady=(6,0))
for c in range(8): scanner.columnconfigure(c,weight=1 if c%2 else 0)

ttk.Label(scanner,text="Agent State").grid(row=0,column=0,sticky="w")
ttk.Label(scanner,textvariable=vars["search_status"],font=("Segoe UI",11,"bold")).grid(row=0,column=1,sticky="w",padx=(8,20))
ttk.Label(scanner,text="Side").grid(row=0,column=2,sticky="w")
ttk.Label(scanner,textvariable=vars["position_side"],font=("Segoe UI",11,"bold")).grid(row=0,column=3,sticky="w",padx=(8,20))
ttk.Label(scanner,text="Symbol").grid(row=0,column=4,sticky="w")
ttk.Label(scanner,textvariable=vars["position_symbol"]).grid(row=0,column=5,sticky="w",padx=(8,20))
ttk.Label(scanner,text="Ticket").grid(row=0,column=6,sticky="w")
ttk.Label(scanner,textvariable=vars["position_ticket"]).grid(row=0,column=7,sticky="w",padx=(8,0))

ttk.Label(scanner,text="Volume").grid(row=1,column=0,sticky="w",pady=(4,0))
ttk.Label(scanner,textvariable=vars["position_volume"]).grid(row=1,column=1,sticky="w",padx=(8,20),pady=(4,0))
ttk.Label(scanner,text="Floating PnL").grid(row=1,column=2,sticky="w",pady=(4,0))
ttk.Label(scanner,textvariable=vars["position_pnl"],font=("Segoe UI",11,"bold")).grid(row=1,column=3,sticky="w",padx=(8,20),pady=(4,0))
ttk.Label(scanner,text="SL").grid(row=1,column=4,sticky="w",pady=(4,0))
ttk.Label(scanner,textvariable=vars["position_sl"]).grid(row=1,column=5,sticky="w",padx=(8,20),pady=(4,0))
ttk.Label(scanner,text="TP").grid(row=1,column=6,sticky="w",pady=(4,0))
ttk.Label(scanner,textvariable=vars["position_tp"]).grid(row=1,column=7,sticky="w",padx=(8,0),pady=(4,0))
ttk.Label(scanner,text="Entry Price").grid(row=2,column=0,sticky="w",pady=(5,0))
ttk.Label(scanner,textvariable=vars["position_entry"],font=("Segoe UI",10,"bold")).grid(row=2,column=1,sticky="w",padx=(8,20),pady=(5,0))

# Multi-position view. The summary fields above show the currently highlighted/
# primary position, while this table shows every open position owned by the bot.
ttk.Separator(scanner,orient="horizontal").grid(
    row=3,column=0,columnspan=8,sticky="ew",pady=(8,6)
)
position_list_title=tk.StringVar(value="Open bot positions: 0")
ttk.Label(
    scanner,
    textvariable=position_list_title,
    font=("Segoe UI",9,"bold")
).grid(row=4,column=0,columnspan=8,sticky="w",pady=(0,5))

position_columns=("ticket","symbol","side","volume","entry","pnl","sl","tp")
position_table=ttk.Treeview(
    scanner,
    columns=position_columns,
    show="headings",
    height=3
)
position_headings={
    "ticket":"Ticket","symbol":"Symbol","side":"Side","volume":"Volume",
    "entry":"Entry","pnl":"Floating PnL","sl":"SL","tp":"TP"
}
position_widths={
    "ticket":110,"symbol":75,"side":65,"volume":70,
    "entry":105,"pnl":95,"sl":105,"tp":105
}
for col in position_columns:
    position_table.heading(col,text=position_headings[col])
    position_table.column(
        col,
        width=position_widths[col],
        minwidth=55,
        anchor=("e" if col in {"volume","entry","pnl","sl","tp"} else "center"),
        stretch=True
    )
position_table.grid(row=5,column=0,columnspan=8,sticky="ew")

def refresh_position_table():
    try:
        rows=[]
        for pos in (mt.positions() or []):
            # mt.positions() is normally bot-scoped, but keep the magic check when available.
            magic=getattr(pos,"magic",None)
            if magic is not None and int(magic or 0)!=int(s.magic):
                continue
            side="BUY" if int(getattr(pos,"type",0) or 0)==0 else "SELL"
            rows.append({
                "ticket":int(getattr(pos,"ticket",0) or 0),
                "symbol":str(getattr(pos,"symbol","-") or "-"),
                "side":side,
                "volume":float(getattr(pos,"volume",0.0) or 0.0),
                "entry":float(getattr(pos,"price_open",0.0) or 0.0),
                "pnl":float(getattr(pos,"profit",0.0) or 0.0)/display_scale(),
                "sl":float(getattr(pos,"sl",0.0) or 0.0),
                "tp":float(getattr(pos,"tp",0.0) or 0.0),
            })

        total_pnl=sum(r["pnl"] for r in rows)
        position_list_title.set(
            f"Open bot positions: {len(rows)} • Combined Floating PnL {total_pnl:+.2f}"
        )

        existing=set(position_table.get_children())
        wanted=set()
        for row in rows:
            iid=str(row["ticket"])
            wanted.add(iid)
            values=(
                row["ticket"],row["symbol"],row["side"],
                f"{row['volume']:.2f}",
                f"{row['entry']:.5f}",
                f"{row['pnl']:+.2f}",
                f"{row['sl']:.5f}" if row["sl"] else "-",
                f"{row['tp']:.5f}" if row["tp"] else "-"
            )
            if iid in existing:
                position_table.item(iid,values=values)
            else:
                position_table.insert("","end",iid=iid,values=values)

        for iid in existing-wanted:
            position_table.delete(iid)

        # Keep summary synchronized with the first open bot position for backward compatibility.
        if rows:
            p=rows[0]
            vars["position_side"].set(p["side"])
            vars["position_symbol"].set(p["symbol"])
            vars["position_ticket"].set(str(p["ticket"]))
            vars["position_volume"].set(f"{p['volume']:.2f}")
            vars["position_entry"].set(f"{p['entry']:.5f}")
            vars["position_pnl"].set(f"{p['pnl']:+.2f}")
            vars["position_sl"].set(f"{p['sl']:.5f}" if p["sl"] else "-")
            vars["position_tp"].set(f"{p['tp']:.5f}" if p["tp"] else "-")
        elif not bool(getattr(engine,"running",False)):
            position_list_title.set("Open bot positions: 0")
    except Exception as e:
        position_list_title.set(f"Open bot positions: ? • refresh error: {e}")


# Portfolio / margin guard
portfolio=ttk.LabelFrame(left,text="Portfolio Guard",padding=(10,9))
portfolio.grid(row=3,column=0,sticky="ew",pady=(6,0))
for c in range(6):
    portfolio.columnconfigure(c,weight=1 if c%2 else 0)

ttk.Label(portfolio,text="Margin Level").grid(row=0,column=0,sticky="w")
ttk.Label(portfolio,textvariable=vars["margin_level"],font=("Segoe UI",10,"bold")).grid(
    row=0,column=1,sticky="w",padx=(8,20)
)
ttk.Label(portfolio,text="Free Margin").grid(row=0,column=2,sticky="w")
ttk.Label(portfolio,textvariable=vars["free_margin"],font=("Segoe UI",10,"bold")).grid(
    row=0,column=3,sticky="w",padx=(8,20)
)
ttk.Label(portfolio,text="Dynamic Entries").grid(row=0,column=4,sticky="w")
ttk.Label(
    portfolio,
    textvariable=vars["dynamic_entries"],
    font=("Segoe UI",9,"bold")
).grid(row=0,column=5,sticky="w",padx=(8,0))

ttk.Label(portfolio,text="Quality").grid(row=1,column=0,sticky="w",pady=(4,0))
ttk.Label(portfolio,textvariable=vars["dynamic_quality"],font=("Segoe UI",10,"bold")).grid(
    row=1,column=1,sticky="w",padx=(8,20),pady=(4,0)
)
ttk.Label(portfolio,text="Risk / Trade").grid(row=1,column=2,sticky="w",pady=(4,0))
ttk.Label(portfolio,textvariable=vars["dynamic_risk"],font=("Segoe UI",10,"bold")).grid(
    row=1,column=3,sticky="w",padx=(8,20),pady=(4,0)
)
ttk.Label(portfolio,text="RR / Session").grid(row=1,column=4,sticky="w",pady=(4,0))
ttk.Label(portfolio,textvariable=vars["dynamic_session"],font=("Segoe UI",9,"bold")).grid(
    row=1,column=5,sticky="w",padx=(8,0),pady=(4,0)
)

# Trading controls

ttk.Label(portfolio,text="Entry Snapshot").grid(row=2,column=0,sticky="w",pady=(4,0))
ttk.Label(
    portfolio,textvariable=vars["entry_snapshot"],font=("Segoe UI",9,"bold")
).grid(row=2,column=1,columnspan=5,sticky="w",padx=(8,0),pady=(4,0))

control=ttk.LabelFrame(left,text="Trading Setup",padding=(10,9))
control.grid(row=4,column=0,sticky="ew",pady=6)
for c in range(8): control.columnconfigure(c,weight=1 if c in (1,3,5,7) else 0)

ttk.Label(control,text="Symbol").grid(row=0,column=0,sticky="w",padx=(0,6))
symbol_wrap=ttk.Frame(control)
symbol_wrap.grid(row=0,column=1,sticky="ew",padx=(0,16))
symbol_entry=ttk.Entry(symbol_wrap,textvariable=symbol,width=18)
symbol_entry.pack(side="left",fill="x",expand=True)
ttk.Button(symbol_wrap,text="Search",command=open_symbol_browser).pack(side="left",padx=(6,0))

ttk.Label(control,text="Timeframe").grid(row=0,column=2,sticky="w",padx=(0,6))
tf_combo=ttk.Combobox(
    control,
    textvariable=tf_display,
    values=["M1","M5","M15","M30","H1","H4"],
    state="readonly",
    width=20
)
tf_combo.grid(row=0,column=3,sticky="ew",padx=(0,16))

ttk.Label(control,text="Trading Mode").grid(row=0,column=4,sticky="w",padx=(0,6))
mode_combo=ttk.Combobox(control,textvariable=trading_mode,values=["AUTO","SCALPING","INTRADAY","SWING"],state="readonly",width=12)
mode_combo.grid(row=0,column=5,sticky="ew",padx=(0,16))

def sync_timeframe_control(*_):
    mode=str(trading_mode.get() or "AUTO").upper()
    if mode=="AUTO":
        tf_display.set("DYNAMIC")
        tf_combo.configure(state="disabled")
    else:
        # Restore the real manual timeframe value.
        tf_display.set(tf.get())
        tf_combo.configure(state="readonly")

def on_tf_display_selected(event=None):
    if str(trading_mode.get() or "AUTO").upper()!="AUTO":
        value=str(tf_display.get() or "").upper()
        if value in {"M1","M5","M15","M30","H1","H4"}:
            tf.set(value)

mode_combo.bind("<<ComboboxSelected>>",sync_timeframe_control)
tf_combo.bind("<<ComboboxSelected>>",on_tf_display_selected)
sync_timeframe_control()

ttk.Label(control,text="Account Unit").grid(row=0,column=6,sticky="w",padx=(0,6))
ttk.Combobox(control,textvariable=account_mode,values=["STANDARD","CENT (x100)"],state="readonly",width=15).grid(row=0,column=7,sticky="ew")

ttk.Label(control,text="SL / TP").grid(row=1,column=0,sticky="w",pady=(5,0))
ttk.Label(
    control,text="DYNAMIC • ATR / confidence / volatility",
    font=("Segoe UI",9,"bold")
).grid(row=1,column=1,columnspan=2,sticky="w",padx=(0,16),pady=(5,0))

ttk.Label(control,text="Session Risk").grid(row=1,column=3,sticky="w",pady=(5,0))
ttk.Label(
    control,text="DYNAMIC • equity / regime / signal quality",
    font=("Segoe UI",9,"bold")
).grid(row=1,column=4,columnspan=4,sticky="w",padx=(8,0),pady=(5,0))

ttk.Checkbutton(
    control,
    text="I understand: send orders to the CURRENT MT5 account",
    variable=ack
).grid(row=2,column=0,columnspan=8,sticky="w",pady=(5,0))



# Activity / Learning Log sidebar
log_card=ttk.LabelFrame(right,text="Activity / Learning Log",padding=(9,8))
log_card.grid(row=0,column=0,sticky="nsew")
log_card.columnconfigure(0,weight=1)
log_card.rowconfigure(1,weight=1)
ttk.Label(
    log_card,
    text="Realtime engine / AI / learning log",
    style="Sub.TLabel"
).grid(row=0,column=0,sticky="ew",pady=(0,5))

log_box=tk.Text(
    log_card,
    wrap="word",
    font=("Cascadia Mono",9),
    bg=COLORS["panel2"],
    fg=COLORS["text"],
    insertbackground=COLORS["text"],
    relief="flat",
    highlightthickness=1,
    highlightbackground=COLORS["border"],
    highlightcolor=COLORS["accent"]
)
log_scroll=ttk.Scrollbar(log_card,orient="vertical",command=log_box.yview)
log_box.configure(yscrollcommand=log_scroll.set)
log_box.grid(row=1,column=0,sticky="nsew")
log_scroll.grid(row=1,column=1,sticky="ns")

# Semantic colors for the realtime log.
log_box.tag_configure("error",foreground="#F87171")
log_box.tag_configure("order",foreground="#4ADE80")
log_box.tag_configure("risk",foreground="#FBBF24")
log_box.tag_configure("intel",foreground="#22D3EE")
log_box.tag_configure("history",foreground="#C084FC")
log_box.tag_configure("muted",foreground="#94A3B8")


# Tradeable candidates
cand_frame=ttk.LabelFrame(left,text="Tradeable Now Candidates",padding=(9,8))
cand_frame.grid(row=5,column=0,sticky="ew",pady=(0,5))
cand_frame.columnconfigure(0,weight=1)

cand_top=ttk.Frame(cand_frame)
cand_top.grid(row=0,column=0,sticky="ew")
ttk.Label(cand_top,text="FULL symbols • session-aware status • manual scan only").pack(side="left")
ttk.Label(cand_top,text="Candidates").pack(side="left",padx=(12,0))
ttk.Label(cand_top,textvariable=vars["candidate_count"],font=("Segoe UI",10,"bold")).pack(side="left",padx=(5,0))
ttk.Label(cand_top,textvariable=vars["candidate_status"],style="Sub.TLabel").pack(side="left",padx=(14,0))

cand_list=tk.Listbox(cand_frame,height=4,font=("Cascadia Mono",9),exportselection=False,bg=COLORS["panel2"],fg=COLORS["text"],selectbackground="#1D4ED8",selectforeground="white",relief="flat",highlightthickness=1,highlightbackground=COLORS["border"],highlightcolor=COLORS["accent"])
cand_list.grid(row=1,column=0,sticky="ew",pady=(3,0))

selected_candidate=tk.StringVar(value="")
use_candidate_text=tk.StringVar(value="SELECT SYMBOL")

def refresh_candidates():
    vars["candidate_status"].set("SCANNING...")
    scan_btn.configure(state="disabled", text="SCANNING...")
    app.update_idletasks()

    def run_scan():
        try:
            if not _all_symbols:
                reload_symbol_cache()

            checked = len(_all_symbols)
            full_names = []

            for name in _all_symbols:
                try:
                    info = mt.symbol_info(name)
                    mode = trade_mode_name(int(getattr(info, "trade_mode", -1)))
                    if mode != "FULL":
                        continue
                    full_names.append(name)
                    try:
                        mt5.symbol_select(name, True)
                    except Exception:
                        pass
                except Exception:
                    continue

            time.sleep(0.7)

            now = int(time.time())
            active_rows, stale_rows, noquote_rows = [], [], []

            for name in full_names:
                try:
                    info = mt.symbol_info(name)
                    tick = mt.tick(name)

                    bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick else 0.0
                    ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick else 0.0
                    tick_time = int(getattr(tick, "time", 0) or 0) if tick else 0
                    category = classify_symbol(name, info)

                    if tick is None or tick_time <= 0 or bid <= 0 or ask <= 0:
                        noquote_rows.append({
                            "symbol": name, "category": category, "status": "NO_QUOTE",
                            "bid": bid, "ask": ask, "age": None
                        })
                        continue

                    age = max(0, now - tick_time)
                    row = {
                        "symbol": name,
                        "category": category,
                        "status": "ACTIVE" if age <= 120 else "STALE",
                        "bid": bid,
                        "ask": ask,
                        "age": age,
                    }
                    (active_rows if age <= 120 else stale_rows).append(row)

                except Exception:
                    noquote_rows.append({
                        "symbol": name, "category": "OTHER", "status": "NO_QUOTE",
                        "bid": 0.0, "ask": 0.0, "age": None
                    })

            active_rows.sort(key=lambda r: r["symbol"])
            stale_rows.sort(key=lambda r: r["symbol"])
            noquote_rows.sort(key=lambda r: r["symbol"])
            rows = active_rows + stale_rows + noquote_rows

            def update_ui():
                global _candidate_symbols
                _candidate_symbols = rows

                cand_list.delete(0, "end")
                selected_candidate.set("")
                use_candidate_text.set("SELECT SYMBOL")
                use_candidate_btn.configure(state="disabled")

                for r in rows:
                    if r["status"] == "ACTIVE":
                        text = (
                            f'{r["symbol"]:<14} {r["category"]:<10} FULL  ACTIVE  '
                            f'bid={r["bid"]:g} ask={r["ask"]:g} age={r["age"]}s'
                        )
                    elif r["status"] == "STALE":
                        text = (
                            f'{r["symbol"]:<14} {r["category"]:<10} FULL  STALE   '
                            f'{r["age"]}s old'
                        )
                    else:
                        text = f'{r["symbol"]:<14} {r["category"]:<10} FULL  NO_QUOTE'
                    cand_list.insert("end", text)

                msg = (
                    f"{checked} checked | {len(full_names)} FULL | "
                    f"{len(active_rows)} ACTIVE | {len(stale_rows)} STALE | "
                    f"{len(noquote_rows)} NO_QUOTE"
                )
                vars["candidate_count"].set(str(len(full_names)))
                vars["candidate_status"].set(msg)
                log("CANDIDATE SCAN: " + msg)
                scan_btn.configure(state="normal", text="SCAN NOW")

            app.after(0, update_ui)

        except Exception as e:
            def fail():
                vars["candidate_status"].set("SCAN ERROR")
                log("CANDIDATE SCAN ERROR: " + str(e))
                scan_btn.configure(state="normal", text="SCAN NOW")
            app.after(0, fail)

    threading.Thread(target=run_scan, daemon=True).start()

def on_candidate_select(_event=None):
    sel=cand_list.curselection()
    if not sel:
        selected_candidate.set("")
        use_candidate_text.set("SELECT SYMBOL")
        use_candidate_btn.configure(state="disabled")
        return
    line=cand_list.get(sel[0])
    chosen=line.split()[0]
    selected_candidate.set(chosen)
    use_candidate_text.set(f"USE {chosen}")
    use_candidate_btn.configure(state="normal")

def choose_candidate(_event=None):
    chosen=selected_candidate.get().strip()
    if not chosen:
        return
    if engine.running:
        messagebox.showinfo("Agent running","STOP SAFE dulu sebelum mengganti symbol.")
        return
    symbol.set(chosen)
    use_candidate_text.set(f"✓ {chosen} SELECTED")
    use_candidate_btn.configure(state="disabled")
    log(f"Symbol selected from candidates: {chosen}")

cand_list.bind("<<ListboxSelect>>",on_candidate_select)
cand_list.bind("<Double-1>",choose_candidate)

cand_btns=ttk.Frame(cand_frame)
cand_btns.grid(row=2,column=0,sticky="w",pady=(3,0))
scan_btn=ttk.Button(cand_btns,text="SCAN NOW",command=refresh_candidates)
scan_btn.pack(side="left")
use_candidate_btn=ttk.Button(cand_btns,textvariable=use_candidate_text,command=choose_candidate,state="disabled")
use_candidate_btn.pack(side="left",padx=(6,0))



def resource_path(relative_path):
    """Resolve files bundled next to main.py, including future packaged builds."""
    return Path(__file__).resolve().parent / relative_path


def _save_model_preference(model_name):
    try:
        _model_pref_path.write_text(
            json.dumps({"ollama_model":model_name},indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        log(f"MODEL PREF WARNING: {e}")

def _benchmark_snapshot_prompt():
    chosen=str(symbol.get() or "").strip()
    if not chosen:
        raise RuntimeError("Select a symbol first.")
    selected_tf=str(getattr(engine,"tf",None) or tf.get() or "M15").upper()
    df=mt.rates(chosen,selected_tf,s.bars)
    snap=snapshot(df)
    tech_side,tech_conf=technical_score(snap)
    compact={
        "symbol":chosen,"timeframe":selected_tf,
        "technical_signal":tech_side,
        "technical_confidence":round(float(tech_conf),4),
        "close":snap.get("close"),"rsi14":snap.get("rsi14"),
        "macd":snap.get("macd"),"macd_signal":snap.get("macd_signal"),
        "atr14":snap.get("atr14"),"atr_pct":snap.get("atr_pct"),
        "adx14":snap.get("adx14"),"volume_ratio":snap.get("volume_ratio"),
        "regime":snap.get("regime"),"structure":snap.get("structure"),
        "trend":snap.get("trend"),
    }
    prompt=(
        "You are benchmarking a trading decision model. Use ONLY the supplied market snapshot. "
        "Return one compact JSON object with exactly these keys: "
        "action (BUY|SELL|HOLD), confidence (0..1), reason, "
        "trend (BULLISH|BEARISH|MIXED), momentum (BULLISH|BEARISH|MIXED), "
        "volatility (LOW|NORMAL|HIGH), structure (BULLISH|BEARISH|NEUTRAL), "
        "conflicts (array of strings). Do not invent news or macro data. "
        "The benchmark never executes an order.\nMARKET SNAPSHOT:\n"
        + json.dumps(compact,separators=(",",":"),default=str)
    )
    return prompt,compact

def _save_council_preference(enabled):
    try:
        _council_pref_path.write_text(
            json.dumps({"enabled":bool(enabled)},indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        log(f"AI COUNCIL PREF WARNING: {e}")


def _council_installed_status():
    try:
        installed=set(llm.installed_models())
    except Exception:
        return {},[]
    result={}
    missing=[]
    for role,model_name in AI_COUNCIL_MODELS.items():
        base=model_name.split(":")[0]
        ok=(
            model_name in installed
            or model_name+":latest" in installed
            or any(str(x).split(":")[0]==base for x in installed)
        )
        result[role]=ok
        if not ok:
            missing.append((role,model_name))
    return result,missing


def set_council_enabled(enabled,parent=None):
    enabled=bool(enabled)

    if enabled and not bool(_ui_connected.get("value",False)):
        messagebox.showwarning(
            "MT5 not connected",
            "Connect MT5 terlebih dahulu, baru aktifkan AI Council.",
            parent=parent or app
        )
        vars["active_ai_model"].set("AI COUNCIL • OFF")
        vars["ai_council"].set("SCOUT - | TECH - | CRITIC - | CHIEF -")
        return False

    if enabled:
        status,missing=_council_installed_status()
        if missing:
            names="\n".join(f"{role}: {model}" for role,model in missing)
            messagebox.showwarning(
                "AI Council models missing",
                "Council belum bisa diaktifkan karena model berikut belum terpasang:\n\n"+names+"\n\nGunakan PULL ALL COUNCIL MODELS.",
                parent=parent or app
            )
            return False
    s.ai_council_enabled=enabled
    _save_council_preference(enabled)
    vars["active_ai_model"].set(
        "AI COUNCIL • 4 agents • ON" if enabled else "AI COUNCIL • OFF"
    )
    vars["ai_council"].set(
        "ENABLED • SCOUT → TECH → CRITIC → CHIEF" if enabled
        else "DISABLED • single/local fallback path"
    )
    log(f"AI COUNCIL {'ENABLED' if enabled else 'DISABLED'}")
    return True


def toggle_council(parent=None):
    return set_council_enabled(not bool(getattr(s,"ai_council_enabled",False)),parent=parent)


def open_model_lab():
    win=tk.Toplevel(app)
    win.title("AI Model Lab • MT5 AI Agent V3.10.28")
    win.geometry("1120x660")
    win.minsize(980,600)
    win.transient(app)

    outer=ttk.Frame(win,padding=14)
    outer.pack(fill="both",expand=True)

    ttk.Label(outer,text="LOCAL AI MODEL LAB",font=("Segoe UI",16,"bold")).pack(anchor="w")
    ttk.Label(
        outer,
        text=("Empat model lokal dibagi peran sebagai AI Council. Connect MT5 terlebih dahulu, lalu aktifkan Council secara manual. "
              "Claude-distill = model komunitas berbasis Qwen, bukan Claude resmi."),
        wraplength=1060
    ).pack(anchor="w",pady=(2,8))

    # Top bar: Council toggle + pull all. Both Council buttons use the same state.
    topbar=ttk.Frame(outer)
    topbar.pack(fill="x")
    council_button_text=tk.StringVar()
    council_state_text=tk.StringVar()

    def refresh_council_ui():
        on=bool(getattr(s,"ai_council_enabled",False))
        council_button_text.set("DEACTIVATE AI COUNCIL" if on else "ACTIVATE AI COUNCIL")
        council_state_text.set("Council: ON • automatic trading path" if on else "Council: OFF")
        vars["active_ai_model"].set("AI COUNCIL • 4 agents • ON" if on else "AI COUNCIL • OFF")

    def toggle_from_lab():
        if toggle_council(win):
            refresh_council_ui()

    def pull_all():
        if bool(getattr(engine,"running",False)):
            messagebox.showwarning("Trading is running","STOP SAFE dulu sebelum pull model Council.",parent=win)
            return
        council_state_text.set("Pulling Council models one-by-one...")
        def worker():
            messages=[]
            for role,model_name in AI_COUNCIL_MODELS.items():
                try:
                    proc=subprocess.run(["ollama","pull",model_name],capture_output=True,text=True,timeout=7200)
                    if proc.returncode==0:
                        messages.append(f"{role}=OK")
                        log(f"AI COUNCIL PULL DONE: {role} | {model_name}")
                    else:
                        err=(proc.stderr or proc.stdout or "unknown error").strip()[-350:]
                        messages.append(f"{role}=FAIL")
                        log(f"AI COUNCIL PULL FAILED: {role} | {model_name} | {err}")
                except Exception as e:
                    messages.append(f"{role}=ERROR")
                    log(f"AI COUNCIL PULL ERROR: {role} | {model_name} | {e}")
            try:
                win.after(0,lambda: council_state_text.set(" • ".join(messages)))
            except tk.TclError:
                pass
        threading.Thread(target=worker,daemon=True).start()

    ttk.Button(topbar,textvariable=council_button_text,command=toggle_from_lab,style="Accent.TButton").pack(side="left")
    ttk.Button(topbar,text="PULL ALL COUNCIL MODELS",command=pull_all).pack(side="left",padx=(8,0))
    ttk.Label(topbar,textvariable=council_state_text).pack(side="right")
    ttk.Separator(outer).pack(fill="x",pady=10)

    body=ttk.Frame(outer)
    body.pack(fill="both",expand=True)
    body.columnconfigure(0,weight=3)
    body.columnconfigure(1,weight=2)
    body.rowconfigure(0,weight=1)

    left=ttk.LabelFrame(body,text=" Same-Snapshot Benchmark ",padding=10)
    left.grid(row=0,column=0,sticky="nsew",padx=(0,7))
    left.columnconfigure(0,weight=1); left.rowconfigure(0,weight=1)
    cols=("model","installed","action","confidence","seconds","json")
    table=ttk.Treeview(left,columns=cols,show="headings",height=7)
    headers={"model":"Model","installed":"Installed","action":"Action","confidence":"Conf.","seconds":"Sec.","json":"JSON"}
    widths={"model":250,"installed":72,"action":65,"confidence":65,"seconds":60,"json":55}
    for c in cols:
        table.heading(c,text=headers[c]); table.column(c,width=widths[c],minwidth=45,stretch=(c=="model"))
    table.grid(row=0,column=0,sticky="nsew")
    benchmark_status=tk.StringVar(value="Benchmark idle.")
    ttk.Label(left,textvariable=benchmark_status,wraplength=610).grid(row=1,column=0,sticky="w",pady=(8,5))

    def benchmark_all():
        if bool(getattr(engine,"running",False)):
            messagebox.showwarning("Trading is running","Benchmark diblok saat bot RUNNING agar tidak mengambil CPU/RAM dari trading engine.",parent=win); return
        for iid in table.get_children(): table.delete(iid)
        try: prompt,compact=_benchmark_snapshot_prompt()
        except Exception as e:
            messagebox.showerror("Benchmark",str(e),parent=win); benchmark_status.set("Benchmark failed before start."); return
        benchmark_status.set(f"Benchmarking: {compact.get('symbol')} {compact.get('timeframe')}...")
        def worker():
            try: installed=set(llm.installed_models())
            except Exception as e: installed=set(); log(f"MODEL BENCHMARK: cannot read installed models: {e}")
            results=[]
            for row in MODEL_CATALOG:
                model_name=row["model"]; label=row["label"]; base_name=model_name.split(":")[0]
                is_installed=(model_name in installed or model_name+":latest" in installed or any(str(x).split(":")[0]==base_name for x in installed))
                if not is_installed: results.append({"label":label,"installed":False,"ok":False,"error":"not installed"}); continue
                bench=llm.benchmark_model(model_name,prompt); bench["label"]=label; bench["installed"]=True; results.append(bench)
            def render():
                for idx,r in enumerate(results):
                    table.insert("","end",iid=str(idx),values=(r.get("label",""),"YES" if r.get("installed") else "NO",r.get("action","-"),f"{float(r.get('confidence',0)):.2f}" if r.get("ok") else "-",f"{float(r.get('elapsed',0)):.2f}" if r.get("installed") else "-","OK" if r.get("ok") else "FAIL"))
                ok=[r for r in results if r.get("ok")]
                if ok:
                    fastest=min(ok,key=lambda x:x.get("elapsed",999999)); benchmark_status.set(f"{len(ok)} model OK • fastest: {fastest.get('label')} {fastest.get('elapsed',0):.2f}s")
                else: benchmark_status.set("No installed model completed the benchmark.")
            try: win.after(0,render)
            except tk.TclError: pass
        threading.Thread(target=worker,daemon=True).start()
    ttk.Button(left,text="BENCHMARK ALL INSTALLED MODELS",command=benchmark_all,style="Accent.TButton").grid(row=2,column=0,sticky="w",pady=(5,0))

    right=ttk.LabelFrame(body,text=" AI Council Roles ",padding=10)
    right.grid(row=0,column=1,sticky="nsew",padx=(7,0))
    ttk.Label(right,text="4-agent conditional council",font=("Segoe UI",11,"bold")).pack(anchor="w")
    ttk.Label(right,text="Weak/HOLD setups stop early. RiskGuard remains authoritative.",wraplength=390).pack(anchor="w",pady=(2,10))
    roles=[
        ("SCOUT","DeepSeek-R1 1.5B","Fast first-pass / direction"),
        ("TECHNICAL","Qwen3.5 4B","Indicators • session • Fibonacci"),
        ("CRITIC","Qwen3.5 Claude-distill 4B","Challenge conflicts / weak setup"),
        ("CHIEF","Qwen3.5 Claude-distill 9B","Final council decision"),
    ]
    for role,model_name,desc in roles:
        row=ttk.Frame(right); row.pack(fill="x",pady=4)
        ttk.Label(row,text=role,width=11,font=("Segoe UI",9,"bold")).pack(side="left")
        mid=ttk.Frame(row); mid.pack(side="left",fill="x",expand=True)
        ttk.Label(mid,text=model_name).pack(anchor="w")
        ttk.Label(mid,text=desc).pack(anchor="w")
    ttk.Separator(right).pack(fill="x",pady=(8,6))
    ttk.Label(right,text="Council Performance • rolling telemetry",font=("Segoe UI",9,"bold")).pack(anchor="w")
    perf_text=tk.StringVar(value="No Council telemetry yet.")
    ttk.Label(right,textvariable=perf_text,wraplength=410,font=("Consolas",8)).pack(anchor="w",pady=(3,6))

    def refresh_perf():
        try:
            rows=llm.council_performance_snapshot()
            role_order=("SCOUT","TECHNICAL","CRITIC","CHIEF")
            lines=[]
            for role in role_order:
                candidates=[x for x in rows if x.get("role")==role]
                if not candidates:
                    lines.append(f"{role:<10} calls=0")
                    continue
                # Prefer the configured model row; for CHIEF show whichever has the most calls.
                r=max(candidates,key=lambda x:x.get("calls",0))
                lines.append(
                    f"{role:<10} n={r['calls']:<3} avg={r['avg_elapsed']:>5.1f}s "
                    f"TO={r['timeout_rate']*100:>3.0f}% AB={r['abstain_rate']*100:>3.0f}% "
                    f"JR={r['repair_rate']*100:>3.0f}%"
                )
            perf_text.set("\n".join(lines))
            if win.winfo_exists():
                win.after(1200,refresh_perf)
        except tk.TclError:
            pass
        except Exception as e:
            perf_text.set(f"Telemetry unavailable: {e}")

    ttk.Separator(right).pack(fill="x",pady=(4,6))
    ttk.Label(right,textvariable=vars["ai_council"],wraplength=390).pack(anchor="w",pady=(0,6))

    right_toggle_text=tk.StringVar()
    def refresh_right_button():
        right_toggle_text.set(
            "AI COUNCIL ON • CLICK TO DISABLE"
            if bool(getattr(s,"ai_council_enabled",False))
            else "RUN / ENABLE AI COUNCIL"
        )
    def toggle_from_right():
        if toggle_council(win):
            refresh_council_ui(); refresh_right_button(); refresh_perf()
    ttk.Button(right,textvariable=right_toggle_text,command=toggle_from_right,style="Accent.TButton").pack(fill="x")
    ttk.Button(right,text="CLOSE",command=win.destroy).pack(fill="x",pady=(7,0))

    refresh_council_ui(); refresh_right_button()


def _council_market_payload():
    _,compact=_benchmark_snapshot_prompt()
    compact["trading_mode"]=str(trading_mode.get() or "AUTO")
    compact["requested_timeframe"]=str(tf.get() or "")
    compact["session_intel"]=str(vars["session_intel"].get() or "")
    compact["fib_detail"]=str(vars["fib_detail"].get() or "")
    return compact


def open_about():
    win = tk.Toplevel(app)
    win.title("About • MT5 AI Agent V3.10.28")
    win.geometry("650x690")
    win.minsize(560, 600)
    win.transient(app)

    # Fixed bottom action bar so OPEN GITHUB can never be pushed off-screen.
    action_bar = ttk.Frame(win, padding=(18, 10))
    action_bar.pack(side="bottom", fill="x")

    ttk.Button(
        action_bar,
        text="OPEN GITHUB",
        command=lambda: webbrowser.open("https://github.com/lumidevcore")
    ).pack(side="left")

    ttk.Button(
        action_bar,
        text="CLOSE",
        command=win.destroy
    ).pack(side="right")

    ttk.Separator(win).pack(side="bottom", fill="x")

    # Scrollable-ish content area: fixed controls remain at bottom.
    outer = ttk.Frame(win, padding=20)
    outer.pack(side="top", fill="both", expand=True)

    logo_frame = ttk.Frame(outer)
    logo_frame.pack(fill="x", pady=(0, 8))

    try:
        logo_file = resource_path("assets/ai_trading_logo.png")
        logo_img = tk.PhotoImage(file=str(logo_file))

        max_px = 190
        if logo_img.width() > max_px or logo_img.height() > max_px:
            sx = max(1, (logo_img.width() + max_px - 1) // max_px)
            sy = max(1, (logo_img.height() + max_px - 1) // max_px)
            logo_img = logo_img.subsample(sx, sy)

        logo_label = ttk.Label(logo_frame, image=logo_img)
        logo_label.image = logo_img
        logo_label.pack()
    except Exception:
        ttk.Label(
            logo_frame,
            text="LUMIDEVCORE",
            font=("Segoe UI", 18, "bold")
        ).pack()

    ttk.Label(
        outer,
        text="MT5 AI AGENT V3.10.28",
        font=("Segoe UI", 20, "bold")
    ).pack(anchor="center")

    ttk.Label(
        outer,
        text="AI Trading Workspace • Local Intelligence • Risk-Aware Execution",
        font=("Segoe UI", 10)
    ).pack(anchor="center", pady=(3, 12))

    info = (
        "MT5 AI Agent adalah proyek eksperimen trading berbasis Python yang "
        "menggabungkan data MetaTrader 5, analisis teknikal, multi-timeframe, "
        "RiskGuard, memory/learning statistics, dan LLM lokal melalui Ollama.\n\n"
        "AI/LLM digunakan sebagai bagian dari sistem analisis dan bukan jaminan "
        "profit. Gunakan akun demo untuk pengujian sebelum mempertimbangkan akun live."
    )

    ttk.Label(
        outer,
        text=info,
        wraplength=590,
        justify="left"
    ).pack(anchor="w")

    ttk.Separator(outer).pack(fill="x", pady=14)

    grid = ttk.Frame(outer)
    grid.pack(fill="x")

    rows = [
        ("Version", "3.10.2"),
        ("Developer", "Lumidev"),
        ("GitHub", "lumidevcore"),
        ("Repository/Profile", "github.com/lumidevcore"),
        ("AI Runtime", "Ollama / 4-Agent AI Council"),
        ("Active Model", MODEL_LABEL_BY_NAME.get(s.ollama_model,s.ollama_model)),
        ("Trading Platform", "MetaTrader 5"),
    ]

    for r, (k, v) in enumerate(rows):
        ttk.Label(
            grid, text=k,
            font=("Segoe UI", 9, "bold")
        ).grid(row=r, column=0, sticky="w", padx=(0, 24), pady=3)

        ttk.Label(
            grid, text=v
        ).grid(row=r, column=1, sticky="w", pady=3)

    ttk.Separator(outer).pack(fill="x", pady=14)

    ttk.Label(
        outer,
        text=(
            "Risk notice: software ini untuk eksperimen/riset. Trading memiliki "
            "risiko kerugian dan keputusan penggunaan akun live tetap berada pada pengguna."
        ),
        wraplength=590,
        justify="left"
    ).pack(anchor="w")



def open_trade_history():
    win = tk.Toplevel(app)
    win.title("Trade History • MT5 AI Agent V3.10.28")
    win.geometry("1240x660")
    win.minsize(900, 480)
    win.transient(app)

    root_h = ttk.Frame(win, padding=12)
    root_h.pack(fill="both", expand=True)
    root_h.columnconfigure(0, weight=1)
    root_h.rowconfigure(2, weight=1)

    top_h = ttk.Frame(root_h)
    top_h.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    top_h.columnconfigure(12, weight=1)

    history_source = tk.StringVar(value="BOT ONLY")
    history_symbol = tk.StringVar(value="ALL")
    history_result = tk.StringVar(value="ALL")
    history_days = tk.StringVar(value="30")
    history_summary = tk.StringVar(value="Loading...")

    ttk.Label(top_h, text="Source").grid(row=0, column=0, sticky="w")
    source_filter = ttk.Combobox(
        top_h,
        textvariable=history_source,
        values=["BOT ONLY", "ALL MT5", "LEARNING DB"],
        state="readonly",
        width=12
    )
    source_filter.grid(row=0, column=1, sticky="w", padx=(6, 14))

    ttk.Label(top_h, text="Days").grid(row=0, column=2, sticky="w")
    days_filter = ttk.Combobox(
        top_h,
        textvariable=history_days,
        values=["1","7","30","90","365"],
        state="readonly",
        width=6
    )
    days_filter.grid(row=0, column=3, sticky="w", padx=(6, 14))

    ttk.Label(top_h, text="Symbol").grid(row=0, column=4, sticky="w")
    symbol_filter = ttk.Combobox(
        top_h,
        textvariable=history_symbol,
        values=["ALL"],
        state="readonly",
        width=14
    )
    symbol_filter.grid(row=0, column=5, sticky="w", padx=(6, 14))

    ttk.Label(top_h, text="Result").grid(row=0, column=6, sticky="w")
    result_filter = ttk.Combobox(
        top_h,
        textvariable=history_result,
        values=["ALL", "WIN", "LOSS", "BREAKEVEN"],
        state="readonly",
        width=13
    )
    result_filter.grid(row=0, column=7, sticky="w", padx=(6, 14))

    ttk.Label(
        top_h,
        textvariable=history_summary,
        font=("Segoe UI", 9, "bold")
    ).grid(row=0, column=12, sticky="e")

    columns = (
        "closed_at","symbol","side","volume","open_price","close_price",
        "sl","tp","pnl","result","close_reason","position_id"
    )
    table = ttk.Treeview(
        root_h,
        columns=columns,
        show="headings",
        selectmode="browse"
    )
    table.grid(row=2, column=0, sticky="nsew")

    headings = {
        "closed_at":"Closed",
        "symbol":"Symbol",
        "side":"Side",
        "volume":"Volume",
        "open_price":"Open",
        "close_price":"Close",
        "sl":"SL",
        "tp":"TP",
        "pnl":"PnL",
        "result":"Result",
        "close_reason":"Reason",
        "position_id":"Position ID",
    }
    widths = {
        "closed_at":160,"symbol":85,"side":60,"volume":75,
        "open_price":95,"close_price":95,"sl":95,"tp":95,
        "pnl":90,"result":80,"close_reason":105,"position_id":110,
    }

    for c in columns:
        table.heading(c,text=headings[c])
        table.column(
            c,
            width=widths[c],
            minwidth=55,
            anchor=("e" if c in {"volume","open_price","close_price","sl","tp","pnl"} else "center")
        )

    scroll_y=ttk.Scrollbar(root_h,orient="vertical",command=table.yview)
    scroll_y.grid(row=2,column=1,sticky="ns")
    table.configure(yscrollcommand=scroll_y.set)

    scroll_x=ttk.Scrollbar(root_h,orient="horizontal",command=table.xview)
    scroll_x.grid(row=3,column=0,sticky="ew")
    table.configure(xscrollcommand=scroll_x.set)

    detail=tk.Text(
        root_h,
        height=8,
        wrap="word",
        font=("Consolas",9),
        state="disabled"
    )
    detail.grid(row=4,column=0,sticky="ew",pady=(8,0))

    history_rows=[]

    def fmt_time(v):
        if not v:
            return "-"
        return str(v).replace("T"," ")[:19]

    def normalized_result(v):
        s=str(v or "").upper()
        return "BREAKEVEN" if s in {"BE","BREAKEVEN"} else s

    def load_source_rows():
        source=history_source.get()
        days=int(history_days.get() or 30)

        if source=="BOT ONLY":
            return engine.mt5_trade_history(days=days,bot_only=True)

        if source=="ALL MT5":
            return engine.mt5_trade_history(days=days,bot_only=False)

        # Learning DB fallback / inspection.
        db_rows=mem.trade_history(limit=5000)
        out=[]
        for r in db_rows:
            out.append({
                "position_id":r.get("position_id"),
                "order_ticket":None,
                "symbol":r.get("symbol"),
                "side":r.get("side"),
                "volume":None,
                "open_price":None,
                "close_price":None,
                "sl":None,
                "tp":None,
                "opened_at":r.get("opened_at"),
                "closed_at":r.get("closed_at"),
                "pnl":engine.to_display(float(r.get("pnl") or 0.0)),
                "result":normalized_result(r.get("result")),
                "close_reason":r.get("close_reason"),
                "is_bot":True,
                "magic":s.magic,
                "timeframe":r.get("timeframe"),
                "features":json.loads(r.get("features") or "{}"),
                "lesson":r.get("lesson"),
                "regime":r.get("regime"),
                "structure":r.get("structure"),
            })
        return out

    def history_display_pnl(row):
        """Normalize Trade History PnL to the selected account display unit.

        MT5 broker history rows are raw account units. Learning DB rows were
        already converted when loaded, so they must not be divided again.
        """
        try:
            source=str(history_source.get() or "").upper()
            if source in {"BOT ONLY","ALL MT5"}:
                raw=row.get("pnl_raw",row.get("pnl",0.0))
                return float(raw or 0.0)/display_scale()
            return float(row.get("pnl") or 0.0)
        except Exception:
            return 0.0

    def reload_history():
        nonlocal history_rows

        try:
            rows=load_source_rows()
        except Exception as e:
            messagebox.showerror("History error",str(e))
            return

        symbol_value=history_symbol.get()
        result_value=history_result.get()

        symbols=sorted({str(r.get("symbol")) for r in rows if r.get("symbol")})
        values=["ALL"]+symbols
        symbol_filter.configure(values=values)
        if history_symbol.get() not in values:
            history_symbol.set("ALL")
            symbol_value="ALL"

        filtered=[]
        for r in rows:
            if symbol_value!="ALL" and str(r.get("symbol"))!=symbol_value:
                continue
            rr=normalized_result(r.get("result"))
            if result_value!="ALL" and rr!=result_value:
                continue
            filtered.append(r)

        history_rows=filtered
        table.delete(*table.get_children())

        wins=losses=bes=0
        total_pnl=0.0

        for idx,row in enumerate(history_rows):
            pnl=history_display_pnl(row)
            rr=normalized_result(row.get("result"))

            if rr=="WIN":
                wins+=1
            elif rr=="LOSS":
                losses+=1
            else:
                bes+=1

            total_pnl+=pnl

            def num(v,d=2):
                if v is None:
                    return "-"
                try:
                    return f"{float(v):.{d}f}"
                except Exception:
                    return str(v)

            table.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    fmt_time(row.get("closed_at")),
                    row.get("symbol") or "-",
                    row.get("side") or "-",
                    num(row.get("volume"),2),
                    num(row.get("open_price"),5),
                    num(row.get("close_price"),5),
                    num(row.get("sl"),5),
                    num(row.get("tp"),5),
                    f"{pnl:+.2f}",
                    rr or "-",
                    row.get("close_reason") or "-",
                    row.get("position_id") or "-",
                )
            )

        total=len(history_rows)
        wr=(wins/total*100.0) if total else 0.0
        history_summary.set(
            f"{history_source.get()} • Trades {total} • "
            f"Win {wins} / Loss {losses} / BE {bes} • "
            f"Win rate {wr:.1f}% • PnL {total_pnl:+.2f}"
        )

        detail.configure(state="normal")
        detail.delete("1.0","end")
        detail.insert(
            "end",
            "Pilih satu trade untuk melihat detail broker dan learning."
        )
        detail.configure(state="disabled")

    def show_trade_detail(_event=None):
        sel=table.selection()
        if not sel:
            return

        idx=int(sel[0])
        if idx<0 or idx>=len(history_rows):
            return
        row=history_rows[idx]

        features=row.get("features") or {}

        lines=[
            f"Source      : {history_source.get()}",
            f"Position ID : {row.get('position_id')}",
            f"Order Ticket: {row.get('order_ticket') or '-'}",
            f"Magic       : {row.get('magic') or '-'}",
            f"Bot Trade   : {'YES' if row.get('is_bot') else 'NO'}",
            f"Symbol      : {row.get('symbol')} {row.get('side')}",
            f"Volume      : {row.get('volume') or '-'}",
            f"Opened      : {fmt_time(row.get('opened_at'))}",
            f"Closed      : {fmt_time(row.get('closed_at'))}",
            f"Open Price  : {row.get('open_price') or '-'}",
            f"Close Price : {row.get('close_price') or '-'}",
            f"SL / TP     : {row.get('sl') or '-'} / {row.get('tp') or '-'}",
            f"PnL         : {history_display_pnl(row):+.2f}",
            f"Result      : {normalized_result(row.get('result'))}",
            f"Close Reason: {row.get('close_reason') or '-'}",
            f"Timeframe   : {row.get('timeframe') or '-'}",
            f"Regime      : {row.get('regime') or '-'}",
            f"Structure   : {row.get('structure') or '-'}",
            "",
            "Learning / Lesson:",
            str(row.get("lesson") or "-"),
        ]

        if features:
            lines += ["","Entry Features:"]
            for k,v in features.items():
                if k in {
                    "rsi14","macd_hist","adx14","stoch_k","atr_pct",
                    "volume_ratio","regime","structure"
                }:
                    lines.append(f"  {k}: {v}")

        detail.configure(state="normal")
        detail.delete("1.0","end")
        detail.insert("end","\n".join(lines))
        detail.configure(state="disabled")

    def sync_learning_now():
        try:
            n=engine.sync_learning_from_mt5_history(days=int(history_days.get() or 30))
            reload_history()
            messagebox.showinfo(
                "History Sync",
                f"Sinkronisasi selesai. {n} trade bot baru ditambahkan ke learning DB."
            )
        except Exception as e:
            messagebox.showerror("History Sync",str(e))

    table.bind("<<TreeviewSelect>>",show_trade_detail)

    for box in (source_filter,days_filter,symbol_filter,result_filter):
        box.bind("<<ComboboxSelected>>",lambda _e:reload_history())

    actions_h=ttk.Frame(root_h)
    actions_h.grid(row=1,column=0,sticky="ew",pady=(0,8))

    ttk.Button(
        actions_h,text="REFRESH",command=reload_history
    ).pack(side="left")

    ttk.Button(
        actions_h,text="SYNC BOT → LEARNING",command=sync_learning_now
    ).pack(side="left",padx=(8,0))

    ttk.Button(
        actions_h,text="CLOSE",command=win.destroy
    ).pack(side="right")

    reload_history()


def update_button_state(running=None, connected=None):
    """Keep header/hamburger status synchronized with real engine state."""
    actual_connected=bool(_ui_connected.get("value",False))
    actual_running=bool(getattr(engine,"running",False))

    if actual_running and actual_connected:
        menu_btn.configure(text="☰  MENU • RUNNING")
        vars["status"].set("RUNNING")
    elif actual_connected:
        menu_btn.configure(text="☰  MENU • CONNECTED")
        if str(vars["status"].get()).upper()!="STOPPED":
            vars["status"].set("CONNECTED")
    else:
        menu_btn.configure(text="☰  MENU • OFFLINE")
        vars["status"].set("DISCONNECTED")


running_anim = {"i": 0}

def animate_running():
    # Do not mutate trading widgets during scroll/repaint; status is driven by engine state.
    try:
        app.after(500,animate_running)
    except tk.TclError:
        pass



def connect():
    try:
        i=mt.connect()
        reload_symbol_cache()
        refresh_account(i)
        _ui_connected["value"]=True

        vars["status"].set("CONNECTED")
        vars["mt5_status"].set("CONNECTED")
        vars["search_status"].set("READY")
        vars["dynamic_quality"].set("waiting")
        vars["dynamic_risk"].set("waiting")
        vars["dynamic_rr"].set("waiting")
        vars["dynamic_entries"].set(f"margin-driven / {s.max_open_positions} emergency")
        vars["dynamic_session"].set("waiting signal")

        update_button_state()

        if symbol.get() not in _all_symbols and _all_symbols:
            symbol.set(_all_symbols[0])

        try:
            llm.ping()
            vars["ollama_status"].set("READY")
            vars["ai_status"].set("IDLE")
            vars["active_ai_model"].set("AI COUNCIL • 4 agents • ON" if bool(getattr(s,"ai_council_enabled",False)) else "AI COUNCIL • OFF")
            s.ai_council_enabled=False
            _save_council_preference(False)
            vars["active_ai_model"].set("AI COUNCIL • OFF")
            vars["ai_council"].set("SCOUT - | TECH - | CRITIC - | CHIEF -")
            log(f"Ollama OK: {s.ollama_model}")
        except Exception as e:
            vars["ollama_status"].set("OFFLINE")
            vars["ai_status"].set("OFFLINE")
            log("WARNING Ollama unavailable: "+str(e))

        log(f"MT5 connected: {i.login} | {i.server} | currency={i.currency}")
        log(f"Symbols loaded: {len(_all_symbols)}")

        vars["candidate_count"].set("-")
        vars["candidate_status"].set("NOT SCANNED")

        try:
            ms=mt.market_status(symbol.get())
            vars["market_permission"].set(ms["trade_mode"])
            vars["market_session"].set(ms["session"])
            vars["market_source"].set(ms["session_source"])
            qs=ms["quote_status"]
            if qs=="STALE":
                qs=f"STALE ({ms['stale_seconds']}s)"
            vars["quote_status"].set(qs)
            vars["market_overall"].set(ms["overall"])
        except Exception:
            pass

    except Exception as e:
        _ui_connected["value"]=False
        vars["status"].set("DISCONNECTED")
        vars["mt5_status"].set("OFFLINE")
        update_button_state()
        messagebox.showerror("Connection failed",str(e))

def disconnect():
    if engine.running:
        messagebox.showinfo(
            "Agent masih berjalan",
            "Tekan STOP SAFE dulu sebelum memutus koneksi MT5."
        )
        return

    try:
        mt.shutdown()
    except Exception:
        pass

    _ui_connected["value"]=False

    if bool(getattr(s,"ai_council_enabled",False)):
        s.ai_council_enabled=False
        _save_council_preference(False)
        vars["active_ai_model"].set("AI COUNCIL • OFF")
        vars["ai_council"].set("SCOUT - | TECH - | CRITIC - | CHIEF -")
        log("AI COUNCIL AUTO-DISABLED: MT5 disconnected")

    vars["status"].set("DISCONNECTED")
    vars["mt5_status"].set("DISCONNECTED")
    vars["search_status"].set("DISCONNECTED")
    vars["ai_status"].set("IDLE")

    vars["account"].set("-")
    vars["server"].set("-")
    vars["balance"].set("-")
    vars["equity"].set("-")

    vars["market_permission"].set("-")
    vars["market_session"].set("-")
    vars["market_source"].set("-")
    vars["quote_status"].set("-")
    vars["market_overall"].set("-")

    vars["candidate_count"].set("-")
    vars["candidate_status"].set("NOT SCANNED")
    try:
        cand_list.delete(0,"end")
    except Exception:
        pass

    update_button_state()
    log("MT5 disconnected from AI Agent. Terminal/account itself remains logged in.")


def toggle_connection():
    if _ui_connected["value"]:
        disconnect()
    else:
        connect()


def start():
    try:
        chosen=symbol.get().strip()
        if not chosen:
            raise RuntimeError("Pilih symbol terlebih dahulu.")
        engine.set_account_unit_mode(account_mode.get())
        engine.start(chosen,tf.get().strip(),ack.get(),trading_mode.get().strip())
        if str(trading_mode.get() or "AUTO").upper()=="AUTO":
            tf_display.set("DYNAMIC • SCANNING")
        vars["status"].set("RUNNING")
        vars["search_status"].set("SEARCHING BUY / SELL")
        vars["ai_status"].set("WAITING CANDLE")
        update_button_state()
    except Exception as e:
        messagebox.showerror("Cannot start",str(e))

def stop():
    engine.stop_safe()
    vars["search_status"].set("STOPPED")
    vars["ai_status"].set("IDLE")
    update_button_state()

def closeall():
    if messagebox.askyesno("Confirm","Close ALL positions opened by this bot and stop?"):
        engine.close_all_stop("manual")
        update_button_state()

def append_log_line(line):
    text=str(line)
    upper=text.upper()
    tag=None
    if any(k in upper for k in ("ERROR","WARNING","TIMEOUT","FAILED")):
        tag="error"
    elif any(k in upper for k in ("ORDER ","ORDER RETCODE","AUTO EXIT","ENTRY SNAPSHOT","ACTIVE TRADE SNAPSHOT")):
        tag="order"
    elif any(k in upper for k in ("RISKGUARD","MARGINGUARD","DYNAMIC RISK","CURRENT MARKET","PREFLIGHT")):
        tag="risk"
    elif any(k in upper for k in ("ZPI ","NEWS:","MACRO:","MICRO:","TV TECH","FEAR/GREED","KLINES")):
        tag="intel"
    elif any(k in upper for k in ("HISTORY","LEARNING HISTORY")):
        tag="history"
    elif any(k in upper for k in ("WAIT:","SKIP AI","STOP SAFE")):
        tag="muted"

    if tag:
        log_box.insert("end",text+"\n",tag)
    else:
        log_box.insert("end",text+"\n")

_last_connection_signature={"value":None}

def pump():
    sig=(bool(_ui_connected.get("value",False)),bool(getattr(engine,"running",False)))
    if sig != _last_connection_signature["value"]:
        _last_connection_signature["value"]=sig
        update_button_state()
    try:
        while True:
            typ,x=q.get_nowait()
            if typ=="log":
                append_log_line(x)
                log_box.see("end")
            else:
                if "status" in x:
                    vars["status"].set(x["status"])
                    if x["status"]=="STOPPED":
                        update_button_state()
                if "session_pnl_raw" in x: vars["pnl"].set(f"{float(x['session_pnl_raw'])/display_scale():+.2f}")
                elif "session_pnl" in x: vars["pnl"].set(f"{x['session_pnl']:+.2f}")
                if "realized_raw" in x: vars["realized"].set(f"{float(x['realized_raw'])/display_scale():+.2f}")
                elif "realized" in x: vars["realized"].set(f"{x['realized']:+.2f}")
                if "floating_raw" in x: vars["floating"].set(f"{float(x['floating_raw'])/display_scale():+.2f}")
                elif "floating" in x: vars["floating"].set(f"{x['floating']:+.2f}")
                if "positions" in x: vars["positions"].set(str(x["positions"]))
                if "total_trades" in x: vars["total_trades"].set(str(x["total_trades"]))
                if "win_rate" in x: vars["win_rate"].set(f"{x['win_rate']*100:.1f}%")
                if "profit_factor" in x:
                    pf=x["profit_factor"]
                    vars["profit_factor"].set("∞" if pf>=999 else f"{pf:.2f}")
                if "consecutive_losses" in x: vars["consecutive_losses"].set(str(x["consecutive_losses"]))
                if "cooldown" in x: vars["cooldown"].set(str(x["cooldown"]))
                if "search_status" in x: vars["search_status"].set(str(x["search_status"]))
                if "ai_status" in x: vars["ai_status"].set(str(x["ai_status"]))
                if "selected_timeframe" in x:
                    selected_tf=str(x.get("selected_timeframe","-") or "-").upper()
                    effective=str(x.get("effective_mode","-") or "-").upper()
                    if str(trading_mode.get() or "AUTO").upper()=="AUTO":
                        tf_display.set(f"DYNAMIC → {selected_tf} ({effective})")
                    else:
                        tf.set(selected_tf)
                        tf_display.set(selected_tf)
                if "market_status" in x:
                    ms=str(x["market_status"])
                    vars["market_status"].set(ms)
                    vars["market_permission"].set(ms)
                if "market_session" in x:
                    vars["market_session"].set(str(x["market_session"]))
                if "market_session_source" in x:
                    vars["market_source"].set(str(x["market_session_source"]))
                if "market_quote_status" in x:
                    qs=str(x["market_quote_status"])
                    stale=int(x.get("market_stale_seconds",0) or 0)
                    if qs=="STALE":
                        qs=f"STALE ({stale}s)"
                    vars["quote_status"].set(qs)
                if "market_overall" in x:
                    vars["market_overall"].set(str(x["market_overall"]))
                if "news_status" in x:
                    vars["news_status"].set(str(x.get("news_status","-")))
                    score=float(x.get("news_score",0) or 0)
                    label="BULLISH" if score>0.15 else ("BEARISH" if score<-0.15 else "NEUTRAL")
                    vars["news_detail"].set(
                        f"{int(x.get('news_count',0) or 0)} headlines • {label} {score:+.2f} • "
                        f"{x.get('news_source','ZPI')} • API calls {int(x.get('zpi_requests',0) or 0)}"
                    )
                if "tvtech_status" in x:
                    vars["tvtech_status"].set(str(x.get("tvtech_status","-")))
                    vars["tvtech_detail"].set(
                        f"{x.get('tvtech_summary','-')} • score {float(x.get('tvtech_score',0)):+.2f}"
                    )
                    if x.get("fear_raw") is not None:
                        vars["fear_detail"].set(
                            f"{x.get('fear_rating','-')} {float(x.get('fear_raw',50)):.0f}/100"
                        )
                    else:
                        vars["fear_detail"].set(str(x.get("fear_status","-")))
                if "macro_status" in x:
                    vars["macro_status"].set(str(x.get("macro_status","-")))
                    vars["macro_detail"].set(
                        f"bias {float(x.get('macro_bias',0)):+.2f} • "
                        f"{x.get('macro_risk','-')} "
                        f"{'• '+str(x.get('macro_event')) if x.get('macro_event') else ''}"
                    )
                if "session_intel_active" in x:
                    vars["session_intel"].set(
                        f"{x.get('session_intel_active','-')} • "
                        f"bias {float(x.get('session_intel_score',0)):+.2f} • "
                        f"{x.get('session_intel_breakout','NONE')}"
                    )
                if "fib_direction" in x:
                    fib_px=float(x.get("fib_nearest_price",0) or 0)
                    vars["fib_detail"].set(
                        f"{x.get('fib_direction','-')} • {x.get('fib_nearest','-')} @ {fib_px:g} • "
                        f"bias {float(x.get('fib_score',0)):+.2f}"
                    )
                if "micro_bias" in x:
                    vars["micro_status"].set(
                        "BULL" if float(x.get("micro_bias",0))>0.15 else
                        ("BEAR" if float(x.get("micro_bias",0))<-0.15 else "NEUTRAL")
                    )
                    vars["micro_detail"].set(
                        f"bias {float(x.get('micro_bias',0)):+.2f} • "
                        f"{x.get('micro_activity','-')} activity • "
                        f"{x.get('micro_volatility','-')} vol"
                    )
                if "dynamic_quality" in x:
                    vars["dynamic_quality"].set(f"{float(x['dynamic_quality']):.2f}")
                if "dynamic_risk_pct" in x:
                    vars["dynamic_risk"].set(f"{float(x['dynamic_risk_pct']):.2f}%")
                if "dynamic_rr" in x:
                    vars["dynamic_rr"].set(f"{float(x['dynamic_rr']):.2f}")
                if "dynamic_max_entries" in x:
                    vars["dynamic_entries"].set(
                        f"margin-driven / {s.max_open_positions} emergency"
                    )
                if "dynamic_session_profit_pct" in x and "dynamic_session_loss_pct" in x:
                    vars["dynamic_session"].set(
                        f"RR {float(x.get('dynamic_rr',0)):.2f} • "
                        f"+{float(x['dynamic_session_profit_pct']):.2f}%/"
                        f"-{float(x['dynamic_session_loss_pct']):.2f}%"
                    )
                if "entry_snapshot" in x:
                    snap=x.get("entry_snapshot")
                    if snap:
                        vars["entry_snapshot"].set(
                            f"{snap.get('mode','AUTO')} • Q {float(snap.get('quality',0)):.2f} • "
                            f"Risk {float(snap.get('risk_pct',0)):.2f}% • "
                            f"RR {float(snap.get('rr',0)):.2f} • "
                            f"Entry {snap.get('entry_price','-')} • "
                            f"SL {snap.get('sl','-')} • TP {snap.get('tp','-')}"
                        )
                    else:
                        vars["entry_snapshot"].set("-")
                if "position" in x:
                    p=x["position"]
                    if p:
                        vars["position_side"].set(str(p.get("side","-")))
                        vars["position_symbol"].set(str(p.get("symbol","-")))
                        vars["position_ticket"].set(str(p.get("ticket","-")))
                        vars["position_volume"].set(str(p.get("volume","-")))
                        vars["position_entry"].set(str(p.get("price_open","-")))
                        vars["position_pnl"].set(f"{float(p.get('profit',0))/display_scale():+.2f}")
                        vars["position_sl"].set(str(p.get("sl","-")))
                        vars["position_tp"].set(str(p.get("tp","-")))
                    else:
                        vars["position_side"].set("-")
                        vars["position_symbol"].set("-")
                        vars["position_ticket"].set("-")
                        vars["position_volume"].set("-")
                        vars["position_entry"].set("-")
                        vars["position_pnl"].set("-")
                        vars["position_sl"].set("-")
                        vars["position_tp"].set("-")
                i=mt.account()
                if i: refresh_account(i)
                refresh_position_table()
    except Empty:
        pass
    app.after(300,pump)

def on_close():
    if engine.running:
        if not messagebox.askyesno("Exit","Agent is running. STOP SAFE and exit?"):
            return
        engine.stop_safe()
    try:
        mt.shutdown()
    except Exception:
        pass
    app.destroy()

def set_initial_split():
    try:
        w=max(app.winfo_width(),1100)
        # Main dashboard ~68%, log sidebar ~32%.
        main_pane.sash_place(0,int(w*0.70),0)
    except Exception:
        pass

# ---------- Floating hamburger action menu ----------
def open_action_menu():
    old=getattr(open_action_menu,"win",None)
    if old is not None and old.winfo_exists():
        old.lift(); old.focus_force(); return
    win=tk.Toplevel(app); open_action_menu.win=win
    win.title("Agent Menu • V3.10.28")
    win.transient(app)

    screen_h=max(560,int(win.winfo_screenheight() or 768))
    target_h=min(600,max(500,screen_h-180))
    win.geometry(f"390x{target_h}")
    win.minsize(350,500)
    win.resizable(False,False)

    body=ttk.Frame(win,padding=(12,8,12,8),style="Header.TFrame")
    body.pack(fill="both",expand=True)

    # Reserve footer space first. This keeps CLOSE MENU visible without scroll.
    close_menu_btn=ttk.Button(
        body,text="CLOSE MENU",command=win.destroy,style="Secondary.TButton"
    )
    close_menu_btn.pack(side="bottom",fill="x",pady=(6,0))

    content=ttk.Frame(body,style="Header.TFrame")
    content.pack(side="top",fill="both",expand=True)

    card=ttk.LabelFrame(content,text="Trading Controls",padding=9); card.pack(fill="x")
    ttk.Label(card,text="Quick actions",style="Metric.TLabel").pack(anchor="w",pady=(0,5))

    menu_status=tk.StringVar(
        value="RUNNING" if bool(getattr(engine,"running",False))
        else ("CONNECTED" if _ui_connected["value"] else "OFFLINE")
    )
    ttk.Label(card,textvariable=menu_status,style="Status.TLabel").pack(anchor="w",pady=(0,8))

    connect_text=tk.StringVar(
        value="DISCONNECT MT5" if _ui_connected["value"] else "CONNECT MT5"
    )

    start_text=tk.StringVar()

    connect_btn=ttk.Button(card,textvariable=connect_text,style="Accent.TButton")
    connect_btn.pack(fill="x",pady=2)

    start_btn=ttk.Button(card,textvariable=start_text,style="Success.TButton")
    start_btn.pack(fill="x",pady=2)

    stop_btn=ttk.Button(card,text="STOP SAFE",style="Danger.TButton")
    stop_btn.pack(fill="x",pady=2)

    close_btn=ttk.Button(card,text="CLOSE ALL & STOP",style="DarkDanger.TButton")
    close_btn.pack(fill="x",pady=2)

    def refresh_menu_controls():
        alive=bool(_ui_connected["value"])
        running=bool(getattr(engine,"running",False))

        connect_text.set("DISCONNECT MT5" if alive else "CONNECT MT5")
        menu_status.set("RUNNING" if running else ("CONNECTED" if alive else "OFFLINE"))

        if running:
            start_text.set("TRADING RUNNING")
            start_btn.state(["disabled"])
            stop_btn.state(["!disabled"])
            close_btn.state(["!disabled"])
            connect_btn.state(["disabled"])
        elif alive:
            start_text.set("START TRADING")
            start_btn.state(["!disabled"])
            stop_btn.state(["disabled"])
            close_btn.state(["disabled"])
            connect_btn.state(["!disabled"])
        else:
            start_text.set("START TRADING")
            start_btn.state(["disabled"])
            stop_btn.state(["disabled"])
            close_btn.state(["disabled"])
            connect_btn.state(["!disabled"])

    def menu_toggle_connection():
        toggle_connection()
        refresh_menu_controls()
        update_button_state()

    def menu_start():
        start()
        refresh_menu_controls()
        update_button_state()

    def menu_stop():
        stop()
        refresh_menu_controls()
        update_button_state()

    def menu_closeall():
        closeall()
        refresh_menu_controls()
        update_button_state()

    connect_btn.configure(command=menu_toggle_connection)
    start_btn.configure(command=menu_start)
    stop_btn.configure(command=menu_stop)
    close_btn.configure(command=menu_closeall)
    refresh_menu_controls()

    def menu_state_tick():
        try:
            if not win.winfo_exists():
                return
            refresh_menu_controls()
            win.after(350,menu_state_tick)
        except tk.TclError:
            pass

    win.after(350,menu_state_tick)
    tools=ttk.LabelFrame(content,text="Agent",padding=9); tools.pack(fill="x",pady=(7,0))
    ttk.Button(tools,text="TRADE HISTORY",command=open_trade_history,style="Secondary.TButton").pack(fill="x",pady=2)
    ttk.Button(tools,text="AI MODEL LAB",command=open_model_lab,style="Accent.TButton").pack(fill="x",pady=2)
    ttk.Button(tools,text="ABOUT MT5 AI AGENT",command=open_about,style="Secondary.TButton").pack(fill="x",pady=2)

menu_btn.configure(command=open_action_menu)

update_button_state()
refresh_position_table()

app.protocol("WM_DELETE_WINDOW",on_close)
pump()
animate_running()
app.after(250,set_initial_split)
app.mainloop()
