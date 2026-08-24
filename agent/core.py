from __future__ import annotations
import json, math, re, sqlite3, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np
import pandas as pd
import requests

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


TRADE_RETCODE_NAMES = {
    10004: "REQUOTE",
    10006: "REJECT",
    10007: "CANCEL",
    10008: "PLACED",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10011: "ERROR",
    10012: "TIMEOUT",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",
    10016: "INVALID_STOPS",
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF",
    10022: "INVALID_EXPIRATION",
    10023: "ORDER_CHANGED",
    10024: "TOO_MANY_REQUESTS",
    10025: "NO_CHANGES",
    10026: "SERVER_DISABLES_AUTOTRADING",
    10027: "CLIENT_DISABLES_AUTOTRADING",
    10028: "LOCKED",
    10029: "FROZEN",
    10030: "INVALID_FILL",
    10031: "CONNECTION",
    10032: "ONLY_REAL",
    10033: "LIMIT_ORDERS",
    10034: "LIMIT_VOLUME",
    10035: "INVALID_ORDER",
    10036: "POSITION_CLOSED",
    10038: "INVALID_CLOSE_VOLUME",
    10039: "CLOSE_ORDER_EXIST",
    10040: "LIMIT_POSITIONS",
    10041: "REJECT_CANCEL",
    10042: "LONG_ONLY",
    10043: "SHORT_ONLY",
    10044: "CLOSE_ONLY",
    10045: "FIFO_CLOSE",
    10046: "HEDGE_PROHIBITED",
}

def retcode_name(code):
    try:
        return TRADE_RETCODE_NAMES.get(int(code), f"UNKNOWN_{code}")
    except Exception:
        return str(code)

def trade_mode_name(mode):
    mapping = {
        mt5.SYMBOL_TRADE_MODE_DISABLED: "DISABLED",
        mt5.SYMBOL_TRADE_MODE_LONGONLY: "LONG_ONLY",
        mt5.SYMBOL_TRADE_MODE_SHORTONLY: "SHORT_ONLY",
        mt5.SYMBOL_TRADE_MODE_CLOSEONLY: "CLOSE_ONLY",
        mt5.SYMBOL_TRADE_MODE_FULL: "FULL",
    }
    return mapping.get(mode, f"UNKNOWN_{mode}")


def _ollama_request_with_retry(request_fn, log_fn=None):
    """Run one LLM request and retry once on invalid/empty output."""
    last_error = None
    for attempt in (1, 2):
        try:
            result = request_fn()
            if result is None:
                raise ValueError("LLM returned no result")
            reason = ""
            if isinstance(result, dict):
                reason = str(result.get("reason", "")).strip()
            if isinstance(result, dict) and not reason:
                raise ValueError("LLM returned empty reason")
            return result
        except Exception as exc:
            last_error = exc
            if log_fn:
                log_fn(f"LLM INVALID_RESPONSE attempt {attempt}/2: {exc}")
    raise ValueError(f"LLM invalid after retry: {last_error}")

class MT5Client:
    def __init__(self, s):
        self.s = s

    def connect(self):
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"No active MT5 account: {mt5.last_error()}")
        return info

    def shutdown(self):
        mt5.shutdown()

    def account(self):
        return mt5.account_info()

    def symbols(self):
        xs = mt5.symbols_get() or []
        return sorted(x.name for x in xs if x.visible)

    def symbol_info(self, symbol):
        i = mt5.symbol_info(symbol)
        if i is None:
            raise RuntimeError(f"Symbol not found: {symbol}")
        if not i.visible and not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Cannot select symbol: {symbol}")
        return mt5.symbol_info(symbol)

    def tick(self, symbol):
        self.symbol_info(symbol)
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            raise RuntimeError(f"No tick for {symbol}")
        return t

    def rates(self, symbol, tf, bars):
        self.symbol_info(symbol)
        code = TIMEFRAMES.get(tf.upper())
        if code is None:
            raise ValueError(f"Unsupported timeframe {tf}")
        arr = mt5.copy_rates_from_pos(symbol, code, 0, bars)
        if arr is None or len(arr) < 220:
            raise RuntimeError(f"Not enough bars for {symbol} {tf}: {mt5.last_error()}")
        df = pd.DataFrame(arr)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df

    def positions(self, symbol=None):
        ps = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return [p for p in (ps or []) if int(p.magic) == int(self.s.magic)]

    def filling(self, symbol):
        info = self.symbol_info(symbol)
        if info.filling_mode in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
            return info.filling_mode
        return mt5.ORDER_FILLING_IOC

    def normalize_volume(self, symbol, vol):
        i = self.symbol_info(symbol)
        vol = max(i.volume_min, min(i.volume_max, vol))
        steps = round((vol - i.volume_min) / i.volume_step)
        return round(i.volume_min + steps * i.volume_step, 8)

    def risk_volume(self, symbol, side, entry, sl, equity, risk_pct_override=None):
        typ = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        one_lot = mt5.order_calc_profit(typ, symbol, 1.0, entry, sl)
        if one_lot is None or one_lot == 0:
            return self.symbol_info(symbol).volume_min
        risk_pct = (
            float(risk_pct_override)
            if risk_pct_override is not None
            else float(self.s.risk_per_trade_pct)
        )
        risk_money = equity * risk_pct / 100.0
        return self.normalize_volume(symbol, risk_money / abs(one_lot))

    def preflight_order(self, symbol, side, volume, sl, tp):
        info = self.symbol_info(symbol)
        tick = self.tick(symbol)

        mode = int(getattr(info, "trade_mode", -1))
        if mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
            return False, "symbol trade mode is DISABLED", None
        if mode == mt5.SYMBOL_TRADE_MODE_CLOSEONLY:
            return False, "symbol trade mode is CLOSE_ONLY: broker currently allows closing positions only", None
        if mode == mt5.SYMBOL_TRADE_MODE_LONGONLY and side != "BUY":
            return False, "symbol is LONG_ONLY; SELL entries are not allowed", None
        if mode == mt5.SYMBOL_TRADE_MODE_SHORTONLY and side != "SELL":
            return False, "symbol is SHORT_ONLY; BUY entries are not allowed", None

        typ = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "BUY" else tick.bid
        volume = self.normalize_volume(symbol, volume)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": typ,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": self.s.deviation_points,
            "magic": self.s.magic,
            "comment": "AI_AGENT_V2_PREFLIGHT",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.filling(symbol),
        }

        check = mt5.order_check(req)
        if check is None:
            return False, f"order_check failed: {mt5.last_error()}", None

        # order_check retcode=0 is commonly success in Python API.
        check_code = int(getattr(check, "retcode", 0))
        if check_code not in (0, mt5.TRADE_RETCODE_DONE):
            comment = getattr(check, "comment", "")
            return False, f"order_check retcode={check_code} {retcode_name(check_code)} | {comment}", check

        return True, f"preflight OK | mode={trade_mode_name(mode)} | volume={volume}", check

    def modify_position_sltp(self, position, sl, tp):
        """Modify SL/TP for an existing bot position without changing volume."""
        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": str(getattr(position, "symbol", "")),
            "position": int(getattr(position, "ticket", 0) or 0),
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "magic": self.s.magic,
            "comment": "AI_AGENT_DYNAMIC_EXIT",
        }
        return mt5.order_send(req)

    def partial_close_position(self, position, requested_volume):
        """Close only part of an existing position without risking accidental full close."""
        symbol=str(getattr(position,"symbol","") or "")
        ticket=int(getattr(position,"ticket",0) or 0)
        current_volume=float(getattr(position,"volume",0.0) or 0.0)
        if not symbol or ticket<=0 or current_volume<=0:
            return None,0.0,"invalid position"

        info=self.symbol_info(symbol)
        vmin=float(getattr(info,"volume_min",0.01) or 0.01)
        step=float(getattr(info,"volume_step",vmin) or vmin)
        vmax=float(getattr(info,"volume_max",current_volume) or current_volume)

        # Floor requested close volume to broker step. Never round upward.
        target=max(0.0,min(float(requested_volume or 0.0),current_volume))
        steps=math.floor((target/step)+1e-9)
        close_volume=round(steps*step,8)

        if close_volume < vmin-1e-9:
            return None,0.0,f"partial volume {close_volume:g} below broker minimum {vmin:g}"

        # Never let a scale-out accidentally flatten the whole position.
        remaining=round(current_volume-close_volume,8)
        if remaining < vmin-1e-9:
            max_close=current_volume-vmin
            steps=math.floor((max_close/step)+1e-9)
            close_volume=round(max(0.0,steps*step),8)
            remaining=round(current_volume-close_volume,8)

        if close_volume < vmin-1e-9 or remaining < vmin-1e-9:
            return None,0.0,(
                f"partial close skipped: current={current_volume:g}, "
                f"broker min={vmin:g}, step={step:g}"
            )

        close_volume=min(close_volume,vmax,current_volume)
        tick=self.tick(symbol)
        is_buy=int(getattr(position,"type",-1))==int(mt5.POSITION_TYPE_BUY)
        req={
            "action":mt5.TRADE_ACTION_DEAL,
            "symbol":symbol,
            "volume":close_volume,
            "type":mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position":ticket,
            "price":float(tick.bid if is_buy else tick.ask),
            "deviation":self.s.deviation_points,
            "magic":self.s.magic,
            "comment":"AI_AGENT_SCALE_OUT",
            "type_time":mt5.ORDER_TIME_GTC,
            "type_filling":self.filling(symbol),
        }
        result=mt5.order_send(req)
        return result,close_volume,f"remaining={remaining:g}"

    def send(self, symbol, side, volume, sl, tp):
        t = self.tick(symbol)
        typ = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        price = t.ask if side == "BUY" else t.bid
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": self.normalize_volume(symbol, volume),
            "type": typ,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": self.s.deviation_points,
            "magic": self.s.magic,
            "comment": "AI_AGENT_V2",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.filling(symbol),
        }
        return mt5.order_send(req)

    def tradeable_candidates(self, preferred_categories=None, limit=12, with_stats=False):
        preferred_categories = preferred_categories or []
        rows = []
        now = int(time.time())

        stats = {
            "total": 0,
            "full": 0,
            "fresh": 0,
            "stale_full": 0,
            "no_tick": 0,
            "blocked": 0,
        }

        symbols = mt5.symbols_get() or []
        for info in symbols:
            stats["total"] += 1
            try:
                mode_raw = int(getattr(info, "trade_mode", -1))
                mode_name = trade_mode_name(mode_raw)

                # Use exactly the same normalized interpretation as the main
                # market-status UI. This avoids broker/package enum mismatches.
                if mode_name != "FULL":
                    stats["blocked"] += 1
                    continue

                stats["full"] += 1
                name = info.name
                tick = mt5.symbol_info_tick(name)

                if tick is None:
                    stats["no_tick"] += 1
                    continue

                bid = float(getattr(tick, "bid", 0.0) or 0.0)
                ask = float(getattr(tick, "ask", 0.0) or 0.0)
                if bid <= 0 or ask <= 0:
                    stats["no_tick"] += 1
                    continue

                tick_time = int(getattr(tick, "time", 0) or 0)
                stale_seconds = max(0, now - tick_time) if tick_time else 999999

                if stale_seconds > 120:
                    stats["stale_full"] += 1
                    continue

                stats["fresh"] += 1
                cat = classify_symbol(info)

                score = 0
                if cat in preferred_categories:
                    score += 10
                if getattr(info, "visible", False):
                    score += 2

                rows.append({
                    "symbol": name,
                    "category": cat,
                    "bid": bid,
                    "ask": ask,
                    "stale_seconds": stale_seconds,
                    "permission": mode_name,
                    "score": score,
                })
            except Exception:
                continue

        rows.sort(key=lambda r: (-r["score"], r["symbol"]))
        rows = rows[:limit]
        return (rows, stats) if with_stats else rows

    def _session_api_probe(self, symbol):
        """Best-effort use of MT5 session APIs if exposed by the installed Python binding.

        Some MetaTrader 5 Python builds do not expose SymbolInfoSessionTrade/
        SymbolInfoSessionQuote equivalents. When unavailable, return UNKNOWN
        and let order_check probe become authoritative.
        """
        # Try plausible Python binding names without assuming they exist.
        trade_fn = getattr(mt5, "symbol_info_session_trade", None)
        quote_fn = getattr(mt5, "symbol_info_session_quote", None)

        if not callable(trade_fn) and not callable(quote_fn):
            return {
                "available": False,
                "session": "UNKNOWN",
                "source": "NO_SESSION_API",
                "details": None,
            }

        now_dt = datetime.now()
        weekday = now_dt.weekday()  # Monday=0
        seconds_now = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second

        sessions = []
        fn = trade_fn if callable(trade_fn) else quote_fn

        # Most session APIs index sessions from 0 upward until failure/None.
        for idx in range(32):
            try:
                item = fn(symbol, weekday, idx)
            except Exception:
                break
            if not item:
                break
            sessions.append(item)

        if not sessions:
            return {
                "available": True,
                "session": "UNKNOWN",
                "source": "MT5_SESSION_API",
                "details": [],
            }

        def to_seconds(v):
            # Cope with datetime/time-like/session integer formats.
            if hasattr(v, "hour"):
                return int(v.hour) * 3600 + int(v.minute) * 60 + int(getattr(v, "second", 0))
            try:
                iv = int(v)
                # Some APIs may represent seconds since midnight directly.
                if 0 <= iv < 86400:
                    return iv
            except Exception:
                pass
            return None

        normalized = []
        is_open = False
        for item in sessions:
            try:
                start, end = item[0], item[1]
            except Exception:
                continue
            a, b = to_seconds(start), to_seconds(end)
            if a is None or b is None:
                continue
            normalized.append((a, b))
            if a <= b:
                active = a <= seconds_now <= b
            else:
                # session crossing midnight
                active = seconds_now >= a or seconds_now <= b
            if active:
                is_open = True

        if normalized:
            return {
                "available": True,
                "session": "OPEN" if is_open else "CLOSED",
                "source": "MT5_SESSION_API",
                "details": normalized,
            }

        return {
            "available": True,
            "session": "UNKNOWN",
            "source": "MT5_SESSION_API",
            "details": None,
        }

    def _order_check_session_probe(self, symbol):
        """Probe broker execution state without sending an order."""
        info = self.symbol_info(symbol)
        tick = self.tick(symbol)

        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            return {
                "session": "UNKNOWN",
                "source": "ORDER_CHECK",
                "retcode": None,
                "retcode_name": "NO_QUOTE",
                "comment": "No valid bid/ask for probe",
            }

        volume = float(getattr(info, "volume_min", 0.01) or 0.01)
        volume = self.normalize_volume(symbol, volume)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY,
            "price": ask,
            "deviation": self.s.deviation_points,
            "magic": self.s.magic,
            "comment": "AI_AGENT_SESSION_PROBE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.filling(symbol),
        }

        try:
            check = mt5.order_check(req)
        except Exception as e:
            return {
                "session": "UNKNOWN",
                "source": "ORDER_CHECK",
                "retcode": None,
                "retcode_name": "EXCEPTION",
                "comment": str(e),
            }

        if check is None:
            return {
                "session": "UNKNOWN",
                "source": "ORDER_CHECK",
                "retcode": None,
                "retcode_name": "NONE",
                "comment": str(mt5.last_error()),
            }

        code = int(getattr(check, "retcode", 0) or 0)
        name = retcode_name(code)
        comment = str(getattr(check, "comment", "") or "")

        # order_check often returns retcode 0 on a valid request.
        if code in (0, getattr(mt5, "TRADE_RETCODE_DONE", 10009)):
            session = "OPEN"
        elif code == getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018) or code == 10018:
            session = "CLOSED"
        elif code in (10044,):  # CLOSE_ONLY is a permission issue, not session proof
            session = "UNKNOWN"
        else:
            # INVALID_VOLUME, INVALID_FILL, NO_MONEY, etc. do not prove closed market.
            session = "UNKNOWN"

        return {
            "session": session,
            "source": "ORDER_CHECK",
            "retcode": code,
            "retcode_name": name,
            "comment": comment,
        }

    def market_status(self, symbol):
        info = self.symbol_info(symbol)
        tick = self.tick(symbol)

        permission = trade_mode_name(int(getattr(info, "trade_mode", -1)))
        tick_time = int(getattr(tick, "time", 0) or 0)
        now = int(time.time())
        stale_seconds = max(0, now - tick_time) if tick_time else 999999

        quote_status = (
            "FRESH" if tick_time and stale_seconds <= 120
            else ("STALE" if tick_time else "NO_QUOTE")
        )

        # 1) Explicit MT5 session API if the installed binding exposes it.
        session_api = self._session_api_probe(symbol)

        if session_api["session"] in {"OPEN", "CLOSED"}:
            session = session_api["session"]
            session_source = session_api["source"]
            probe = None
        else:
            # 2) Non-invasive broker-side order_check probe.
            probe = self._order_check_session_probe(symbol)
            if probe["session"] in {"OPEN", "CLOSED"}:
                session = probe["session"]
                session_source = probe["source"]
            else:
                # 3) Quote freshness is only a fallback hint, never a hard CLOSED claim.
                if quote_status == "FRESH":
                    session = "LIKELY_OPEN"
                elif quote_status == "STALE":
                    session = "UNKNOWN_STALE"
                else:
                    session = "UNKNOWN"
                session_source = "QUOTE_FRESHNESS_FALLBACK"

        if permission == "DISABLED":
            overall = "NOT_TRADEABLE"
        elif permission == "CLOSE_ONLY":
            overall = "CLOSE_ONLY"
        elif permission == "FULL" and session in {"OPEN", "LIKELY_OPEN"} and quote_status == "FRESH":
            overall = "TRADEABLE"
        elif permission == "FULL" and session == "CLOSED":
            overall = "MARKET_CLOSED"
        elif permission == "FULL":
            overall = "WAITING_SESSION"
        else:
            overall = permission

        return {
            "trade_mode": permission,
            "session": session,
            "session_source": session_source,
            "session_api": session_api,
            "probe": probe,
            "quote_status": quote_status,
            "overall": overall,
            "tick_time": tick_time,
            "stale_seconds": stale_seconds,
            "bid": float(getattr(tick, "bid", 0.0) or 0.0),
            "ask": float(getattr(tick, "ask", 0.0) or 0.0),
        }

    def position_snapshot(self):
        out=[]
        for p in self.positions():
            side="BUY" if p.type==mt5.POSITION_TYPE_BUY else "SELL"
            out.append({
                "ticket":int(p.ticket),
                "symbol":p.symbol,
                "side":side,
                "volume":float(p.volume),
                "price_open":float(p.price_open),
                "price_current":float(p.price_current),
                "sl":float(p.sl),
                "tp":float(p.tp),
                "profit":float(p.profit),
                "swap":float(getattr(p,"swap",0.0)),
            })
        return out

    def close_all(self):
        out = []
        for p in list(self.positions()):
            t = self.tick(p.symbol)
            is_buy = p.type == mt5.POSITION_TYPE_BUY
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position": p.ticket,
                "price": t.bid if is_buy else t.ask,
                "deviation": self.s.deviation_points,
                "magic": self.s.magic,
                "comment": "AI_AGENT_V2_CLOSE",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self.filling(p.symbol),
            }
            out.append((p.ticket, mt5.order_send(req)))
        return out

def add_indicators(df):
    x = df.copy()
    c = x["close"]

    x["ema20"] = c.ewm(span=20, adjust=False).mean()
    x["ema50"] = c.ewm(span=50, adjust=False).mean()
    x["ema200"] = c.ewm(span=200, adjust=False).mean()

    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    x["rsi14"] = 100 - 100/(1+rs)

    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    x["macd"] = e12-e26
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    x["macd_hist"] = x["macd"] - x["macd_signal"]

    pc = c.shift(1)
    tr = pd.concat([(x.high-x.low).abs(), (x.high-pc).abs(), (x.low-pc).abs()], axis=1).max(axis=1)
    x["atr14"] = tr.ewm(alpha=1/14, adjust=False).mean()

    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    x["bb_mid"], x["bb_upper"], x["bb_lower"] = mid, mid+2*sd, mid-2*sd

    upmove = x.high.diff()
    downmove = -x.low.diff()
    pdm = upmove.where((upmove > downmove) & (upmove > 0), 0.0)
    mdm = downmove.where((downmove > upmove) & (downmove > 0), 0.0)
    atr = x["atr14"].replace(0, np.nan)
    pdi = 100 * pdm.ewm(alpha=1/14, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=1/14, adjust=False).mean() / atr
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan)
    x["adx14"] = dx.ewm(alpha=1/14, adjust=False).mean()
    x["plus_di"], x["minus_di"] = pdi, mdi

    lo = x.low.rolling(14).min()
    hi = x.high.rolling(14).max()
    x["stoch_k"] = 100*(c-lo)/(hi-lo).replace(0,np.nan)
    x["stoch_d"] = x["stoch_k"].rolling(3).mean()
    x["ret1"] = c.pct_change()
    x["volume_ratio"] = x.tick_volume / x.tick_volume.rolling(20).mean().replace(0,np.nan)

    # Structure helpers
    x["swing_high_20"] = x["high"].rolling(20).max().shift(1)
    x["swing_low_20"] = x["low"].rolling(20).min().shift(1)
    x["atr_pct"] = x["atr14"] / c.replace(0, np.nan)
    return x

def snapshot(df):
    x = add_indicators(df)
    r = x.iloc[-2]
    prev = x.iloc[-3]

    def val(k):
        v = r[k]
        return float(v) if pd.notna(v) else 0.0

    close = val("close")
    ema50, ema200 = val("ema50"), val("ema200")
    trend = "BULL" if close > ema50 > ema200 else ("BEAR" if close < ema50 < ema200 else "MIXED")

    # Market regime: simple deterministic classifier
    adx = val("adx14")
    atr_pct = val("atr_pct")
    if adx >= 25:
        regime = "TRENDING"
    elif adx < 18:
        regime = "RANGING"
    else:
        regime = "TRANSITION"
    if atr_pct > 0.012:
        regime += "_HIGH_VOL"
    elif atr_pct < 0.003:
        regime += "_LOW_VOL"

    sh, sl = val("swing_high_20"), val("swing_low_20")
    if close > sh and sh > 0:
        structure = "BREAKOUT_UP"
    elif close < sl and sl > 0:
        structure = "BREAKOUT_DOWN"
    else:
        prev_close = float(prev["close"])
        if close > ema50 and close > prev_close:
            structure = "BULLISH_STRUCTURE"
        elif close < ema50 and close < prev_close:
            structure = "BEARISH_STRUCTURE"
        else:
            structure = "NEUTRAL"

    keys = ["open","high","low","close","ema20","ema50","ema200","rsi14","macd","macd_signal",
            "macd_hist","atr14","bb_mid","bb_upper","bb_lower","adx14","plus_di","minus_di",
            "stoch_k","stoch_d","ret1","volume_ratio","swing_high_20","swing_low_20","atr_pct"]
    out = {k: val(k) for k in keys}
    out.update({"time": str(r["time"]), "trend": trend, "regime": regime, "structure": structure})
    return out

def technical_score(base):
    # 0..1 directional conviction plus suggested side
    bull = 0.0
    bear = 0.0

    # Trend
    if base["close"] > base["ema20"] > base["ema50"]:
        bull += 2.0
    if base["close"] < base["ema20"] < base["ema50"]:
        bear += 2.0
    if base["close"] > base["ema200"]:
        bull += 1.0
    if base["close"] < base["ema200"]:
        bear += 1.0

    # Momentum
    if base["macd_hist"] > 0:
        bull += 1.0
    elif base["macd_hist"] < 0:
        bear += 1.0
    if 52 <= base["rsi14"] <= 70:
        bull += 1.0
    if 30 <= base["rsi14"] <= 48:
        bear += 1.0
    if base["plus_di"] > base["minus_di"]:
        bull += 1.0
    elif base["minus_di"] > base["plus_di"]:
        bear += 1.0

    # Structure
    if "UP" in base["structure"] or "BULLISH" in base["structure"]:
        bull += 1.5
    if "DOWN" in base["structure"] or "BEARISH" in base["structure"]:
        bear += 1.5

    total = bull + bear
    if total == 0:
        return "HOLD", 0.50
    side = "BUY" if bull > bear else ("SELL" if bear > bull else "HOLD")
    conviction = max(bull, bear) / max(total, 1e-9)
    return side, max(0.50, min(0.95, conviction))

class Memory:
    def __init__(self):
        data_dir = Path(__file__).resolve().parent.parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.cx = sqlite3.connect(data_dir / "trading.db", check_same_thread=False)
        self.cx.row_factory = sqlite3.Row

        self.cx.execute("""CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, symbol TEXT, timeframe TEXT, candle_time TEXT,
            action TEXT, confidence REAL, reason TEXT,
            features TEXT, order_ticket INTEGER,
            technical_score REAL, memory_score REAL, final_score REAL,
            regime TEXT, structure TEXT
        )""")
        self.cx.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER UNIQUE,
            symbol TEXT, timeframe TEXT, side TEXT,
            opened_at TEXT, closed_at TEXT,
            pnl REAL, result TEXT, close_reason TEXT,
            features TEXT, lesson TEXT, regime TEXT, structure TEXT
        )""")
        self.cx.execute("""CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, symbol TEXT, timeframe TEXT, side TEXT,
            entry REAL, sl REAL, tp REAL, rr REAL, final_score REAL,
            mode TEXT, regime TEXT, structure TEXT,
            status TEXT DEFAULT 'OPEN', closed_at TEXT, exit_price REAL,
            pnl_points REAL, result TEXT, close_reason TEXT, features TEXT
        )""")
        self.cx.commit()
        self._migrate()

    def _migrate(self):
        # allows using an old V1/V1.2 database without crashing
        for table, columns in {
            "decisions": {
                "technical_score":"REAL","memory_score":"REAL","final_score":"REAL",
                "regime":"TEXT","structure":"TEXT"
            },
            "trades": {
                "timeframe":"TEXT","result":"TEXT","close_reason":"TEXT",
                "regime":"TEXT","structure":"TEXT"
            }
        }.items():
            existing = {r[1] for r in self.cx.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, typ in columns.items():
                if name not in existing:
                    self.cx.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        self.cx.commit()

    def save_decision(self, symbol, tf, candle, action, conf, reason, features, ticket=None,
                      technical_score=None, memory_score=None, final_score=None):
        self.cx.execute("""INSERT INTO decisions
            (ts,symbol,timeframe,candle_time,action,confidence,reason,features,order_ticket,
             technical_score,memory_score,final_score,regime,structure)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), symbol, tf, candle, action, conf, reason,
             json.dumps(features), ticket, technical_score, memory_score, final_score,
             features.get("regime"), features.get("structure")))
        self.cx.commit()

    def similar(self, symbol, timeframe, feat, limit=20):
        rows = self.cx.execute(
            "SELECT * FROM trades WHERE symbol=? AND timeframe=? ORDER BY id DESC LIMIT 1000",
            (symbol, timeframe)
        ).fetchall()
        keys = ["rsi14","macd_hist","adx14","stoch_k","volume_ratio","ret1","atr_pct"]
        scales = {"rsi14":20,"macd_hist":1,"adx14":20,"stoch_k":25,"volume_ratio":1,"ret1":0.01,"atr_pct":0.01}
        scored=[]
        for r in rows:
            try:
                old=json.loads(r["features"] or "{}")
                d=0.0
                for k in keys:
                    a,b=float(feat.get(k,0)),float(old.get(k,0))
                    sc=max(scales[k],abs(a)*0.2,abs(b)*0.2,1e-9)
                    d += ((a-b)/sc)**2
                if old.get("regime") == feat.get("regime"):
                    d *= 0.85
                if old.get("structure") == feat.get("structure"):
                    d *= 0.90
                scored.append((math.sqrt(d), dict(r)))
            except Exception:
                pass
        scored.sort(key=lambda z:z[0])
        return [r for _,r in scored[:limit]]

    def similar_stats(self, rows, proposed_side):
        if not rows:
            return {
                "count":0,"same_side_count":0,"same_side_win_rate":0.5,
                "avg_pnl":0.0,"memory_score":0.5
            }
        same = [r for r in rows if r.get("side") == proposed_side]
        if not same:
            return {
                "count":len(rows),"same_side_count":0,"same_side_win_rate":0.5,
                "avg_pnl":0.0,"memory_score":0.5
            }
        wins = sum(1 for r in same if float(r.get("pnl") or 0) > 0)
        wr = wins / len(same)
        avg = sum(float(r.get("pnl") or 0) for r in same) / len(same)
        return {
            "count":len(rows),"same_side_count":len(same),
            "same_side_win_rate":wr,"avg_pnl":avg,
            "memory_score":max(0.05,min(0.95,wr))
        }

    def save_shadow_trade(self,symbol,timeframe,side,entry,sl,tp,rr,final_score,mode,features):
        self.cx.execute("""INSERT INTO shadow_trades
            (created_at,symbol,timeframe,side,entry,sl,tp,rr,final_score,mode,
             regime,structure,status,features) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(),str(symbol),str(timeframe),str(side),
             float(entry),float(sl),float(tp),float(rr),float(final_score),str(mode),
             str((features or {}).get("regime","")),str((features or {}).get("structure","")),
             "OPEN",json.dumps(features or {})))
        self.cx.commit()
        return int(self.cx.execute("SELECT last_insert_rowid()").fetchone()[0])

    def open_shadow_trades(self):
        return [dict(r) for r in self.cx.execute(
            "SELECT * FROM shadow_trades WHERE status='OPEN' ORDER BY id").fetchall()]

    def close_shadow_trade(self,shadow_id,exit_price,result,reason,pnl_points):
        self.cx.execute("""UPDATE shadow_trades SET status='CLOSED',closed_at=?,
            exit_price=?,pnl_points=?,result=?,close_reason=? WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(),float(exit_price),float(pnl_points),
             str(result),str(reason),int(shadow_id)))
        self.cx.commit()

    def shadow_stats(self):
        rows=[dict(r) for r in self.cx.execute(
            "SELECT * FROM shadow_trades WHERE status='CLOSED' ORDER BY id DESC LIMIT 500").fetchall()]
        n=len(rows);wins=sum(1 for r in rows if r.get("result")=="WIN")
        losses=sum(1 for r in rows if r.get("result")=="LOSS")
        return {"total":n,"wins":wins,"losses":losses,
                "win_rate":wins/n if n else 0.0,"open":len(self.open_shadow_trades())}

    def sync_trade_from_mt5(self, trade_obj):
        """Upsert one completed broker trade into local learning DB."""
        self.upsert_trade(
            int(trade_obj["position_id"]),
            str(trade_obj["symbol"]),
            str(trade_obj.get("timeframe") or "UNKNOWN"),
            str(trade_obj["side"]),
            str(trade_obj["opened_at"]),
            str(trade_obj["closed_at"]),
            float(trade_obj["pnl_raw"]),
            trade_obj.get("features") or {},
            str(trade_obj.get("lesson") or ""),
            str(trade_obj.get("close_reason") or "UNKNOWN"),
        )

    def trade_history(self, limit=500, symbol=None, result=None):
        sql = "SELECT * FROM trades"
        args = []
        where = []

        if symbol:
            where.append("symbol=?")
            args.append(symbol)

        if result and result != "ALL":
            if result == "BREAKEVEN":
                # Older rows may use BE while newer UI uses BREAKEVEN.
                where.append("result IN ('BE','BREAKEVEN')")
            else:
                where.append("result=?")
                args.append(result)

        if where:
            sql += " WHERE " + " AND ".join(where)

        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))

        return [dict(r) for r in self.cx.execute(sql, args).fetchall()]

    def delete_trade_history(self):
        self.cx.execute("DELETE FROM trades")
        self.cx.commit()

    def recent_results(self, symbol, timeframe, n=20):
        return [dict(r) for r in self.cx.execute(
            "SELECT * FROM trades WHERE symbol=? AND timeframe=? ORDER BY id DESC LIMIT ?",
            (symbol,timeframe,n)
        ).fetchall()]

    def regime_expectancy(self,symbol,timeframe,side,regime,mode="",limit=300):
        """Historical expectancy specifically inside the current market regime.

        Exact regime matches are preferred. Results are sample-shrunk and never
        create a direction; they are advisory inputs for strategy/risk adaptation.
        """
        symbol=str(symbol or "")
        timeframe=str(timeframe or "").upper()
        side=str(side or "").upper()
        regime=str(regime or "").upper()
        mode=str(mode or "").upper()
        if not regime:
            return {
                "samples":0,"win_rate":0.5,"expectancy":0.0,"score":0.5,
                "sample_confidence":0.0,"grade":"INSUFFICIENT"
            }

        rows=[dict(r) for r in self.cx.execute(
            "SELECT * FROM trades WHERE side=? AND UPPER(COALESCE(regime,''))=? "
            "ORDER BY id DESC LIMIT ?",
            (side,regime,int(limit))
        ).fetchall()]

        ranked=[]
        for r in rows:
            weight=1.0
            if str(r.get("symbol",""))==symbol:
                weight+=2.0
            if str(r.get("timeframe","")).upper()==timeframe:
                weight+=1.5
            try:
                feat=json.loads(r.get("features") or "{}")
            except Exception:
                feat={}
            hist_mode=str(feat.get("effective_mode",feat.get("trading_mode","")) or "").upper()
            if mode and hist_mode==mode:
                weight+=0.75
            ranked.append((weight,int(r.get("id",0) or 0),r))

        ranked.sort(key=lambda x:(x[0],x[1]),reverse=True)
        selected=[x[2] for x in ranked[:50]]
        n=len(selected)
        if not n:
            return {
                "samples":0,"win_rate":0.5,"expectancy":0.0,"score":0.5,
                "sample_confidence":0.0,"grade":"INSUFFICIENT"
            }

        pnl=[float(r.get("pnl",0) or 0) for r in selected]
        wins=[x for x in pnl if x>0]
        losses=[x for x in pnl if x<0]
        wr=len(wins)/n
        avg_win=sum(wins)/len(wins) if wins else 0.0
        avg_loss=abs(sum(losses)/len(losses)) if losses else 0.0
        expectancy=wr*avg_win-(1.0-wr)*avg_loss
        scale=max(avg_win+avg_loss,1e-9)
        raw_score=max(0.0,min(1.0,0.5+0.5*max(-1.0,min(1.0,expectancy/scale))))

        sample_conf=max(0.0,min(1.0,(n-3)/17.0))
        score=0.5+(raw_score-0.5)*sample_conf
        if n<5:
            grade="INSUFFICIENT"
        elif score>=0.65:
            grade="STRONG"
        elif score>=0.55:
            grade="POSITIVE"
        elif score<=0.35:
            grade="POOR"
        elif score<=0.45:
            grade="WEAK"
        else:
            grade="NEUTRAL"

        return {
            "samples":n,"wins":len(wins),"losses":len(losses),
            "win_rate":wr,"avg_win":avg_win,"avg_loss":avg_loss,
            "expectancy":expectancy,"score":score,
            "sample_confidence":sample_conf,"grade":grade,
        }

    def expectancy_profile(self, symbol, timeframe, side, regime="", structure="", mode="", limit=300):
        """Historical setup expectancy with sample-size shrinkage.

        This never creates BUY/SELL signals. It only describes how similar
        completed bot trades have performed so the deterministic risk layer can
        slightly de-risk weak historical setups or cautiously reward robust ones.
        """
        symbol=str(symbol or "")
        timeframe=str(timeframe or "").upper()
        side=str(side or "").upper()
        regime=str(regime or "").upper()
        structure=str(structure or "").upper()
        mode=str(mode or "").upper()

        rows=[dict(r) for r in self.cx.execute(
            "SELECT * FROM trades WHERE side=? ORDER BY id DESC LIMIT ?",
            (side,int(limit))
        ).fetchall()]

        scored=[]
        for r in rows:
            try:
                score=0.0
                if str(r.get("symbol",""))==symbol: score+=4.0
                if str(r.get("timeframe","")).upper()==timeframe: score+=3.0
                rr_regime=str(r.get("regime","") or "").upper()
                rr_structure=str(r.get("structure","") or "").upper()
                if regime and rr_regime==regime: score+=1.5
                if structure and rr_structure==structure: score+=1.5

                feat={}
                try:
                    feat=json.loads(r.get("features") or "{}")
                except Exception:
                    feat={}
                hist_mode=str(feat.get("effective_mode",feat.get("trading_mode","")) or "").upper()
                if mode and hist_mode==mode:
                    score+=1.0

                # Require at least same symbol OR same timeframe to avoid mixing
                # unrelated historical conditions.
                if score>=3.0:
                    scored.append((score,int(r.get("id",0) or 0),r))
            except Exception:
                continue

        scored.sort(key=lambda x:(x[0],x[1]),reverse=True)
        selected=[x[2] for x in scored[:60]]
        n=len(selected)
        if not n:
            return {
                "samples":0,"wins":0,"losses":0,"win_rate":0.5,
                "avg_win":0.0,"avg_loss":0.0,"expectancy":0.0,
                "score":0.5,"sample_confidence":0.0,"risk_multiplier":1.0,
                "grade":"INSUFFICIENT"
            }

        pnl=[float(r.get("pnl",0) or 0) for r in selected]
        wins=[x for x in pnl if x>0]
        losses=[x for x in pnl if x<0]
        wr=len(wins)/n
        avg_win=(sum(wins)/len(wins)) if wins else 0.0
        avg_loss=abs(sum(losses)/len(losses)) if losses else 0.0
        expectancy=wr*avg_win-(1.0-wr)*avg_loss

        scale=max(avg_win+avg_loss,1e-9)
        normalized=max(-1.0,min(1.0,expectancy/scale))
        raw_score=max(0.0,min(1.0,0.5+0.5*normalized))

        # Shrink tiny samples strongly toward neutral.
        sample_conf=max(0.0,min(1.0,(n-3)/17.0))
        score=0.5+(raw_score-0.5)*sample_conf

        # Historical learning modifies risk only modestly. Never martingale.
        # Poor expectancy can reduce risk to 75%; strong/robust expectancy can
        # increase at most 10%, still bounded by existing dynamic risk limits.
        risk_multiplier=1.0
        if n>=5:
            risk_multiplier=max(0.75,min(1.10,0.85+0.50*score))
        if n<8:
            risk_multiplier=1.0+(risk_multiplier-1.0)*0.50

        if n<5:
            grade="INSUFFICIENT"
        elif score>=0.65:
            grade="STRONG"
        elif score>=0.55:
            grade="POSITIVE"
        elif score<=0.35:
            grade="POOR"
        elif score<=0.45:
            grade="WEAK"
        else:
            grade="NEUTRAL"

        return {
            "samples":n,"wins":len(wins),"losses":len(losses),
            "win_rate":wr,"avg_win":avg_win,"avg_loss":avg_loss,
            "expectancy":expectancy,"score":score,
            "sample_confidence":sample_conf,
            "risk_multiplier":risk_multiplier,"grade":grade
        }

    def consensus_calibration(self, symbol="", timeframe="", side="", limit=300):
        """Learn whether past Council consensus grades were actually predictive.

        Uses only completed bot trades already stored in SQLite. Small samples are
        strongly shrunk toward neutral and can only modestly reduce/increase risk.
        """
        rows=[dict(r) for r in self.cx.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?",(int(limit),)
        ).fetchall()]
        buckets={"VERY_HIGH":[],"HIGH":[],"MODERATE":[],"MIXED":[],"LOW":[],"CONFLICT":[]}
        matched=[]
        for r in rows:
            try:
                feat=json.loads(r.get("features") or "{}")
            except Exception:
                feat={}
            grade=str(feat.get("council_consensus_grade","") or "").upper()
            if grade not in buckets:
                continue
            pnl=float(r.get("pnl",0) or 0)
            buckets[grade].append(pnl)

            similarity=0
            if symbol and str(r.get("symbol",""))==str(symbol): similarity+=3
            if timeframe and str(r.get("timeframe","")).upper()==str(timeframe).upper(): similarity+=2
            if side and str(r.get("side","")).upper()==str(side).upper(): similarity+=2
            if similarity>=4:
                matched.append((similarity,int(r.get("id",0) or 0),grade,pnl))

        def summarize(vals):
            n=len(vals)
            if not n:
                return {"samples":0,"win_rate":0.5,"pnl":0.0,"score":0.5}
            wins=sum(1 for x in vals if x>0)
            wr=wins/n
            total=sum(vals)
            # Sample shrinkage prevents tiny histories from changing risk much.
            sample_conf=max(0.0,min(1.0,(n-4)/26.0))
            score=0.5+(wr-0.5)*sample_conf
            return {"samples":n,"win_rate":wr,"pnl":total,"score":score}

        bucket_stats={k:summarize(v) for k,v in buckets.items()}
        matched.sort(key=lambda x:(x[0],x[1]),reverse=True)
        local=[x[3] for x in matched[:60]]
        local_stats=summarize(local)

        # Calibration quality asks: do high-consensus buckets outperform low ones?
        hi=buckets["VERY_HIGH"]+buckets["HIGH"]
        lo=buckets["LOW"]+buckets["CONFLICT"]
        hi_s=summarize(hi); lo_s=summarize(lo)
        evidence_n=hi_s["samples"]+lo_s["samples"]
        if evidence_n>=10:
            separation=hi_s["win_rate"]-lo_s["win_rate"]
            sep_conf=max(0.0,min(1.0,(evidence_n-8)/32.0))
            calibration_score=max(0.0,min(1.0,0.5+separation*sep_conf))
        else:
            separation=0.0
            calibration_score=0.5

        if evidence_n<10:
            grade="LEARNING"
        elif calibration_score>=0.62:
            grade="CALIBRATED"
        elif calibration_score<=0.38:
            grade="INVERTED"
        else:
            grade="NEUTRAL"

        # This layer is intentionally asymmetric: suspicious calibration de-risks
        # more than good calibration can increase exposure.
        risk_multiplier=max(0.85,min(1.03,0.70+0.60*calibration_score))
        if evidence_n<10:
            risk_multiplier=1.0

        return {
            "grade":grade,
            "score":calibration_score,
            "risk_multiplier":risk_multiplier,
            "evidence_samples":evidence_n,
            "high_samples":hi_s["samples"],
            "high_win_rate":hi_s["win_rate"],
            "low_samples":lo_s["samples"],
            "low_win_rate":lo_s["win_rate"],
            "separation":separation,
            "local":local_stats,
            "buckets":bucket_stats,
        }

    def stats(self, symbol=None, timeframe=None):
        sql="SELECT * FROM trades"
        args=[]
        where=[]
        if symbol:
            where.append("symbol=?"); args.append(symbol)
        if timeframe:
            where.append("timeframe=?"); args.append(timeframe)
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows=[dict(r) for r in self.cx.execute(sql,args).fetchall()]
        total=len(rows)
        if not total:
            return {"total":0,"wins":0,"losses":0,"win_rate":0.0,"profit_factor":0.0,"pnl":0.0,"consecutive_losses":0}
        wins=[r for r in rows if float(r.get("pnl") or 0)>0]
        losses=[r for r in rows if float(r.get("pnl") or 0)<0]
        gp=sum(float(r["pnl"]) for r in wins)
        gl=abs(sum(float(r["pnl"]) for r in losses))
        ordered=sorted(rows,key=lambda r:r["id"], reverse=True)
        streak=0
        for r in ordered:
            if float(r.get("pnl") or 0)<0: streak+=1
            else: break
        return {
            "total":total,"wins":len(wins),"losses":len(losses),
            "win_rate":len(wins)/total if total else 0.0,
            "profit_factor": gp/gl if gl>0 else (999.0 if gp>0 else 0.0),
            "pnl":sum(float(r.get("pnl") or 0) for r in rows),
            "consecutive_losses":streak
        }

    def upsert_trade(self, position_id, symbol, timeframe, side, opened_at, closed_at, pnl,
                     features, lesson, close_reason="UNKNOWN"):
        result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE")
        self.cx.execute("""INSERT OR IGNORE INTO trades
            (position_id,symbol,timeframe,side,opened_at,closed_at,pnl,result,close_reason,
             features,lesson,regime,structure)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (position_id,symbol,timeframe,side,opened_at,closed_at,pnl,result,close_reason,
             json.dumps(features),lesson,features.get("regime"),features.get("structure")))
        self.cx.commit()

class LocalLLM:

    def _extract_json_object(self, text):
        if text is None:
            raise ValueError("LLM returned empty response")
        s=str(text).strip()
        if not s:
            raise ValueError("LLM returned empty response")

        s=s.replace("```json","").replace("```JSON","").replace("```","").strip()
        start=s.find("{")
        if start < 0:
            raise ValueError("LLM returned no JSON object")

        depth=0
        in_string=False
        escape=False
        for i in range(start,len(s)):
            ch=s[i]
            if in_string:
                if escape:
                    escape=False
                elif ch=="\\":
                    escape=True
                elif ch=='"':
                    in_string=False
                continue
            if ch=='"':
                in_string=True
                continue
            if ch=="{":
                depth+=1
            elif ch=="}":
                depth-=1
                if depth==0:
                    return json.loads(s[start:i+1])

        # V3.10.12: Council/local models sometimes stop after valid fields but
        # before the final quote/brace. Try conservative structural repair first.
        try:
            return self._repair_partial_json_object(s[start:])
        except Exception:
            raise ValueError("LLM JSON object is incomplete")

    def _repair_partial_json_object(self, text):
        """Best-effort repair for a truncated top-level JSON object.

        Intended for short Council outputs where Ollama stopped after useful
        action/confidence fields but before the final quote/brace. It never
        invents trading fields; it only closes an unfinished JSON container or
        removes an incomplete trailing key/value fragment.
        """
        s=str(text or "").strip()
        s=s.replace("```json","").replace("```JSON","").replace("```","").strip()
        start=s.find("{")
        if start < 0:
            raise ValueError("no JSON object start")
        s=s[start:]

        # Track strings and containers.
        stack=[]
        in_string=False
        escape=False
        for ch in s:
            if in_string:
                if escape:
                    escape=False
                elif ch=="\\":
                    escape=True
                elif ch=='"':
                    in_string=False
                continue
            if ch=='"':
                in_string=True
            elif ch in "[{":
                stack.append(ch)
            elif ch=="}" and stack and stack[-1]=="{":
                stack.pop()
            elif ch=="]" and stack and stack[-1]=="[":
                stack.pop()

        candidate=s.rstrip()

        # If generation stopped inside a quoted string, close only that string.
        if in_string:
            # Do not end with a dangling escape before adding the quote.
            if candidate.endswith("\\") and not candidate.endswith("\\\\"):
                candidate=candidate[:-1]
            candidate+='"'

        # If it ended on punctuation that cannot complete a value, trim it.
        candidate=re.sub(r'[,:\s]+$','',candidate)

        # Close remaining arrays/objects in reverse order.
        # Recompute stack after the quote/punctuation repair.
        stack=[]
        in_string=False
        escape=False
        for ch in candidate:
            if in_string:
                if escape:
                    escape=False
                elif ch=="\\":
                    escape=True
                elif ch=='"':
                    in_string=False
                continue
            if ch=='"':
                in_string=True
            elif ch in "[{":
                stack.append(ch)
            elif ch=="}" and stack and stack[-1]=="{":
                stack.pop()
            elif ch=="]" and stack and stack[-1]=="[":
                stack.pop()

        candidate += "".join("}" if x=="{" else "]" for x in reversed(stack))
        obj=json.loads(candidate)
        if not isinstance(obj,dict):
            raise ValueError("repaired JSON is not an object")
        obj["_partial_json_repaired"]=True
        return obj

    def __init__(self,s):
        self.s=s
        self.empty_until=0.0
        self.last_health_status="UNKNOWN"
        self.empty_streak=0
        self.degraded_until=0.0

        # V3.10.14: persistent Council telemetry used by Smart Model Routing.
        self._council_metrics_path=Path.cwd()/"data"/"ai_council_metrics.json"
        self._council_metrics={}
        self._council_route_cooldown={}
        self._council_breakers={}
        self._council_breaker_failures=2
        self._council_breaker_cooldown=600
        try:
            self._council_metrics_path.parent.mkdir(parents=True,exist_ok=True)
            if self._council_metrics_path.exists():
                obj=json.loads(self._council_metrics_path.read_text(encoding="utf-8"))
                if isinstance(obj,dict):
                    self._council_metrics=obj
        except Exception:
            self._council_metrics={}

    @staticmethod
    def _council_metric_key(role,model):
        return f"{str(role).upper()}|{str(model)}"

    def _save_council_metrics(self):
        try:
            self._council_metrics_path.parent.mkdir(parents=True,exist_ok=True)
            self._council_metrics_path.write_text(
                json.dumps(self._council_metrics,indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def _record_council_metric(self,role,model,result):
        key=self._council_metric_key(role,model)
        row=dict(self._council_metrics.get(key) or {})
        row["role"]=str(role).upper()
        row["model"]=str(model)
        row["calls"]=int(row.get("calls",0) or 0)+1
        elapsed=max(0.0,float((result or {}).get("_elapsed",0) or 0))
        row["total_elapsed"]=float(row.get("total_elapsed",0) or 0)+elapsed
        row["ok"]=int(row.get("ok",0) or 0)+(1 if bool((result or {}).get("_ok")) else 0)
        row["fail"]=int(row.get("fail",0) or 0)+(0 if bool((result or {}).get("_ok")) else 1)
        row["abstain"]=int(row.get("abstain",0) or 0)+(1 if bool((result or {}).get("_abstain")) else 0)
        row["json_repair"]=int(row.get("json_repair",0) or 0)+(1 if bool((result or {}).get("_partial_json_repaired")) else 0)
        reason=str((result or {}).get("reason","") or "").lower()
        timeout=("timeout" in reason or "timed out" in reason)
        row["timeout"]=int(row.get("timeout",0) or 0)+(1 if timeout else 0)
        row["last_elapsed"]=elapsed
        row["last_ok"]=bool((result or {}).get("_ok"))
        row["last_reason"]=str((result or {}).get("reason","") or "")[:180]
        row["updated_at"]=time.time()

        # Rolling window for routing decisions (last 12 calls).
        recent=list(row.get("recent") or [])
        recent.append({
            "ok":bool((result or {}).get("_ok")),
            "abstain":bool((result or {}).get("_abstain")),
            "timeout":bool(timeout),
            "elapsed":elapsed,
            "ts":time.time(),
        })
        row["recent"]=recent[-12:]
        self._council_metrics[key]=row
        self._save_council_metrics()

    def council_performance_snapshot(self):
        out=[]
        for row in self._council_metrics.values():
            if not isinstance(row,dict):
                continue
            calls=max(1,int(row.get("calls",0) or 0))
            recent=list(row.get("recent") or [])
            recent_n=max(1,len(recent))
            out.append({
                "role":str(row.get("role","")),
                "model":str(row.get("model","")),
                "calls":int(row.get("calls",0) or 0),
                "avg_elapsed":float(row.get("total_elapsed",0) or 0)/calls,
                "timeout_rate":sum(1 for x in recent if x.get("timeout"))/recent_n if recent else 0.0,
                "abstain_rate":sum(1 for x in recent if x.get("abstain"))/recent_n if recent else 0.0,
                "fail_rate":sum(1 for x in recent if not x.get("ok"))/recent_n if recent else 0.0,
                "repair_rate":int(row.get("json_repair",0) or 0)/calls,
                "last_elapsed":float(row.get("last_elapsed",0) or 0),
            })
        return sorted(out,key=lambda x:(x["role"],x["model"]))

    def _council_breaker_key(self,role,model):
        return self._council_metric_key(role,model)

    def council_breaker_status(self,role,model):
        key=self._council_breaker_key(role,model)
        row=dict(self._council_breakers.get(key) or {})
        now=time.time()
        opened_until=float(row.get("opened_until",0) or 0)
        if opened_until>now:
            return "OPEN",int(opened_until-now)
        if opened_until>0:
            if not bool(row.get("probe_inflight",False)):
                row["probe_inflight"]=True
                self._council_breakers[key]=row
                return "HALF_OPEN",0
            return "OPEN",1
        return "CLOSED",0

    def _update_council_breaker(self,role,model,result):
        key=self._council_breaker_key(role,model)
        row=dict(self._council_breakers.get(key) or {})
        ok=bool((result or {}).get("_ok")) and not bool((result or {}).get("_abstain",False))
        reason=str((result or {}).get("reason","") or "").lower()
        infra_failure=(not ok and any(x in reason for x in (
            "timeout","timed out","connection","incomplete","empty response"
        )))
        if ok:
            row={"consecutive_failures":0,"opened_until":0.0,"probe_inflight":False,
                 "last_state":"CLOSED","last_success":time.time()}
        elif infra_failure:
            fails=int(row.get("consecutive_failures",0) or 0)+1
            row.update({"consecutive_failures":fails,"probe_inflight":False,
                        "last_failure":time.time()})
            if fails>=self._council_breaker_failures:
                row["opened_until"]=time.time()+self._council_breaker_cooldown
                row["last_state"]="OPEN"
            else:
                row["last_state"]="CLOSED"
        else:
            row["probe_inflight"]=False
        self._council_breakers[key]=row
        return dict(row)

    def _breaker_abstain(self,role,model,remaining):
        return {
            "_ok":False,"_abstain":True,"_confidence_valid":False,
            "_elapsed":0.0,"_model":model,"_role":str(role).upper(),
            "_circuit_open":True,"_circuit_remaining":int(max(0,remaining)),
            "action":"HOLD" if str(role).upper()!="CRITIC" else None,
            "verdict":"REJECT" if str(role).upper()=="CRITIC" else None,
            "confidence":0.0,
            "reason":f"CIRCUIT_OPEN: model temporarily skipped ({int(max(0,remaining))}s cooldown)"
        }

    def smart_route_model(self,role,primary_model,fallback_model=None):
        """Route expensive Council roles away from a recently degraded model."""
        role=str(role).upper()
        primary=str(primary_model)
        fallback=str(fallback_model or "")
        if not fallback or fallback==primary:
            return primary,False,"PRIMARY"

        key=self._council_metric_key(role,primary)
        row=dict(self._council_metrics.get(key) or {})
        recent=list(row.get("recent") or [])[-8:]
        now=time.time()

        # Cooldown after a degraded decision; retry primary after 15 minutes.
        cooldown_until=float(self._council_route_cooldown.get(key,0) or 0)
        if now < cooldown_until:
            return fallback,True,f"DEGRADED_COOLDOWN {int(cooldown_until-now)}s"

        if len(recent)>=3:
            timeout_rate=sum(1 for x in recent if x.get("timeout"))/len(recent)
            fail_rate=sum(1 for x in recent if not x.get("ok"))/len(recent)
            abstain_rate=sum(1 for x in recent if x.get("abstain"))/len(recent)
            avg=sum(float(x.get("elapsed",0) or 0) for x in recent)/len(recent)
            degraded=(
                timeout_rate>=0.34
                or fail_rate>=0.50
                or abstain_rate>=0.60
                or avg>=60.0
            )
            if degraded:
                self._council_route_cooldown[key]=now+900
                return fallback,True,(
                    f"DEGRADED timeout={timeout_rate:.0%} fail={fail_rate:.0%} "
                    f"abstain={abstain_rate:.0%} avg={avg:.1f}s"
                )

        return primary,False,"PRIMARY"

    def ping(self):
        r=requests.get(self.s.ollama_url+"/api/tags",timeout=4)
        r.raise_for_status()

    def installed_models(self):
        r=requests.get(self.s.ollama_url+"/api/tags",timeout=6)
        r.raise_for_status()
        data=r.json() or {}
        return [
            str(x.get("name") or x.get("model") or "").strip()
            for x in (data.get("models") or [])
            if str(x.get("name") or x.get("model") or "").strip()
        ]

    def benchmark_model(self, model, prompt):
        started=time.perf_counter()
        try:
            obj=self._generate_json(
                prompt,
                timeout=max(45,min(int(getattr(self.s,"ollama_timeout",180)),240)),
                temperature=0.0,
                num_predict_override=max(180,int(getattr(self.s,"ollama_schema_num_predict",220))),
                model_override=model,
            )
            elapsed=time.perf_counter()-started
            obj=self._validate_decision_contract(obj)
            return {
                "ok":True,"model":model,"elapsed":elapsed,
                "action":str(obj.get("action","HOLD")).upper(),
                "confidence":float(obj.get("confidence",0.0) or 0.0),
                "trend":str(obj.get("trend","MIXED")),
                "momentum":str(obj.get("momentum","MIXED")),
                "structure":str(obj.get("structure","NEUTRAL")),
                "reason":str(obj.get("reason",""))[:280],
            }
        except Exception as e:
            return {
                "ok":False,"model":model,
                "elapsed":time.perf_counter()-started,
                "error":f"{type(e).__name__}: {e}"[:320],
            }

    def council_call(self, model, role, payload, timeout=None):
        role=str(role).upper().strip()
        state,remaining=self.council_breaker_status(role,model)
        if state=="OPEN":
            return self._breaker_abstain(role,model,remaining)

        # Council roles should not repeat the full market analysis. Keep the
        # response contract small to reduce truncation and local-model latency.
        schemas={
            "SCOUT":'{"action":"BUY|SELL|HOLD","confidence":0.65,"reason":"..."}',
            "TECHNICAL":'{"action":"BUY|SELL|HOLD","confidence":0.65,"structure":"BULLISH|BEARISH|NEUTRAL","conflicts":[],"reason":"..."}',
            "CRITIC":'{"verdict":"APPROVE|REJECT","confidence":0.65,"risks":[],"reason":"..."}',
            "CHIEF":'{"action":"BUY|SELL|HOLD","confidence":0.65,"reason":"..."}',
        }
        jobs={
            "SCOUT":"Fast screening only. Decide whether the supplied snapshot has a directional candidate.",
            "TECHNICAL":"Audit technical direction, structure, Fibonacci/session alignment, and important conflicts.",
            "CRITIC":"Challenge the proposed setup. APPROVE only when the trade is sufficiently coherent; otherwise REJECT.",
            "CHIEF":"Final synthesis only. Use prior Council outputs and supplied evidence. Do not repeat indicator calculations.",
        }
        calibration=(
            " Confidence is confidence in your stated action/verdict. "
            "Do not use 0.00 for a normal HOLD/REJECT. Mixed evidence usually means 0.35-0.60. "
            "Use <=0.01 only if the input is genuinely unusable."
        )

        # Role-specific budgets keep lightweight roles fast and prevent the 9B
        # Chief from occupying the trading loop for several minutes.
        role_timeout={
            "SCOUT":35,
            "TECHNICAL":60,
            "CRITIC":60,
            "CHIEF":75,
        }.get(role,60)
        effective_timeout=min(
            int(timeout) if timeout is not None else role_timeout,
            role_timeout
        )
        role_tokens={
            "SCOUT":110,
            "TECHNICAL":170,
            "CRITIC":150,
            "CHIEF":120,
        }.get(role,150)

        prompt=(
            "You are the "+role+" member of a local AI trading council. "
            +jobs[role]+calibration+
            " Use ONLY supplied data. Never invent facts. Keep reason <= 24 words. "
            "Return ONE compact JSON object only. Schema: "+schemas[role]+
            "\nPAYLOAD:\n"+json.dumps(payload,separators=(",",":"),default=str)
        )
        t0=time.perf_counter()
        try:
            obj=self._generate_json(
                prompt,
                timeout=effective_timeout,
                temperature=0.0,
                num_predict_override=role_tokens,
                model_override=model
            )
            if not isinstance(obj,dict):
                raise ValueError("response is not a JSON object")

            if role=="CRITIC":
                v=str(obj.get("verdict","REJECT")).upper()
                obj["verdict"]=v if v in {"APPROVE","REJECT"} else "REJECT"
            else:
                a=str(obj.get("action","HOLD")).upper()
                obj["action"]=a if a in {"BUY","SELL","HOLD"} else "HOLD"

            try:
                obj["confidence"]=max(0.0,min(1.0,float(obj.get("confidence",0) or 0)))
            except Exception:
                obj["confidence"]=0.0

            # Only retry confidence calibration when the first response was
            # structurally complete/repaired. Keep retry short.
            if float(obj.get("confidence",0) or 0)<=0.01:
                retry_prompt=(
                    prompt+
                    "\nCALIBRATION RETRY: return the same assessment with a meaningful confidence >0.01 "
                    "unless the input is genuinely unusable."
                )
                retry_obj=self._generate_json(
                    retry_prompt,
                    timeout=min(effective_timeout,35),
                    temperature=0.0,
                    num_predict_override=max(90,min(role_tokens,120)),
                    model_override=model
                )
                if isinstance(retry_obj,dict):
                    if role=="CRITIC":
                        rv=str(retry_obj.get("verdict","REJECT")).upper()
                        retry_obj["verdict"]=rv if rv in {"APPROVE","REJECT"} else "REJECT"
                    else:
                        ra=str(retry_obj.get("action","HOLD")).upper()
                        retry_obj["action"]=ra if ra in {"BUY","SELL","HOLD"} else "HOLD"
                    try:
                        retry_obj["confidence"]=max(0.0,min(1.0,float(retry_obj.get("confidence",0) or 0)))
                    except Exception:
                        retry_obj["confidence"]=0.0
                    if float(retry_obj.get("confidence",0) or 0)>float(obj.get("confidence",0) or 0):
                        obj=retry_obj
                        obj["_confidence_retry"]=True

            conf=float(obj.get("confidence",0) or 0)
            obj["_abstain"]=bool(conf<=0.01)
            obj["_confidence_valid"]=bool(conf>0.01)
            obj.update({
                "_ok":True,
                "_elapsed":time.perf_counter()-t0,
                "_model":model,
                "_role":role,
                "_timeout_budget":effective_timeout,
            })
            self._record_council_metric(role,model,obj)
            breaker=self._update_council_breaker(role,model,obj)
            obj["_circuit_state"]=str(breaker.get("last_state","CLOSED"))
            return obj
        except Exception as e:
            failed={
                "_ok":False,"_abstain":True,"_confidence_valid":False,
                "_elapsed":time.perf_counter()-t0,"_model":model,"_role":role,
                "_timeout_budget":effective_timeout,
                "action":"HOLD" if role!="CRITIC" else None,
                "verdict":"REJECT" if role=="CRITIC" else None,
                "confidence":0.0,
                "reason":f"{type(e).__name__}: {e}"[:320]
            }
            self._record_council_metric(role,model,failed)
            breaker=self._update_council_breaker(role,model,failed)
            failed["_circuit_state"]=str(breaker.get("last_state","CLOSED"))
            if failed["_circuit_state"]=="OPEN":
                failed["_circuit_opened"]=True
            return failed

    @staticmethod
    def _fast_adjudication(payload,scout,technical,deterministic_strength,critic=None):
        tech_side=str(payload.get("technical_signal","HOLD") or "HOLD").upper()
        tech_conf=max(0.0,min(1.0,float(payload.get("technical_confidence",0.0) or 0.0)))
        scout_action=str((scout or {}).get("action","HOLD") or "HOLD").upper()
        scout_conf=float((scout or {}).get("confidence",0.0) or 0.0)
        llm_action=str((technical or {}).get("action","HOLD") or "HOLD").upper()
        llm_conf=float((technical or {}).get("confidence",0.0) or 0.0)
        critic_conf=float((critic or {}).get("confidence",0.0) or 0.0)
        critic_verdict=str((critic or {}).get("verdict","") or "").upper()
        if bool((critic or {}).get("_ok")) and critic_verdict=="REJECT" and critic_conf>=0.50:
            return "HOLD",critic_conf,"valid Critic REJECT preserved"
        aligned=(tech_side in {"BUY","SELL"} and scout_action==tech_side and
                 llm_action==tech_side and scout_conf>=0.62 and llm_conf>=0.70 and
                 tech_conf>=0.82 and float(deterministic_strength)>=0.76)
        if not aligned:
            return "HOLD",0.0,"insufficient agreement for degraded fast adjudication"
        confidence=min(0.72,0.30*scout_conf+0.35*llm_conf+0.35*tech_conf)
        return tech_side,confidence,"Scout + Technical + deterministic evidence aligned; downstream Council unavailable"

    def run_ai_council(self, payload, models, candidate_threshold=0.60):
        # V3.10.4: Scout is a cheap first opinion, not an absolute veto.
        # Strong deterministic evidence may escalate a HOLD/zero-confidence Scout
        # to TECHNICAL, while genuinely weak setups still stop early.
        out={"stages":[],"final_action":"HOLD","final_confidence":0.0,"escalated":False,"scout_overridden":False}
        scout=self.council_call(models["SCOUT"],"SCOUT",payload); out["stages"].append(scout)

        tech_side=str(payload.get("technical_signal","HOLD") or "HOLD").upper()
        tech_conf=max(0.0,min(1.0,float(payload.get("technical_confidence",0.0) or 0.0)))
        primary=dict(payload.get("primary") or {})
        micro=dict(payload.get("micro") or {})
        micro_score=max(-1.0,min(1.0,float(micro.get("directional_score",0.0) or 0.0)))
        structure=str(primary.get("structure","NEUTRAL") or "NEUTRAL").upper()
        direction=1.0 if tech_side=="BUY" else (-1.0 if tech_side=="SELL" else 0.0)
        micro_align=max(0.0,micro_score*direction) if direction else 0.0
        structure_align=(1.0 if ((tech_side=="BUY" and "BULL" in structure) or (tech_side=="SELL" and "BEAR" in structure)) else 0.0)
        deterministic_strength=max(0.0,min(1.0,0.72*tech_conf+0.18*micro_align+0.10*structure_align))
        scout_ok=bool(scout.get("_ok")) and not bool(scout.get("_abstain",False)) and scout.get("action") in {"BUY","SELL"} and float(scout.get("confidence",0) or 0)>=candidate_threshold
        evidence_override=(tech_side in {"BUY","SELL"} and tech_conf>=0.78 and deterministic_strength>=0.68)

        if not scout_ok and not evidence_override:
            out["reason"]=(f"Scout weak and deterministic evidence insufficient "
                           f"(TECH={tech_side} {tech_conf:.2f}, evidence={deterministic_strength:.2f}).")
            return out
        if not scout_ok and evidence_override:
            out["scout_overridden"]=True
            out["reason"]=(f"Scout abstained; deterministic evidence escalated to Technical "
                           f"(TECH={tech_side} {tech_conf:.2f}, evidence={deterministic_strength:.2f}).")

        tech=self.council_call(models["TECHNICAL"],"TECHNICAL",{
            "market":payload,"scout":scout,
            "deterministic_evidence":{"side":tech_side,"technical_confidence":tech_conf,
                                      "micro_alignment":micro_align,"structure_alignment":structure_align,
                                      "strength":deterministic_strength,
                                      "scout_abstained":not scout_ok}
        }); out["stages"].append(tech)

        # V3.10.7: zero-confidence/HOLD from Technical is an abstention, not a veto,
        # when deterministic evidence is strong enough to justify full Council review.
        tech_action=str(tech.get("action","HOLD") or "HOLD").upper()
        tech_llm_conf=float(tech.get("confidence",0) or 0)
        tech_ok=bool(tech.get("_ok")) and tech_action in {"BUY","SELL"} and tech_llm_conf>=candidate_threshold
        technical_abstain=(
            bool(tech.get("_abstain",False))
            or (bool(tech.get("_ok")) and tech_action=="HOLD" and tech_llm_conf<=0.01)
        )
        technical_evidence_override=(
            technical_abstain and tech_side in {"BUY","SELL"} and
            tech_conf>=0.78 and deterministic_strength>=0.68
        )

        if not tech_ok and not technical_evidence_override:
            out["adaptive_stop"]="TECHNICAL"
            out["reason"]="Technical did not confirm; Adaptive Council skipped Critic/Chief."
            return out

        if technical_evidence_override:
            out["technical_overridden"]=True
            out["reason"]=(f"Technical abstained at HOLD 0.00; deterministic evidence escalated to Critic "
                           f"(TECH={tech_side} {tech_conf:.2f}, evidence={deterministic_strength:.2f}).")
        else:
            expected_side=scout.get("action") if scout_ok else tech_side
            if expected_side in {"BUY","SELL"} and tech_action!=expected_side:
                out["adaptive_stop"]="DIRECTION_CONFLICT"
                out["reason"]="Technical disagrees with directional evidence; Adaptive Council skipped Critic/Chief."
                return out

        out["escalated"]=True
        critic=self.council_call(models["CRITIC"],"CRITIC",{
            "market":payload,
            "scout":scout,
            "technical":tech,
            "deterministic_evidence":{
                "side":tech_side,
                "technical_confidence":tech_conf,
                "micro_alignment":micro_align,
                "structure_alignment":structure_align,
                "strength":deterministic_strength,
                "scout_abstained":not scout_ok,
                "technical_abstained":technical_evidence_override
            }
        }); out["stages"].append(critic)

        # V3.10.9: CHIEF always performs the final Council review after CRITIC,
        # even when CRITIC rejects. A CRITIC reject is still a hard safety veto:
        # CHIEF may explain/disagree, but cannot force an entry through it.
        critic_conf=float(critic.get("confidence",0) or 0)
        critic_verdict=str(critic.get("verdict","REJECT") or "REJECT").upper()
        critic_abstained=(bool(critic.get("_abstain",False)) or not bool(critic.get("_ok")) or critic_conf<=0.01)
        critic_approved=(not critic_abstained and critic_verdict=="APPROVE" and critic_conf>=0.35)
        critic_valid_reject=(not critic_abstained and critic_verdict=="REJECT" and critic_conf>=0.50)
        out["critic_approved"]=critic_approved
        out["critic_abstained"]=critic_abstained
        out["critic_valid_reject"]=critic_valid_reject

        critic_infra_failed=(
            bool(critic.get("_circuit_open",False)) or
            (not bool(critic.get("_ok")) and any(
                x in str(critic.get("reason","") or "").lower()
                for x in ("timeout","timed out","connection","incomplete","empty response","circuit_open")
            ))
        )
        if critic_infra_failed:
            fast_action,fast_conf,fast_reason=self._fast_adjudication(
                payload,scout,tech,deterministic_strength,critic=critic
            )
            out.update({
                "fast_adjudication":True,"chief_skipped":True,
                "adaptive_stop":"FAST_ADJUDICATION",
                "final_action":fast_action,"final_confidence":fast_conf,
                "reason":"FAST ADJUDICATION: "+fast_reason
            })
            return out

        # V3.10.13: a valid Critic REJECT is already a hard veto.
        # Chief cannot override it, so skip both Chief 9B and fallback 4B.
        if critic_valid_reject:
            out["final_action"]="HOLD"
            out["final_confidence"]=critic_conf
            out["chief_skipped"]=True
            out["adaptive_stop"]="CRITIC_REJECT"
            out["reason"]=f"Critic valid REJECT {critic_conf:.2f}; hard veto. Adaptive Council skipped Chief."
            return out

        chief_payload={
            "market":payload,
            "scout":scout,
            "technical":tech,
            "critic":critic,
            "critic_abstained":critic_abstained,
            "critic_hard_veto":False,
            "instruction":"Perform final review. No valid Critic hard veto exists. Decide BUY/SELL/HOLD."
        }

        primary_chief_model=models["CHIEF"]
        fallback_chief_model=models.get("CRITIC")
        routed_model,routed,route_reason=self.smart_route_model(
            "CHIEF",primary_chief_model,fallback_chief_model
        )
        out["chief_route_model"]=routed_model
        out["chief_smart_routed"]=routed
        out["chief_route_reason"]=route_reason
        chief_timeout=45 if routed else 75
        chief=self.council_call(routed_model,"CHIEF",chief_payload,timeout=chief_timeout)
        if routed:
            chief["_smart_routed"]=True
            chief["_route_reason"]=route_reason

        # V3.10.12 recovery remains, but only when Chief is actually needed.
        if (not bool(chief.get("_ok")) or bool(chief.get("_abstain",False))
                or float(chief.get("confidence",0) or 0)<=0.01):
            if bool(chief.get("_circuit_open",False)) or bool(chief.get("_circuit_opened",False)):
                fast_action,fast_conf,fast_reason=self._fast_adjudication(
                    payload,scout,tech,deterministic_strength,critic=critic
                )
                chief["_role"]="CHIEF_PRIMARY"
                out["stages"].append(chief)
                out.update({
                    "chief_reviewed":False,"chief_skipped":True,"fast_adjudication":True,
                    "adaptive_stop":"CHIEF_CIRCUIT_FAST_ADJUDICATION",
                    "final_action":fast_action,"final_confidence":fast_conf,
                    "reason":"FAST ADJUDICATION: "+fast_reason
                })
                return out
            primary_chief=dict(chief)
            primary_chief["_role"]="CHIEF_PRIMARY"
            out["stages"].append(primary_chief)
            fallback_model=models.get("CRITIC")
            if routed_model==fallback_model:
                # Already on 4B because Smart Routing degraded the 9B.
                fallback=dict(chief)
                fallback["_chief_fallback"]=True
                fallback["_fallback_skipped_same_model"]=True
            else:
                fallback=self.council_call(fallback_model,"CHIEF",chief_payload,timeout=45)
                fallback["_fallback_from"]=models.get("CHIEF")
                fallback["_chief_fallback"]=True
            chief=fallback

        out["stages"].append(chief)
        out["chief_reviewed"]=True

        if critic_abstained:
            out["reason"]="Critic abstained; Chief performed independent final review."

        if chief.get("_ok") and not bool(chief.get("_abstain",False)) and float(chief.get("confidence",0) or 0)>0.01:
            out["final_action"]=chief.get("action","HOLD")
            out["final_confidence"]=float(chief.get("confidence",0) or 0)
            out["reason"]=str(chief.get("reason","") or out.get("reason","") or "Chief completed council decision.")
        else:
            out["final_action"]="HOLD"
            out["final_confidence"]=0.0
            out["reason"]="Chief abstained or final review failed; fail-safe HOLD."
        return out

    def health_check(self):
        try:
            r=requests.get(self.s.ollama_url+"/api/tags",timeout=4)
            r.raise_for_status()
            self.last_health_status="READY"
            return True,"READY"
        except requests.exceptions.Timeout:
            self.last_health_status="TIMEOUT"
            return False,"TIMEOUT"
        except requests.exceptions.ConnectionError:
            self.last_health_status="OFFLINE"
            return False,"OFFLINE"
        except Exception as e:
            self.last_health_status="ERROR"
            return False,f"ERROR:{type(e).__name__}"


    def generation_probe(self):
        """Verify the selected model can produce a short final structured response."""
        prompt='Return only this JSON object: {"ok":true}'
        try:
            obj=self._generate_json(
                prompt,
                timeout=min(int(getattr(self.s,"ollama_timeout",120)),25),
                temperature=0.0,
                num_predict_override=int(getattr(self.s,"ollama_probe_num_predict",48))
            )
            ok=bool(obj.get("ok",False))
            return ok,("GENERATION_OK" if ok else "GENERATION_INVALID")
        except requests.exceptions.Timeout:
            return False,"GENERATION_TIMEOUT"
        except requests.exceptions.ConnectionError:
            return False,"GENERATION_OFFLINE"
        except Exception as e:
            msg=str(e).lower()
            if "empty response" in msg or "raw=<empty>" in msg:
                return False,"GENERATION_EMPTY"
            return False,f"GENERATION_ERROR:{type(e).__name__}"

    @staticmethod
    def _decision_json_schema():
        return {
            "type":"object",
            "properties":{
                "action":{"type":"string","enum":["BUY","SELL","HOLD"]},
                "confidence":{"type":"number","minimum":0.0,"maximum":1.0},
                "reason":{"type":"string"},
                "trend":{"type":"string","enum":["BULLISH","BEARISH","MIXED"]},
                "momentum":{"type":"string","enum":["BULLISH","BEARISH","MIXED"]},
                "volatility":{"type":"string","enum":["LOW","NORMAL","HIGH"]},
                "structure":{"type":"string","enum":["BULLISH","BEARISH","NEUTRAL"]},
                "conflicts":{"type":"array","items":{"type":"string"}},
            },
            "required":[
                "action","confidence","reason","trend","momentum",
                "volatility","structure","conflicts"
            ],
            "additionalProperties":False,
        }

    @staticmethod
    def _looks_like_decision_prompt(prompt):
        p=str(prompt or "")
        return (
            "action" in p.lower()
            and "confidence" in p.lower()
            and "trend" in p.lower()
            and ("BUY" in p or "SELL" in p or "HOLD" in p)
        )

    def _generate_json(self, prompt, timeout=None, temperature=0.12, num_predict_override=None, model_override=None):
        if timeout is None:
            timeout=self.s.ollama_timeout
        retry_like=(float(temperature)==0.0)
        num_predict=(
            int(num_predict_override)
            if num_predict_override is not None
            else int(
                getattr(
                    self.s,
                    "ollama_retry_num_predict" if retry_like else "ollama_num_predict",
                    110 if retry_like else 180
                )
            )
        )
        payload={
            "model":str(model_override or self.s.ollama_model),
            "prompt":prompt,
            "stream":False,
            "options":{
                "temperature":float(temperature),
                "num_predict":max(48,num_predict),
            }
        }
        # DeepSeek-R1 is a reasoning model. On some Ollama versions the whole
        # generation budget can be consumed by hidden reasoning and `response`
        # arrives empty. Trading decisions only need a compact structured result.
        if bool(getattr(self.s,"ollama_disable_thinking",True)):
            payload["think"]=False
        if bool(getattr(self.s,"ollama_force_json_format",True)):
            if (
                bool(getattr(self.s,"ollama_schema_output",True))
                and self._looks_like_decision_prompt(prompt)
            ):
                payload["format"]=self._decision_json_schema()
                payload["options"]["num_predict"]=max(
                    int(payload["options"].get("num_predict",0) or 0),
                    int(getattr(self.s,"ollama_schema_num_predict",220))
                )
            else:
                payload["format"]="json"
        r=requests.post(
            self.s.ollama_url+"/api/generate",
            json=payload,
            timeout=(5,timeout)
        )
        r.raise_for_status()

        data=r.json()
        response_text=str(data.get("response","") or "").strip()
        thinking_text=str(data.get("thinking","") or "").strip()

        text=re.sub(
            r"<think>.*?</think>",
            "",
            response_text,
            flags=re.S
        ).strip()

        # Primary path: final response. Compatibility path: a few Ollama/model
        # combinations expose structured output in `thinking` while final response
        # is empty. Only accept thinking if it actually contains a JSON object.
        candidates=[("response",text)]
        if thinking_text and thinking_text != text:
            candidates.append(("thinking",thinking_text))

        errors=[]
        for source,candidate in candidates:
            if not candidate:
                continue
            try:
                obj=self._extract_json_object(candidate)
                if source=="thinking":
                    obj=dict(obj)
                    obj["_ollama_json_source"]="thinking"
                return obj
            except Exception as e:
                errors.append(f"{source}:{e}")

        preview=re.sub(r"\s+"," ",text)[:180] or "<empty>"
        thinking_preview=re.sub(r"\s+"," ",thinking_text)[:120] or "<empty>"
        reason="; ".join(errors) if errors else "LLM returned empty response"
        raise RuntimeError(
            f"{reason} | raw={preview} | thinking={thinking_preview}"
        )

    def _validate_decision_contract(self,obj):
        if not isinstance(obj,dict):
            raise ValueError("LLM decision is not a JSON object")
        required={
            "action","confidence","reason","trend","momentum",
            "volatility","structure","conflicts"
        }
        missing=sorted(required-set(obj.keys()))
        if missing:
            raise ValueError("LLM decision missing fields: "+",".join(missing))

        # Detect malformed token-map output such as {"BUY":0,"SELL":0,...}.
        suspicious=sum(
            1 for k in obj.keys()
            if str(k).upper() in {
                "BUY","SELL","HOLD","BULLISH","BEARISH","MIXED",
                "LOW","NORMAL","HIGH","NEUTRAL"
            }
        )
        if suspicious >= 3 and "action" not in obj:
            raise ValueError("LLM returned token-map instead of decision object")
        return obj

    def decide(self,symbol,tf,mtf,memory_stats,technical_side,technical_confidence,macro=None,micro=None):
        compact = {}
        for k,v in mtf.items():
            compact[k] = {
                "trend": v.get("trend"),
                "regime": v.get("regime"),
                "structure": v.get("structure"),
                "rsi": round(float(v.get("rsi14",0)),2),
                "macd_hist": round(float(v.get("macd_hist",0)),6),
                "adx": round(float(v.get("adx14",0)),2),
                "stoch_k": round(float(v.get("stoch_k",0)),2),
                "atr_pct": round(float(v.get("atr_pct",0)),6),
                "close_vs_ema20": round(float(v.get("close",0)-v.get("ema20",0)),6),
                "close_vs_ema50": round(float(v.get("close",0)-v.get("ema50",0)),6),
                "close_vs_ema200": round(float(v.get("close",0)-v.get("ema200",0)),6),
            }

        prompt=f"""Return ONLY one JSON object with these keys:
action, confidence, reason, trend, momentum, volatility, structure, conflicts.

Enum rules:
- action: exactly BUY, SELL, or HOLD
- trend: exactly BULLISH, BEARISH, or MIXED
- momentum: exactly BULLISH, BEARISH, or MIXED
- volatility: exactly LOW, NORMAL, or HIGH
- structure: exactly BULLISH, BEARISH, or NEUTRAL
- Never return combined values such as BULLISH|BEARISH|MIXED.
- confidence must be numeric 0.0 to 1.0.
- conflicts must be a JSON array.

Example shape:
{{"action":"HOLD","confidence":0.55,"reason":"mixed evidence","trend":"MIXED","momentum":"MIXED","volatility":"NORMAL","structure":"NEUTRAL","conflicts":[]}}

Rules:
- RSI may be overbought/oversold.
- MACD is momentum, never overbought/oversold.
- ADX = trend strength only.
- ATR = volatility only.
- HOLD on meaningful conflict.
- Never infer whether the market is open/closed; MT5 handles that.
- Never choose lot size or override risk rules.
- Treat Macro as external context only; if status is UNAVAILABLE/STALE, do not invent macro facts.
- Treat Micro as instrument-specific market context from MT5.
- A high-impact macro blackout should favor HOLD.

Symbol={symbol} TF={tf}
Technical={technical_side} confidence={technical_confidence:.2f}
MTF={json.dumps(compact,separators=(',',':'))}
Memory={json.dumps(memory_stats,separators=(',',':'))}
Macro={json.dumps(macro or {},separators=(',',':'))}
Micro={json.dumps(micro or {},separators=(',',':'))}
"""
        obj=self._validate_decision_contract(self._generate_json(prompt))
        action=str(obj.get("action","HOLD")).upper()
        if action not in {"BUY","SELL","HOLD"}: action="HOLD"
        raw_conf=obj.get("confidence",None)
        confidence_missing=raw_conf is None
        conf=max(0,min(1,float(raw_conf or 0)))
        detail={
            "reason":str(obj.get("reason",""))[:500],
            "trend":str(obj.get("trend","MIXED")).upper()[:20],
            "momentum":str(obj.get("momentum","MIXED")).upper()[:20],
            "volatility":str(obj.get("volatility","NORMAL")).upper()[:20],
            "structure":str(obj.get("structure","NEUTRAL")).upper()[:20],
            "conflicts":obj.get("conflicts",[]) if isinstance(obj.get("conflicts",[]),list) else [],
            "confidence_missing":confidence_missing
        }
        return action,conf,detail

    def _retry_empty_response(
        self,symbol,tf,mtf,technical_side,technical_confidence,macro=None,micro=None
    ):
        primary=(mtf or {}).get(tf,{}) or {}
        prompt=f"""Return exactly ONE JSON object and nothing else.
Keys: action, confidence, reason, trend, momentum, volatility, structure, conflicts.

Allowed:
action BUY/SELL/HOLD
trend BULLISH/BEARISH/MIXED
momentum BULLISH/BEARISH/MIXED
volatility LOW/NORMAL/HIGH
structure BULLISH/BEARISH/NEUTRAL

Symbol={symbol}
TF={tf}
Technical={technical_side}
TechnicalConfidence={float(technical_confidence):.2f}
Trend={primary.get('trend','MIXED')}
Structure={primary.get('structure','NEUTRAL')}
RSI={float(primary.get('rsi14',50) or 50):.1f}
MacroBlackout={bool((macro or {}).get('blackout',False))}
MicroBias={float((micro or {}).get('directional_score',0) or 0):+.2f}

If uncertain, action HOLD.
JSON only."""
        obj=self._validate_decision_contract(self._generate_json(
            prompt,
            timeout=min(int(self.s.ollama_timeout),45),
            temperature=0.0,
            num_predict_override=max(
                int(getattr(self.s,"ollama_empty_retry_num_predict",72)),
                int(getattr(self.s,"ollama_schema_num_predict",220))
            )
        ))
        action=str(obj.get("action","HOLD")).upper()
        if action not in {"BUY","SELL","HOLD"}:
            action="HOLD"
        raw_conf=obj.get("confidence",None)
        conf=max(0.0,min(1.0,float(raw_conf or 0)))
        detail={
            "reason":str(obj.get("reason",""))[:300],
            "trend":str(obj.get("trend","MIXED")).upper()[:20],
            "momentum":str(obj.get("momentum","MIXED")).upper()[:20],
            "volatility":str(obj.get("volatility","NORMAL")).upper()[:20],
            "structure":str(obj.get("structure","NEUTRAL")).upper()[:20],
            "conflicts":obj.get("conflicts",[]) if isinstance(obj.get("conflicts",[]),list) else [],
            "confidence_missing":raw_conf is None,
            "retry_used":True,
            "empty_retry_used":True,
        }
        return action,conf,detail

    def _retry_decide_json_only(
        self,symbol,tf,mtf,technical_side,technical_confidence,macro=None,micro=None
    ):
        """One strict retry when the normal response is not valid JSON."""
        primary=(mtf or {}).get(tf,{})
        compact={
            "trend":primary.get("trend","MIXED"),
            "regime":primary.get("regime",""),
            "structure":primary.get("structure","NEUTRAL"),
            "rsi":round(float(primary.get("rsi14",0) or 0),2),
            "macd_hist":round(float(primary.get("macd_hist",0) or 0),6),
            "adx":round(float(primary.get("adx14",0) or 0),2),
            "atr_pct":round(float(primary.get("atr_pct",0) or 0),6),
        }

        prompt=f"""JSON ONLY. No markdown. No explanation before or after JSON.
Output exactly one object with these keys:
action, confidence, reason, trend, momentum, volatility, structure, conflicts.

Rules for enum fields:
- action must be exactly one of: BUY, SELL, HOLD
- trend must be exactly one of: BULLISH, BEARISH, MIXED
- momentum must be exactly one of: BULLISH, BEARISH, MIXED
- volatility must be exactly one of: LOW, NORMAL, HIGH
- structure must be exactly one of: BULLISH, BEARISH, NEUTRAL
- confidence must be a number from 0.0 to 1.0
- conflicts must be a JSON array
Do NOT write the option list itself as a value.

Example shape only:
{{"action":"HOLD","confidence":0.55,"reason":"signals conflict","trend":"MIXED","momentum":"MIXED","volatility":"NORMAL","structure":"NEUTRAL","conflicts":[]}}

Symbol={symbol}
TF={tf}
Technical={technical_side}
TechnicalConfidence={float(technical_confidence):.2f}
Primary={json.dumps(compact,separators=(',',':'))}
MacroStatus={str((macro or {}).get("status","UNAVAILABLE"))}
MacroBias={float((macro or {}).get("directional_score",0) or 0):+.2f}
MacroBlackout={bool((macro or {}).get("blackout",False))}
MicroBias={float((micro or {}).get("directional_score",0) or 0):+.2f}

If evidence conflicts, action must be HOLD.
Return JSON now."""

        obj=self._validate_decision_contract(self._generate_json(
            prompt,
            timeout=min(int(self.s.ollama_timeout),75),
            temperature=0.0,
            num_predict_override=int(getattr(self.s,"ollama_schema_num_predict",220))
        ))

        action=str(obj.get("action","HOLD")).upper()
        if action not in {"BUY","SELL","HOLD"}:
            action="HOLD"

        raw_conf=obj.get("confidence",None)
        confidence_missing=raw_conf is None
        conf=max(0.0,min(1.0,float(raw_conf or 0)))

        detail={
            "reason":str(obj.get("reason",""))[:500],
            "trend":str(obj.get("trend","MIXED")).upper()[:20],
            "momentum":str(obj.get("momentum","MIXED")).upper()[:20],
            "volatility":str(obj.get("volatility","NORMAL")).upper()[:20],
            "structure":str(obj.get("structure","NEUTRAL")).upper()[:20],
            "conflicts":obj.get("conflicts",[]) if isinstance(obj.get("conflicts",[]),list) else [],
            "confidence_missing":confidence_missing,
            "retry_used":True,
        }
        return action,conf,detail

    def _normalize_semantics(self, action, detail):
        """Normalize engine-style LLM aliases into the canonical fusion enums."""
        d=dict(detail or {})
        aliases={
            "trend":{
                "UP":"BULLISH","UPTREND":"BULLISH","BULL":"BULLISH",
                "DOWN":"BEARISH","DOWNTREND":"BEARISH","BEAR":"BEARISH",
                "SIDEWAYS":"MIXED","RANGING":"MIXED","RANGE":"MIXED",
            },
            "momentum":{
                "UP":"BULLISH","POSITIVE":"BULLISH","BULL":"BULLISH",
                "DOWN":"BEARISH","NEGATIVE":"BEARISH","BEAR":"BEARISH",
                "FLAT":"MIXED","NEUTRAL":"MIXED","SIDEWAYS":"MIXED",
            },
            "volatility":{
                "QUIET":"LOW","LOW_VOL":"LOW","LOW_VOLATILITY":"LOW",
                "MEDIUM":"NORMAL","MODERATE":"NORMAL","NORMAL_VOL":"NORMAL",
                "HIGH_VOL":"HIGH","HIGH_VOLATILITY":"HIGH","VOLATILE":"HIGH",
            },
            "structure":{
                "BULLISH_STRUCTURE":"BULLISH","BREAKOUT_UP":"BULLISH","UPTREND":"BULLISH",
                "BEARISH_STRUCTURE":"BEARISH","BREAKOUT_DOWN":"BEARISH","DOWNTREND":"BEARISH",
                "RANGE":"NEUTRAL","RANGING":"NEUTRAL","SIDEWAYS":"NEUTRAL","MIXED":"NEUTRAL",
            },
        }
        for field,mapping in aliases.items():
            raw=str(d.get(field,"") or "").upper().strip()
            if raw in mapping:
                d[field]=mapping[raw]
        a=str(action or "HOLD").upper().strip()
        return a,d

    def _validate_semantics(self, action, detail):
        action,detail=self._normalize_semantics(action,detail)
        allowed={
            "action":{"BUY","SELL","HOLD"},
            "trend":{"BULLISH","BEARISH","MIXED"},
            "momentum":{"BULLISH","BEARISH","MIXED"},
            "volatility":{"LOW","NORMAL","HIGH"},
            "structure":{"BULLISH","BEARISH","NEUTRAL"},
        }

        values={
            "action":str(action or "").upper(),
            "trend":str((detail or {}).get("trend","")).upper(),
            "momentum":str((detail or {}).get("momentum","")).upper(),
            "volatility":str((detail or {}).get("volatility","")).upper(),
            "structure":str((detail or {}).get("structure","")).upper(),
        }

        invalid=[]
        for field,value in values.items():
            if value not in allowed[field]:
                invalid.append(f"{field}={value or '<empty>'}")

        if invalid:
            raise ValueError(
                "LLM semantic invalid: " + ", ".join(invalid)
            )
        return action,detail

    def validate_evidence(self, action, conf, detail, tf, mtf, technical_side, technical_confidence, macro=None):
        """Deterministic V3.7.4 guard against LLM claims that contradict supplied indicators."""
        primary=(mtf or {}).get(tf,{}) or {}
        issues=[]
        reason=str((detail or {}).get("reason","")).lower()
        try: rsi=float(primary.get("rsi14",50) or 50)
        except Exception: rsi=50.0
        # Catch explicit factual RSI hallucinations in the explanation.
        if "rsi below 50" in reason and rsi >= 50: issues.append(f"reason says RSI<50 but RSI={rsi:.1f}")
        if "rsi above 50" in reason and rsi <= 50: issues.append(f"reason says RSI>50 but RSI={rsi:.1f}")
        # Macro reasoning must agree with the structured macro state.
        macro=macro or {}
        macro_blackout=bool(macro.get("blackout",False))
        macro_event=str(macro.get("event","") or "").strip()
        macro_risk=str(macro.get("risk_level","NORMAL") or "NORMAL").upper()
        mentions_blackout=("blackout" in reason or "high-impact macro" in reason or "high impact macro" in reason)
        if mentions_blackout and not macro_blackout:
            issues.append(
                f"reason claims macro blackout but structured macro blackout=False "
                f"(risk={macro_risk}, event={macro_event or '-'})"
            )
        # Directional contradiction: a high-confidence LLM may not oppose a very strong deterministic signal.
        if action in {"BUY","SELL"} and technical_side in {"BUY","SELL"} and action != technical_side and float(technical_confidence)>=0.85:
            issues.append(f"action {action} opposes strong TECH {technical_side} {float(technical_confidence):.2f}")
        # Context fields should not strongly contradict the chosen action without declaring conflict.
        trend=str((detail or {}).get("trend","MIXED")).upper()
        momentum=str((detail or {}).get("momentum","MIXED")).upper()
        structure=str((detail or {}).get("structure","NEUTRAL")).upper()
        opposite="BEARISH" if action=="BUY" else "BULLISH"
        if action in {"BUY","SELL"}:
            opposed=sum(x==opposite for x in (trend,momentum,structure))
            if opposed>=2: issues.append(f"{opposed}/3 LLM context fields oppose action {action}")
        if issues:
            detail=dict(detail or {})
            conflicts=list(detail.get("conflicts",[]) or [])
            conflicts.extend([f"Evidence guard: {x}" for x in issues])
            detail["conflicts"]=conflicts
            detail["evidence_invalid"]=True
            detail["original_action"]=action
            # Do not force a trade. Downgrade to fail-safe HOLD and confidence.
            return "HOLD", min(float(conf or 0.0),0.35), detail, issues
        detail=dict(detail or {})
        detail["evidence_invalid"]=False
        return action,float(conf or 0.0),detail,[]

    def _default_council_models(self):
        return {
            "SCOUT":"deepseek-r1:1.5b",
            "TECHNICAL":"qwen3.5:4b",
            "CRITIC":"kwangsuklee/Qwen3.5-4B.Q4_K_M-Claude-4.6-Opus-Reasoning-Distilled-v2",
            "CHIEF":"kwangsuklee/Qwen3.5-9B.Q4_K_M-Claude-4.6-Opus-Reasoning-Distilled-v2",
        }

    @staticmethod
    def _council_consensus_score(stages,final_action,technical_side="HOLD"):
        """Quantify how coherent the Council was, without replacing its authority.

        0.50 = neutral/insufficient evidence.
        >0.50 = increasing directional agreement.
        <0.50 = disagreement/abstention penalty.
        Critic REJECT is never converted into approval by this score.
        """
        final_action=str(final_action or "HOLD").upper()
        technical_side=str(technical_side or "HOLD").upper()
        role_weights={"SCOUT":0.18,"TECHNICAL":0.27,"CRITIC":0.20,"CHIEF":0.35}
        weighted=0.0
        possible=0.0
        valid_roles=0
        disagree_roles=0
        abstain_roles=0
        role_detail={}

        for st in list(stages or []):
            role=str(st.get("_role","") or "").upper()
            # CHIEF_PRIMARY is diagnostic only; final CHIEF/fallback is authoritative.
            if role=="CHIEF_PRIMARY":
                continue
            w=float(role_weights.get(role,0.0))
            if w<=0:
                continue
            possible+=w

            conf=max(0.0,min(1.0,float(st.get("confidence",0.0) or 0.0)))
            abstain=bool(st.get("_abstain",False)) or not bool(st.get("_ok",False)) or conf<=0.01
            if abstain:
                abstain_roles+=1
                role_detail[role]={"state":"ABSTAIN","confidence":conf,"agreement":0.0}
                continue

            valid_roles+=1
            if role=="CRITIC":
                verdict=str(st.get("verdict","REJECT") or "REJECT").upper()
                # APPROVE supports a directional final action; REJECT opposes it.
                agree=1.0 if verdict=="APPROVE" and final_action in {"BUY","SELL"} else -1.0
                state=verdict
            else:
                action=str(st.get("action","HOLD") or "HOLD").upper()
                if final_action in {"BUY","SELL"}:
                    if action==final_action:
                        agree=1.0
                    elif action in {"BUY","SELL"} and action!=final_action:
                        agree=-1.0
                    else:
                        agree=-0.35
                else:
                    agree=1.0 if action=="HOLD" else -0.50
                state=action

            if agree<0:
                disagree_roles+=1
            weighted += w*conf*agree
            role_detail[role]={"state":state,"confidence":conf,"agreement":agree}

        # Deterministic technical side is a small independent sanity vote.
        tech_component=0.0
        if final_action in {"BUY","SELL"} and technical_side in {"BUY","SELL"}:
            tech_component=0.08 if technical_side==final_action else -0.08

        denom=max(possible,1e-9)
        signed=max(-1.0,min(1.0,weighted/denom + tech_component))
        score=max(0.0,min(1.0,0.5+0.5*signed))

        # Missing roles should not look like strong agreement.
        coverage=(valid_roles/max(1,len([r for r in role_weights if role_weights[r]>0])))
        score=0.5+(score-0.5)*max(0.35,min(1.0,coverage))

        if score>=0.78:
            grade="VERY_HIGH"
        elif score>=0.66:
            grade="HIGH"
        elif score>=0.55:
            grade="MODERATE"
        elif score<=0.35:
            grade="CONFLICT"
        elif score<=0.45:
            grade="LOW"
        else:
            grade="MIXED"

        # Bounded modifier: consensus can modestly de-risk/reward, never overpower guards.
        risk_multiplier=max(0.80,min(1.05,0.75+0.40*score))
        quality_multiplier=max(0.90,min(1.05,0.80+0.35*score))

        return {
            "score":score,
            "grade":grade,
            "signed":signed,
            "coverage":coverage,
            "valid_roles":valid_roles,
            "abstain_roles":abstain_roles,
            "disagree_roles":disagree_roles,
            "risk_multiplier":risk_multiplier,
            "quality_multiplier":quality_multiplier,
            "roles":role_detail,
        }

    def _safe_decide_council(self, symbol, tf, mtf, memory_stats, technical_side, technical_confidence, macro=None, micro=None):
        primary=dict((mtf or {}).get(tf,{}) or {})
        payload={
            "symbol":symbol,
            "timeframe":tf,
            "technical_signal":technical_side,
            "technical_confidence":float(technical_confidence or 0.0),
            "primary":primary,
            "memory":memory_stats or {},
            "macro":macro or {},
            "micro":micro or {},
        }
        result=self.run_ai_council(payload,self._default_council_models())
        stages=list(result.get("stages",[]) or [])
        chief=next((x for x in reversed(stages) if x.get("_role")=="CHIEF"),None)
        last=stages[-1] if stages else {}
        action=str(result.get("final_action","HOLD") or "HOLD").upper()
        confidence=max(0.0,min(1.0,float(result.get("final_confidence",0.0) or 0.0)))
        consensus=self._council_consensus_score(
            stages,action,technical_side=technical_side
        )

        source=chief or last or {}
        technical_stage=next((x for x in reversed(stages) if x.get("_role")=="TECHNICAL"),{})
        primary=dict((mtf or {}).get(tf,{}) or {})
        trend=str(source.get("trend",technical_stage.get("trend",primary.get("trend","MIXED"))) or "MIXED").upper()
        momentum=str(source.get("momentum",technical_stage.get("momentum","MIXED")) or "MIXED").upper()
        volatility=str(source.get("volatility",primary.get("volatility","NORMAL")) or "NORMAL").upper()
        structure=str(source.get("structure",technical_stage.get("structure",primary.get("structure","NEUTRAL"))) or "NEUTRAL").upper()
        if trend not in {"BULLISH","BEARISH","MIXED"}: trend="MIXED"
        if momentum not in {"BULLISH","BEARISH","MIXED"}: momentum="MIXED"
        if volatility not in {"LOW","NORMAL","HIGH"}: volatility="NORMAL"
        if structure not in {"BULLISH","BEARISH","NEUTRAL"}: structure="NEUTRAL"

        conflicts=[]
        for st in stages:
            conflicts.extend(list(st.get("conflicts",[]) or []))
            conflicts.extend(list(st.get("risks",[]) or []))
        detail={
            "reason":str(result.get("reason") or source.get("reason") or "AI Council returned HOLD."),
            "trend":trend,
            "momentum":momentum,
            "volatility":volatility,
            "structure":structure,
            "conflicts":conflicts[:12],
            "retry_used":False,
            "ai_council":True,
            "council_stages":stages,
            "council_escalated":bool(result.get("escalated",False)),
            "chief_authoritative":True,
            "chief_completed":bool(chief and chief.get("_ok")),
            "critic_approved":bool(result.get("critic_approved",False)),
            "critic_abstained":bool(result.get("critic_abstained",False)),
            "critic_valid_reject":bool(result.get("critic_valid_reject",False)),
            "chief_reviewed":bool(result.get("chief_reviewed",False)),
            "council_consensus":consensus,
            "council_consensus_score":float(consensus.get("score",0.5)),
            "council_consensus_grade":str(consensus.get("grade","MIXED")),
        }
        # Council failures/early exits are deliberately fail-safe HOLD.
        if action not in {"BUY","SELL","HOLD"}: action="HOLD"
        return action,confidence,detail,"COUNCIL_READY"

    def safe_decide(self, symbol, tf, mtf, memory_stats, technical_side, technical_confidence, macro=None, micro=None):
        if bool(getattr(self.s,"ai_council_enabled",False)):
            try:
                return self._safe_decide_council(
                    symbol,tf,mtf,memory_stats,technical_side,technical_confidence,
                    macro=macro,micro=micro
                )
            except Exception as e:
                return "HOLD",0.0,{
                    "reason":f"AI Council error: {type(e).__name__}: {e}. Fail-safe HOLD.",
                    "trend":"MIXED","momentum":"MIXED","volatility":"NORMAL","structure":"NEUTRAL",
                    "conflicts":["AI Council runtime error"],"retry_used":False,"ai_council":True,
                },"COUNCIL_ERROR"
        now=time.time()
        if now < float(getattr(self,"degraded_until",0.0) or 0.0):
            remaining=max(0,int(self.degraded_until-now))
            return "HOLD",0.0,{
                "reason":f"Ollama model degraded after repeated empty responses ({remaining}s recovery window).",
                "trend":"MIXED","momentum":"MIXED",
                "volatility":"NORMAL","structure":"NEUTRAL",
                "conflicts":["Ollama model degraded"],
                "retry_used":False,
                "ollama_health":self.last_health_status,
            },"MODEL_DEGRADED"

        if now < float(getattr(self,"empty_until",0.0) or 0.0):
            remaining=max(0,int(self.empty_until-time.time()))
            return "HOLD",0.0,{
                "reason":f"Ollama cooldown after empty response ({remaining}s). Fail-safe HOLD.",
                "trend":"MIXED","momentum":"MIXED",
                "volatility":"NORMAL","structure":"NEUTRAL",
                "conflicts":["Ollama empty-response cooldown"],
                "retry_used":False,
            },"COOLDOWN"
        try:
            action, conf, detail = self.decide(
                symbol, tf, mtf, memory_stats, technical_side, technical_confidence,
                macro=macro, micro=micro
            )
            action,detail=self._validate_semantics(action,detail)
            detail.setdefault("retry_used",False)
            self.empty_streak=0
            self.degraded_until=0.0
            return action, conf, detail, "READY"

        except requests.exceptions.Timeout:
            return "HOLD", 0.0, {
                "reason":"DeepSeek lokal timeout. Fail-safe HOLD untuk candle ini.",
                "trend":"MIXED",
                "momentum":"MIXED",
                "volatility":"NORMAL",
                "structure":"NEUTRAL",
                "conflicts":["LLM timeout"],
                "retry_used":False,
            }, "TIMEOUT"

        except requests.exceptions.ConnectionError:
            return "HOLD", 0.0, {
                "reason":"Ollama tidak dapat dihubungi. Fail-safe HOLD untuk candle ini.",
                "trend":"MIXED",
                "momentum":"MIXED",
                "volatility":"NORMAL",
                "structure":"NEUTRAL",
                "conflicts":["Ollama offline"],
                "retry_used":False,
            }, "OFFLINE"

        except Exception as first_error:
            first_text=str(first_error)
            empty_failure=(
                "empty response" in first_text.lower()
                or ("raw=<empty>" in first_text.lower() and "thinking=<empty>" in first_text.lower())
            )

            if empty_failure:
                try:
                    action,conf,detail=self._retry_empty_response(
                        symbol,tf,mtf,technical_side,technical_confidence,
                        macro=macro,micro=micro
                    )
                    action,detail=self._validate_semantics(action,detail)
                    detail["first_error"]=first_text[:220]
                    self.empty_streak=0
                    self.degraded_until=0.0
                    return action,conf,detail,"EMPTY_RETRY_OK"
                except Exception as retry_error:
                    health_ok,health=self.health_check()
                    generation_ok,generation_health=self.generation_probe() if health_ok else (False,health)
                    self.last_health_status=(
                        "READY" if (health_ok and generation_ok)
                        else f"{health}/{generation_health}"
                    )
                    self.empty_streak=int(getattr(self,"empty_streak",0) or 0)+1
                    now=time.time()
                    self.empty_until=now+max(
                        5,int(getattr(self.s,"ollama_empty_cooldown_sec",20))
                    )
                    threshold=max(2,int(getattr(self.s,"ollama_degraded_empty_threshold",3)))
                    if self.empty_streak >= threshold:
                        self.degraded_until=now+max(
                            30,int(getattr(self.s,"ollama_degraded_recovery_sec",60))
                        )
                    return "HOLD",0.0,{
                        "reason":(
                            f"Ollama empty response after retry. Server={health}, "
                            f"generation={generation_health}. Fail-safe HOLD."
                        ),
                        "trend":"MIXED","momentum":"MIXED",
                        "volatility":"NORMAL","structure":"NEUTRAL",
                        "conflicts":["LLM empty response"],
                        "retry_used":True,
                        "empty_retry_used":True,
                        "first_error":first_text[:220],
                        "retry_error":str(retry_error)[:220],
                        "ollama_health":health,
                        "ollama_generation_health":generation_health,
                        "empty_streak":self.empty_streak,
                        "model_degraded":bool(self.degraded_until>time.time()),
                    },("MODEL_DEGRADED" if self.degraded_until>time.time() else "EMPTY_RESPONSE")

            # Retry only format/JSON failures. Do not double-call Ollama for unrelated
            # runtime/network failures.
            format_failure=any(
                token in first_text.lower()
                for token in (
                    "json","no json","raw=","expecting","delimiter",
                    "extra data","incomplete","empty response","semantic invalid"
                )
            )

            if format_failure:
                try:
                    action,conf,detail=self._retry_decide_json_only(
                        symbol,tf,mtf,technical_side,technical_confidence,
                        macro=macro,micro=micro
                    )
                    action,detail=self._validate_semantics(action,detail)
                    detail["first_error"]=first_text[:220]
                    return action,conf,detail,"RETRY_OK"
                except requests.exceptions.Timeout:
                    return "HOLD",0.0,{
                        "reason":"Retry JSON DeepSeek timeout. Fail-safe HOLD.",
                        "trend":"MIXED","momentum":"MIXED",
                        "volatility":"NORMAL","structure":"NEUTRAL",
                        "conflicts":["LLM retry timeout"],
                        "retry_used":True,
                        "first_error":first_text[:220],
                    },"TIMEOUT"
                except requests.exceptions.ConnectionError:
                    return "HOLD",0.0,{
                        "reason":"Ollama terputus saat retry. Fail-safe HOLD.",
                        "trend":"MIXED","momentum":"MIXED",
                        "volatility":"NORMAL","structure":"NEUTRAL",
                        "conflicts":["Ollama offline during retry"],
                        "retry_used":True,
                        "first_error":first_text[:220],
                    },"OFFLINE"
                except Exception as retry_error:
                    self.empty_until=max(
                        float(getattr(self,"empty_until",0.0) or 0.0),
                        time.time()+max(
                            3,int(getattr(self.s,"ollama_malformed_cooldown_sec",10))
                        )
                    )
                    return "HOLD",0.0,{
                        "reason":(
                            "LLM malformed after structured JSON retry. "
                            f"Fail-safe HOLD: {str(retry_error)[:150]}"
                        ),
                        "trend":"MIXED","momentum":"MIXED",
                        "volatility":"NORMAL","structure":"NEUTRAL",
                        "conflicts":["LLM malformed response"],
                        "retry_used":True,
                        "first_error":first_text[:220],
                        "retry_error":str(retry_error)[:220],
                    },"MALFORMED_RESPONSE"

            return "HOLD", 0.0, {
                "reason":f"Kesalahan LLM. Fail-safe HOLD: {first_text[:180]}",
                "trend":"MIXED",
                "momentum":"MIXED",
                "volatility":"NORMAL",
                "structure":"NEUTRAL",
                "conflicts":["LLM error"],
                "retry_used":False,
            }, "ERROR"


    def reflect(self, trade):
        prompt=f"""Return ONLY JSON:
{{"lesson":"one concise reusable trading lesson"}}

Review this completed trade. The lesson must describe what evidence mattered and must not say
'do the opposite next time' merely because the trade lost.

Trade:
{json.dumps(trade)}
"""
        try:
            obj=self._generate_json(prompt, timeout=90)
            return str(obj.get("lesson",""))[:700]
        except Exception:
            pnl=float(trade.get("pnl",0))
            return "Setup was profitable; retain only when similar confirmations align." if pnl>0 else "Setup failed; require stronger confirmation from regime, structure, and higher timeframe before repeating."


def classify_symbol(info):
    name=(getattr(info,"name","") or "").upper()
    path=(getattr(info,"path","") or "").upper()
    desc=(getattr(info,"description","") or "").upper()
    blob=f"{name} {path} {desc}"

    if any(x in blob for x in ["BTC","ETH","LTC","XRP","SOL","DOGE","ADA","CRYPTO"]):
        return "CRYPTO"
    if any(x in blob for x in ["XAU","XAG","GOLD","SILVER","PLATINUM","PALLADIUM","METAL"]):
        return "METALS"
    if any(x in blob for x in ["USOIL","UKOIL","WTI","BRENT","NATGAS","OIL","ENERG"]):
        return "ENERGIES"
    if any(x in path for x in ["STOCK","SHARE","EQUITIES"]) or any(x in desc for x in [" INC"," CORP"," PLC"," LTD"]):
        return "STOCKS"
    if any(x in blob for x in ["INDEX","INDICES","US30","US500","SPX","NAS100","USTEC","GER40","DE40","UK100","JP225"]):
        return "INDICES"
    if any(x in path for x in ["SYNTH","VOLATILITY","BOOM","CRASH"]):
        return "SYNTHETICS"

    core_name=re.sub(r"[^A-Z]","",name)
    majors=["USD","EUR","GBP","JPY","CHF","AUD","NZD","CAD","CNH","SGD","HKD","ZAR","TRY","MXN","NOK","SEK","PLN"]
    if len(core_name)>=6 and core_name[:3] in majors and core_name[3:6] in majors:
        return "FOREX"
    if "FOREX" in path or "FX" in path:
        return "FOREX"
    return "OTHER"



def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

class RiskGuard:
    def __init__(self,s,mt):
        self.s=s
        self.mt=mt
        self._corr_cache={}
        self._corr_cache_ttl=60.0

    def spread_profile(self, symbol):
        info=self.mt.symbol_info(symbol)
        tick=self.mt.tick(symbol)
        category=classify_symbol(info)

        bid=float(tick.bid)
        ask=float(tick.ask)
        spread=max(0.0,ask-bid)
        mid=(ask+bid)/2.0 if (ask+bid)>0 else 0.0
        spread_pct=(spread/mid*100.0) if mid>0 else 999.0
        spread_points=(spread/info.point) if getattr(info,"point",0) else 999999.0
        tick_size=float(getattr(info,"trade_tick_size",0) or getattr(info,"point",0) or 0)
        spread_ticks=(spread/tick_size) if tick_size>0 else 999999.0

        limits={
            "FOREX":self.s.max_spread_pct_forex,
            "METALS":self.s.max_spread_pct_metals,
            "CRYPTO":self.s.max_spread_pct_crypto,
            "INDICES":self.s.max_spread_pct_indices,
            "ENERGIES":self.s.max_spread_pct_energies,
            "STOCKS":self.s.max_spread_pct_stocks,
            "SYNTHETICS":self.s.max_spread_pct_synthetics,
            "OTHER":self.s.max_spread_pct_other,
        }
        limit=float(limits.get(category,self.s.max_spread_pct_other))
        return {
            "category":category,
            "spread":spread,
            "spread_pct":spread_pct,
            "spread_points":spread_points,
            "spread_ticks":spread_ticks,
            "limit_pct":limit,
            "bid":bid,
            "ask":ask,
        }

    def validate(self,symbol,action,llm_conf,final_score):
        if action not in {"BUY","SELL"}:
            return False,"HOLD"
        if llm_conf < self.s.min_confidence:
            return False,f"LLM confidence {llm_conf:.2f} < {self.s.min_confidence:.2f}"
        if final_score < self.s.min_final_score:
            return False,f"final score {final_score:.2f} < {self.s.min_final_score:.2f}"
        if len(self.mt.positions()) >= self.s.max_open_positions:
            return False,"emergency bot-position ceiling reached"

        info=self.mt.symbol_info(symbol)
        mode=int(getattr(info,"trade_mode",-1))
        if mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
            return False,"symbol trading is DISABLED by broker"
        if mode == mt5.SYMBOL_TRADE_MODE_CLOSEONLY:
            return False,"symbol is CLOSE_ONLY: broker allows closing positions only"
        if mode == mt5.SYMBOL_TRADE_MODE_LONGONLY and action != "BUY":
            return False,"symbol is LONG_ONLY: SELL entry blocked"
        if mode == mt5.SYMBOL_TRADE_MODE_SHORTONLY and action != "SELL":
            return False,"symbol is SHORT_ONLY: BUY entry blocked"

        p=self.spread_profile(symbol)
        if p["spread_pct"] > p["limit_pct"]:
            return False,(
                f"spread too high for {p['category']}: "
                f"{p['spread_pct']:.4f}% > {p['limit_pct']:.4f}% "
                f"({p['spread_points']:.1f} points / {p['spread_ticks']:.1f} ticks)"
            )

        return True,(
            f"spread OK {p['category']}: {p['spread_pct']:.4f}% "
            f"<= {p['limit_pct']:.4f}%"
        )

    def plan(self,symbol,action,atr,risk_pct_override=None,rr_override=None,min_tp_pct_override=None,min_rr_override=None):
        i=self.mt.symbol_info(symbol)
        t=self.mt.tick(symbol)
        entry=t.ask if action=="BUY" else t.bid
        mind=max(i.trade_stops_level*i.point,2*i.point)

        sld=max(float(atr)*self.s.sl_atr_mult,mind)
        target_rr=float(rr_override if rr_override is not None else self.s.rr)
        target_rr=max(float(self.s.dynamic_rr_min),min(float(self.s.dynamic_rr_max),target_rr))
        minimum_rr=max(
            1.0,
            float(
                min_rr_override
                if min_rr_override is not None
                else getattr(self.s,"adaptive_min_reward_risk",2.0)
            )
        )
        target_rr=max(target_rr,minimum_rr)

        min_tp_pct=max(0.0,float(min_tp_pct_override or 0.0))
        rr_distance=sld*target_rr
        pct_distance=abs(float(entry))*min_tp_pct/100.0
        tpd=max(rr_distance,pct_distance,mind)

        if action=="BUY":
            sl,tp=entry-sld,entry+tpd
        else:
            sl,tp=entry+sld,entry-tpd

        sl,tp=round(sl,i.digits),round(tp,i.digits)
        rr=tpd/sld if sld>0 else 0.0

        vol=self.mt.risk_volume(
            symbol,action,entry,sl,self.mt.account().equity,
            risk_pct_override=risk_pct_override
        )
        return {
            "entry":entry,
            "sl":sl,
            "tp":tp,
            "volume":vol,
            "rr":rr,
            "target_rr":target_rr,
            "minimum_rr":minimum_rr,
            "min_tp_pct":min_tp_pct,
            "tp_move_pct":((tpd/abs(float(entry)))*100.0 if float(entry) else 0.0),
            "risk_pct":float(
                risk_pct_override
                if risk_pct_override is not None
                else self.s.risk_per_trade_pct
            )
        },"OK"

    def _bot_positions(self):
        out=[]
        for p in (mt5.positions_get() or []):
            try:
                if int(getattr(p,"magic",0) or 0) == int(self.s.magic):
                    out.append(p)
            except Exception:
                pass
        return out

    def _position_stop_risk_money(self, p):
        """Approximate maximum money loss from current/open price to SL.

        Returns 0 when SL is missing; missing SL is treated as unsafe by portfolio_guard.
        """
        symbol=str(getattr(p,"symbol",""))
        sl=_safe_float(getattr(p,"sl",0))
        volume=_safe_float(getattr(p,"volume",0))
        open_price=_safe_float(getattr(p,"price_open",0))
        if not symbol or sl <= 0 or volume <= 0 or open_price <= 0:
            return 0.0

        side_type=int(getattr(p,"type",-1))
        action = mt5.ORDER_TYPE_BUY if side_type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_SELL

        # order_calc_profit estimates P/L from open -> SL for the position volume.
        try:
            loss = mt5.order_calc_profit(action, symbol, volume, open_price, sl)
            if loss is None:
                return 0.0
            return abs(min(0.0, float(loss)))
        except Exception:
            return 0.0

    def _normalize_volume_down(self, symbol, vol):
        info=mt5.symbol_info(symbol)
        if info is None:
            return 0.0
        vmin=float(getattr(info,"volume_min",0.01) or 0.01)
        vmax=float(getattr(info,"volume_max",vol) or vol)
        step=float(getattr(info,"volume_step",vmin) or vmin)
        target=max(0.0,min(float(vol),vmax))
        if target < vmin:
            return 0.0
        # Floor, never round upward beyond the safety cap.
        steps=math.floor(((target-vmin)/step)+1e-9)
        out=vmin+steps*step
        if out > target+1e-9:
            out-=step
        if out < vmin-1e-9:
            return 0.0
        return round(max(vmin,min(vmax,out)),8)

    def adapt_volume_for_margin(self, symbol, side, proposed_volume, entry, sl):
        """Reduce a risk-sized volume until broker margin constraints are safe.

        This method only reduces volume. It never raises the risk-sized proposal.
        """
        original=float(proposed_volume or 0.0)
        if original <= 0:
            return None,"invalid proposed volume"

        if not bool(getattr(self.s,"adaptive_margin_sizing",True)):
            return original,"adaptive sizing disabled"

        account=mt5.account_info()
        info=mt5.symbol_info(symbol)
        if account is None or info is None:
            return None,"margin sizing unavailable: account/symbol info missing"

        equity=_safe_float(getattr(account,"equity",0))
        current_margin=_safe_float(getattr(account,"margin",0))
        current_free=_safe_float(getattr(account,"margin_free",0))
        if equity <= 0 or current_free <= 0:
            return None,"margin sizing unavailable: equity/free margin invalid"

        order_type=mt5.ORDER_TYPE_BUY if side=="BUY" else mt5.ORDER_TYPE_SELL

        # Preserve a configurable portion of current free margin even if the
        # minimum margin-level rule would allow using more.
        buffer_pct=max(0.0,min(50.0,float(getattr(self.s,"margin_free_buffer_pct",5.0))))
        free_cap=current_free*(1.0-buffer_pct/100.0)

        floor=max(100.0,float(getattr(self.s,"min_margin_level_pct",500.0)))
        max_total_margin=(equity*100.0/floor)
        level_cap=max(0.0,max_total_margin-current_margin)

        additional_margin_cap=max(0.0,min(free_cap,level_cap))
        if additional_margin_cap <= 0:
            return None,(
                f"no safe margin capacity | free={current_free:.2f} | "
                f"margin floor={floor:.1f}%"
            )

        def margin_for(v):
            try:
                x=mt5.order_calc_margin(order_type,symbol,float(v),float(entry))
                return float(x or 0.0)
            except Exception:
                return 0.0

        original_margin=margin_for(original)
        if original_margin > 0 and original_margin <= additional_margin_cap:
            return original,(
                f"volume OK {original:g} | margin={original_margin:.2f}/"
                f"{additional_margin_cap:.2f} safe cap"
            )

        vmin=float(getattr(info,"volume_min",0.01) or 0.01)
        min_margin=margin_for(vmin)
        if min_margin <= 0:
            # Cannot estimate safely; keep existing portfolio_guard as final authority.
            return original,"broker margin estimate unavailable; final guard required"
        if min_margin > additional_margin_cap:
            return None,(
                f"even minimum volume {vmin:g} needs margin {min_margin:.2f} > "
                f"safe cap {additional_margin_cap:.2f}"
            )

        # Broker margin is usually linear, but binary search remains robust for
        # symbol-specific margin tiers. Always search downward from original.
        lo=vmin
        hi=original
        best=vmin
        iterations=max(8,min(40,int(getattr(self.s,"margin_volume_search_steps",20))))
        for _ in range(iterations):
            mid=(lo+hi)/2.0
            candidate=self._normalize_volume_down(symbol,mid)
            if candidate <= 0:
                hi=mid
                continue
            req=margin_for(candidate)
            if req > 0 and req <= additional_margin_cap:
                best=max(best,candidate)
                lo=mid
            else:
                hi=mid

        safe=self._normalize_volume_down(symbol,best)
        if safe <= 0:
            return None,"no broker-valid safe volume found"

        safe_margin=margin_for(safe)
        projected=current_margin+safe_margin
        projected_level=(equity/projected*100.0) if projected>0 else 999999.0

        return safe,(
            f"ADAPTIVE LOT: {original:g} -> {safe:g} | "
            f"margin {safe_margin:.2f}/{additional_margin_cap:.2f} | "
            f"projected level {projected_level:.1f}% | "
            f"free buffer {buffer_pct:.1f}%"
        )

    @staticmethod
    def _fx_components(symbol):
        """Return (base, quote) for common FX symbols, tolerating broker suffixes."""
        name=re.sub(r"[^A-Z]","",str(symbol or "").upper())
        majors=("USD","EUR","GBP","JPY","CHF","AUD","NZD","CAD","CNH","SGD","HKD","ZAR","TRY","MXN","NOK","SEK","PLN")
        for i in range(max(0,len(name)-5)):
            a=name[i:i+3]; b=name[i+3:i+6]
            if a in majors and b in majors and a!=b:
                return a,b
        return None,None

    @staticmethod
    def _directional_currency_exposure(symbol,side):
        """Map an FX trade to signed currency exposures.

        BUY EURUSD = long EUR / short USD.
        SELL EURUSD = short EUR / long USD.
        """
        base,quote=RiskGuard._fx_components(symbol)
        if not base or not quote:
            return {}
        sign=1.0 if str(side).upper()=="BUY" else -1.0
        return {base:+sign,quote:-sign}

    def _return_series_for_correlation(self,symbol,tf="M15",bars=120):
        """Return recent log-ish percentage returns without requiring full indicator history."""
        key=(str(symbol),str(tf).upper(),int(bars))
        now=time.time()
        cached=self._corr_cache.get(key)
        if cached and now-float(cached.get("ts",0) or 0)<self._corr_cache_ttl:
            return cached.get("series")
        try:
            self.mt.symbol_info(symbol)
            code=TIMEFRAMES.get(str(tf).upper(),mt5.TIMEFRAME_M15)
            arr=mt5.copy_rates_from_pos(symbol,code,0,max(60,int(bars)))
            if arr is None or len(arr)<50:
                return None
            df=pd.DataFrame(arr)
            close=pd.to_numeric(df["close"],errors="coerce").replace(0,np.nan)
            ret=close.pct_change().replace([np.inf,-np.inf],np.nan).dropna().tail(int(bars)-1)
            if len(ret)<40:
                return None
            values=ret.to_numpy(dtype=float)
            self._corr_cache[key]={"ts":now,"series":values}
            return values
        except Exception:
            return None

    def rolling_return_correlation(self,symbol_a,symbol_b,tf="M15",bars=120):
        if str(symbol_a)==str(symbol_b):
            return 1.0,0
        a=self._return_series_for_correlation(symbol_a,tf,bars)
        b=self._return_series_for_correlation(symbol_b,tf,bars)
        if a is None or b is None:
            return None,0
        n=min(len(a),len(b))
        if n<40:
            return None,n
        a=np.asarray(a[-n:],dtype=float)
        b=np.asarray(b[-n:],dtype=float)
        if np.nanstd(a)<=1e-12 or np.nanstd(b)<=1e-12:
            return None,n
        corr=float(np.corrcoef(a,b)[0,1])
        if not np.isfinite(corr):
            return None,n
        return max(-1.0,min(1.0,corr)),n

    @staticmethod
    def _correlation_reinforces(new_side,existing_side,corr):
        """Positive corr reinforces same side; negative corr reinforces opposite side."""
        if corr is None:
            return False
        same=str(new_side).upper()==str(existing_side).upper()
        return (corr>=0 and same) or (corr<0 and not same)

    def real_correlation_guard(self,symbol,side):
        """Use recent M15 return correlation against currently open bot positions.

        This complements, rather than replaces, deterministic FX/factor exposure.
        """
        rows=[]
        reinforcing=[]
        for p in self._bot_positions():
            psym=str(getattr(p,"symbol","") or "")
            if not psym or psym==str(symbol):
                continue
            pside="BUY" if int(getattr(p,"type",-1))==int(mt5.POSITION_TYPE_BUY) else "SELL"
            corr,n=self.rolling_return_correlation(symbol,psym,tf="M15",bars=120)
            if corr is None:
                continue
            item={"symbol":psym,"side":pside,"corr":corr,"samples":n}
            rows.append(item)
            if abs(corr)>=0.75 and self._correlation_reinforces(side,pside,corr):
                reinforcing.append(item)

        # Hard block when a new trade would add at least two highly correlated
        # reinforcing exposures. One strong match is caution only.
        very_high=[x for x in reinforcing if abs(x["corr"])>=0.88]
        if len(reinforcing)>=2 or len(very_high)>=2:
            top=sorted(reinforcing,key=lambda x:abs(x["corr"]),reverse=True)[:4]
            detail=", ".join(
                f"{x['symbol']}:{x['side']} corr={x['corr']:+.2f}"
                for x in top
            )
            return False,(
                f"REAL CORRELATION: concentrated live-return exposure | "
                f"{symbol} {side} reinforces {len(reinforcing)} position(s) | {detail}"
            ),rows

        if reinforcing:
            x=max(reinforcing,key=lambda y:abs(y["corr"]))
            return True,(
                f"real correlation CAUTION | {symbol} {side} vs {x['symbol']} {x['side']} "
                f"corr={x['corr']:+.2f} n={x['samples']}"
            ),rows

        strongest=max(rows,key=lambda x:abs(x["corr"])) if rows else None
        if strongest:
            return True,(
                f"real correlation OK | strongest={strongest['symbol']} "
                f"corr={strongest['corr']:+.2f} n={strongest['samples']}"
            ),rows
        return True,"real correlation unavailable/insufficient history",rows

    def correlation_exposure_guard(self,symbol,side,proposed_volume):
        """Block/flag concentrated portfolio exposure before margin execution.

        This is intentionally deterministic and conservative. It does not claim
        statistical correlation from live returns; it detects shared currency/
        asset-factor exposure that commonly makes nominally different positions
        behave like the same macro bet.
        """
        positions=self._bot_positions()
        new_symbol=str(symbol or "")
        new_side=str(side or "").upper()
        new_volume=max(0.0,float(proposed_volume or 0.0))
        category=classify_symbol(mt5.symbol_info(new_symbol))

        # ---------- FX currency-factor exposure ----------
        if category=="FOREX":
            new_exp=self._directional_currency_exposure(new_symbol,new_side)
            if new_exp:
                aggregate={}
                contributors=[]
                for p in positions:
                    psym=str(getattr(p,"symbol","") or "")
                    if classify_symbol(mt5.symbol_info(psym))!="FOREX":
                        continue
                    pside="BUY" if int(getattr(p,"type",-1))==mt5.POSITION_TYPE_BUY else "SELL"
                    pvol=max(0.0,float(getattr(p,"volume",0.0) or 0.0))
                    exp=self._directional_currency_exposure(psym,pside)
                    for c,v in exp.items():
                        aggregate[c]=aggregate.get(c,0.0)+v*pvol
                    contributors.append(f"{psym}:{pside}")

                projected=dict(aggregate)
                for c,v in new_exp.items():
                    projected[c]=projected.get(c,0.0)+v*new_volume

                # Count how many existing FX positions reinforce one of the new
                # trade's currency directions.
                reinforcing=0
                for p in positions:
                    psym=str(getattr(p,"symbol","") or "")
                    try:
                        if classify_symbol(mt5.symbol_info(psym))!="FOREX":
                            continue
                    except Exception:
                        continue
                    pside="BUY" if int(getattr(p,"type",-1))==mt5.POSITION_TYPE_BUY else "SELL"
                    pexp=self._directional_currency_exposure(psym,pside)
                    if any(pexp.get(c,0.0)*v>0 for c,v in new_exp.items()):
                        reinforcing+=1

                # Hard ceiling: too many trades expressing the same currency bet.
                if reinforcing>=3:
                    return False,(
                        f"CORRELATION GUARD: FX concentration too high | "
                        f"{new_symbol} {new_side} reinforces {reinforcing} existing FX position(s) | "
                        f"projected={','.join(f'{k}:{v:+.2f}' for k,v in sorted(projected.items()) if abs(v)>1e-9)}"
                    )

                # Soft warning at two reinforcing positions; MarginGuard still decides.
                if reinforcing==2:
                    return True,(
                        f"correlation CAUTION | {new_symbol} {new_side} reinforces 2 FX positions | "
                        f"projected={','.join(f'{k}:{v:+.2f}' for k,v in sorted(projected.items()) if abs(v)>1e-9)}"
                    )

                return True,(
                    f"correlation OK FX | reinforcing={reinforcing} | "
                    f"projected={','.join(f'{k}:{v:+.2f}' for k,v in sorted(projected.items()) if abs(v)>1e-9) or 'neutral'}"
                )

        # ---------- Crypto / metals / indices factor concentration ----------
        factor_map={
            "CRYPTO":"CRYPTO_BETA",
            "METALS":"METALS",
            "INDICES":"RISK_ASSETS",
            "ENERGIES":"ENERGY",
            "STOCKS":"EQUITY_BETA",
        }
        factor=factor_map.get(category)
        if factor:
            same_factor=[]
            same_direction=[]
            for p in positions:
                psym=str(getattr(p,"symbol","") or "")
                try:
                    pcat=classify_symbol(mt5.symbol_info(psym))
                except Exception:
                    continue
                if factor_map.get(pcat)!=factor:
                    continue
                pside="BUY" if int(getattr(p,"type",-1))==mt5.POSITION_TYPE_BUY else "SELL"
                same_factor.append(psym)
                if pside==new_side:
                    same_direction.append(psym)

            if len(same_direction)>=3:
                return False,(
                    f"CORRELATION GUARD: {factor} concentration too high | "
                    f"{len(same_direction)} same-direction position(s): {','.join(same_direction[:5])}"
                )
            if len(same_direction)==2:
                return True,(
                    f"correlation CAUTION | {factor} same-direction exposure=2 | "
                    f"{','.join(same_direction)}"
                )
            return True,f"correlation OK {factor} | same-direction={len(same_direction)}"

        return True,f"correlation OK {category}"

    def portfolio_guard(self, symbol, side, proposed_volume, entry, sl, final_confidence):
        """Decide whether another position may be added without stressing margin/risk."""
        account=mt5.account_info()
        if account is None:
            return False, "account_info unavailable"

        positions=self._bot_positions()
        total_positions=len(positions)
        same_symbol=[p for p in positions if str(getattr(p,"symbol","")) == str(symbol)]

        correlation_ok,correlation_msg=self.correlation_exposure_guard(
            symbol,side,proposed_volume
        )
        if not correlation_ok:
            return False,correlation_msg

        real_corr_ok,real_corr_msg,real_corr_rows=self.real_correlation_guard(symbol,side)
        if not real_corr_ok:
            return False,real_corr_msg

        if total_positions >= int(self.s.max_open_positions):
            return False, f"emergency bot-position ceiling reached ({total_positions}/{self.s.max_open_positions})"

        if len(same_symbol) >= int(self.s.max_symbol_positions):
            return False, (
                f"emergency {symbol} position ceiling reached "
                f"({len(same_symbol)}/{self.s.max_symbol_positions})"
            )

        # Scaling into an existing symbol requires stronger confidence.
        if same_symbol and float(final_confidence) < float(self.s.add_position_min_conf):
            return False, (
                f"add-position confidence {final_confidence:.2f} "
                f"< {self.s.add_position_min_conf:.2f}"
            )

        # Avoid stacking several entries at essentially the same price.
        if same_symbol:
            info=mt5.symbol_info(symbol)
            point=_safe_float(getattr(info,"point",0))
            tick=mt5.symbol_info_tick(symbol)
            px=_safe_float(getattr(tick,"ask" if side=="BUY" else "bid",0))
            # Dynamic same-symbol spacing: stronger FINAL signals may pyramid closer,
            # while margin and aggregate stop-risk remain the ultimate portfolio guards.
            new_risk_distance=abs(float(entry)-float(sl))
            base_min_distance=new_risk_distance * float(self.s.add_position_min_distance_atr)
            conf=float(final_confidence)
            if conf >= 0.93:
                spacing_factor=0.50
            elif conf >= 0.85:
                spacing_factor=0.70
            else:
                spacing_factor=1.00
            min_distance=base_min_distance * spacing_factor
            if min_distance > 0:
                for p in same_symbol:
                    old_px=_safe_float(getattr(p,"price_open",0))
                    actual_distance=abs(px-old_px)
                    if old_px > 0 and actual_distance < min_distance:
                        return False, (
                            f"entry too close to existing {symbol} position "
                            f"({actual_distance:.5f} < {min_distance:.5f}) | "
                            f"dynamic spacing={spacing_factor:.2f}x | FINAL={conf:.2f} | "
                            f"existing entry={old_px:.5f} | new entry={px:.5f}"
                        )

        # Existing portfolio stop-risk.
        equity=_safe_float(getattr(account,"equity",0))
        if equity <= 0:
            return False, "equity unavailable"

        existing_risk=0.0
        missing_sl=0
        for p in positions:
            r=self._position_stop_risk_money(p)
            if r <= 0:
                missing_sl += 1
            existing_risk += r

        if missing_sl:
            return False, f"{missing_sl} bot position(s) have no valid SL"

        order_type=mt5.ORDER_TYPE_BUY if side=="BUY" else mt5.ORDER_TYPE_SELL

        # Proposed trade stop risk.
        try:
            proposed_loss=mt5.order_calc_profit(
                order_type, symbol, float(proposed_volume), float(entry), float(sl)
            )
            proposed_risk=abs(min(0.0, float(proposed_loss or 0.0)))
        except Exception:
            proposed_risk=0.0

        total_risk=existing_risk+proposed_risk
        max_risk=equity*(float(self.s.max_total_risk_pct)/100.0)
        if max_risk > 0 and total_risk > max_risk:
            return False, (
                f"portfolio SL risk too high: {total_risk:.2f} > {max_risk:.2f} "
                f"({self.s.max_total_risk_pct:.2f}% equity)"
            )

        # Broker-estimated margin for the new position.
        try:
            required_margin=mt5.order_calc_margin(
                order_type, symbol, float(proposed_volume), float(entry)
            )
            required_margin=float(required_margin or 0.0)
        except Exception:
            required_margin=0.0

        current_margin=_safe_float(getattr(account,"margin",0))
        current_free=_safe_float(getattr(account,"margin_free",0))

        if required_margin > 0 and current_free <= required_margin:
            return False, (
                f"insufficient free margin: need {required_margin:.2f}, "
                f"free {current_free:.2f}"
            )

        projected_margin=current_margin+required_margin
        projected_margin_level=(
            (equity/projected_margin)*100.0 if projected_margin > 0 else 999999.0
        )

        if projected_margin_level < float(self.s.min_margin_level_pct):
            return False, (
                f"projected margin level {projected_margin_level:.1f}% "
                f"< safety floor {self.s.min_margin_level_pct:.1f}%"
            )

        spacing_detail=""
        if same_symbol:
            info=mt5.symbol_info(symbol)
            tick=mt5.symbol_info_tick(symbol)
            px=_safe_float(getattr(tick,"ask" if side=="BUY" else "bid",0))
            old_prices=[_safe_float(getattr(p,"price_open",0)) for p in same_symbol]
            old_prices=[v for v in old_prices if v > 0]
            if old_prices:
                nearest_px=min(old_prices,key=lambda v:abs(px-v))
                actual_distance=abs(px-nearest_px)
                new_risk_distance=abs(float(entry)-float(sl))
                base_min_distance=new_risk_distance * float(self.s.add_position_min_distance_atr)
                conf=float(final_confidence)
                spacing_factor=0.50 if conf >= 0.93 else (0.70 if conf >= 0.85 else 1.00)
                min_distance=base_min_distance*spacing_factor
                spacing_detail=(
                    f" | spacing OK={actual_distance:.5f}>={min_distance:.5f} "
                    f"({spacing_factor:.2f}x, FINAL={conf:.2f}) | "
                    f"existing entry={nearest_px:.5f} | new entry={px:.5f}"
                )

        return True, (
            f"portfolio OK | positions={total_positions+1}/{self.s.max_open_positions} emergency | "
            f"{symbol}={len(same_symbol)+1}/{self.s.max_symbol_positions} emergency | "
            f"projected margin level={projected_margin_level:.1f}% | "
            f"SL risk={total_risk:.2f}/{max_risk:.2f} | "
            f"{correlation_msg} | {real_corr_msg}"
        ) + spacing_detail




class ZpiIntelligence:
    """Quota-aware Zapi/ZPI intelligence provider for news and macro calendar."""
    POSITIVE={
        "surge","rally","gain","gains","bullish","breakout","approval","approved",
        "adoption","record high","inflow","inflows","beats","upgrade","growth",
        "partnership","launch","rises","rise","rebound","strong"
    }
    NEGATIVE={
        "crash","drop","drops","bearish","selloff","hack","hacked","exploit",
        "lawsuit","ban","banned","outflow","outflows","misses","downgrade",
        "decline","falls","fall","liquidation","fraud","weak","risk"
    }

    def __init__(self,s,log=None):
        self.s=s
        self.log=log or (lambda *_:None)
        self.cache={}
        self.negative_cache={}
        self.request_count=0
        # V3.10.8: account-wide ZPI 429 cooldown. ZPI keys share the account quota,
        # so one 429 pauses all ZPI endpoints instead of hammering the API each loop.
        self.rate_limit_until=0.0
        self.rate_limit_cooldown_seconds=600
        self._rate_limit_logged=False

        # V3.10.28: endpoint-isolated resilience. One slow upstream must not
        # degrade the whole ZPI context.
        self.endpoint_health={}
        self.endpoint_backoff={}
        self.endpoint_timeout_default=max(3,int(getattr(self.s,"zpi_timeout",12) or 12))
        self.endpoint_timeout_overrides={
            "fear-greed:crypto":8,
            "finance:tradingview:technicals":10,
            "finance:tradingview:news":12,
            "finance:tradingview:calendar":12,
            "finance:binance:klines":10,
            "finance:binance:ticker":10,
            "finance:binance:depth":10,
        }

    def configured(self):
        return bool(getattr(self.s,"zpi_enabled",True) and getattr(self.s,"zpi_api_key",""))

    def _endpoint_id(self,project,endpoint):
        return f"{str(project).strip(':')}:{str(endpoint).strip(':')}"

    def _endpoint_timeout(self,project,endpoint):
        eid=self._endpoint_id(project,endpoint)
        # allow project-specific and endpoint-only overrides
        return max(3,int(
            self.endpoint_timeout_overrides.get(
                eid,
                self.endpoint_timeout_overrides.get(
                    str(endpoint),
                    self.endpoint_timeout_default
                )
            )
        ))

    def _endpoint_state(self,project,endpoint):
        eid=self._endpoint_id(project,endpoint)
        row=dict(self.endpoint_health.get(eid) or {})
        now=time.time()
        until=float(row.get("open_until",0.0) or 0.0)
        if until>now:
            return eid,"OPEN",int(until-now)
        if until>0:
            row["open_until"]=0.0
            row["failures"]=0
            row["state"]="HALF_OPEN"
            self.endpoint_health[eid]=row
            return eid,"HALF_OPEN",0
        return eid,"CLOSED",0

    def _mark_endpoint_success(self,eid,elapsed):
        self.endpoint_health[eid]={
            "state":"CLOSED","failures":0,"open_until":0.0,
            "last_ok":time.time(),"last_elapsed":float(elapsed)
        }

    def _mark_endpoint_failure(self,eid,kind,elapsed):
        row=dict(self.endpoint_health.get(eid) or {})
        fails=int(row.get("failures",0) or 0)+1
        row.update({
            "failures":fails,"last_error":str(kind),
            "last_error_at":time.time(),"last_elapsed":float(elapsed)
        })
        # 503/timeout are treated as upstream-health issues. Backoff grows
        # modestly and caps well below the account-wide 429 cooldown.
        if fails>=2:
            backoff=min(300,30*(2**min(3,fails-2)))
            row["open_until"]=time.time()+backoff
            row["state"]="OPEN"
            self.endpoint_backoff[eid]=backoff
        else:
            row["state"]="DEGRADED"
        self.endpoint_health[eid]=row
        return row

    def _stale_or_none(self,hit,status):
        if hit:
            return hit["data"],status
        return None,status

    def _get(self,project,endpoint,params,cache_key,ttl_minutes):
        if not self.configured():
            return None,"NO_KEY"

        now=time.time()
        hit=self.cache.get(cache_key)
        fresh_ttl=max(60,int(ttl_minutes)*60)

        # Serve fresh cache first.
        if hit and now-hit["at"] < fresh_ttl:
            return hit["data"],"CACHE"

        # Account-wide 429 cooldown remains authoritative.
        if now < self.rate_limit_until:
            if hit:
                return hit["data"],"STALE_CACHE"
            if not self._rate_limit_logged:
                remaining=max(1,int(self.rate_limit_until-now))
                self.log(f"ZPI COOLDOWN: rate limit active for ~{remaining}s; network calls paused.")
                self._rate_limit_logged=True
            return None,"RATE_LIMIT_COOLDOWN"

        if self.rate_limit_until and now >= self.rate_limit_until:
            self.rate_limit_until=0.0
            self._rate_limit_logged=False
            self.log("ZPI COOLDOWN ENDED: API requests resumed.")

        # Per-endpoint circuit: only the sick endpoint pauses.
        eid,state,remaining=self._endpoint_state(project,endpoint)
        if state=="OPEN":
            if hit:
                return hit["data"],"STALE_CACHE_ENDPOINT"
            return None,"ENDPOINT_COOLDOWN"

        neg=self.negative_cache.get(cache_key)
        if neg and now-neg["at"] < max(900,fresh_ttl):
            return None,neg.get("status","UNSUPPORTED")

        url=f"{self.s.zpi_base_url}/v1/{project}/{endpoint}"
        timeout=self._endpoint_timeout(project,endpoint)
        started=time.perf_counter()

        # One network attempt per cycle. Retry storms waste quota and make the
        # trading loop less responsive, so retries happen only after backoff.
        try:
            r=requests.get(
                url,
                params=params,
                headers={"x-api-key":self.s.zpi_api_key},
                timeout=timeout,
            )
            self.request_count+=1
            elapsed=time.perf_counter()-started

            if r.status_code==429:
                retry_after=r.headers.get("Retry-After")
                try:
                    cooldown=max(60,min(3600,int(float(retry_after)))) if retry_after else self.rate_limit_cooldown_seconds
                except Exception:
                    cooldown=self.rate_limit_cooldown_seconds
                self.rate_limit_until=now+cooldown
                self._rate_limit_logged=False
                if hit:
                    self.log(f"ZPI RATE LIMIT 429: pausing API calls for {cooldown}s; using stale cached context.")
                    return hit["data"],"STALE_CACHE"
                self.log(f"ZPI RATE LIMIT 429: pausing API calls for {cooldown}s; no cache available, using fail-safe neutral context.")
                return None,"RATE_LIMIT"

            if r.status_code==404:
                self.negative_cache[cache_key]={"at":now,"status":"UNSUPPORTED"}
                self.log(
                    f"ZPI ENDPOINT UNSUPPORTED: {eid} | "
                    f"{params.get('symbol','?')} {params.get('timeframe','')} | neutral fallback"
                )
                return None,"UNSUPPORTED"

            if r.status_code in {500,502,503,504}:
                health=self._mark_endpoint_failure(eid,f"HTTP_{r.status_code}",elapsed)
                backoff=max(0,int(float(health.get("open_until",0) or 0)-time.time()))
                self.log(
                    f"ZPI ENDPOINT DEGRADED: {eid} | HTTP {r.status_code} | "
                    f"{elapsed:.1f}s | failures={health.get('failures',1)}"
                    + (f" | circuit {backoff}s" if backoff>0 else "")
                )
                if hit:
                    return hit["data"],"STALE_CACHE_ENDPOINT"
                return None,f"HTTP_{r.status_code}"

            r.raise_for_status()
            body=r.json()

            if isinstance(body,dict):
                if body.get("errors"):
                    self.log(f"ZPI API ERROR [{eid}]: {body.get('errors')}")
                if "content" in body:
                    data=body.get("content")
                elif "data" in body:
                    data=body.get("data")
                else:
                    data=body
            else:
                data=body

            self.cache[cache_key]={"at":now,"data":data}
            self._mark_endpoint_success(eid,elapsed)
            return data,"LIVE"

        except requests.exceptions.Timeout as e:
            elapsed=time.perf_counter()-started
            health=self._mark_endpoint_failure(eid,"TIMEOUT",elapsed)
            backoff=max(0,int(float(health.get("open_until",0) or 0)-time.time()))
            self.log(
                f"ZPI ENDPOINT TIMEOUT: {eid} | timeout={timeout}s | "
                f"elapsed={elapsed:.1f}s | failures={health.get('failures',1)}"
                + (f" | circuit {backoff}s" if backoff>0 else "")
            )
            if hit:
                return hit["data"],"STALE_CACHE_ENDPOINT"
            return None,"TIMEOUT"

        except requests.exceptions.RequestException as e:
            elapsed=time.perf_counter()-started
            health=self._mark_endpoint_failure(eid,type(e).__name__,elapsed)
            backoff=max(0,int(float(health.get("open_until",0) or 0)-time.time()))
            self.log(
                f"ZPI ENDPOINT ERROR: {eid} | {type(e).__name__}: {e} | "
                f"failures={health.get('failures',1)}"
                + (f" | circuit {backoff}s" if backoff>0 else "")
            )
            if hit:
                return hit["data"],"STALE_CACHE_ENDPOINT"
            return None,"ERROR"

        except Exception as e:
            elapsed=time.perf_counter()-started
            health=self._mark_endpoint_failure(eid,type(e).__name__,elapsed)
            self.log(f"ZPI ENDPOINT ERROR: {eid} | {type(e).__name__}: {e}")
            if hit:
                return hit["data"],"STALE_CACHE_ENDPOINT"
            return None,"ERROR"

    def endpoint_health_snapshot(self):
        now=time.time()
        out=[]
        for eid,row in sorted(self.endpoint_health.items()):
            open_until=float(row.get("open_until",0) or 0)
            state="OPEN" if open_until>now else str(row.get("state","CLOSED"))
            out.append({
                "endpoint":eid,
                "state":state,
                "failures":int(row.get("failures",0) or 0),
                "cooldown_remaining":max(0,int(open_until-now)),
                "last_elapsed":float(row.get("last_elapsed",0) or 0),
                "last_error":str(row.get("last_error","") or ""),
            })
        return out

    def symbol_mapping(self,symbol):
        raw=re.sub(r"[^A-Z0-9]","",str(symbol or "").upper())
        crypto_assets=["BTC","ETH","SOL","TON","XRP","DOGE","LTC","BNB","ADA","AVAX","DOT","LINK"]
        asset=next((x for x in crypto_assets if raw.startswith(x) or raw==x),None)
        if asset:
            return {
                "asset":asset,
                "market":"crypto",
                "tv_symbol":f"BINANCE:{asset}USDT",
                "binance_symbol":f"{asset}USDT",
                "countries":"US",
            }
        if raw.startswith("XAU"):
            return {"asset":"XAU","market":"cfd","tv_symbol":"OANDA:XAUUSD","binance_symbol":None,"countries":"US"}
        if len(raw)>=6 and raw[:3].isalpha() and raw[3:6].isalpha():
            base,quote=raw[:3],raw[3:6]
            country_map={"USD":"US","EUR":"DE","GBP":"GB","JPY":"JP","AUD":"AU","CAD":"CA","CHF":"CH","NZD":"NZ"}
            countries=",".join(dict.fromkeys([country_map.get(base,""),country_map.get(quote,"")])).strip(",") or "US"
            return {"asset":base,"market":"forex","tv_symbol":f"FX:{base}{quote}","binance_symbol":None,"countries":countries}
        return {"asset":raw[:8] or "UNKNOWN","market":"crypto","tv_symbol":raw,"binance_symbol":None,"countries":"US"}

    @staticmethod
    def tv_timeframe(tf):
        return {
            "M1":"1m","M5":"5m","M15":"15m","M30":"30m",
            "H1":"1h","H2":"2h","H4":"4h","D1":"1d","W1":"1w","MN1":"1M"
        }.get(str(tf or "").upper(),"15m")

    @staticmethod
    def binance_interval(tf):
        return {
            "M1":"1m","M5":"5m","M15":"15m","M30":"30m",
            "H1":"1h","H2":"2h","H4":"4h","D1":"1d","W1":"1w","MN1":"1M"
        }.get(str(tf or "").upper(),"15m")

    @classmethod
    def lexical_sentiment(cls,title):
        words=re.sub(r"[^a-z0-9 ]"," ",str(title).lower())
        pos=sum(1 for x in cls.POSITIVE if x in words)
        neg=sum(1 for x in cls.NEGATIVE if x in words)
        if pos+neg==0: return 0.0
        return max(-1.0,min(1.0,(pos-neg)/max(1,pos+neg)))

    def news(self,symbol):
        mp=self.symbol_mapping(symbol)
        params={"symbol":mp["tv_symbol"],"market":mp["market"],"lang":"en","count":max(5,min(50,int(self.s.zpi_news_count)))}
        data,source=self._get(
            "finance:tradingview","news",params,
            f"news:{mp['tv_symbol']}",
            int(self.s.zpi_news_cache_minutes)
        )
        if not isinstance(data,dict):
            return {"status":source,"score":0.0,"items":[],"count":0,"source":"ZPI"}

        items=data.get("items",[]) if isinstance(data.get("items",[]),list) else []
        scored=[]
        now=time.time()
        weighted=0.0
        weights=0.0
        for item in items:
            if not isinstance(item,dict): continue
            title=str(item.get("title","")).strip()
            if not title: continue
            sent=self.lexical_sentiment(title)
            published=float(item.get("publishedAt",0) or 0)
            age_h=max(0.0,(now-published)/3600) if published else 24.0
            freshness=max(0.15,1.0-min(age_h,48.0)/56.0)
            urgency=float(item.get("urgency",1) or 1)
            w=freshness*max(0.5,min(2.0,urgency))
            weighted+=sent*w
            weights+=w
            scored.append({
                "title":title,
                "source":str(item.get("source","")),
                "url":str(item.get("url","")),
                "publishedAt":published,
                "age_hours":age_h,
                "sentiment":sent,
                "urgency":urgency,
            })
        score=weighted/weights if weights else 0.0
        status="READY" if source in {"LIVE","CACHE"} else "DEGRADED"
        return {"status":status,"transport":source,"score":max(-1,min(1,score)),"items":scored,"count":len(scored),"source":"ZPI/TradingView","mapping":mp}

    def calendar(self,symbol):
        mp=self.symbol_mapping(symbol)
        today=datetime.now(timezone.utc).date()
        params={
            "countries":mp["countries"],
            "from":today.isoformat(),
            "to":(today+timedelta(days=2)).isoformat(),
            "importance":"medium",
        }
        data,source=self._get(
            "finance:tradingview","calendar",params,
            f"calendar:{mp['countries']}:{today.isoformat()}",
            int(self.s.zpi_calendar_cache_minutes)
        )
        if not isinstance(data,dict):
            return {"status":source,"events":[],"blackout":False,"nearest":""}

        events=[]
        blackout=False
        nearest=None
        now=datetime.now(timezone.utc)
        for ev in data.get("items",[]) if isinstance(data.get("items",[]),list) else []:
            if not isinstance(ev,dict): continue
            try:
                dt=datetime.fromisoformat(str(ev.get("date","")).replace("Z","+00:00"))
                if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
                mins=(dt.astimezone(timezone.utc)-now).total_seconds()/60
            except Exception:
                continue
            if -15 <= mins <= 24*60:
                row={
                    "title":str(ev.get("title","")),
                    "country":str(ev.get("country","")),
                    "currency":str(ev.get("currency","")),
                    "importance":str(ev.get("importance","")).upper(),
                    "minutes_to_event":mins,
                    "actual":ev.get("actual"),
                    "forecast":ev.get("forecast"),
                    "previous":ev.get("previous"),
                }
                events.append(row)
                if nearest is None or abs(mins)<abs(nearest["minutes_to_event"]):
                    nearest=row
                if row["importance"]=="HIGH" and -5 <= mins <= int(self.s.macro_high_impact_block_minutes):
                    blackout=True
        risk_level="NORMAL"
        if nearest:
            imp=str(nearest.get("importance","")).upper()
            mins=abs(float(nearest.get("minutes_to_event",999999) or 999999))
            if imp=="HIGH" and mins<=int(self.s.macro_high_impact_block_minutes):
                risk_level="HIGH"
            elif imp=="HIGH":
                risk_level="ELEVATED"
            elif imp=="MEDIUM" and mins<=60:
                risk_level="ELEVATED"

        return {
            "status":"READY" if source in {"LIVE","CACHE"} else "DEGRADED","transport":source,
            "events":events,"blackout":blackout,
            "nearest":nearest or {},"risk_level":risk_level
        }

    def binance_micro(self,symbol):
        mp=self.symbol_mapping(symbol)
        pair=mp.get("binance_symbol")
        if not pair:
            return {"status":"N/A","score":0.0}
        ticker,src1=self._get("finance:binance","ticker",{"symbol":pair},f"bt:{pair}",5)
        depth,src2=self._get("finance:binance","depth",{"symbol":pair,"limit":20},f"bd:{pair}",5)
        if not isinstance(ticker,dict):
            return {"status":src1,"score":0.0}
        change=float(ticker.get("changePercent",0) or 0)
        score=max(-0.6,min(0.6,change/12.0))
        imbalance=0.0
        if isinstance(depth,dict):
            bids=depth.get("bids",[]) or []
            asks=depth.get("asks",[]) or []
            b=sum(float(x.get("amount",0) or 0) for x in bids if isinstance(x,dict))
            a=sum(float(x.get("amount",0) or 0) for x in asks if isinstance(x,dict))
            if b+a>0: imbalance=(b-a)/(b+a)
            score=max(-1,min(1,score+0.35*imbalance))
        return {"status":"READY","score":score,"change24h":change,"orderbook_imbalance":imbalance,"pair":pair}

    def tradingview_technicals(self,symbol,tf):
        mp=self.symbol_mapping(symbol)
        params={
            "symbol":mp["tv_symbol"],
            "market":mp["market"],
            "timeframe":self.tv_timeframe(tf),
        }
        data,source=self._get(
            "finance:tradingview","technicals",params,
            f"tvtech:{mp['tv_symbol']}:{params['timeframe']}",
            int(self.s.zpi_technicals_cache_minutes)
        )
        if not isinstance(data,dict):
            return {
                "status":source,"score":0.0,"summary":"UNAVAILABLE",
                "timeframe":params["timeframe"],
                "symbol":mp["tv_symbol"],
            }

        raw=data.get("ratingAll")
        if raw is None:
            summary=str(data.get("summary","neutral")).lower()
            if "strong buy" in summary:
                raw=1.0
            elif "buy" in summary:
                raw=0.55
            elif "strong sell" in summary:
                raw=-1.0
            elif "sell" in summary:
                raw=-0.55
            else:
                raw=0.0
        try:
            score=max(-1.0,min(1.0,float(raw)))
        except Exception:
            score=0.0

        return {
            "status":"READY","transport":source,
            "score":score,
            "summary":str(data.get("summary","neutral")).upper(),
            "oscillators":str(data.get("oscillatorsSummary","")).upper(),
            "moving_averages":str(data.get("movingAveragesSummary","")).upper(),
            "rsi":data.get("rsi"),"adx":data.get("adx"),"atr":data.get("atr"),
            "timeframe":params["timeframe"],
        }

    def fear_greed(self,symbol):
        mp=self.symbol_mapping(symbol)
        if mp.get("market")!="crypto":
            return {"status":"N/A","score":0.0,"raw":None,"rating":"N/A"}

        data,source=self._get(
            "finance:fear-greed","crypto",{"count":7},
            "feargreed:crypto",
            int(self.s.zpi_fear_greed_cache_minutes)
        )
        if not isinstance(data,dict):
            return {"status":source,"score":0.0,"raw":None,"rating":"UNKNOWN"}

        try:
            raw=float(data.get("score",50) or 50)
        except Exception:
            raw=50.0
        score=max(-1.0,min(1.0,(raw-50.0)/50.0))
        return {
            "status":"READY" if source in {"LIVE","CACHE"} else "DEGRADED","transport":source,
            "score":score,"raw":raw,
            "rating":str(data.get("rating","neutral")).upper(),
        }

    def binance_klines(self,symbol,tf):
        mp=self.symbol_mapping(symbol)
        pair=mp.get("binance_symbol")
        if not pair:
            return {"status":"N/A","score":0.0}

        params={"symbol":pair,"interval":self.binance_interval(tf),"count":30}
        data,source=self._get(
            "finance:binance","klines",params,
            f"bk:{pair}:{params['interval']}",
            int(self.s.zpi_klines_cache_minutes)
        )
        if not isinstance(data,dict):
            return {"status":source,"score":0.0}

        candles=data.get("candles",[]) if isinstance(data.get("candles",[]),list) else []
        closes=[]
        volumes=[]
        for c in candles:
            if not isinstance(c,dict):
                continue
            try:
                closes.append(float(c.get("close")))
                volumes.append(float(c.get("volumeQuote",c.get("volumeBase",0)) or 0))
            except Exception:
                pass

        if len(closes)<3:
            return {"status":"READY","score":0.0,"pair":pair,"count":len(closes)}

        ref=closes[-min(6,len(closes))]
        ret=(closes[-1]/ref-1.0) if ref else 0.0
        score=max(-1.0,min(1.0,ret*18.0))

        vol_ratio=1.0
        if len(volumes)>=6:
            avg=sum(volumes[-6:-1])/5.0
            if avg>0:
                vol_ratio=volumes[-1]/avg
            multiplier=1.0+0.15*max(-0.5,min(1.0,vol_ratio-1.0))
            score=max(-1.0,min(1.0,score*multiplier))

        return {
            "status":"READY","transport":source,
            "score":score,"pair":pair,"count":len(closes),
            "return_window":ret,"volume_ratio":vol_ratio,
            "interval":params["interval"],
        }

    def invalidate_market_cache(self, symbol=None):
        """Drop timeframe-sensitive ZPI cache entries when mode/TF changes."""
        if not symbol:
            self.cache.clear()
            return
        mp=self.symbol_mapping(symbol)
        tokens=(str(mp.get("tv_symbol","")).upper(),str(mp.get("binance_symbol","") or "").upper())
        # V3.10.11: invalidate every symbol-dependent ZPI namespace, not just
        # technicals/klines. Explicit invalidation is safer than attempting to
        # keep stale symbol state synchronized.
        prefixes=("TVTECH:","BK:","BINANCE:","DEPTH:","TICKER:","KLINES:","NEWS:")
        for store in (self.cache,self.negative_cache):
            for key in list(store.keys()):
                ku=str(key).upper()
                if ku.startswith(prefixes) and any(t and t in ku for t in tokens):
                    store.pop(key,None)

    def snapshot(self,symbol,tf=None):
        news=self.news(symbol)
        cal=self.calendar(symbol)
        bm=self.binance_micro(symbol)
        tech=self.tradingview_technicals(symbol,tf)
        fear=self.fear_greed(symbol)
        klines=self.binance_klines(symbol,tf)
        return {
            "news":news,"calendar":cal,"binance":bm,
            "technicals":tech,"fear_greed":fear,"klines":klines,
            "requests":self.request_count
        }

class MacroMicroContext:
    """Macro + instrument-specific context with fail-safe neutral fallback.

    Macro data is never fabricated. It can come from:
      1. a local JSON file, or
      2. an optional HTTP endpoint configured by MACRO_API_URL.

    Expected payload example:
    {
      "updated_at": "2026-08-22T14:00:00+00:00",
      "risk_level": "MEDIUM",
      "bias": {"USD": 0.25, "EUR": -0.10, "BTC": 0.20},
      "events": [
        {"title":"Fed speech","currency":"USD","impact":"HIGH","minutes_to_event":18}
      ]
    }

    Bias range is -1 bearish ... +1 bullish for that asset/currency.
    """
    def __init__(self,s,log=None):
        self.s=s
        self.log=log or (lambda *_:None)
        self._cache=None
        self._cache_at=0.0

    @staticmethod
    def _clamp(v,lo=-1.0,hi=1.0):
        try:
            return max(lo,min(hi,float(v)))
        except Exception:
            return 0.0

    def _load_macro_payload(self):
        if not bool(getattr(self.s,"macro_micro_enabled",True)):
            return None,"DISABLED"

        now=time.time()
        if self._cache is not None and now-self._cache_at < 60:
            return self._cache,"CACHE"

        payload=None
        source="UNAVAILABLE"

        url=str(getattr(self.s,"macro_api_url","") or "").strip()
        if url:
            try:
                headers={}
                api_key=str(getattr(self.s,"macro_api_key","") or "").strip()
                if api_key:
                    headers["Authorization"]="Bearer "+api_key
                r=requests.get(
                    url,headers=headers,
                    timeout=max(2,int(getattr(self.s,"macro_timeout",8)))
                )
                r.raise_for_status()
                obj=r.json()
                if isinstance(obj,dict):
                    payload=obj
                    source="API"
            except Exception:
                payload=None

        if payload is None:
            try:
                p=Path(str(getattr(self.s,"macro_context_file","data/macro_context.json")))
                if not p.is_absolute():
                    p=Path.cwd()/p
                if p.exists():
                    obj=json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(obj,dict):
                        payload=obj
                        source="FILE"
            except Exception:
                payload=None

        self._cache=payload
        self._cache_at=now
        return payload,source

    def _symbol_legs(self,symbol):
        s=str(symbol or "").upper()
        # Strip common broker suffixes/prefix separators conservatively.
        clean=re.sub(r"[^A-Z0-9]","",s)

        known=["USD","EUR","GBP","JPY","CHF","AUD","NZD","CAD","BTC","ETH","XAU","XAG","SOL","DOGE","TON","LTC","XRP"]
        hits=[]
        for asset in known:
            if asset in clean:
                hits.append((clean.find(asset),asset))
        hits=[x[1] for x in sorted(hits)[:2]]

        if len(hits)>=2:
            return hits[0],hits[1]
        if len(hits)==1:
            # Crypto/metals quoted by USD on many brokers even if symbol is simply BTC/TON.
            if hits[0] in {"BTC","ETH","XAU","XAG","SOL","DOGE","TON","LTC","XRP"}:
                return hits[0],"USD"
            return hits[0],None
        return clean[:8] or "UNKNOWN",None

    def macro(self,symbol):
        payload,source=self._load_macro_payload()
        neutral={
            "status":"UNAVAILABLE" if source!="DISABLED" else "DISABLED",
            "source":source,
            "directional_score":0.0,
            "risk_level":"UNKNOWN",
            "blackout":False,
            "event":"",
            "age_minutes":None,
            "base_asset":None,
            "quote_asset":None,
        }
        if not isinstance(payload,dict):
            return neutral

        age_minutes=None
        updated=payload.get("updated_at")
        if updated:
            try:
                dt=datetime.fromisoformat(str(updated).replace("Z","+00:00"))
                if dt.tzinfo is None:
                    dt=dt.replace(tzinfo=timezone.utc)
                age_minutes=max(0.0,(datetime.now(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds()/60.0)
            except Exception:
                age_minutes=None

        max_age=max(1,int(getattr(self.s,"macro_max_age_minutes",180)))
        if age_minutes is not None and age_minutes > max_age:
            neutral.update({"status":"STALE","source":source,"age_minutes":age_minutes})
            return neutral

        base,quote=self._symbol_legs(symbol)
        biases=payload.get("bias",{}) if isinstance(payload.get("bias",{}),dict) else {}
        b=self._clamp(biases.get(base,0.0))
        q=self._clamp(biases.get(quote,0.0)) if quote else 0.0
        directional=self._clamp((b-q)/2.0 if quote else b)

        blackout=False
        event=""
        block_m=max(0,int(getattr(self.s,"macro_high_impact_block_minutes",30)))
        relevant={x for x in (base,quote) if x}
        nearest=None

        for ev in payload.get("events",[]) if isinstance(payload.get("events",[]),list) else []:
            if not isinstance(ev,dict):
                continue
            impact=str(ev.get("impact","")).upper()
            cur=str(ev.get("currency",ev.get("asset",""))).upper()
            try:
                mins=abs(float(ev.get("minutes_to_event",999999)))
            except Exception:
                mins=999999
            if cur in relevant and impact=="HIGH":
                if nearest is None or mins<nearest[0]:
                    nearest=(mins,str(ev.get("title","High-impact event")))
                if mins<=block_m:
                    blackout=True

        if nearest:
            event=f"{nearest[1]} ({nearest[0]:.0f}m)"

        return {
            "status":"READY",
            "source":source,
            "directional_score":directional,
            "risk_level":str(payload.get("risk_level","NORMAL")).upper(),
            "blackout":blackout,
            "event":event,
            "age_minutes":age_minutes,
            "base_asset":base,
            "quote_asset":quote,
        }

    def micro(self,symbol,base,mtf):
        # Directional score >0 bullish, <0 bearish.
        components=[]
        trend=str((base or {}).get("trend","")).upper()
        structure=str((base or {}).get("structure","")).upper()
        rsi=float((base or {}).get("rsi14",50) or 50)
        macd=float((base or {}).get("macd_hist",0) or 0)
        adx=float((base or {}).get("adx14",0) or 0)
        vol_ratio=float((base or {}).get("volume_ratio",1) or 1)
        atr_pct=abs(float((base or {}).get("atr_pct",0) or 0))

        if "BULL" in trend: components.append(0.35)
        elif "BEAR" in trend: components.append(-0.35)

        if "BREAKOUT_UP" in structure or "BULLISH" in structure: components.append(0.30)
        elif "BREAKOUT_DOWN" in structure or "BEARISH" in structure: components.append(-0.30)

        if macd>0: components.append(0.15)
        elif macd<0: components.append(-0.15)

        if rsi>=55: components.append(min(0.15,(rsi-50)/100))
        elif rsi<=45: components.append(max(-0.15,(rsi-50)/100))

        # Higher-timeframe directional agreement.
        dirs=[]
        if isinstance(mtf,dict):
            for v in mtf.values():
                if not isinstance(v,dict): continue
                tr=str(v.get("trend","")).upper()
                if "BULL" in tr: dirs.append(1)
                elif "BEAR" in tr: dirs.append(-1)
        mtf_bias=(sum(dirs)/len(dirs)) if dirs else 0.0
        components.append(0.25*mtf_bias)

        score=self._clamp(sum(components))

        activity="NORMAL"
        if vol_ratio>=1.5: activity="HIGH"
        elif vol_ratio<0.65: activity="LOW"

        volatility="NORMAL"
        if atr_pct>=0.025: volatility="HIGH"
        elif atr_pct<=0.006: volatility="LOW"

        return {
            "status":"READY",
            "directional_score":score,
            "activity":activity,
            "volatility":volatility,
            "volume_ratio":vol_ratio,
            "adx":adx,
            "atr_pct":atr_pct,
            "mtf_bias":mtf_bias,
        }

    def action_alignment(self,action,ctx):
        if action not in {"BUY","SELL"}:
            return 0.0
        d=float((ctx or {}).get("directional_score",0.0) or 0.0)
        return d if action=="BUY" else -d

    def adjust_score(self,action,score,macro,micro):
        if action not in {"BUY","SELL"}:
            return max(0.0,min(1.0,float(score)))

        ma=self.action_alignment(action,macro)
        mi=self.action_alignment(action,micro)
        out=float(score)
        out += float(getattr(self.s,"macro_score_weight",0.10))*ma
        out += float(getattr(self.s,"micro_score_weight",0.08))*mi

        # Explicit macro conflicts and elevated systemic risk reduce conviction.
        risk=str((macro or {}).get("risk_level","")).upper()
        if risk in {"HIGH","EXTREME"}:
            out*=0.88
        if ma < -0.35:
            out*=0.82
        if mi < -0.45:
            out*=0.88

        return max(0.0,min(1.0,out))

    def apply_risk_context(self,profile,action,macro,micro):
        p=dict(profile)
        ma=self.action_alignment(action,macro)
        mi=self.action_alignment(action,micro)
        risk=str((macro or {}).get("risk_level","")).upper()

        factor=1.0
        if risk=="HIGH": factor*=0.75
        elif risk=="EXTREME": factor*=0.55
        if ma < -0.25: factor*=0.70
        if mi < -0.35: factor*=0.80

        p["risk_pct"]=max(
            float(self.s.dynamic_risk_min_pct),
            float(p["risk_pct"])*factor
        )

        if risk in {"HIGH","EXTREME"} or ma < -0.35:
            p["max_entries"]=1

        return p

class TradingEngine:
    def __init__(self,s,mt,llm,mem,risk,log,state):
        self.s=s; self.mt=mt; self.llm=llm; self.mem=mem; self.risk=risk
        self.log=log; self.state=state
        self.running=False
        self.session_start=None
        self.last_candle=None
        self.account_scale=1.0
        self.cooldown_remaining=0
        self.known_closed_positions=set()
        self._last_status_logs={}
        self.session_start_equity=None
        self.auto_profit_target_value=0.0
        self.auto_max_loss_value=0.0
        self.open_trade_context={}
        self.entry_risk_snapshot={}
        self.partial_close_state={}
        self._last_active_bot_position_ids=set()
        self.context_engine=MacroMicroContext(s,log)
        self.zpi=ZpiIntelligence(s,log)
        self._last_intelligence={}

    def set_account_unit_mode(self,mode):
        self.account_scale=100.0 if str(mode).upper().startswith("CENT") else 1.0

    def to_display(self,v): return float(v)/self.account_scale
    def to_raw(self,v): return float(v)*self.account_scale

    def session_pnl(self):
        """Return bot-only realized/floating PnL for the current trading session.

        Broker-triggered SL/TP exits may have magic=0, so ownership cannot be
        determined from the closing deal alone. Completed-position reconstruction
        identifies ownership from the opening deal/order/decision instead.
        """
        if not self.session_start:
            return 0.0,0.0,0.0

        realized=0.0
        try:
            rows=self.mt5_trade_history(days=30,bot_only=True)
            session_ts=float(self.session_start.timestamp())
            for row in rows:
                try:
                    closed_at=str(row.get("closed_at","") or "")
                    if not closed_at:
                        continue
                    closed_dt=datetime.fromisoformat(closed_at.replace("Z","+00:00"))
                    if closed_dt.tzinfo is None:
                        closed_dt=closed_dt.replace(tzinfo=timezone.utc)
                    if float(closed_dt.timestamp()) >= session_ts:
                        realized += float(row.get("pnl_raw",0.0) or 0.0)
                except Exception:
                    continue
        except Exception as e:
            self.log_once(
                "session_realized_history_error",
                f"SESSION REALIZED WARNING: broker-history reconstruction failed: {e}",
                repeat_after=120
            )

        floating=sum(
            float(getattr(p,"profit",0.0) or 0.0)
            + float(getattr(p,"swap",0.0) or 0.0)
            for p in self.mt.positions()
        )
        return realized,floating,realized+floating

    def start(self,symbol,tf,live_ack,trading_mode="AUTO"):
        import threading
        if self.running:
            return
        if self.s.live_trading and not live_ack:
            raise RuntimeError("Centang konfirmasi SEND ORDERS terlebih dahulu.")

        self.symbol=str(symbol or "").strip()
        # V3.10.11: every start gets a fresh symbol-context generation.
        # Market intelligence produced for another symbol/generation must never
        # be allowed into Council or order planning.
        self._context_generation=int(getattr(self,"_context_generation",0) or 0)+1
        self._context_symbol=self.symbol
        self._last_intelligence={}
        self.requested_tf=str(tf).upper()
        self.trading_mode=str(trading_mode or "AUTO").upper()
        if self.trading_mode not in {"AUTO","SCALPING","INTRADAY","SWING"}:
            self.trading_mode="AUTO"
        self.tf=self._resolve_mode_timeframe(self.trading_mode,self.requested_tf)
        # V3.7.4: a mode/TF switch must never reuse old TV technical/klines context.
        try:
            self.zpi.invalidate_market_cache(symbol)
        except Exception:
            pass
        self.session_start=datetime.now(timezone.utc)
        self.session_start_equity=None
        self._refresh_auto_session_limits()
        self.last_candle=None
        self.running=True
        self.cooldown_remaining=0

        if self.trading_mode=="AUTO" and bool(getattr(self.s,"auto_dynamic_timeframe",True)):
            self.log(
                f"TRADING MODE: AUTO | entry TF=DYNAMIC | initial/fallback TF={self.tf} | "
                f"candidates={getattr(self.s,'auto_tf_candidates','M1,M5,M15,M30,H1,H4')}"
            )
        else:
            self.log(
                f"TRADING MODE: {self.trading_mode} | entry TF={self.tf} | requested TF={self.requested_tf}"
            )
        self.log(
            "DYNAMIC AUTO RISK READY: waiting for a valid setup to calculate "
            "risk %, RR, entry cap, and session guard."
        )
        threading.Thread(target=self.loop,daemon=True).start()
    def stop_safe(self):
        self.running=False
        self.log("ENGINE STATE: SAFE_STOP | NEW ENTRY=DISABLED | existing broker SL/TP remain active.")
        self.state({"status":"STOPPED","search_status":"SAFE STOP","ai_status":"IDLE"})

    def close_all_stop(self,why):
        self.running=False
        self.log("CLOSE ALL & STOP: "+why)
        for ticket,res in self.mt.close_all():
            self.log(f"close {ticket}: retcode={getattr(res,'retcode',None)}")
        self.state({"status":"STOPPED"})

    def mt5_trade_history(self, days=30, bot_only=True):
        """Reconstruct completed positions from broker history.

        Uses entry/exit deals and order history. Balance/credit/deposit rows are excluded.
        """
        end_dt=datetime.now(timezone.utc)+timedelta(seconds=5)
        start_dt=end_dt-timedelta(days=max(1,int(days)))

        deals=list(mt5.history_deals_get(start_dt,end_dt) or [])
        orders=list(mt5.history_orders_get(start_dt,end_dt) or [])

        # Orders keyed for SL/TP and comments.
        order_by_ticket={}
        for o in orders:
            try:
                order_by_ticket[int(getattr(o,"ticket",0) or 0)] = o
            except Exception:
                pass

        bypos={}
        for d in deals:
            try:
                dtype=int(getattr(d,"type",-1))
                # Only real BUY/SELL market deals. Excludes balance/credit/etc.
                if dtype not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                    continue
                pid=int(getattr(d,"position_id",0) or 0)
                if not pid:
                    continue
                bypos.setdefault(pid,[]).append(d)
            except Exception:
                continue

        result=[]

        for pid, ds in bypos.items():
            entries=[
                d for d in ds
                if int(getattr(d,"entry",-1)) in (
                    mt5.DEAL_ENTRY_IN,
                    getattr(mt5,"DEAL_ENTRY_INOUT",2)
                )
            ]
            exits=[
                d for d in ds
                if int(getattr(d,"entry",-1)) in (
                    mt5.DEAL_ENTRY_OUT,
                    mt5.DEAL_ENTRY_OUT_BY,
                    getattr(mt5,"DEAL_ENTRY_INOUT",2)
                )
            ]
            if not entries or not exits:
                continue

            opened=min(entries,key=lambda d:int(getattr(d,"time_msc",0) or getattr(d,"time",0) or 0))
            closed=max(exits,key=lambda d:int(getattr(d,"time_msc",0) or getattr(d,"time",0) or 0))

            # BOT ownership is determined from the OPENING side of the position.
            # A manual close normally has magic=0 / DEAL_REASON_CLIENT, but the
            # position must still remain BOT ONLY if the bot created the entry.
            open_order_ticket=int(getattr(opened,"order",0) or 0)
            open_order=order_by_ticket.get(open_order_ticket)

            entry_magic_match=any(
                int(getattr(d,"magic",0) or 0)==int(self.s.magic)
                for d in entries
            )
            order_magic_match=(
                open_order is not None
                and int(getattr(open_order,"magic",0) or 0)==int(self.s.magic)
            )

            decision_match=False
            try:
                if open_order_ticket:
                    decision_match=self.mem.cx.execute(
                        "SELECT 1 FROM decisions WHERE order_ticket=? LIMIT 1",
                        (open_order_ticket,)
                    ).fetchone() is not None
            except Exception:
                decision_match=False

            is_bot=bool(entry_magic_match or order_magic_match or decision_match)

            if bot_only and not is_bot:
                continue

            symbol=str(getattr(opened,"symbol","") or getattr(closed,"symbol",""))
            side="BUY" if int(getattr(opened,"type",-1))==mt5.DEAL_TYPE_BUY else "SELL"

            open_price=float(getattr(opened,"price",0.0) or 0.0)
            close_price=float(getattr(closed,"price",0.0) or 0.0)

            # Sum exit P/L + exit commission/swap/fees. Entry commission is also real cost,
            # so include commission/fee from all position deals.
            profit=sum(float(getattr(d,"profit",0.0) or 0.0) for d in exits)
            swap=sum(float(getattr(d,"swap",0.0) or 0.0) for d in ds)
            commission=sum(float(getattr(d,"commission",0.0) or 0.0) for d in ds)
            fee=sum(float(getattr(d,"fee",0.0) or 0.0) for d in ds)
            pnl_raw=profit+swap+commission+fee

            volume_in=sum(float(getattr(d,"volume",0.0) or 0.0) for d in entries)
            volume_out=sum(float(getattr(d,"volume",0.0) or 0.0) for d in exits)
            volume=min(volume_in,volume_out) if volume_in and volume_out else max(volume_in,volume_out)

            opened_at=datetime.fromtimestamp(
                int(getattr(opened,"time",0) or 0), tz=timezone.utc
            ).isoformat()
            closed_at=datetime.fromtimestamp(
                int(getattr(closed,"time",0) or 0), tz=timezone.utc
            ).isoformat()

            # Opening order provides the originally requested SL/TP.
            sl=float(getattr(open_order,"sl",0.0) or 0.0) if open_order else 0.0
            tp=float(getattr(open_order,"tp",0.0) or 0.0) if open_order else 0.0

            reason_code=int(getattr(closed,"reason",-999))
            close_reason="UNKNOWN"
            if reason_code==getattr(mt5,"DEAL_REASON_SL",-100):
                close_reason="SL"
            elif reason_code==getattr(mt5,"DEAL_REASON_TP",-101):
                close_reason="TP"
            elif reason_code in {
                getattr(mt5,"DEAL_REASON_CLIENT",-102),
                getattr(mt5,"DEAL_REASON_MOBILE",-104),
                getattr(mt5,"DEAL_REASON_WEB",-105),
            }:
                close_reason="MANUAL/CLIENT"
            elif reason_code==getattr(mt5,"DEAL_REASON_EXPERT",-103):
                close_reason="BOT/EXPERT"

            result_label="WIN" if pnl_raw>0 else ("LOSS" if pnl_raw<0 else "BREAKEVEN")

            # Find local decision context if available.
            dec=None
            try:
                if open_order_ticket:
                    dec=self.mem.cx.execute(
                        "SELECT * FROM decisions WHERE order_ticket=? ORDER BY id DESC LIMIT 1",
                        (open_order_ticket,)
                    ).fetchone()
                if not dec:
                    dec=self.mem.cx.execute(
                        "SELECT * FROM decisions WHERE symbol=? AND action=? ORDER BY id DESC LIMIT 1",
                        (symbol,side)
                    ).fetchone()
            except Exception:
                dec=None

            features={}
            timeframe="UNKNOWN"
            lesson=""
            regime=""
            structure=""

            if dec:
                timeframe=str(dec["timeframe"] or "UNKNOWN")
                try:
                    features=json.loads(dec["features"] or "{}")
                except Exception:
                    features={}
                regime=str(features.get("regime","") or "")
                structure=str(features.get("structure","") or "")

            # Existing learned trade may already contain a lesson.
            try:
                dbrow=self.mem.cx.execute(
                    "SELECT lesson,regime,structure FROM trades WHERE position_id=? LIMIT 1",
                    (pid,)
                ).fetchone()
                if dbrow:
                    lesson=str(dbrow["lesson"] or "")
                    regime=str(dbrow["regime"] or regime)
                    structure=str(dbrow["structure"] or structure)
            except Exception:
                pass

            result.append({
                "position_id":pid,
                "order_ticket":open_order_ticket,
                "symbol":symbol,
                "side":side,
                "volume":volume,
                "open_price":open_price,
                "close_price":close_price,
                "sl":sl,
                "tp":tp,
                "opened_at":opened_at,
                "closed_at":closed_at,
                "profit_raw":profit,
                "swap_raw":swap,
                "commission_raw":commission,
                "fee_raw":fee,
                "pnl_raw":pnl_raw,
                "pnl":self.to_display(pnl_raw),
                "result":result_label,
                "close_reason":close_reason,
                "is_bot":is_bot,
                "magic":int(getattr(opened,"magic",0) or 0),
                "entry_magic_match":entry_magic_match,
                "order_magic_match":order_magic_match,
                "decision_match":decision_match,
                "timeframe":timeframe,
                "features":features,
                "lesson":lesson,
                "regime":regime,
                "structure":structure,
            })

        result.sort(key=lambda r:r["closed_at"], reverse=True)
        return result

    def sync_learning_from_mt5_history(self, days=30):
        """Use broker-completed bot trades as the learning source of truth."""
        rows=self.mt5_trade_history(days=days,bot_only=True)
        synced=0

        for row in rows:
            try:
                existing=self.mem.cx.execute(
                    "SELECT id FROM trades WHERE position_id=? LIMIT 1",
                    (int(row["position_id"]),)
                ).fetchone()

                if existing:
                    continue

                lesson=row.get("lesson") or ""
                if not lesson:
                    try:
                        lesson=self.llm.reflect({
                            "position_id":row["position_id"],
                            "symbol":row["symbol"],
                            "timeframe":row["timeframe"],
                            "side":row["side"],
                            "opened_at":row["opened_at"],
                            "closed_at":row["closed_at"],
                            "pnl":row["pnl"],
                            "close_reason":row["close_reason"],
                            "features":row["features"],
                        })
                    except Exception:
                        lesson=""

                if not str(lesson).strip():
                    lesson=(
                        f"{row['result']}: broker history synchronized. "
                        f"Close reason={row['close_reason']}."
                    )

                row2=dict(row)
                row2["lesson"]=lesson
                self.mem.sync_trade_from_mt5(row2)
                synced += 1

                self.log(
                    f"HISTORY SYNC: position={row['position_id']} | "
                    f"{row['side']} {row['symbol']} | PnL={row['pnl']:+.2f} | "
                    f"{row['result']} | {row['close_reason']}"
                )
            except Exception as e:
                self.log(
                    f"HISTORY SYNC WARNING: position={row.get('position_id')} | {e}"
                )

        if synced:
            self.log(f"LEARNING HISTORY SYNCED: {synced} completed bot trade(s).")

        # Always refresh UI stats. A trade may already exist in SQLite while the
        # GUI card is still stale after reconnect/restart.
        try:
            stats=self.mem.stats()
            self.state({
                "trades":int(stats.get("total",0) or 0),
                "win_rate":float(stats.get("win_rate",0.0) or 0.0),
                "profit_factor":float(stats.get("profit_factor",0.0) or 0.0),
                "loss_streak":int(stats.get("consecutive_losses",0) or 0),
            })
        except Exception as e:
            self.log(f"LEARNING STATS REFRESH WARNING: {e}")
        return synced

    def live_readiness_guard(self,profile,plan,final_score):
        """Hard gate used only when MT5 account type is REAL.

        Demo accounts remain available for normal forward testing. On a REAL
        account, passing signal/risk logic alone is not enough: account,
        execution and testing-readiness checks must also pass.
        """
        info=self.mt.account()
        if info is None:
            return False,["account_info unavailable"],{"account_type":"UNKNOWN"}

        trade_mode=int(getattr(info,"trade_mode",-1) or -1)
        real_mode=int(getattr(mt5,"ACCOUNT_TRADE_MODE_REAL",2))
        demo_mode=int(getattr(mt5,"ACCOUNT_TRADE_MODE_DEMO",0))
        contest_mode=int(getattr(mt5,"ACCOUNT_TRADE_MODE_CONTEST",1))
        account_type=(
            "REAL" if trade_mode==real_mode else
            "DEMO" if trade_mode==demo_mode else
            "CONTEST" if trade_mode==contest_mode else "UNKNOWN"
        )
        detail={"account_type":account_type,"trade_mode":trade_mode}

        # Guard is deliberately REAL-only. Demo/contest remain test environments.
        if account_type!="REAL":
            return True,[],detail

        failures=[]
        warnings=[]

        terminal=mt5.terminal_info()
        if terminal is None:
            failures.append("terminal_info unavailable")
        else:
            if not bool(getattr(terminal,"connected",True)):
                failures.append("terminal not connected")
            if not bool(getattr(terminal,"trade_allowed",True)):
                failures.append("terminal AutoTrading/trade permission disabled")

        if not bool(getattr(info,"trade_allowed",True)):
            failures.append("account trading not allowed")
        if bool(getattr(info,"trade_expert",True)) is False:
            failures.append("expert/algorithmic trading not allowed")

        equity=float(getattr(info,"equity",0.0) or 0.0)
        margin_free=float(getattr(info,"margin_free",0.0) or 0.0)
        margin_level=float(getattr(info,"margin_level",0.0) or 0.0)
        if equity<=0:
            failures.append("equity <= 0")
        if margin_free<=0:
            failures.append("free margin <= 0")
        if margin_level>0 and margin_level<1000:
            failures.append(f"margin level too low for live entry ({margin_level:.0f}%)")

        risk_pct=float((profile or {}).get("risk_pct",0.0) or 0.0)
        live_risk_cap=float(getattr(self.s,"live_readiness_max_risk_pct",0.50) or 0.50)
        if risk_pct<=0:
            failures.append("invalid risk percentage")
        elif risk_pct>live_risk_cap:
            failures.append(f"risk {risk_pct:.2f}% exceeds live cap {live_risk_cap:.2f}%")

        entry=float((plan or {}).get("entry",0.0) or 0.0)
        sl=float((plan or {}).get("sl",0.0) or 0.0)
        tp=float((plan or {}).get("tp",0.0) or 0.0)
        rr=float((plan or {}).get("rr",0.0) or 0.0)
        min_rr=max(2.0,float(getattr(self.s,"adaptive_min_reward_risk",2.0) or 2.0))
        if entry<=0 or sl<=0 or tp<=0:
            failures.append("ENTRY/SL/TP must all be valid")
        if rr<min_rr:
            failures.append(f"RR {rr:.2f} below live minimum 1:{min_rr:.2f}")

        stress=dict((profile or {}).get("account_stress") or {})
        stress_level=str(stress.get("level","NORMAL") or "NORMAL").upper()
        if stress_level=="CRITICAL":
            failures.append("account stress is CRITICAL")
        elif stress_level=="DEFENSIVE":
            warnings.append("account stress is DEFENSIVE")

        global_stats=self.mem.stats()
        shadow_stats=self.mem.shadow_stats()
        tested=int(global_stats.get("total",0) or 0)+int(shadow_stats.get("total",0) or 0)
        min_tests=max(3,int(getattr(self.s,"live_readiness_min_test_trades",5) or 5))
        if tested<min_tests:
            failures.append(f"insufficient forward/shadow testing ({tested}/{min_tests} completed)")
        detail["completed_tests"]=tested
        detail["required_tests"]=min_tests

        loss_streak=int(global_stats.get("consecutive_losses",0) or 0)
        if loss_streak>=3:
            failures.append(f"live entry blocked after loss streak {loss_streak}")

        if float(final_score or 0.0)<max(0.70,float(getattr(self.s,"min_final_score",0.0) or 0.0)):
            failures.append(f"final score {float(final_score or 0.0):.2f} below live readiness floor 0.70")

        # Context ownership from V3.10.11 must still be current at execution.
        if str(getattr(self,"_context_symbol","") or "")!=str(self.symbol or ""):
            failures.append("symbol context ownership mismatch")

        detail.update({
            "risk_pct":risk_pct,
            "risk_cap":live_risk_cap,
            "rr":rr,
            "stress":stress_level,
            "margin_level":margin_level,
            "warnings":warnings,
        })
        return len(failures)==0,failures,detail

    def update_shadow_trades(self):
        """Resolve PAPER trades by hypothetical TP/SL without contaminating real learning."""
        closed=0
        for row in self.mem.open_shadow_trades():
            try:
                tick=mt5.symbol_info_tick(str(row["symbol"]))
                if tick is None: continue
                side=str(row["side"]).upper()
                px=float(getattr(tick,"bid" if side=="BUY" else "ask",0.0) or 0.0)
                if px<=0: continue
                entry=float(row["entry"]);sl=float(row["sl"]);tp=float(row["tp"])
                hit=None
                if side=="BUY":
                    if px<=sl: hit=("LOSS","SL",sl)
                    elif px>=tp: hit=("WIN","TP",tp)
                else:
                    if px>=sl: hit=("LOSS","SL",sl)
                    elif px<=tp: hit=("WIN","TP",tp)
                if not hit: continue
                result,reason,exit_px=hit
                move=(exit_px-entry) if side=="BUY" else (entry-exit_px)
                self.mem.close_shadow_trade(row["id"],exit_px,result,reason,move)
                closed+=1
                self.log(f"SHADOW CLOSED: #{row['id']} {side} {row['symbol']} {row['timeframe']} | "
                         f"{result} {reason} | entry={entry} exit={exit_px} | move={move:+.6f}")
            except Exception as e:
                self.log(f"SHADOW UPDATE WARNING: #{row.get('id')} | {e}")
        if closed:
            st=self.mem.shadow_stats()
            self.log(f"SHADOW STATS: closed={st['total']} | W={st['wins']} L={st['losses']} | "
                     f"WR={st['win_rate']*100:.1f}% | open={st['open']}")
        return closed

    def active_entry_snapshot(self):
        try:
            positions=[
                p for p in (mt5.positions_get() or [])
                if int(getattr(p,"magic",0) or 0)==int(self.s.magic)
            ]
        except Exception:
            positions=[]
        if not positions or not self.entry_risk_snapshot:
            return None

        for p in positions:
            ids={
                int(getattr(p,"ticket",0) or 0),
                int(getattr(p,"identifier",0) or 0),
            }
            for k,v in self.entry_risk_snapshot.items():
                if int(k) in ids:
                    return dict(v)

        symbol=str(getattr(positions[0],"symbol",""))
        matches=[
            v for v in self.entry_risk_snapshot.values()
            if str(v.get("symbol",""))==symbol
        ]
        if matches:
            return dict(max(matches,key=lambda x:int(x.get("entry_time",0) or 0)))
        return None

    def _sync_closed_trades(self):
        # Broker history is the source of truth for completed bot positions.
        try:
            current_ids={
                int(getattr(p,"ticket",0) or 0)
                for p in (mt5.positions_get() or [])
                if int(getattr(p,"magic",0) or 0)==int(self.s.magic)
            }
        except Exception:
            current_ids=set()

        disappeared=set(getattr(self,"_last_active_bot_position_ids",set()) or set())-current_ids
        synced=self.sync_learning_from_mt5_history(days=30)

        if disappeared:
            self.log(
                "POSITION CLOSED DETECTED: "
                + ", ".join(str(x) for x in sorted(disappeared))
                + f" | history synced={synced}"
            )
        self._last_active_bot_position_ids=current_ids
        return synced

    def _score(self, technical_side, technical_conf, llm_action, llm_conf, memory_stats):
        # Weighted ensemble. Memory weight grows only after enough same-side examples.
        memory_score=float(memory_stats.get("memory_score",0.5))
        n=int(memory_stats.get("same_side_count",0))
        mem_weight=0.25 if n>=self.s.min_similar_for_weight else 0.08
        tech_weight=0.42
        llm_weight=1.0-tech_weight-mem_weight

        agree = 1.0 if llm_action==technical_side and llm_action in {"BUY","SELL"} else 0.0
        if llm_action=="HOLD":
            llm_component=max(0.0,llm_conf*0.4)
        elif agree:
            llm_component=llm_conf
        else:
            llm_component=max(0.0,1.0-llm_conf)

        final=tech_weight*technical_conf + llm_weight*llm_component + mem_weight*memory_score

        # conflict penalty
        if llm_action in {"BUY","SELL"} and technical_side in {"BUY","SELL"} and llm_action!=technical_side:
            final*=0.72
        return max(0.0,min(1.0,final))

    def _select_auto_timeframe(self):
        """Select the best entry timeframe from local MT5 market structure.

        AUTO mode evaluates multiple timeframes using only already-available MT5
        OHLCV/indicators, then ZPI/LLM analysis runs on the selected timeframe.
        This avoids multiplying external API calls by the number of candidates.
        """
        raw=str(getattr(self.s,"auto_tf_candidates","M1,M5,M15,M30,H1,H4") or "")
        candidates=[x.strip().upper() for x in raw.split(",") if x.strip()]
        allowed={"M1","M5","M15","M30","H1","H4","D1"}
        candidates=[x for x in candidates if x in allowed]
        if not candidates:
            candidates=["M1","M5","M15","M30","H1","H4"]

        scored=[]
        snapshots={}
        for tf in candidates:
            try:
                snap=snapshot(self.mt.rates(self.symbol,tf,self.s.bars))
                side,conf=technical_score(snap)
                if side not in {"BUY","SELL"}:
                    continue

                regime=str(snap.get("regime","")).upper()
                structure=str(snap.get("structure","")).upper()
                trend=str(snap.get("trend","")).upper()
                adx=max(0.0,float(snap.get("adx14",0.0) or 0.0))
                vol_ratio=max(0.0,float(snap.get("volume_ratio",1.0) or 1.0))
                atr_pct=max(0.0,abs(float(snap.get("atr_pct",0.0) or 0.0)))

                score=float(conf)
                if "TRENDING" in regime:
                    score+=0.08
                elif "RANGING" in regime:
                    score-=0.04

                if side=="BUY":
                    if "BULLISH" in structure or "BREAKOUT_UP" in structure:
                        score+=0.08
                    if "BULL" in trend:
                        score+=0.05
                else:
                    if "BEARISH" in structure or "BREAKOUT_DOWN" in structure:
                        score+=0.08
                    if "BEAR" in trend:
                        score+=0.05

                if adx>=30:
                    score+=0.08
                elif adx>=25:
                    score+=0.04
                elif adx<15:
                    score-=0.04

                if vol_ratio>=1.2:
                    score+=0.04
                elif vol_ratio<0.5:
                    score-=0.03

                # Penalize extremely dead candles and extremely noisy micro TFs.
                if atr_pct<=0:
                    score-=0.05
                if tf=="M1" and "RANGING" in regime:
                    score-=0.06

                score=max(0.0,min(1.20,score))
                snapshots[tf]=snap
                scored.append({
                    "tf":tf,"side":side,"confidence":float(conf),"score":score,
                    "regime":regime,"structure":structure,"trend":trend,
                    "adx":adx,"volume_ratio":vol_ratio,"atr_pct":atr_pct,
                })
            except Exception as e:
                self.log_once(
                    f"auto_tf:{self.symbol}:{tf}",
                    f"AUTO TF WARNING: {self.symbol} {tf} unavailable: {e}",
                    repeat_after=180
                )

        if not scored:
            return str(getattr(self,"requested_tf","M15") or "M15").upper(),{},[]

        # Cross-timeframe directional agreement adds confirmation.
        for row in scored:
            agree=sum(
                1 for other in scored
                if other["tf"]!=row["tf"]
                and other["side"]==row["side"]
                and other["confidence"]>=0.65
            )
            oppose=sum(
                1 for other in scored
                if other["tf"]!=row["tf"]
                and other["side"]!=row["side"]
                and other["confidence"]>=0.75
            )
            row["score"]=max(0.0,min(1.25,row["score"]+min(agree,3)*0.025-min(oppose,2)*0.04))
            row["agree"]=agree
            row["oppose"]=oppose

        scored.sort(key=lambda x:(x["score"],x["confidence"]),reverse=True)
        best=scored[0]
        min_conf=float(getattr(self.s,"auto_tf_min_confidence",0.58))
        if best["confidence"]<min_conf:
            fallback=str(getattr(self,"requested_tf","M15") or "M15").upper()
            return fallback,snapshots.get(fallback,{}),scored

        # Hysteresis prevents AUTO jumping timeframes on tiny score changes.
        current=str(getattr(self,"tf","") or "").upper()
        if current and current!=best["tf"]:
            current_row=next((x for x in scored if x["tf"]==current),None)
            margin=float(getattr(self.s,"auto_tf_switch_margin",0.06))
            if current_row and best["score"] < current_row["score"]+margin:
                best=current_row

        return best["tf"],snapshots.get(best["tf"],{}),scored

    def _resolve_mode_timeframe(self, mode, requested_tf):
        mode=str(mode or "AUTO").upper()
        requested=str(requested_tf or "M15").upper()
        if mode=="SCALPING":
            return requested if requested in {"M1","M5"} else "M5"
        if mode=="INTRADAY":
            return requested if requested in {"M5","M15","M30","H1"} else "M15"
        if mode=="SWING":
            return requested if requested in {"H1","H4"} else "H1"
        # AUTO starts from the requested TF only as an initial/fallback value.
        # The live loop dynamically selects the actual entry timeframe.
        return requested

    def _effective_trade_mode(self):
        """Resolve the mode that actually governs risk/SL/TP for this entry.

        Explicit modes always win. AUTO derives the effective style from the
        entry timeframe so M1/M5 behaves as scalping, M15/M30/H1 as intraday,
        and H4/D1 as swing.
        """
        selected=str(getattr(self,"trading_mode","AUTO") or "AUTO").upper()
        if selected in {"SCALPING","INTRADAY","SWING"}:
            return selected
        tf=str(getattr(self,"tf","M15") or "M15").upper()
        if tf in {"M1","M5"}:
            return "SCALPING"
        if tf in {"H4","D1"}:
            return "SWING"
        return "INTRADAY"

    def _mode_context_timeframes(self):
        mode=self._effective_trade_mode()
        if mode=="SCALPING": return [self.tf,"M15","H1"]
        if mode=="INTRADAY": return [self.tf,"H1","H4"]
        if mode=="SWING": return [self.tf,"H4","D1"]
        return [self.tf,"H1","H4"]

    def _apply_mode_profile(self, profile):
        p=dict(profile)
        mode=self._effective_trade_mode()
        p["effective_mode"]=mode
        p["min_tp_pct"]=0.0

        if mode=="SCALPING":
            p["risk_pct"]=min(p["risk_pct"],0.35)
            p["rr"]=max(1.20,min(1.60,p["rr"]))
        elif mode=="INTRADAY":
            p["risk_pct"]=min(p["risk_pct"],0.55)
            p["rr"]=max(1.40,min(2.20,p["rr"]))
        elif mode=="SWING":
            p["risk_pct"]=min(p["risk_pct"],0.60)
            p["rr"]=max(1.80,min(3.00,p["rr"]))
        return p

    def _deterministic_llm_fallback(self, tech_side, tech_conf, tvtech_ctx, micro_ctx, ai_status, klines_ctx=None, binance_ctx=None):
        if not bool(getattr(self.s,"deterministic_fallback_enabled",True)):
            return None
        if ai_status not in {"EMPTY_RESPONSE","TIMEOUT","COOLDOWN","MODEL_DEGRADED","MALFORMED_RESPONSE"}:
            return None
        if tech_side not in {"BUY","SELL"}:
            return None

        tech_conf=float(tech_conf or 0.0)
        direction=1.0 if tech_side=="BUY" else -1.0

        tv_status=str((tvtech_ctx or {}).get("status","READY") or "READY").upper()
        tv_supported=tv_status not in {"UNSUPPORTED","UNAVAILABLE","ERROR"}
        tv=(float((tvtech_ctx or {}).get("score",0.0) or 0.0)*direction) if tv_supported else 0.0
        micro=float((micro_ctx or {}).get("directional_score",0.0) or 0.0)*direction
        klines=float((klines_ctx or {}).get("score",0.0) or 0.0)*direction
        book=float((binance_ctx or {}).get("orderbook_imbalance",0.0) or 0.0)*direction

        tv=max(-1.0,min(1.0,tv))
        micro=max(-1.0,min(1.0,micro))
        klines=max(-1.0,min(1.0,klines))
        book=max(-1.0,min(1.0,book))

        if tv_supported:
            if tech_conf < float(getattr(self.s,"deterministic_fallback_min_tech",0.92)):
                return None
            consensus=(0.38*micro)+(0.32*tv)+(0.20*klines)+(0.10*book)
            positive_votes=sum(x>=0.15 for x in (tv,micro,klines,book))
            negative_votes=sum(x<=-0.15 for x in (tv,micro,klines,book))
            min_consensus=float(getattr(self.s,"deterministic_fallback_min_consensus",0.28))
            min_votes=int(getattr(self.s,"deterministic_fallback_min_votes",2))
            strong_core=(micro>=0.65 and tv>=0.20)
            if not strong_core or positive_votes<min_votes or negative_votes>=2 or consensus<min_consensus:
                return None
            source_mode="FULL"
        else:
            if tech_conf < float(getattr(self.s,"unsupported_fallback_min_tech",0.95)):
                return None
            if micro < float(getattr(self.s,"unsupported_fallback_min_micro",0.80)):
                return None

            klines_available=str((klines_ctx or {}).get("status","")).upper()=="READY"
            binance_available=str((binance_ctx or {}).get("status","")).upper()=="READY"
            if not (klines_available or binance_available):
                # With TV unsupported, TECH+MICRO alone is not enough to trade.
                return None

            # Renormalize over the sources that actually exist. MICRO is always
            # required; KLINES/book can confirm or penalize the fallback.
            weighted_sum=0.62*micro
            weight_total=0.62
            support_values=[micro]
            if klines_available:
                weighted_sum += 0.25*klines
                weight_total += 0.25
                support_values.append(klines)
            if binance_available:
                weighted_sum += 0.13*book
                weight_total += 0.13
                support_values.append(book)
            consensus=weighted_sum/max(weight_total,1e-9)

            positive_votes=sum(x>=0.12 for x in support_values)
            negative_votes=sum(x<=-0.15 for x in support_values)
            min_consensus=float(getattr(self.s,"unsupported_fallback_min_consensus",0.34))
            if positive_votes < 2 or negative_votes >= 2 or consensus < min_consensus:
                return None
            source_mode="TV_UNSUPPORTED"

        base=float(getattr(self.s,"deterministic_fallback_confidence",0.69))
        if source_mode=="TV_UNSUPPORTED":
            base-=0.03
        conf=base+max(0.0,consensus-min_consensus)*0.08
        conf=max(0.58,min(0.72,conf))

        return {
            "action":tech_side,
            "confidence":conf,
            "consensus":consensus,
            "positive_votes":positive_votes,
            "negative_votes":negative_votes,
            "source_mode":source_mode,
            "reason":(
                f"weighted fallback after {ai_status}: mode={source_mode}, "
                f"TECH={tech_conf:.2f}, TV={tv:+.2f}, MICRO={micro:+.2f}, "
                f"KLINES={klines:+.2f}, BOOK={book:+.2f}, "
                f"consensus={consensus:+.2f}, votes={positive_votes}+/ {negative_votes}-"
            )
        }

    def _hold_abstain_override(
        self, tech_side, tech_conf, llm_action, llm_conf, llm_detail,
        tvtech_ctx, micro_ctx, klines_ctx, binance_ctx, macro_ctx, tf
    ):
        """Treat a cautious LLM HOLD as abstention only under strong consensus.

        This does not override an opposing BUY/SELL LLM decision. It only applies
        when the LLM explicitly chooses HOLD and deterministic/external evidence
        is unusually strong in one direction.
        """
        if not bool(getattr(self.s,"hold_override_enabled",True)):
            return None
        if str(llm_action).upper()!="HOLD":
            return None
        if tech_side not in {"BUY","SELL"}:
            return None
        if bool((macro_ctx or {}).get("blackout",False)):
            return None

        direction=1.0 if tech_side=="BUY" else -1.0
        tech_conf=float(tech_conf or 0.0)
        tv_status=str((tvtech_ctx or {}).get("status","READY") or "READY").upper()
        if tv_status in {"UNSUPPORTED","UNAVAILABLE","ERROR"}:
            # HOLD override requires an independent TV confirmation. Unsupported
            # sources continue to use the stricter failure/degraded fallback path.
            return None

        tv=max(-1.0,min(1.0,float((tvtech_ctx or {}).get("score",0.0) or 0.0)*direction))
        micro=max(-1.0,min(1.0,float((micro_ctx or {}).get("directional_score",0.0) or 0.0)*direction))
        klines=max(-1.0,min(1.0,float((klines_ctx or {}).get("score",0.0) or 0.0)*direction))
        book=max(-1.0,min(1.0,float((binance_ctx or {}).get("orderbook_imbalance",0.0) or 0.0)*direction))

        is_m1=str(tf or "").upper()=="M1"
        if is_m1:
            min_tech=float(getattr(self.s,"hold_override_m1_min_tech",0.95))
            min_tv=float(getattr(self.s,"hold_override_m1_min_tv",0.30))
            min_micro=float(getattr(self.s,"hold_override_m1_min_micro",0.80))
            min_consensus=float(getattr(self.s,"hold_override_m1_min_consensus",0.38))
        else:
            min_tech=float(getattr(self.s,"hold_override_other_min_tech",0.93))
            min_tv=float(getattr(self.s,"hold_override_other_min_tv",0.25))
            min_micro=float(getattr(self.s,"hold_override_other_min_micro",0.70))
            min_consensus=float(getattr(self.s,"hold_override_other_min_consensus",0.34))

        if tech_conf < min_tech or tv < min_tv or micro < min_micro:
            return None

        # LLM context may be MIXED/NEUTRAL while abstaining, but two explicit
        # directional contradictions are enough to block an override.
        d=llm_detail or {}
        expected="BULLISH" if tech_side=="BUY" else "BEARISH"
        opposite="BEARISH" if tech_side=="BUY" else "BULLISH"
        context_values=[
            str(d.get("trend","MIXED")).upper(),
            str(d.get("momentum","MIXED")).upper(),
            str(d.get("structure","NEUTRAL")).upper(),
        ]
        context_opposition=sum(x==opposite for x in context_values)
        if context_opposition>=2:
            return None

        # TECH is already a mandatory gate; consensus measures independent market
        # confirmation. TV/MICRO dominate, KLINES/book can support or penalize.
        consensus=(0.42*micro)+(0.34*tv)+(0.16*klines)+(0.08*book)
        positive_votes=sum(x>=0.12 for x in (tv,micro,klines,book))
        negative_votes=sum(x<=-0.15 for x in (tv,micro,klines,book))

        if consensus < min_consensus:
            return None
        if positive_votes < 2:
            return None
        if negative_votes >= 2:
            return None

        base=float(getattr(self.s,"hold_override_confidence",0.69))
        # Never let an abstain override masquerade as stronger than a normal LLM.
        conf=base+max(0.0,consensus-min_consensus)*0.06
        if is_m1:
            conf-=0.01
        conf=max(0.62,min(0.72,conf))

        return {
            "action":tech_side,
            "confidence":conf,
            "consensus":consensus,
            "positive_votes":positive_votes,
            "negative_votes":negative_votes,
            "llm_hold_confidence":float(llm_conf or 0.0),
            "timeframe":str(tf or "").upper(),
            "reason":(
                f"LLM HOLD treated as abstain: TECH={tech_conf:.2f}, "
                f"TV={tv:+.2f}, MICRO={micro:+.2f}, KLINES={klines:+.2f}, "
                f"BOOK={book:+.2f}, consensus={consensus:+.2f}, "
                f"votes={positive_votes}+/ {negative_votes}-"
            )
        }

    def _regime_strategy_profile(self,base,final_action):
        """Deterministic strategy adaptation for the current market regime.

        This layer never flips BUY/SELL. It changes how selective/risky/ambitious
        the existing setup is allowed to be under trending, ranging or transition
        conditions, then blends in exact-regime historical memory.
        """
        regime=str((base or {}).get("regime","UNKNOWN") or "UNKNOWN").upper()
        structure=str((base or {}).get("structure","NEUTRAL") or "NEUTRAL").upper()
        adx=max(0.0,float((base or {}).get("adx14",0.0) or 0.0))
        volume_ratio=max(0.0,float((base or {}).get("volume_ratio",1.0) or 1.0))
        mode=self._effective_trade_mode()

        if "TRENDING" in regime:
            style="TREND_FOLLOWING"
            quality_mult=1.04
            risk_mult=1.00
            target_mult=1.10
            min_quality=0.48
            if "HIGH_VOL" in regime:
                risk_mult=0.90
                target_mult=1.18
            elif "LOW_VOL" in regime:
                target_mult=1.05
        elif "RANGING" in regime:
            style="RANGE_DEFENSIVE"
            quality_mult=0.88
            risk_mult=0.82
            target_mult=0.78
            min_quality=0.62
            if "HIGH_VOL" in regime:
                risk_mult=0.75
                min_quality=0.68
            if adx<15:
                quality_mult*=0.92
        elif "TRANSITION" in regime:
            style="TRANSITION_DEFENSIVE"
            quality_mult=0.91
            risk_mult=0.78
            target_mult=0.90
            min_quality=0.66
        else:
            style="BALANCED"
            quality_mult=0.96
            risk_mult=0.90
            target_mult=0.95
            min_quality=0.58

        # Structure alignment makes trend-following safer, while neutral structure
        # in transition/range conditions demands more selectivity.
        aligned=(
            (final_action=="BUY" and ("BULLISH" in structure or "BREAKOUT_UP" in structure))
            or (final_action=="SELL" and ("BEARISH" in structure or "BREAKOUT_DOWN" in structure))
        )
        if aligned:
            quality_mult=min(1.08,quality_mult+0.03)
        elif structure=="NEUTRAL" and style!="TREND_FOLLOWING":
            quality_mult=max(0.75,quality_mult-0.04)
            min_quality=min(0.75,min_quality+0.03)

        if volume_ratio<0.50 and style in {"RANGE_DEFENSIVE","TRANSITION_DEFENSIVE"}:
            risk_mult*=0.90
            min_quality=min(0.78,min_quality+0.03)

        memory=self.mem.regime_expectancy(
            self.symbol,self.tf,final_action,regime,mode=mode
        )
        mem_score=float(memory.get("score",0.5) or 0.5)
        mem_n=int(memory.get("samples",0) or 0)
        mem_conf=float(memory.get("sample_confidence",0.0) or 0.0)

        # Exact-regime history has bounded influence and is ignored with tiny data.
        if mem_n>=5:
            hist_quality=1.0+(mem_score-0.5)*0.20*mem_conf
            hist_risk=1.0+(mem_score-0.5)*0.20*mem_conf
            hist_target=1.0+(mem_score-0.5)*0.25*mem_conf
            quality_mult*=max(0.90,min(1.08,hist_quality))
            risk_mult*=max(0.85,min(1.05,hist_risk))
            target_mult*=max(0.88,min(1.10,hist_target))
            if mem_score<=0.40:
                min_quality=min(0.80,min_quality+0.05)
            elif mem_score>=0.65:
                min_quality=max(0.45,min_quality-0.03)

        return {
            "regime":regime,
            "style":style,
            "quality_multiplier":max(0.70,min(1.10,quality_mult)),
            "risk_multiplier":max(0.60,min(1.00,risk_mult)),
            "target_multiplier":max(0.65,min(1.25,target_mult)),
            "minimum_quality":max(0.40,min(0.80,min_quality)),
            "structure_aligned":bool(aligned),
            "history":memory,
        }

    def _drawdown_exposure_controller(self):
        """Return a deterministic de-risk multiplier from account stress.

        This controller can only keep or reduce risk. It never increases risk
        after a loss and therefore cannot behave like martingale.
        """
        info=self.mt.account()
        equity=float(getattr(info,"equity",0.0) or 0.0) if info is not None else 0.0
        balance=float(getattr(info,"balance",0.0) or 0.0) if info is not None else 0.0
        margin=float(getattr(info,"margin",0.0) or 0.0) if info is not None else 0.0
        margin_free=float(getattr(info,"margin_free",0.0) or 0.0) if info is not None else 0.0
        margin_level=float(getattr(info,"margin_level",0.0) or 0.0) if info is not None else 0.0

        if self.session_start_equity is None and equity>0:
            self.session_start_equity=equity
        start_eq=float(self.session_start_equity or equity or 0.0)

        session_dd_pct=0.0
        if start_eq>0 and equity>0 and equity<start_eq:
            session_dd_pct=(start_eq-equity)/start_eq*100.0

        balance_dd_pct=0.0
        if balance>0 and equity>0 and equity<balance:
            balance_dd_pct=(balance-equity)/balance*100.0

        stats=self.mem.stats()
        loss_streak=int(stats.get("consecutive_losses",0) or 0)

        positions=list(self.mt.position_snapshot() or [])
        position_count=len(positions)

        # Start neutral, then only tighten.
        multiplier=1.0
        reasons=[]

        # Session/equity drawdown ladder.
        dd=max(session_dd_pct,balance_dd_pct)
        if dd>=3.0:
            multiplier=min(multiplier,0.45); reasons.append(f"DD {dd:.2f}%")
        elif dd>=2.0:
            multiplier=min(multiplier,0.60); reasons.append(f"DD {dd:.2f}%")
        elif dd>=1.0:
            multiplier=min(multiplier,0.75); reasons.append(f"DD {dd:.2f}%")
        elif dd>=0.50:
            multiplier=min(multiplier,0.88); reasons.append(f"DD {dd:.2f}%")

        # Losing streak never increases risk. Existing hard cooldown still remains.
        if loss_streak>=4:
            multiplier=min(multiplier,0.50); reasons.append(f"loss streak {loss_streak}")
        elif loss_streak==3:
            multiplier=min(multiplier,0.65); reasons.append("loss streak 3")
        elif loss_streak==2:
            multiplier=min(multiplier,0.82); reasons.append("loss streak 2")

        # Portfolio exposure pressure. MarginGuard remains the hard blocker.
        if margin_level>0:
            if margin_level<700:
                multiplier=min(multiplier,0.50); reasons.append(f"margin level {margin_level:.0f}%")
            elif margin_level<1000:
                multiplier=min(multiplier,0.70); reasons.append(f"margin level {margin_level:.0f}%")
            elif margin_level<1500:
                multiplier=min(multiplier,0.85); reasons.append(f"margin level {margin_level:.0f}%")

        if position_count>=8:
            multiplier=min(multiplier,0.60); reasons.append(f"{position_count} open positions")
        elif position_count>=5:
            multiplier=min(multiplier,0.75); reasons.append(f"{position_count} open positions")
        elif position_count>=3:
            multiplier=min(multiplier,0.88); reasons.append(f"{position_count} open positions")

        # Free-margin pressure catches accounts where broker margin_level is zero/unset.
        if equity>0 and margin_free>=0:
            free_ratio=margin_free/equity
            if free_ratio<0.25:
                multiplier=min(multiplier,0.50); reasons.append(f"free margin {free_ratio:.0%}")
            elif free_ratio<0.50:
                multiplier=min(multiplier,0.75); reasons.append(f"free margin {free_ratio:.0%}")

        multiplier=max(0.35,min(1.0,multiplier))
        level=(
            "CRITICAL" if multiplier<=0.50 else
            "DEFENSIVE" if multiplier<=0.75 else
            "CAUTIOUS" if multiplier<1.0 else
            "NORMAL"
        )
        return {
            "multiplier":multiplier,
            "level":level,
            "session_drawdown_pct":session_dd_pct,
            "balance_drawdown_pct":balance_dd_pct,
            "loss_streak":loss_streak,
            "positions":position_count,
            "margin_level":margin_level,
            "margin":margin,
            "margin_free":margin_free,
            "reason":"; ".join(reasons) if reasons else "account stress normal"
        }

    @staticmethod
    def _apply_council_consensus_to_profile(profile,consensus):
        p=dict(profile or {})
        c=dict(consensus or {})
        score=max(0.0,min(1.0,float(c.get("score",0.5) or 0.5)))
        qmult=max(0.90,min(1.05,float(c.get("quality_multiplier",1.0) or 1.0)))
        rmult=max(0.80,min(1.05,float(c.get("risk_multiplier",1.0) or 1.0)))

        p["quality"]=max(0.0,min(1.0,float(p.get("quality",0.0) or 0.0)*qmult))
        # Existing dynamic risk bounds remain authoritative.
        risk_min=float(p.get("_risk_min",0.0) or 0.0)
        risk_max=float(p.get("_risk_max",100.0) or 100.0)
        risk=float(p.get("risk_pct",0.0) or 0.0)*rmult
        if risk_min>0 or risk_max<100:
            risk=max(risk_min,min(risk_max,risk))
        p["risk_pct"]=risk
        p["council_consensus"]=c
        p["council_consensus_score"]=score
        p["council_consensus_grade"]=str(c.get("grade","MIXED"))
        p["council_consensus_quality_multiplier"]=qmult
        p["council_consensus_risk_multiplier"]=rmult
        return p

    def _dynamic_risk_profile(self, final_confidence, base, mtf, final_action):
        """Build a bounded risk profile from signal quality and market conditions.

        LLM never selects lot size. final_confidence is only one input to this
        deterministic/clamped risk policy.
        """
        conf=max(0.0,min(1.0,float(final_confidence or 0.0)))
        regime=str((base or {}).get("regime","")).upper()
        structure=str((base or {}).get("structure","")).upper()
        atr_pct=abs(float((base or {}).get("atr_pct",0.0) or 0.0))
        adx=float((base or {}).get("adx14",0.0) or 0.0)

        # Directional MTF agreement with the actual proposed action.
        agree=0
        directional=0
        if isinstance(mtf,dict):
            for v in mtf.values():
                if not isinstance(v,dict):
                    continue
                trend=str(v.get("trend","")).upper()
                if "BULL" in trend:
                    directional += 1
                    if final_action=="BUY":
                        agree += 1
                elif "BEAR" in trend:
                    directional += 1
                    if final_action=="SELL":
                        agree += 1
        mtf_alignment=(agree/directional) if directional else 0.50

        conf_quality=max(0.0,min(1.0,(conf-0.55)/0.40))

        trend_quality=0.45
        if "TRENDING" in regime:
            trend_quality += 0.18
        if "BREAKOUT" in structure:
            trend_quality += 0.17
        if (
            (final_action=="BUY" and "BULLISH" in structure)
            or (final_action=="SELL" and "BEARISH" in structure)
        ):
            trend_quality += 0.10
        if adx >= 25:
            trend_quality += 0.10
        trend_quality=max(0.0,min(1.0,trend_quality))

        # High volatility lowers risk. ATR% values differ by symbol, so use broad bands.
        if atr_pct >= 0.030:
            volatility_factor=0.50
        elif atr_pct >= 0.020:
            volatility_factor=0.65
        elif atr_pct >= 0.012:
            volatility_factor=0.80
        else:
            volatility_factor=1.00

        raw_quality=(
            0.50*conf_quality
            + 0.30*mtf_alignment
            + 0.20*trend_quality
        )
        quality=max(0.0,min(1.0,raw_quality*volatility_factor))

        # V3.10.20: adapt quality/risk to current market regime before sizing.
        regime_strategy=self._regime_strategy_profile(base,final_action)
        quality=max(
            0.0,min(1.0,
                quality*float(regime_strategy.get("quality_multiplier",1.0) or 1.0)
            )
        )

        risk_min=float(self.s.dynamic_risk_min_pct)
        risk_max=float(self.s.dynamic_risk_max_pct)
        risk_pct=risk_min+(risk_max-risk_min)*quality

        # V3.10.15: historical expectancy is advisory to risk sizing only.
        # It cannot create/flip a trading signal and is ignored with tiny samples.
        expectancy=self.mem.expectancy_profile(
            self.symbol,self.tf,final_action,
            regime=regime,structure=structure,
            mode=self._effective_trade_mode()
        )
        expectancy_mult=float(expectancy.get("risk_multiplier",1.0) or 1.0)
        risk_pct*=expectancy_mult

        regime_risk_mult=float(regime_strategy.get("risk_multiplier",1.0) or 1.0)
        risk_pct*=regime_risk_mult

        # V3.10.16: drawdown/exposure controller may only reduce this proposal.
        stress=self._drawdown_exposure_controller()
        stress_mult=float(stress.get("multiplier",1.0) or 1.0)
        risk_pct*=stress_mult

        rr_min=float(self.s.dynamic_rr_min)
        rr_max=float(self.s.dynamic_rr_max)
        # Weak setup needs more reward relative to risk; strong setup can use a closer TP.
        rr=rr_max-(rr_max-rr_min)*quality

        # V3.8.4: entry count is no longer capped by setup quality or trade mode.
        # MarginGuard/portfolio risk decide whether another entry is allowed.
        # max_open_positions remains only an emergency runaway ceiling.
        max_entries=max(1,int(self.s.max_open_positions))

        sp_min=float(self.s.dynamic_session_profit_min_pct)
        sp_max=float(self.s.dynamic_session_profit_max_pct)
        loss_min=float(self.s.dynamic_session_loss_min_pct)
        loss_max=float(self.s.dynamic_session_loss_max_pct)

        session_profit_pct=sp_min+(sp_max-sp_min)*quality
        session_loss_pct=loss_min+(loss_max-loss_min)*quality

        return {
            "quality":quality,
            "risk_pct":max(risk_min,min(risk_max,risk_pct)),
            "_risk_min":risk_min,
            "_risk_max":risk_max,
            "rr":max(rr_min,min(rr_max,rr)),
            "max_entries":max_entries,
            "session_profit_pct":max(sp_min,min(sp_max,session_profit_pct)),
            "session_loss_pct":max(loss_min,min(loss_max,session_loss_pct)),
            "mtf_alignment":mtf_alignment,
            "volatility_factor":volatility_factor,
            "expectancy":expectancy,
            "expectancy_risk_multiplier":expectancy_mult,
            "regime_strategy":regime_strategy,
            "regime_risk_multiplier":regime_risk_mult,
            "minimum_regime_quality":float(regime_strategy.get("minimum_quality",0.0) or 0.0),
            "account_stress":stress,
            "account_stress_multiplier":stress_mult,
        }

    @staticmethod
    def _parse_hhmm(value):
        try:
            hh,mm=str(value).strip().split(":",1)
            return int(hh)%24,int(mm)%60
        except Exception:
            return 0,0

    @staticmethod
    def _clock_in_window(minutes,start_minutes,end_minutes):
        if start_minutes==end_minutes:
            return True
        if start_minutes < end_minutes:
            return start_minutes <= minutes < end_minutes
        return minutes >= start_minutes or minutes < end_minutes

    def _session_intelligence(self, df, base):
        out={
            "status":"DISABLED","active":"OFF_SESSION","score":0.0,
            "high":0.0,"low":0.0,"mid":0.0,"vwap":0.0,"range_pct":0.0,
            "breakout":"NONE","location":"MID","overlap":False,"timeframe":"-","valid":False
        }
        if not bool(getattr(self.s,"session_intelligence_enabled",True)):
            return out
        try:
            local=df.copy()
            offset=float(getattr(self.s,"session_timezone_offset_hours",7.0))
            local["_local_time"]=pd.to_datetime(local["time"])+pd.to_timedelta(offset,unit="h")
            completed=local.iloc[:-1].copy()
            if len(completed)<5:
                return out
            now=completed.iloc[-1]["_local_time"]
            minute=int(now.hour)*60+int(now.minute)
            definitions=[
                ("TOKYO",getattr(self.s,"session_tokyo_start","07:00"),getattr(self.s,"session_tokyo_end","14:00")),
                ("LONDON",getattr(self.s,"session_london_start","14:00"),getattr(self.s,"session_london_end","20:30")),
                ("NEW_YORK",getattr(self.s,"session_newyork_start","20:30"),getattr(self.s,"session_newyork_end","05:00")),
            ]
            active=[]; masks=[]
            for name,start_s,end_s in definitions:
                sh,sm=self._parse_hhmm(start_s); eh,em=self._parse_hhmm(end_s)
                start_m=sh*60+sm; end_m=eh*60+em
                if self._clock_in_window(minute,start_m,end_m):
                    active.append(name)
                    day=now.normalize()
                    start=day+pd.Timedelta(hours=sh,minutes=sm)
                    end=day+pd.Timedelta(hours=eh,minutes=em)
                    if start_m > end_m:
                        if minute < end_m:
                            start-=pd.Timedelta(days=1)
                        else:
                            end+=pd.Timedelta(days=1)
                    masks.append((completed["_local_time"]>=start)&(completed["_local_time"]<=now))

            if not active:
                out.update({"status":"READY","active":"OFF_SESSION"})
                return out
            mask=masks[0]
            for m in masks[1:]:
                mask=mask|m
            sd=completed.loc[mask].copy()
            if len(sd)<2:
                out.update({"status":"READY","active":"+ ".join(active),"overlap":len(active)>1})
                return out

            prior=sd.iloc[:-1] if len(sd)>2 else sd
            high=float(prior["high"].max())
            low=float(prior["low"].min())
            close=float(base.get("close",sd.iloc[-1]["close"]))
            mid=(high+low)/2.0
            rng=max(high-low,1e-12)
            range_pct=rng/max(abs(close),1e-12)*100.0
            vol=sd["tick_volume"].astype(float).clip(lower=0)
            typical=(sd["high"].astype(float)+sd["low"].astype(float)+sd["close"].astype(float))/3.0
            vwap=float((typical*vol).sum()/vol.sum()) if float(vol.sum())>0 else float(typical.mean())
            atr=max(float(base.get("atr14",0.0) or 0.0),rng*0.05,1e-12)
            breakout="NONE"
            if close > high + 0.10*atr:
                breakout="BREAKOUT_UP"
            elif close < low - 0.10*atr:
                breakout="BREAKOUT_DOWN"

            position=max(0.0,min(1.0,(close-low)/rng))
            score=(position-0.5)*1.2
            score += 0.20 if close>vwap else (-0.20 if close<vwap else 0.0)
            if breakout=="BREAKOUT_UP": score+=0.35
            elif breakout=="BREAKOUT_DOWN": score-=0.35
            score=max(-1.0,min(1.0,score))
            location="UPPER" if position>=0.67 else ("LOWER" if position<=0.33 else "MID")
            out.update({
                "status":"READY","active":"+".join(active),"score":score,
                "high":high,"low":low,"mid":mid,"vwap":vwap,
                "range_pct":range_pct,"breakout":breakout,
                "location":location,"overlap":len(active)>1,"local_time":str(now),"valid":True
            })
            return out
        except Exception as e:
            out.update({"status":"ERROR","error":str(e)})
            return out

    def _fibonacci_context(self, df, base):
        out={
            "status":"DISABLED","direction":"NEUTRAL","score":0.0,
            "swing_high":0.0,"swing_low":0.0,"nearest":"-",
            "nearest_price":0.0,"distance_atr":999.0,
            "extension_1272":0.0,"extension_1618":0.0
        }
        if not bool(getattr(self.s,"fibonacci_enabled",True)):
            return out
        try:
            lookback=max(21,int(getattr(self.s,"fibonacci_lookback_bars",55)))
            completed=df.iloc[:-1].tail(lookback).copy()
            if len(completed)<20:
                return out
            hi_idx=completed["high"].astype(float).idxmax()
            lo_idx=completed["low"].astype(float).idxmin()
            hi=float(completed.loc[hi_idx,"high"]); lo=float(completed.loc[lo_idx,"low"])
            rng=hi-lo
            if rng<=0: return out
            hi_pos=int(completed.index.get_loc(hi_idx)); lo_pos=int(completed.index.get_loc(lo_idx))
            direction="UPSWING" if lo_pos < hi_pos else "DOWNSWING"
            ratios=(0.236,0.382,0.500,0.618,0.786)
            if direction=="UPSWING":
                levels={r:hi-rng*r for r in ratios}
                ext1272=hi+rng*0.272; ext1618=hi+rng*0.618; score=0.35
            else:
                levels={r:lo+rng*r for r in ratios}
                ext1272=lo-rng*0.272; ext1618=lo-rng*0.618; score=-0.35
            close=float(base.get("close",0.0) or 0.0)
            atr=max(float(base.get("atr14",0.0) or 0.0),rng*0.01,1e-12)
            nearest_ratio=min(levels,key=lambda r:abs(close-levels[r]))
            nearest_price=float(levels[nearest_ratio])
            distance_atr=abs(close-nearest_price)/atr
            near_threshold=max(0.05,float(getattr(self.s,"fibonacci_near_atr",0.40)))
            if distance_atr<=near_threshold:
                bonus=0.35 if nearest_ratio in {0.382,0.5,0.618} else 0.20
                score += bonus if direction=="UPSWING" else -bonus
            if direction=="UPSWING" and close<lo: score=-0.15
            elif direction=="DOWNSWING" and close>hi: score=0.15
            score=max(-1.0,min(1.0,score))
            out.update({
                "status":"READY","direction":direction,"score":score,
                "swing_high":hi,"swing_low":lo,
                "nearest":f"{nearest_ratio*100:.1f}%","nearest_price":nearest_price,
                "distance_atr":distance_atr,"extension_1272":ext1272,"extension_1618":ext1618,
                "levels":{f"{r*100:.1f}%":float(px) for r,px in levels.items()}
            })
            return out
        except Exception as e:
            out.update({"status":"ERROR","error":str(e)})
            return out

    def _adaptive_profit_target(
        self, profile, base, final_action, final_score,
        tvtech_ctx=None, micro_ctx=None, klines_ctx=None, binance_ctx=None,
        session_ctx=None, fib_ctx=None
    ):
        """Estimate a realistic price-move target from current market evidence.

        The returned percentage is a target price movement from entry, not an
        account-level guaranteed profit. RiskGuard.plan will additionally enforce
        the minimum reward:risk ratio.
        """
        mode=str(profile.get("effective_mode") or self._effective_trade_mode()).upper()
        if not bool(getattr(self.s,"adaptive_target_enabled",True)):
            return {
                "target_pct":0.0,"floor_pct":0.0,"raw_pct":0.0,
                "max_pct":0.0,"strength":0.0,"reason":"disabled"
            }

        # Configured floors (historically 2%) are now preferred targets.
        # The effective hard floor is instrument/timeframe/ATR aware so a M1 FX
        # scalp is not forced to target a 2% price move when the market projects
        # only a few basis points.
        if mode=="SCALPING":
            preferred_floor=float(getattr(self.s,"adaptive_scalping_floor_pct",2.0))
            ceiling=float(getattr(self.s,"adaptive_scalping_max_target_pct",6.0))
            horizon=4.0
        elif mode=="SWING":
            preferred_floor=float(getattr(self.s,"adaptive_swing_floor_pct",2.0))
            ceiling=float(getattr(self.s,"adaptive_swing_max_target_pct",25.0))
            horizon=14.0
        else:
            preferred_floor=float(getattr(self.s,"adaptive_intraday_floor_pct",2.0))
            ceiling=float(getattr(self.s,"adaptive_intraday_max_target_pct",12.0))
            horizon=8.0

        try:
            asset_category=classify_symbol(self.mt.symbol_info(self.symbol))
        except Exception:
            asset_category="OTHER"

        direction=1.0 if final_action=="BUY" else -1.0
        atr_pct=max(0.0,abs(float((base or {}).get("atr_pct",0.0) or 0.0))*100.0)
        adx=max(0.0,float((base or {}).get("adx14",0.0) or 0.0))
        regime=str((base or {}).get("regime","")).upper()
        structure=str((base or {}).get("structure","")).upper()

        tv=max(-1.0,min(1.0,float((tvtech_ctx or {}).get("score",0.0) or 0.0)*direction))
        micro=max(-1.0,min(1.0,float((micro_ctx or {}).get("directional_score",0.0) or 0.0)*direction))
        klines=max(-1.0,min(1.0,float((klines_ctx or {}).get("score",0.0) or 0.0)*direction))
        book=max(-1.0,min(1.0,float((binance_ctx or {}).get("orderbook_imbalance",0.0) or 0.0)*direction))
        change24=float((binance_ctx or {}).get("change24h",0.0) or 0.0)*direction

        quality=max(0.0,min(1.0,float(profile.get("quality",final_score) or 0.0)))
        final_strength=max(0.0,min(1.0,float(final_score or 0.0)))

        session_align=max(-1.0,min(1.0,float((session_ctx or {}).get("score",0.0) or 0.0)*direction))
        fib_align=max(-1.0,min(1.0,float((fib_ctx or {}).get("score",0.0) or 0.0)*direction))
        evidence=(
            0.27*max(0.0,tv)
            + 0.27*max(0.0,micro)
            + 0.15*max(0.0,klines)
            + 0.08*max(0.0,book)
            + 0.11*final_strength
            + 0.06*max(0.0,session_align)
            + 0.06*max(0.0,fib_align)
        )
        trend_boost=1.0
        if "TRENDING" in regime:
            trend_boost+=0.20
        if (
            (final_action=="BUY" and ("BULLISH" in structure or "BREAKOUT_UP" in structure))
            or (final_action=="SELL" and ("BEARISH" in structure or "BREAKOUT_DOWN" in structure))
        ):
            trend_boost+=0.15
        if adx>=30:
            trend_boost+=0.15
        elif adx>=25:
            trend_boost+=0.08

        # ATR projected over a mode-dependent horizon, then strengthened by
        # independent directional evidence. A strong existing 24h move can
        # support a larger target but is deliberately capped to avoid chasing.
        atr_projection=atr_pct*horizon
        momentum_extension=min(max(change24,0.0)*0.35,ceiling*0.35)
        raw=(atr_projection*(0.55+0.45*quality)*(0.75+0.50*evidence)*trend_boost)
        raw+=momentum_extension

        regime_target_mult=float(
            (profile.get("regime_strategy",{}) or {}).get("target_multiplier",1.0) or 1.0
        )
        raw*=regime_target_mult

        # Build an instrument-aware realistic floor from projected volatility.
        # These are price-move floors, not account-profit guarantees.
        if asset_category=="FOREX":
            absolute_min={"SCALPING":0.02,"INTRADAY":0.08,"SWING":0.20}.get(mode,0.08)
            atr_factor={"SCALPING":0.70,"INTRADAY":0.65,"SWING":0.55}.get(mode,0.65)
        elif asset_category in {"METALS","INDICES","ENERGIES"}:
            absolute_min={"SCALPING":0.05,"INTRADAY":0.15,"SWING":0.40}.get(mode,0.15)
            atr_factor={"SCALPING":0.75,"INTRADAY":0.70,"SWING":0.60}.get(mode,0.70)
        elif asset_category=="CRYPTO":
            absolute_min={"SCALPING":0.15,"INTRADAY":0.40,"SWING":0.80}.get(mode,0.40)
            atr_factor={"SCALPING":0.80,"INTRADAY":0.75,"SWING":0.65}.get(mode,0.75)
        else:
            absolute_min={"SCALPING":0.05,"INTRADAY":0.12,"SWING":0.30}.get(mode,0.12)
            atr_factor={"SCALPING":0.75,"INTRADAY":0.70,"SWING":0.60}.get(mode,0.70)

        realistic_floor=max(absolute_min, atr_projection*atr_factor)
        realistic_floor=min(realistic_floor,preferred_floor,ceiling)

        # Use the preferred 2%+ target only when current market projection/evidence
        # actually supports it; otherwise use the realistic volatility-aware floor.
        preferred_supported=(
            raw >= preferred_floor*0.80
            or (
                atr_projection >= preferred_floor*0.55
                and evidence >= 0.45
                and quality >= 0.75
            )
        )
        effective_floor=preferred_floor if preferred_supported else realistic_floor

        # Weak/mixed evidence should not create huge targets merely from volatility.
        if evidence < 0.20:
            raw=min(raw,effective_floor*1.25)
        elif evidence < 0.35:
            raw=min(raw,max(effective_floor*1.75,effective_floor))

        target=max(effective_floor,min(ceiling,raw))
        fib_extension_pct=0.0
        if (
            bool(getattr(self.s,"fibonacci_tp_extension_enabled",True))
            and quality>=0.85 and fib_align>=0.35
            and str((fib_ctx or {}).get("status","")).upper()=="READY"
        ):
            entry=max(abs(float(base.get("close",0.0) or 0.0)),1e-12)
            ext=float((fib_ctx or {}).get("extension_1272",0.0) or 0.0)
            if final_action=="BUY" and ext>entry:
                fib_extension_pct=(ext-entry)/entry*100.0
            elif final_action=="SELL" and 0<ext<entry:
                fib_extension_pct=(entry-ext)/entry*100.0
            if fib_extension_pct>0:
                target=max(target,min(ceiling,fib_extension_pct))
        return {
            "target_pct":target,
            "floor_pct":effective_floor,
            "preferred_floor_pct":preferred_floor,
            "preferred_supported":preferred_supported,
            "asset_category":asset_category,
            "regime_target_multiplier":regime_target_mult,
            "regime_style":str((profile.get("regime_strategy",{}) or {}).get("style","BALANCED")),
            "raw_pct":raw,
            "max_pct":ceiling,
            "strength":evidence,
            "atr_projection_pct":atr_projection,
            "change24_support":max(change24,0.0),
            "session_alignment":session_align,
            "fibonacci_alignment":fib_align,
            "fibonacci_extension_pct":fib_extension_pct,
            "reason":(
                f"mode={mode}, asset={asset_category}, ATR projection={atr_projection:.2f}%, "
                f"evidence={evidence:.2f}, ADX={adx:.1f}, "
                f"preferred={preferred_floor:.2f}% ({'SUPPORTED' if preferred_supported else 'NOT_SUPPORTED'}), "
                f"effective floor={effective_floor:.2f}%, "
                f"24h support={max(change24,0.0):.2f}%"
            )
        }

    def _manage_active_dynamic_exits(self, symbol, side, target_plan, profile):
        """V3.10.21: professional-style dynamic exit management.

        Safety properties:
        - Broker SL is always retained.
        - SL is monotonic: never loosened away from profit protection.
        - Break-even/profit-lock/trailing are based on R, ATR and structure.
        - TP may extend only when the current regime/evidence still supports it.
        - No partial-close logic here; that remains a later, separate feature.
        """
        if not bool(getattr(self.s,"dynamic_exit_management_enabled",True)):
            return
        if side not in {"BUY","SELL"}:
            return

        target_pct=float((target_plan or {}).get("target_pct",0.0) or 0.0)
        if target_pct<=0:
            return

        try:
            positions=[
                p for p in self.mt.positions(symbol)
                if ("BUY" if int(getattr(p,"type",0))==int(mt5.POSITION_TYPE_BUY) else "SELL")==side
            ]
            if not positions:
                return

            info=self.mt.symbol_info(symbol)
            tick=self.mt.tick(symbol)
            point=float(getattr(info,"point",0.0) or 0.0)
            digits=int(getattr(info,"digits",5) or 5)
            stops=max(0.0,float(getattr(info,"trade_stops_level",0) or 0)*point)
            min_gap=max(stops,point*2.0)

            regime_info=dict((profile or {}).get("regime_strategy") or {})
            regime_style=str(regime_info.get("style","BALANCED") or "BALANCED").upper()
            regime_target_mult=float(regime_info.get("target_multiplier",1.0) or 1.0)
            evidence=float((target_plan or {}).get("strength",0.0) or 0.0)

            # Pull a fresh market snapshot for ATR/structure-aware trailing.
            try:
                exit_df=self.mt.rates(symbol,self.tf,self.s.bars)
                exit_base=snapshot(exit_df)
            except Exception:
                exit_base={}
            atr=max(0.0,float(exit_base.get("atr14",0.0) or 0.0))
            structure=str(exit_base.get("structure","NEUTRAL") or "NEUTRAL").upper()
            swing_low=max(0.0,float(exit_base.get("swing_low_20",0.0) or 0.0))
            swing_high=max(0.0,float(exit_base.get("swing_high_20",0.0) or 0.0))

            # Mode-specific break-even/trailing policy.
            mode=str((profile or {}).get("effective_mode",self._effective_trade_mode()) or "INTRADAY").upper()
            if mode=="SCALPING":
                be_trigger_r=0.70
                lock_trigger_r=1.00
                trail_trigger_r=1.35
                atr_gap_mult=1.10
            elif mode=="SWING":
                be_trigger_r=1.00
                lock_trigger_r=1.50
                trail_trigger_r=2.00
                atr_gap_mult=1.80
            else:
                be_trigger_r=0.85
                lock_trigger_r=1.25
                trail_trigger_r=1.70
                atr_gap_mult=1.45

            # Defensive regimes tighten sooner. Strong trends give price more room.
            if regime_style in {"RANGE_DEFENSIVE","TRANSITION_DEFENSIVE"}:
                be_trigger_r=max(0.50,be_trigger_r-0.15)
                lock_trigger_r=max(be_trigger_r+0.15,lock_trigger_r-0.20)
                trail_trigger_r=max(lock_trigger_r+0.20,trail_trigger_r-0.25)
                atr_gap_mult*=0.85
            elif regime_style=="TREND_FOLLOWING" and evidence>=0.55:
                trail_trigger_r+=0.20
                atr_gap_mult*=1.10

            for p in positions:
                entry=float(getattr(p,"price_open",0.0) or 0.0)
                old_sl=float(getattr(p,"sl",0.0) or 0.0)
                old_tp=float(getattr(p,"tp",0.0) or 0.0)
                if entry<=0:
                    continue

                current=float(tick.bid if side=="BUY" else tick.ask)
                initial_snapshot=None
                ids={
                    int(getattr(p,"ticket",0) or 0),
                    int(getattr(p,"identifier",0) or 0),
                }
                for k,v in self.entry_risk_snapshot.items():
                    if int(k) in ids:
                        initial_snapshot=dict(v); break

                original_sl=float((initial_snapshot or {}).get("sl",0.0) or 0.0)
                if original_sl<=0:
                    original_sl=old_sl

                risk_distance=abs(entry-original_sl) if original_sl>0 else 0.0
                if risk_distance<=max(point,1e-12):
                    # Fall back to ATR rather than inventing a tiny R denominator.
                    risk_distance=max(atr*1.5,entry*0.001,point*10.0)

                favorable_move=(current-entry) if side=="BUY" else (entry-current)
                r_multiple=favorable_move/risk_distance if risk_distance>0 else 0.0
                profit_move=(favorable_move/entry*100.0) if entry else 0.0

                # V3.10.22: scale out only after profit milestones. This never adds
                # exposure and never replaces the broker SL on the remaining position.
                ticket=int(getattr(p,"ticket",0) or 0)
                pc_state=self.partial_close_state.setdefault(ticket,{"stage1":False,"stage2":False})

                if mode=="SCALPING":
                    scale1_r,scale2_r=1.00,1.65
                    scale1_frac,scale2_frac=0.35,0.30
                elif mode=="SWING":
                    scale1_r,scale2_r=1.50,2.75
                    scale1_frac,scale2_frac=0.25,0.30
                else:
                    scale1_r,scale2_r=1.25,2.10
                    scale1_frac,scale2_frac=0.30,0.30

                # Defensive regimes bank profit a little sooner; trending regimes
                # keep more size for the larger move.
                if regime_style in {"RANGE_DEFENSIVE","TRANSITION_DEFENSIVE"}:
                    scale1_r=max(0.80,scale1_r-0.15)
                    scale2_r=max(scale1_r+0.40,scale2_r-0.20)
                    scale1_frac=min(0.40,scale1_frac+0.05)
                elif regime_style=="TREND_FOLLOWING" and evidence>=0.60:
                    scale1_r+=0.10
                    scale2_r+=0.20
                    scale1_frac=max(0.20,scale1_frac-0.05)

                def do_scale_out(stage_key,fraction,trigger_r):
                    if pc_state.get(stage_key):
                        return
                    live_volume=float(getattr(p,"volume",0.0) or 0.0)
                    requested=live_volume*float(fraction)
                    result,closed_vol,msg=self.mt.partial_close_position(p,requested)
                    if result is None:
                        self.log_once(
                            f"partial:{ticket}:{stage_key}:skip",
                            f"PARTIAL CLOSE SKIPPED: {side} {symbol} ticket={ticket} | "
                            f"{stage_key} @ {trigger_r:.2f}R | {msg}",
                            repeat_after=1800
                        )
                        return
                    code=int(getattr(result,"retcode",-1) or -1)
                    if code==getattr(mt5,"TRADE_RETCODE_DONE",10009):
                        pc_state[stage_key]=True
                        pc_state[f"{stage_key}_r"]=float(r_multiple)
                        pc_state[f"{stage_key}_volume"]=float(closed_vol)
                        self.log(
                            f"PARTIAL CLOSE [{stage_key.upper()}]: {side} {symbol} ticket={ticket} | "
                            f"R={r_multiple:+.2f} >= {trigger_r:.2f} | closed={closed_vol:g} | "
                            f"{msg} | regime={regime_style}"
                        )
                        # After realizing partial profit, remaining size must be
                        # protected at least around break-even when broker distance allows.
                    else:
                        self.log(
                            f"PARTIAL CLOSE FAILED: {side} {symbol} ticket={ticket} | "
                            f"{stage_key} | retcode={code} {retcode_name(code)} | "
                            f"{getattr(result,'comment','') if result else mt5.last_error()}"
                        )

                if r_multiple>=scale1_r and not pc_state.get("stage1"):
                    do_scale_out("stage1",scale1_frac,scale1_r)
                if r_multiple>=scale2_r and not pc_state.get("stage2"):
                    # Stage 2 can run only after Stage 1 really executed.
                    if pc_state.get("stage1"):
                        # Refresh position volume after first scale-out when possible.
                        try:
                            fresh=[x for x in self.mt.positions(symbol) if int(getattr(x,"ticket",0) or 0)==ticket]
                            if fresh:
                                p=fresh[0]
                        except Exception:
                            pass
                        do_scale_out("stage2",scale2_frac,scale2_r)

                candidate_sl=old_sl
                exit_stage="HOLD_SL"

                # 1) Break-even: remove most downside after a meaningful favorable move.
                # A successful first scale-out also activates break-even protection.
                if r_multiple>=be_trigger_r or pc_state.get("stage1"):
                    be_buffer=max(point*2.0,min(risk_distance*0.05,atr*0.15 if atr>0 else risk_distance*0.05))
                    breakeven=entry+be_buffer if side=="BUY" else entry-be_buffer
                    if side=="BUY":
                        candidate_sl=max(candidate_sl,breakeven)
                    else:
                        candidate_sl=min(candidate_sl,breakeven) if candidate_sl>0 else breakeven
                    exit_stage="BREAK_EVEN"

                # 2) Profit lock: at >= 1R-ish, retain a portion of initial risk as profit.
                if r_multiple>=lock_trigger_r:
                    locked_r=0.25 if regime_style!="TREND_FOLLOWING" else 0.20
                    locked=entry+risk_distance*locked_r if side=="BUY" else entry-risk_distance*locked_r
                    if side=="BUY":
                        candidate_sl=max(candidate_sl,locked)
                    else:
                        candidate_sl=min(candidate_sl,locked) if candidate_sl>0 else locked
                    exit_stage="PROFIT_LOCK"

                # 3) ATR + market-structure trailing.
                if r_multiple>=trail_trigger_r:
                    atr_gap=max(atr*atr_gap_mult,min_gap*2.0) if atr>0 else max(risk_distance*0.50,min_gap*2.0)
                    atr_trail=current-atr_gap if side=="BUY" else current+atr_gap

                    structure_trail=0.0
                    structure_buffer=max(atr*0.20,point*3.0) if atr>0 else point*3.0
                    if side=="BUY" and swing_low>0 and swing_low<current:
                        structure_trail=swing_low-structure_buffer
                    elif side=="SELL" and swing_high>0 and swing_high>current:
                        structure_trail=swing_high+structure_buffer

                    if side=="BUY":
                        candidate_sl=max(candidate_sl,atr_trail)
                        if structure_trail>0:
                            # Use the tighter valid structural/ATR stop, but never above market gap.
                            candidate_sl=max(candidate_sl,structure_trail)
                    else:
                        candidate_sl=min(candidate_sl,atr_trail) if candidate_sl>0 else atr_trail
                        if structure_trail>0:
                            candidate_sl=min(candidate_sl,structure_trail) if candidate_sl>0 else structure_trail
                    exit_stage="ATR_STRUCTURE_TRAIL"

                # Broker distance + monotonic safety.
                if side=="BUY" and candidate_sl>0:
                    candidate_sl=min(candidate_sl,current-min_gap)
                    if old_sl>0:
                        candidate_sl=max(candidate_sl,old_sl)
                elif side=="SELL" and candidate_sl>0:
                    candidate_sl=max(candidate_sl,current+min_gap)
                    if old_sl>0:
                        candidate_sl=min(candidate_sl,old_sl)

                # TP extension only in supportive trend/evidence. In defensive regimes
                # never chase farther TP just because a raw target got larger.
                desired_tp=entry*(1.0+target_pct/100.0) if side=="BUY" else entry*(1.0-target_pct/100.0)
                new_tp=old_tp
                old_tp_move=(abs(old_tp-entry)/entry*100.0) if old_tp>0 else 0.0
                extend_step=float(getattr(self.s,"dynamic_tp_min_extension_pct",0.15))
                can_extend=(
                    regime_style=="TREND_FOLLOWING"
                    and regime_target_mult>=1.0
                    and evidence>=0.50
                    and target_pct>=old_tp_move+extend_step
                )
                if old_tp<=0:
                    can_extend=True

                if can_extend:
                    if side=="BUY":
                        desired_tp=max(desired_tp,current+min_gap)
                    else:
                        desired_tp=min(desired_tp,current-min_gap)
                    new_tp=desired_tp

                new_sl=round(candidate_sl,digits) if candidate_sl>0 else old_sl
                new_tp=round(new_tp,digits) if new_tp>0 else old_tp

                sl_changed=abs(new_sl-old_sl)>point*0.5
                tp_changed=abs(new_tp-old_tp)>point*0.5
                if not (sl_changed or tp_changed):
                    continue

                result=self.mt.modify_position_sltp(p,new_sl,new_tp)
                code=int(getattr(result,"retcode",-1) or -1) if result is not None else -1
                if code==getattr(mt5,"TRADE_RETCODE_DONE",10009):
                    self.log(
                        f"DYNAMIC EXIT [{exit_stage}]: {side} {symbol} ticket={getattr(p,'ticket',0)} | "
                        f"move={profit_move:+.2f}% | R={r_multiple:+.2f} | mode={mode} | "
                        f"regime={regime_style} | structure={structure} | "
                        f"SL {old_sl:.{digits}f}->{new_sl:.{digits}f} | "
                        f"TP {old_tp:.{digits}f}->{new_tp:.{digits}f}"
                    )
                    # Keep local entry snapshot synchronized with the tightened broker stop.
                    if initial_snapshot is not None:
                        for k,v in list(self.entry_risk_snapshot.items()):
                            if int(k) in ids:
                                v["managed_sl"]=float(new_sl)
                                v["managed_tp"]=float(new_tp)
                                v["exit_stage"]=exit_stage
                                v["last_r_multiple"]=float(r_multiple)
                                v["last_exit_update"]=int(time.time())
                                v["partial_stage1"]=bool(pc_state.get("stage1"))
                                v["partial_stage2"]=bool(pc_state.get("stage2"))
                                v["partial_stage1_volume"]=float(pc_state.get("stage1_volume",0.0) or 0.0)
                                v["partial_stage2_volume"]=float(pc_state.get("stage2_volume",0.0) or 0.0)
                else:
                    self.log(
                        f"DYNAMIC EXIT UPDATE FAILED: {side} {symbol} ticket={getattr(p,'ticket',0)} | "
                        f"stage={exit_stage} | R={r_multiple:+.2f} | "
                        f"retcode={code} {retcode_name(code)} | "
                        f"{getattr(result,'comment','') if result else mt5.last_error()}"
                    )
        except Exception as exc:
            self.log(f"DYNAMIC EXIT WARNING: {symbol} | {exc}")

    def _apply_dynamic_session_limits(self, profile):
        info=self.mt.account()
        equity=float(getattr(info,"equity",0.0) or 0.0)
        if self.session_start_equity is None:
            self.session_start_equity=equity

        new_profit=self.session_start_equity*float(profile["session_profit_pct"])/100.0
        new_loss=self.session_start_equity*float(profile["session_loss_pct"])/100.0

        # Profit target can adapt. Loss limit may only tighten during a session;
        # it is never widened after trading has started.
        self.auto_profit_target_value=max(0.0,new_profit)
        if self.auto_max_loss_value <= 0:
            self.auto_max_loss_value=max(0.0,new_loss)
        else:
            self.auto_max_loss_value=min(self.auto_max_loss_value,max(0.0,new_loss))

    def _refresh_auto_session_limits(self):
        info=self.mt.account()
        equity=float(getattr(info,"equity",0.0) or 0.0)
        if self.session_start_equity is None:
            self.session_start_equity=equity

        self.auto_profit_target_value = self.session_start_equity * (
            float(getattr(self.s,"auto_session_profit_pct",1.0)) / 100.0
        )
        self.auto_max_loss_value = self.session_start_equity * (
            float(getattr(self.s,"auto_session_loss_pct",2.0)) / 100.0
        )

    def auto_session_limits_display(self):
        self._refresh_auto_session_limits()
        return {
            "profit_target": self.to_display(self.auto_profit_target_value),
            "max_loss": self.to_display(self.auto_max_loss_value),
            "profit_pct": float(getattr(self.s,"auto_session_profit_pct",1.0)),
            "loss_pct": float(getattr(self.s,"auto_session_loss_pct",2.0)),
        }

    def log_once(self, key, message, repeat_after=300):
        """Suppress repeated waiting/status logs while still repeating occasionally."""
        now=time.time()
        last=float(self._last_status_logs.get(key,0))
        if now-last >= repeat_after:
            self.log(message)
            self._last_status_logs[key]=now

    def find_tradeable_candidates(self, with_stats=False):
        try:
            current_info=self.mt.symbol_info(self.symbol)
            current_cat=classify_symbol(current_info)
            return self.mt.tradeable_candidates(
                [current_cat],
                self.s.symbol_scan_limit,
                with_stats=with_stats
            )
        except Exception:
            if with_stats:
                return [], {"total":0,"full":0,"fresh":0,"stale_full":0,"no_tick":0}
            return []

    def loop(self):
        while self.running:
            try:
                self.update_shadow_trades()
                cycle_symbol=str(self.symbol or "").strip()
                cycle_generation=int(getattr(self,"_context_generation",0) or 0)
                if not cycle_symbol:
                    self.log_once("context:no_symbol","CONTEXT GUARD: no selected symbol; cycle blocked.",repeat_after=30)
                    time.sleep(1)
                    continue

                self._sync_closed_trades()

                # Drop stale scale-out state after positions have closed.
                try:
                    active_tickets={
                        int(getattr(x,"ticket",0) or 0)
                        for x in (mt5.positions_get() or [])
                        if int(getattr(x,"magic",0) or 0)==int(self.s.magic)
                    }
                    for stale_ticket in list(self.partial_close_state.keys()):
                        if int(stale_ticket) not in active_tickets:
                            self.partial_close_state.pop(stale_ticket,None)
                except Exception:
                    pass

                realized,floating,total=self.session_pnl()
                # Dashboard Learning Stats are global bot stats. Symbol/TF-specific
                # memory is still used separately for decision weighting.
                stats=self.mem.stats()
                pos_snap=self.mt.position_snapshot()
                entry_snapshot=self.active_entry_snapshot()
                first_pos=pos_snap[0] if pos_snap else None
                market=self.mt.market_status(self.symbol)
                self.state({
                    "status":"RUNNING",
                    "market_status":market["trade_mode"],
                    "market_session":market["session"],
                    "market_session_source":market["session_source"],
                    "market_quote_status":market["quote_status"],
                    "market_overall":market["overall"],
                    "market_stale_seconds":market["stale_seconds"],
                    "selected_timeframe":self.tf,
                    "effective_mode":self._effective_trade_mode(),
                    "search_status":("MANAGING POSITION" if first_pos else "SEARCHING BUY / SELL"),
                    "realized":self.to_display(realized),
                    "floating":self.to_display(floating),
                    "session_pnl":self.to_display(total),
                    "realized_raw":realized,
                    "floating_raw":floating,
                    "session_pnl_raw":total,
                    "positions":len(pos_snap),
                    "entry_snapshot":entry_snapshot,
                    "position":first_pos,
                    "total_trades":stats["total"],
                    "win_rate":stats["win_rate"],
                    "profit_factor":stats["profit_factor"],
                    "consecutive_losses":stats["consecutive_losses"],
                    "cooldown":self.cooldown_remaining,
                })

                self._refresh_auto_session_limits()
                if self.auto_profit_target_value > 0 and total >= self.auto_profit_target_value:
                    self.close_all_stop(
                        f"automatic session profit target touched {self.to_display(total):+.2f}"
                    )
                    break
                if self.auto_max_loss_value > 0 and total <= -self.auto_max_loss_value:
                    self.close_all_stop(
                        f"automatic session max loss touched {self.to_display(total):+.2f}"
                    )
                    break

                market_now=self.mt.market_status(self.symbol)
                mode_now=market_now["trade_mode"]
                session_now=market_now["session"]
                quote_now=market_now["quote_status"]

                self.state({
                    "market_status":mode_now,
                    "market_session":session_now,
                    "market_session_source":market_now["session_source"],
                    "market_quote_status":quote_now,
                    "market_overall":market_now["overall"],
                    "market_stale_seconds":market_now["stale_seconds"],
                })

                if mode_now != "FULL":
                    self.state({
                        "search_status":"WAITING MARKET / SYMBOL",
                        "ai_status":"IDLE"
                    })
                    self.log_once(
                        f"permission:{self.symbol}:{mode_now}",
                        f"WAIT: {self.symbol} permission={mode_now}."
                    )
                    time.sleep(2)
                    continue

                if session_now == "CLOSED":
                    self.state({
                        "search_status":"WAITING SESSION",
                        "ai_status":"IDLE"
                    })
                    self.log_once(
                        f"closed:{self.symbol}",
                        f"WAIT: {self.symbol} session=CLOSED "
                        f"(source={market_now['session_source']})."
                    )
                    time.sleep(2)
                    continue

                if quote_now != "FRESH":
                    self.state({
                        "search_status":"WAITING FRESH QUOTE",
                        "ai_status":"IDLE"
                    })
                    self.log_once(
                        f"quote:{self.symbol}:{quote_now}",
                        f"WAIT: {self.symbol} session={session_now}, quote={quote_now} "
                        f"({market_now['stale_seconds']}s old)."
                    )
                    time.sleep(2)
                    continue

                auto_tf_rank=[]
                auto_snap={}
                if (
                    str(getattr(self,"trading_mode","AUTO")).upper()=="AUTO"
                    and bool(getattr(self.s,"auto_dynamic_timeframe",True))
                ):
                    selected_tf,auto_snap,auto_tf_rank=self._select_auto_timeframe()
                    selected_tf=str(selected_tf or self.tf).upper()
                    if selected_tf != self.tf:
                        old_tf=self.tf
                        self.tf=selected_tf
                        self.last_candle=None
                        try:
                            self.zpi.invalidate_market_cache(self.symbol)
                        except Exception:
                            pass
                        top=", ".join(
                            f"{x['tf']}={x['side']} {x['score']:.2f}"
                            for x in auto_tf_rank[:4]
                        )
                        self.log(
                            f"AUTO TF SWITCH: {old_tf} -> {self.tf} | "
                            f"effective mode={self._effective_trade_mode()} | ranked: {top}"
                        )
                        self.state({
                            "selected_timeframe":self.tf,
                            "effective_mode":self._effective_trade_mode(),
                        })

                df=self.mt.rates(self.symbol,self.tf,self.s.bars)
                candle=str(df.iloc[-2]["time"])
                if candle==self.last_candle:
                    time.sleep(1)
                    continue
                self.last_candle=candle

                # Streak protection
                stats=self.mem.stats(self.symbol,self.tf)
                if stats["consecutive_losses"] >= self.s.max_consecutive_losses:
                    self.cooldown_remaining=max(self.cooldown_remaining,self.s.pause_candles_after_streak)

                if self.cooldown_remaining>0:
                    self.log(f"COOLDOWN: skip candle, remaining={self.cooldown_remaining}")
                    self.cooldown_remaining-=1
                    continue

                tfs=[]
                for tf in self._mode_context_timeframes():
                    if tf not in tfs: tfs.append(tf)
                mtf={}
                for tf in tfs:
                    if tf==self.tf and auto_snap:
                        mtf[tf]=auto_snap
                    else:
                        mtf[tf]=snapshot(self.mt.rates(self.symbol,tf,self.s.bars))
                base=mtf[self.tf]
                # Session intelligence is intentionally independent from entry TF.
                # Intraday session boundaries/VWAP are read from M5 (fallback M15),
                # even when AUTO selects H1/H4 for the actual trade.
                session_tf="M5"
                try:
                    session_df=self.mt.rates(self.symbol,session_tf,self.s.bars)
                    session_base=snapshot(session_df)
                except Exception:
                    session_tf="M15"
                    try:
                        session_df=self.mt.rates(self.symbol,session_tf,self.s.bars)
                        session_base=snapshot(session_df)
                    except Exception:
                        session_tf=self.tf
                        session_df=df
                        session_base=base
                # Symbol Context Isolation: if UI/engine symbol changes while this
                # cycle is building MTF/session data, discard the entire cycle.
                if (str(self.symbol or "").strip()!=cycle_symbol
                        or int(getattr(self,"_context_generation",0) or 0)!=cycle_generation):
                    self.log(
                        f"CONTEXT GUARD BLOCKED: symbol/generation changed during market-data build "
                        f"({cycle_symbol} -> {self.symbol}); discarding stale cycle."
                    )
                    self.last_candle=None
                    continue

                session_ctx=self._session_intelligence(session_df,session_base)
                session_ctx["timeframe"]=session_tf
                session_ctx["symbol"]=cycle_symbol
                session_ctx["context_generation"]=cycle_generation
                # Never present zero range/VWAP as valid session intelligence.
                if (session_ctx.get("status")=="READY" and session_ctx.get("active") not in {"OFF_SESSION","OFF","-"}
                        and (float(session_ctx.get("high",0) or 0)<=float(session_ctx.get("low",0) or 0)
                             or float(session_ctx.get("vwap",0) or 0)<=0)):
                    session_ctx.update({"status":"INSUFFICIENT","score":0.0,"breakout":"NONE","valid":False})
                fib_ctx=self._fibonacci_context(df,base)
                fib_ctx["symbol"]=cycle_symbol
                fib_ctx["context_generation"]=cycle_generation

                tech_side,tech_conf=technical_score(base)
                macro_ctx=self.context_engine.macro(cycle_symbol)
                micro_ctx=self.context_engine.micro(cycle_symbol,base,mtf)
                zpi_ctx=self.zpi.snapshot(cycle_symbol,self.tf)
                zpi_ctx["symbol"]=cycle_symbol
                zpi_ctx["timeframe"]=self.tf
                zpi_ctx["context_generation"]=cycle_generation

                # Hard integrity check before any context can reach AI Council.
                # Besides generation/symbol ownership, compare session/fib prices
                # with the current instrument price. A gross scale mismatch catches
                # contamination such as EURUSD ~1.16 context appearing in BTC ~77k.
                integrity_errors=[]
                if str(self.symbol or "").strip()!=cycle_symbol:
                    integrity_errors.append(f"engine symbol changed to {self.symbol}")
                if int(getattr(self,"_context_generation",0) or 0)!=cycle_generation:
                    integrity_errors.append("context generation changed")
                current_price=abs(float(base.get("close",0.0) or 0.0))
                if current_price>0:
                    for label,val in (
                        ("session_vwap",session_ctx.get("vwap",0)),
                        ("session_low",session_ctx.get("low",0)),
                        ("session_high",session_ctx.get("high",0)),
                        ("fib_swing_low",fib_ctx.get("swing_low",0)),
                        ("fib_swing_high",fib_ctx.get("swing_high",0)),
                    ):
                        try:
                            px=abs(float(val or 0))
                        except Exception:
                            px=0.0
                        if px>0:
                            ratio=max(px,current_price)/max(min(px,current_price),1e-12)
                            if ratio>25.0:
                                integrity_errors.append(f"{label} scale mismatch x{ratio:.1f}")
                if integrity_errors:
                    self.log(
                        f"CONTEXT GUARD BLOCKED [{cycle_symbol} {self.tf}]: "
                        + "; ".join(integrity_errors)
                        + " | invalidating symbol cache and rebuilding next cycle."
                    )
                    try:
                        self.zpi.invalidate_market_cache(cycle_symbol)
                    except Exception:
                        pass
                    self._last_intelligence={}
                    self.last_candle=None
                    time.sleep(0.25)
                    continue

                self._last_intelligence=zpi_ctx

                news_ctx=zpi_ctx.get("news",{})
                cal_ctx=zpi_ctx.get("calendar",{})
                bin_ctx=zpi_ctx.get("binance",{})
                tvtech_ctx=zpi_ctx.get("technicals",{})
                fear_ctx=zpi_ctx.get("fear_greed",{})
                klines_ctx=zpi_ctx.get("klines",{})

                news_score=float(news_ctx.get("score",0.0) or 0.0)
                bin_score=float(bin_ctx.get("score",0.0) or 0.0)
                micro_ctx=dict(micro_ctx)
                micro_ctx["directional_score"]=max(
                    -1.0,min(1.0,
                        float(micro_ctx.get("directional_score",0.0) or 0.0)
                        + 0.20*bin_score
                        + 0.15*float(klines_ctx.get("score",0.0) or 0.0)
                    )
                )
                micro_ctx["zpi_binance"]=bin_ctx

                macro_ctx=dict(macro_ctx)
                if cal_ctx.get("status")=="READY":
                    macro_ctx["source"]="ZPI/TradingView"
                    macro_ctx["status"]="READY"
                    macro_ctx["risk_level"]=str(cal_ctx.get("risk_level","NORMAL")).upper()
                    macro_ctx["blackout"]=bool(macro_ctx.get("blackout",False) or cal_ctx.get("blackout",False))
                    nearest=cal_ctx.get("nearest") or {}
                    if nearest:
                        macro_ctx["event"]=f"{nearest.get('title','')} ({float(nearest.get('minutes_to_event',0)):.0f}m)"
                similar=self.mem.similar(self.symbol,self.tf,base,self.s.similar_trades_limit)
                mem_stats=self.mem.similar_stats(similar,tech_side)

                self.state({"ai_status":"ANALYZING"})
                llm_action,llm_conf,llm_detail,ai_status=self.llm.safe_decide(
                    self.symbol,self.tf,mtf,mem_stats,tech_side,tech_conf,
                    macro=macro_ctx,micro=micro_ctx
                )
                if bool(llm_detail.get("ai_council",False)):
                    stage_bits=[]
                    for st in list(llm_detail.get("council_stages",[]) or []):
                        role=str(st.get("_role","?"))
                        verdict=st.get("verdict") if role=="CRITIC" else st.get("action")
                        conf=float(st.get("confidence",0) or 0)
                        if bool(st.get("_abstain",False)):
                            tag="ABSTAIN"
                        elif role=="CRITIC" and str(verdict or "").upper()=="REJECT":
                            tag="VALID_REJECT" if conf>=0.50 else "LOW_CONF_REJECT"
                        elif bool(st.get("_confidence_retry",False)):
                            tag="CALIBRATED"
                        else:
                            tag="VALID"
                        detail=f"{role}={verdict or '-'} {conf:.2f} ({tag})"
                        if bool(st.get("_partial_json_repaired",False)):
                            detail += " [JSON_REPAIRED]"
                        if bool(st.get("_smart_routed",False)):
                            detail += " [SMART_ROUTE_4B]"
                        if bool(st.get("_chief_fallback",False)):
                            detail += " [4B_FALLBACK]"
                        if bool(st.get("_circuit_open",False)):
                            detail += " [CIRCUIT_OPEN]"
                        elif str(st.get("_circuit_state",""))=="OPEN":
                            detail += " [CIRCUIT_OPENED]"
                        if bool(st.get("_abstain",False)) or not bool(st.get("_ok",False)):
                            model_name=str(st.get("_model","?"))
                            elapsed=float(st.get("_elapsed",0) or 0)
                            reason=str(st.get("reason","") or "").replace("\n"," ")[:110]
                            detail += f"[model={model_name} elapsed={elapsed:.1f}s"
                            if reason:
                                detail += f" reason={reason}"
                            detail += "]"
                        stage_bits.append(detail)
                    self.log("AI COUNCIL AUTO: " + " | ".join(stage_bits) + f" | FINAL={llm_action} {float(llm_conf):.2f}")
                    if bool(llm_detail.get("fast_adjudication",False)):
                        self.log(
                            f"COUNCIL FAST ADJUDICATION: {llm_action} {float(llm_conf):.2f} | "
                            f"{llm_detail.get('reason','downstream model unavailable')}"
                        )

                if ai_status in {"READY","RETRY_OK","EMPTY_RETRY_OK","COUNCIL_READY"}:
                    llm_action,llm_conf,llm_detail,evidence_issues=self.llm.validate_evidence(
                        llm_action,llm_conf,llm_detail,self.tf,mtf,tech_side,tech_conf,
                        macro=macro_ctx
                    )
                    if evidence_issues:
                        ai_status="EVIDENCE_HOLD"
                        self.log("LLM EVIDENCE GUARD: " + "; ".join(evidence_issues[:3]) + " -> HOLD")
                if bool(llm_detail.get("retry_used",False)):
                    if ai_status in {"RETRY_OK","EMPTY_RETRY_OK"}:
                        self.log(
                            f"LLM JSON RETRY: recovered successfully | "
                            f"{llm_action} {float(llm_conf):.2f}"
                        )
                    else:
                        self.log(
                            f"LLM RETRY FAILED [{ai_status}]: "
                            f"{llm_detail.get('retry_error','no valid structured output')}"
                        )
                fallback=self._deterministic_llm_fallback(
                    tech_side,tech_conf,tvtech_ctx,micro_ctx,ai_status,
                    klines_ctx=klines_ctx,binance_ctx=bin_ctx
                )
                if fallback:
                    llm_action=fallback["action"]
                    llm_conf=fallback["confidence"]
                    llm_detail=dict(llm_detail or {})
                    llm_detail["reason"]=fallback["reason"]
                    llm_detail["conflicts"]=list(llm_detail.get("conflicts",[]) or [])
                    llm_detail["deterministic_fallback"]=True
                    ai_status="DETERMINISTIC_FALLBACK"
                    self.log(
                        f"AI WEIGHTED FALLBACK: {llm_action} {llm_conf:.2f} | "
                        f"mode={fallback.get('source_mode','FULL')} | "
                        f"consensus={float(fallback.get('consensus',0)):+.2f} | "
                        f"votes={fallback.get('positive_votes',0)}+/"
                        f"{fallback.get('negative_votes',0)}-"
                    )

                if (
                    ai_status in {"READY","RETRY_OK","EMPTY_RETRY_OK"}
                    and llm_action in {"BUY","SELL"}
                    and float(llm_conf or 0.0)<=0.0
                    and bool(llm_detail.get("confidence_missing",False))
                    and llm_action==tech_side
                    and float(tech_conf)>=0.80
                ):
                    # Deterministic conservative recovery for a missing field only.
                    # Explicit confidence=0 from the model is never overwritten.
                    llm_conf=min(0.74,max(0.68,float(tech_conf)*0.75))
                    llm_detail["confidence_recovered"]=True
                    self.log(
                        f"LLM CONFIDENCE RECOVERED: {llm_action} agrees with "
                        f"TECH {tech_conf:.2f} -> {llm_conf:.2f}"
                    )
                if llm_action=="HOLD" and float(llm_conf or 0.0)<=0.0:
                    llm_detail=dict(llm_detail or {})
                    llm_detail["invalid_abstain"]=True
                    llm_detail["reason"]=(llm_detail.get("reason") or "LLM zero-confidence response")
                    self.log("LLM ZERO_CONF ABSTAIN: HOLD 0.00 is not treated as a valid directional opinion.")

                # V3.10.7: when Council is ON, CHIEF is authoritative.
                # Legacy deterministic HOLD override is only allowed on the non-Council path.
                council_active=bool(llm_detail.get("ai_council",False))
                hold_override=None if council_active else self._hold_abstain_override(
                    tech_side,tech_conf,llm_action,llm_conf,llm_detail,
                    tvtech_ctx,micro_ctx,klines_ctx,bin_ctx,macro_ctx,self.tf
                )
                if council_active and llm_action=="HOLD":
                    self.log("AI COUNCIL HOLD AUTHORITATIVE: legacy abstain override bypassed; no trade without CHIEF BUY/SELL.")
                if hold_override:
                    original_hold_conf=float(llm_conf or 0.0)
                    llm_action=hold_override["action"]
                    llm_conf=hold_override["confidence"]
                    llm_detail=dict(llm_detail or {})
                    llm_detail["reason"]=hold_override["reason"]
                    llm_detail["hold_override"]=True
                    llm_detail["original_action"]="HOLD"
                    llm_detail["original_hold_confidence"]=original_hold_conf
                    ai_status="HOLD_OVERRIDE"
                    self.log(
                        f"LLM ABSTAIN OVERRIDE: HOLD {original_hold_conf:.2f} -> "
                        f"{llm_action} {llm_conf:.2f} | TF={self.tf} | "
                        f"consensus={float(hold_override.get('consensus',0)):+.2f} | "
                        f"votes={hold_override.get('positive_votes',0)}+/"
                        f"{hold_override.get('negative_votes',0)}-"
                    )

                self.state({"ai_status":ai_status})
                reason=llm_detail["reason"]
                if ai_status != "READY":
                    self.log(f"AI {ai_status}: {reason}")

                # V2.4: if deterministic technical signal is extremely strong and
                # LLM only says HOLD with low/moderate confidence, keep a WAIT_CONFIRM
                # state instead of treating it as a hard contradiction.
                wait_confirm = (
                    tech_side in {"BUY","SELL"} and tech_conf >= 0.90 and
                    llm_action=="HOLD" and llm_conf < 0.60
                )

                if llm_action==tech_side:
                    final_action=llm_action
                elif wait_confirm:
                    final_action="HOLD"
                else:
                    final_action="HOLD"

                final_score=self._score(tech_side,tech_conf,llm_action,llm_conf,mem_stats)
                final_score=self.context_engine.adjust_score(
                    final_action,final_score,macro_ctx,micro_ctx
                )
                if final_action in {"BUY","SELL"}:
                    news_align=news_score if final_action=="BUY" else -news_score

                    tv_score=float(tvtech_ctx.get("score",0.0) or 0.0)
                    tv_align=tv_score if final_action=="BUY" else -tv_score

                    fear_score=float(fear_ctx.get("score",0.0) or 0.0)
                    fear_align=fear_score if final_action=="BUY" else -fear_score

                    session_align=(float(session_ctx.get("score",0.0) or 0.0) if final_action=="BUY" else -float(session_ctx.get("score",0.0) or 0.0))
                    fib_align=(float(fib_ctx.get("score",0.0) or 0.0) if final_action=="BUY" else -float(fib_ctx.get("score",0.0) or 0.0))
                    final_score=max(0.0,min(1.0,
                        final_score
                        + float(self.s.zpi_sentiment_weight)*news_align
                        + float(self.s.zpi_technical_weight)*tv_align
                        + float(self.s.zpi_fear_greed_weight)*fear_align
                        + float(getattr(self.s,"session_signal_weight",0.05))*session_align
                        + float(getattr(self.s,"fibonacci_signal_weight",0.06))*fib_align
                    ))

                macro_blackout=bool(macro_ctx.get("blackout",False))
                if macro_blackout and final_action in {"BUY","SELL"}:
                    final_action="HOLD"
                    final_score=min(final_score,0.49)
                    reason=(reason+" | Macro high-impact blackout").strip(" |")

                self.state({
                    "news_status":news_ctx.get("status","-"),
                    "news_score":news_score,
                    "news_count":int(news_ctx.get("count",0) or 0),
                    "news_source":news_ctx.get("source","ZPI"),
                    "tvtech_status":tvtech_ctx.get("status","-"),
                    "tvtech_score":float(tvtech_ctx.get("score",0.0) or 0.0),
                    "tvtech_summary":tvtech_ctx.get("summary","-"),
                    "fear_status":fear_ctx.get("status","-"),
                    "fear_score":float(fear_ctx.get("score",0.0) or 0.0),
                    "fear_raw":fear_ctx.get("raw"),
                    "fear_rating":fear_ctx.get("rating","-"),
                    "klines_score":float(klines_ctx.get("score",0.0) or 0.0),
                    "zpi_requests":int(zpi_ctx.get("requests",0) or 0),
                    "binance_score":bin_score,
                    "binance_pair":bin_ctx.get("pair",""),
                    "macro_status":macro_ctx.get("status","-"),
                    "macro_bias":macro_ctx.get("directional_score",0.0),
                    "macro_risk":macro_ctx.get("risk_level","-"),
                    "macro_event":macro_ctx.get("event",""),
                    "micro_bias":micro_ctx.get("directional_score",0.0),
                    "micro_activity":micro_ctx.get("activity","-"),
                    "micro_volatility":micro_ctx.get("volatility","-"),
                    "session_intel_status":session_ctx.get("status","-"),
                    "session_intel_active":session_ctx.get("active","-"),
                    "session_intel_score":float(session_ctx.get("score",0.0) or 0.0),
                    "session_intel_breakout":session_ctx.get("breakout","NONE"),
                    "fib_status":fib_ctx.get("status","-"),
                    "fib_direction":fib_ctx.get("direction","NEUTRAL"),
                    "fib_score":float(fib_ctx.get("score",0.0) or 0.0),
                    "fib_nearest":fib_ctx.get("nearest","-"),
                    "fib_nearest_price":float(fib_ctx.get("nearest_price",0.0) or 0.0),
                })

                self.log(
                    f"{self.symbol} {self.tf} | regime={base['regime']} | structure={base['structure']} | "
                    f"TECH={tech_side} {tech_conf:.2f} | LLM={llm_action} {llm_conf:.2f} | "
                    f"MEM={mem_stats['memory_score']:.2f} n={mem_stats['same_side_count']} | "
                    f"FINAL={final_action} {final_score:.2f}"
                )
                self.log(
                    f"LLM context: trend={llm_detail['trend']} | momentum={llm_detail['momentum']} | "
                    f"volatility={llm_detail['volatility']} | structure={llm_detail['structure']}"
                )
                self.log(
                    f"MACRO: {macro_ctx.get('status')} source={macro_ctx.get('source')} | "
                    f"bias={float(macro_ctx.get('directional_score',0)):+.2f} | "
                    f"risk={macro_ctx.get('risk_level')} | "
                    f"blackout={macro_ctx.get('blackout')} | event={macro_ctx.get('event') or '-'}"
                )
                self.log(
                    f"MICRO: bias={float(micro_ctx.get('directional_score',0)):+.2f} | "
                    f"activity={micro_ctx.get('activity')} | volatility={micro_ctx.get('volatility')} | "
                    f"volume_ratio={float(micro_ctx.get('volume_ratio',0)):.2f}"
                )
                self.log(
                    f"NEWS: {news_ctx.get('status')} | {int(news_ctx.get('count',0) or 0)} headlines | "
                    f"sentiment={news_score:+.2f} | source={news_ctx.get('source','ZPI')}"
                )
                self.log(
                    f"SESSION INTEL [{session_ctx.get('timeframe','-')}]: {session_ctx.get('active','-')} | "
                    f"status={session_ctx.get('status','-')} | bias={float(session_ctx.get('score',0)):+.2f} | "
                    f"range={float(session_ctx.get('low',0)):.5f}-{float(session_ctx.get('high',0)):.5f} | "
                    f"VWAP={float(session_ctx.get('vwap',0)):.5f} | {session_ctx.get('breakout','NONE')}"
                )
                self.log(
                    f"FIBONACCI: {fib_ctx.get('direction','NEUTRAL')} | "
                    f"bias={float(fib_ctx.get('score',0)):+.2f} | "
                    f"nearest={fib_ctx.get('nearest','-')} @ {float(fib_ctx.get('nearest_price',0)):.5f} | "
                    f"swing={float(fib_ctx.get('swing_low',0)):.5f}-{float(fib_ctx.get('swing_high',0)):.5f} | "
                    f"ext127.2={float(fib_ctx.get('extension_1272',0)):.5f}"
                )
                if bin_ctx.get("status")=="READY":
                    self.log(
                        f"ZPI BINANCE: {bin_ctx.get('pair')} | 24h={float(bin_ctx.get('change24h',0)):+.2f}% | "
                        f"book={float(bin_ctx.get('orderbook_imbalance',0)):+.2f}"
                    )
                if tvtech_ctx.get("status")=="READY":
                    self.log(
                        f"ZPI TV TECH: {tvtech_ctx.get('timeframe')} | "
                        f"{tvtech_ctx.get('summary')} | score={float(tvtech_ctx.get('score',0)):+.2f} | "
                        f"MA={tvtech_ctx.get('moving_averages') or '-'} | "
                        f"OSC={tvtech_ctx.get('oscillators') or '-'}"
                    )
                if fear_ctx.get("status")=="READY":
                    self.log(
                        f"ZPI FEAR/GREED: {fear_ctx.get('rating')} "
                        f"{float(fear_ctx.get('raw',50)):.0f}/100 | "
                        f"bias={float(fear_ctx.get('score',0)):+.2f}"
                    )
                if klines_ctx.get("status")=="READY":
                    self.log(
                        f"ZPI KLINES: {klines_ctx.get('pair')} {klines_ctx.get('interval')} | "
                        f"bias={float(klines_ctx.get('score',0)):+.2f} | "
                        f"volRatio={float(klines_ctx.get('volume_ratio',1)):.2f}"
                    )
                if llm_detail["conflicts"]:
                    self.log("Conflicts: " + "; ".join(str(x) for x in llm_detail["conflicts"][:4]))
                self.log("Reason: "+reason)
                if wait_confirm:
                    self.log("WAIT_CONFIRM: technical signal is very strong, but LLM is not confirming yet.")

                profile=self._dynamic_risk_profile(
                    final_score,base,mtf,final_action
                )
                profile=self._apply_mode_profile(profile)
                profile=self.context_engine.apply_risk_context(
                    profile,final_action,macro_ctx,micro_ctx
                )
                council_consensus=dict(llm_detail.get("council_consensus",{}) or {})
                if bool(llm_detail.get("ai_council",False)):
                    profile=self._apply_council_consensus_to_profile(
                        profile,council_consensus
                    )
                    self.log(
                        f"COUNCIL CONSENSUS: {profile.get('council_consensus_grade','MIXED')} | "
                        f"score={float(profile.get('council_consensus_score',0.5)):.2f} | "
                        f"coverage={float(council_consensus.get('coverage',0.0))*100:.0f}% | "
                        f"valid={int(council_consensus.get('valid_roles',0) or 0)} | "
                        f"abstain={int(council_consensus.get('abstain_roles',0) or 0)} | "
                        f"disagree={int(council_consensus.get('disagree_roles',0) or 0)} | "
                        f"quality x{float(profile.get('council_consensus_quality_multiplier',1.0)):.2f} | "
                        f"risk x{float(profile.get('council_consensus_risk_multiplier',1.0)):.2f}"
                    )

                    calibration=self.mem.consensus_calibration(
                        self.symbol,self.tf,final_action
                    )
                    cal_mult=float(calibration.get("risk_multiplier",1.0) or 1.0)
                    profile["risk_pct"]=max(
                        float(profile.get("_risk_min",0.0) or 0.0),
                        min(
                            float(profile.get("_risk_max",100.0) or 100.0),
                            float(profile.get("risk_pct",0.0) or 0.0)*cal_mult
                        )
                    )
                    profile["consensus_calibration"]=calibration
                    profile["consensus_calibration_risk_multiplier"]=cal_mult
                    self.log(
                        f"CONSENSUS CALIBRATION: {calibration.get('grade','LEARNING')} | "
                        f"score={float(calibration.get('score',0.5)):.2f} | "
                        f"evidence={int(calibration.get('evidence_samples',0) or 0)} | "
                        f"HIGH WR={float(calibration.get('high_win_rate',0.5))*100:.1f}% "
                        f"n={int(calibration.get('high_samples',0) or 0)} | "
                        f"LOW WR={float(calibration.get('low_win_rate',0.5))*100:.1f}% "
                        f"n={int(calibration.get('low_samples',0) or 0)} | "
                        f"risk x{cal_mult:.2f}"
                    )

                regime_adapt=profile.get("regime_strategy",{}) or {}
                regime_hist=regime_adapt.get("history",{}) or {}
                self.log(
                    f"REGIME ADAPT [{self.symbol} {self.tf}]: "
                    f"{regime_adapt.get('regime','UNKNOWN')} → {regime_adapt.get('style','BALANCED')} | "
                    f"quality x{float(regime_adapt.get('quality_multiplier',1.0)):.2f} | "
                    f"risk x{float(regime_adapt.get('risk_multiplier',1.0)):.2f} | "
                    f"target x{float(regime_adapt.get('target_multiplier',1.0)):.2f} | "
                    f"minQ={float(regime_adapt.get('minimum_quality',0.0)):.2f} | "
                    f"history={regime_hist.get('grade','INSUFFICIENT')} n={int(regime_hist.get('samples',0) or 0)} "
                    f"WR={float(regime_hist.get('win_rate',0.5))*100:.1f}%"
                )

                exp=profile.get("expectancy",{}) or {}
                self.log(
                    f"EXPECTANCY [{self.symbol} {self.tf} {final_action}]: "
                    f"grade={exp.get('grade','INSUFFICIENT')} | n={int(exp.get('samples',0) or 0)} | "
                    f"WR={float(exp.get('win_rate',0.5))*100:.1f}% | "
                    f"score={float(exp.get('score',0.5)):.2f} | "
                    f"E={float(exp.get('expectancy',0.0)):+.2f} | "
                    f"risk x{float(exp.get('risk_multiplier',1.0)):.2f}"
                )

                stress=profile.get("account_stress",{}) or {}
                self.log(
                    f"ACCOUNT STRESS: {stress.get('level','NORMAL')} | "
                    f"risk x{float(stress.get('multiplier',1.0)):.2f} | "
                    f"session DD={float(stress.get('session_drawdown_pct',0.0)):.2f}% | "
                    f"loss streak={int(stress.get('loss_streak',0) or 0)} | "
                    f"positions={int(stress.get('positions',0) or 0)} | "
                    f"margin level={float(stress.get('margin_level',0.0)):.0f}% | "
                    f"{stress.get('reason','-')}"
                )

                target_plan=self._adaptive_profit_target(
                    profile,base,final_action,final_score,
                    tvtech_ctx=tvtech_ctx,
                    micro_ctx=micro_ctx,
                    klines_ctx=klines_ctx,
                    binance_ctx=bin_ctx,
                    session_ctx=session_ctx,
                    fib_ctx=fib_ctx
                )
                profile["min_tp_pct"]=float(target_plan.get("target_pct",0.0) or 0.0)
                profile["target_plan"]=target_plan
                # V3.9.1: current evidence can improve exits of an existing same-side trade.
                # This runs before considering another entry and never loosens SL protection.
                self._manage_active_dynamic_exits(self.symbol,final_action,target_plan,profile)
                self._apply_dynamic_session_limits(profile)
                self.state({
                    "dynamic_quality":profile["quality"],
                    "dynamic_risk_pct":profile["risk_pct"],
                    "dynamic_rr":profile["rr"],
                    "dynamic_max_entries":profile["max_entries"],
                    "dynamic_session_profit_pct":profile["session_profit_pct"],
                    "dynamic_session_loss_pct":profile["session_loss_pct"],
                    "effective_mode":profile.get("effective_mode",self._effective_trade_mode()),
                    "min_tp_pct":profile.get("min_tp_pct",0.0),
                    "adaptive_target_pct":profile.get("min_tp_pct",0.0),
                    "adaptive_target_strength":float(profile.get("target_plan",{}).get("strength",0.0) or 0.0),
                    "expectancy_score":float(profile.get("expectancy",{}).get("score",0.5) or 0.5),
                    "expectancy_samples":int(profile.get("expectancy",{}).get("samples",0) or 0),
                    "expectancy_grade":str(profile.get("expectancy",{}).get("grade","INSUFFICIENT")),
                    "expectancy_risk_multiplier":float(profile.get("expectancy_risk_multiplier",1.0) or 1.0),
                    "account_stress_level":str(profile.get("account_stress",{}).get("level","NORMAL")),
                    "account_stress_multiplier":float(profile.get("account_stress_multiplier",1.0) or 1.0),
                    "session_drawdown_pct":float(profile.get("account_stress",{}).get("session_drawdown_pct",0.0) or 0.0),
                    "regime_strategy_style":str(profile.get("regime_strategy",{}).get("style","BALANCED")),
                    "regime_quality_multiplier":float(profile.get("regime_strategy",{}).get("quality_multiplier",1.0) or 1.0),
                    "regime_risk_multiplier":float(profile.get("regime_risk_multiplier",1.0) or 1.0),
                    "regime_target_multiplier":float(profile.get("regime_strategy",{}).get("target_multiplier",1.0) or 1.0),
                    "regime_history_score":float(profile.get("regime_strategy",{}).get("history",{}).get("score",0.5) or 0.5),
                    "regime_history_samples":int(profile.get("regime_strategy",{}).get("history",{}).get("samples",0) or 0),
                    "council_consensus_score":float(profile.get("council_consensus_score",0.5) or 0.5),
                    "council_consensus_grade":str(profile.get("council_consensus_grade","N/A")),
                    "council_consensus_risk_multiplier":float(profile.get("council_consensus_risk_multiplier",1.0) or 1.0),
                    "council_consensus_quality_multiplier":float(profile.get("council_consensus_quality_multiplier",1.0) or 1.0),
                })
                active_snap=self.active_entry_snapshot()
                if active_snap:
                    self.log(
                        f"ACTIVE TRADE SNAPSHOT: {active_snap.get('side','?')} {active_snap.get('symbol','?')} | "
                        f"quality={float(active_snap.get('quality',0)):.2f} | "
                        f"risk={float(active_snap.get('risk_pct',0)):.2f}% | "
                        f"RR={float(active_snap.get('rr',0)):.2f} | "
                        f"SL={active_snap.get('sl','-')} | TP={active_snap.get('tp','-')}"
                    )
                    self.log(
                        f"CURRENT MARKET [{getattr(self,'trading_mode','AUTO')}→{profile.get('effective_mode','-')}]: action={final_action} | "
                        f"quality={profile['quality']:.2f} | hypothetical risk={profile['risk_pct']:.2f}% | "
                        f"RR floor=1:{float(getattr(self.s,'adaptive_min_reward_risk',2.0)):.2f} | "
                        f"adaptive TP={profile.get('min_tp_pct',0.0):.2f}% | "
                        f"consensus={profile.get('council_consensus_grade','N/A')} "
                        f"{float(profile.get('council_consensus_score',0.5)):.2f} | "
                        f"expectancy={profile.get('expectancy',{}).get('grade','INSUFFICIENT')} "
                        f"x{float(profile.get('expectancy_risk_multiplier',1.0)):.2f} | multi-entry=MARGIN | emergency cap={profile['max_entries']}"
                    )
                else:
                    self.log(
                        f"DYNAMIC RISK [{getattr(self,'trading_mode','AUTO')}→{profile.get('effective_mode','-')} {self.tf}]: quality={profile['quality']:.2f} | "
                        f"risk={profile['risk_pct']:.2f}% | RR floor=1:{float(getattr(self.s,'adaptive_min_reward_risk',2.0)):.2f} | "
                        f"adaptive TP={profile.get('min_tp_pct',0.0):.2f}% | "
                        f"consensus={profile.get('council_consensus_grade','N/A')} "
                        f"{float(profile.get('council_consensus_score',0.5)):.2f} | "
                        f"expectancy={profile.get('expectancy',{}).get('grade','INSUFFICIENT')} "
                        f"x{float(profile.get('expectancy_risk_multiplier',1.0)):.2f} | "
                        f"regime={profile.get('regime_strategy',{}).get('style','BALANCED')} "
                        f"x{float(profile.get('regime_risk_multiplier',1.0)):.2f} | "
                        f"stress={profile.get('account_stress',{}).get('level','NORMAL')} "
                        f"x{float(profile.get('account_stress_multiplier',1.0)):.2f} | "
                        f"multi-entry=MARGIN | emergency cap={profile['max_entries']} | "
                        f"session +{profile['session_profit_pct']:.2f}%/"
                        f"-{profile['session_loss_pct']:.2f}%"
                    )

                tp_meta=profile.get("target_plan",{}) or {}
                self.log(
                    f"ADAPTIVE TARGET [{profile.get('effective_mode','-')}]: "
                    f"{float(profile.get('min_tp_pct',0.0)):.2f}% price move | "
                    f"floor={float(tp_meta.get('floor_pct',0.0)):.2f}% | "
                    f"potential(raw)={float(tp_meta.get('raw_pct',0.0)):.2f}% | "
                    f"strength={float(tp_meta.get('strength',0.0)):.2f} | "
                    f"{tp_meta.get('reason','-')}"
                )

                decision_features=dict(base or {})
                decision_features.update({
                    "effective_mode":str(profile.get("effective_mode","")),
                    "risk_pct":float(profile.get("risk_pct",0.0) or 0.0),
                    "quality":float(profile.get("quality",0.0) or 0.0),
                    "council_consensus_score":float(profile.get("council_consensus_score",0.5) or 0.5),
                    "council_consensus_grade":str(profile.get("council_consensus_grade","N/A")),
                    "council_consensus_risk_multiplier":float(profile.get("council_consensus_risk_multiplier",1.0) or 1.0),
                    "consensus_calibration_grade":str(profile.get("consensus_calibration",{}).get("grade","LEARNING")),
                    "consensus_calibration_score":float(profile.get("consensus_calibration",{}).get("score",0.5) or 0.5),
                    "consensus_calibration_risk_multiplier":float(profile.get("consensus_calibration_risk_multiplier",1.0) or 1.0),
                    "expectancy_grade":str(profile.get("expectancy",{}).get("grade","INSUFFICIENT")),
                    "regime_strategy":str(profile.get("regime_strategy",{}).get("style","BALANCED")),
                })

                if bool(llm_detail.get("ai_council",False)) and final_action in {"BUY","SELL"}:
                    cscore=float(profile.get("council_consensus_score",0.5) or 0.5)
                    ccoverage=float((profile.get("council_consensus",{}) or {}).get("coverage",0.0) or 0.0)
                    # Only hard-block clearly conflicted Council outcomes when enough
                    # roles participated; low coverage remains a de-risk modifier.
                    if ccoverage>=0.50 and cscore<0.32:
                        self.log(
                            f"ConsensusGuard BLOCKED: score={cscore:.2f} with "
                            f"coverage={ccoverage*100:.0f}% indicates Council conflict."
                        )
                        continue

                regime_min_q=float(profile.get("minimum_regime_quality",0.0) or 0.0)
                if regime_min_q>0 and float(profile.get("quality",0.0) or 0.0)<regime_min_q:
                    self.log(
                        f"RegimeGuard BLOCKED: {profile.get('regime_strategy',{}).get('style','BALANCED')} "
                        f"requires quality>={regime_min_q:.2f}, current={float(profile.get('quality',0.0)):.2f}"
                    )
                    continue

                ok,why=self.risk.validate(self.symbol,final_action,llm_conf,final_score)
                if ok:
                    self.log("RiskGuard: "+why)
                if not ok:
                    self.mem.save_decision(self.symbol,self.tf,candle,final_action,llm_conf,
                                           reason+" | BLOCKED: "+why,decision_features,None,
                                           tech_conf,mem_stats["memory_score"],final_score)
                    self.log("RiskGuard BLOCKED: "+why)
                    continue

                plan,why=self.risk.plan(
                    self.symbol,final_action,base["atr14"],
                    risk_pct_override=profile["risk_pct"],
                    rr_override=profile["rr"],
                    min_tp_pct_override=profile.get("min_tp_pct",0.0),
                    min_rr_override=float(getattr(self.s,"adaptive_min_reward_risk",2.0))
                )
                if plan is None:
                    self.mem.save_decision(self.symbol,self.tf,candle,final_action,llm_conf,
                                           reason+" | BLOCKED: "+why,decision_features,None,
                                           tech_conf,mem_stats["memory_score"],final_score)
                    self.log("RR BLOCKED: "+why)
                    continue

                self.log(
                    f"PLAN {final_action} [{profile.get('effective_mode','-')}] vol={plan['volume']} "
                    f"ENTRY={plan['entry']} RR={plan['rr']:.2f} SL={plan['sl']} TP={plan['tp']} | "
                    f"TP move={plan.get('tp_move_pct',0.0):.2f}% "
                    f"(adaptive target {plan.get('min_tp_pct',0.0):.2f}% | "
                    f"min RR 1:{plan.get('minimum_rr',2.0):.2f})"
                )

                safe_volume,margin_resize_msg=self.risk.adapt_volume_for_margin(
                    self.symbol,final_action,plan["volume"],plan["entry"],plan["sl"]
                )
                if safe_volume is None:
                    self.log("RiskGuard BLOCKED: " + margin_resize_msg)
                    continue
                if abs(float(safe_volume)-float(plan["volume"])) > 1e-9:
                    old_volume=plan["volume"]
                    plan["volume"]=safe_volume
                    self.log(margin_resize_msg)
                    self.log(
                        f"PLAN RESIZED: {final_action} vol={old_volume} -> {plan['volume']} | "
                        f"SL/TP unchanged; monetary SL risk reduced"
                    )
                elif margin_resize_msg and "volume OK" not in margin_resize_msg:
                    self.log("MarginSizer: " + margin_resize_msg)

                bot_positions_now=[
                    p for p in (mt5.positions_get() or [])
                    if int(getattr(p,"magic",0) or 0)==int(self.s.magic)
                ]
                # Entry count is margin-driven. profile["max_entries"] is now
                # only the global emergency ceiling, not a mode/quality cap.
                if len(bot_positions_now) >= int(self.s.max_open_positions):
                    self.log(
                        f"RiskGuard BLOCKED: emergency position ceiling reached "
                        f"({len(bot_positions_now)}/{self.s.max_open_positions})"
                    )
                    continue

                portfolio_ok, portfolio_msg = self.risk.portfolio_guard(
                    self.symbol,
                    final_action,
                    plan["volume"],
                    plan["entry"],
                    plan["sl"],
                    final_score
                )
                if not portfolio_ok:
                    if str(portfolio_msg).startswith("REAL CORRELATION:"):
                        self.log("RealCorrelationGuard BLOCKED: " + portfolio_msg)
                    elif str(portfolio_msg).startswith("CORRELATION GUARD:"):
                        self.log("CorrelationGuard BLOCKED: " + portfolio_msg)
                    else:
                        self.log("RiskGuard BLOCKED: " + portfolio_msg)
                    continue
                if "real correlation CAUTION" in str(portfolio_msg):
                    self.log("RealCorrelationGuard CAUTION: " + portfolio_msg)
                if "correlation CAUTION" in str(portfolio_msg):
                    self.log("CorrelationGuard CAUTION: " + portfolio_msg)
                self.log("MarginGuard: " + portfolio_msg)

                if not self.s.live_trading:
                    self.mem.save_decision(self.symbol,self.tf,candle,final_action,llm_conf,
                                           reason+" | PAPER",decision_features,None,
                                           tech_conf,mem_stats["memory_score"],final_score)
                    shadow_features=dict(decision_features or {})
                    shadow_features.update({
                        "effective_mode":str(profile.get("effective_mode","")),
                        "risk_pct":float(profile.get("risk_pct",0.0) or 0.0),
                        "quality":float(profile.get("quality",0.0) or 0.0),
                        "expectancy_grade":str(profile.get("expectancy",{}).get("grade","INSUFFICIENT")),
                        "account_stress_level":str(profile.get("account_stress",{}).get("level","NORMAL")),
                    })
                    shadow_id=self.mem.save_shadow_trade(
                        self.symbol,self.tf,final_action,plan["entry"],plan["sl"],plan["tp"],
                        plan["rr"],final_score,profile.get("effective_mode",""),shadow_features)
                    self.log(f"SHADOW OPEN: #{shadow_id} {final_action} {self.symbol} {self.tf} | "
                             f"ENTRY={plan['entry']} SL={plan['sl']} TP={plan['tp']} RR={plan['rr']:.2f} | NO ORDER SENT")
                    self.log("PAPER/SHADOW MODE: hypothetical trade recorded; MT5 order not sent.")
                    continue

                live_ready,live_failures,live_meta=self.live_readiness_guard(
                    profile,plan,final_score
                )
                if str(live_meta.get("account_type","UNKNOWN"))=="REAL":
                    if not live_ready:
                        self.log(
                            "LIVE READINESS BLOCKED: "
                            + " | ".join(str(x) for x in live_failures)
                        )
                        self.mem.save_decision(
                            self.symbol,self.tf,candle,final_action,llm_conf,
                            reason+" | LIVE READINESS BLOCKED: "+"; ".join(live_failures),
                            base,None,tech_conf,mem_stats["memory_score"],final_score
                        )
                        continue
                    self.log(
                        f"LIVE READINESS PASSED: REAL account | risk={live_meta.get('risk_pct',0):.2f}%/"
                        f"{live_meta.get('risk_cap',0):.2f}% cap | RR={live_meta.get('rr',0):.2f} | "
                        f"tests={live_meta.get('completed_tests',0)}/{live_meta.get('required_tests',0)} | "
                        f"stress={live_meta.get('stress','NORMAL')} | "
                        f"margin level={live_meta.get('margin_level',0):.0f}%"
                    )
                else:
                    self.log_once(
                        "live_readiness:test_account",
                        f"LIVE READINESS: {live_meta.get('account_type','UNKNOWN')} account detected; "
                        "REAL-account hard gate not required.",
                        repeat_after=900
                    )

                pf_ok,pf_msg,pf=self.mt.preflight_order(
                    self.symbol,final_action,plan["volume"],plan["sl"],plan["tp"]
                )
                if not pf_ok:
                    self.mem.save_decision(
                        self.symbol,self.tf,candle,final_action,llm_conf,
                        reason+" | PREFLIGHT BLOCKED: "+pf_msg,decision_features,None,
                        tech_conf,mem_stats["memory_score"],final_score
                    )
                    self.log("PREFLIGHT BLOCKED: "+pf_msg)
                    continue

                self.log("Preflight: "+pf_msg)

                result=self.mt.send(self.symbol,final_action,plan["volume"],plan["sl"],plan["tp"])
                ticket=getattr(result,"order",None)
                code=getattr(result,"retcode",None)
                code_name=retcode_name(code)
                self.mem.save_decision(self.symbol,self.tf,candle,final_action,llm_conf,reason,decision_features,ticket,
                                       tech_conf,mem_stats["memory_score"],final_score)
                self.log(
                    f"ORDER retcode={code} {code_name} | "
                    f"order={ticket} deal={getattr(result,'deal',None)} | "
                    f"comment={getattr(result,'comment','')}"
                )
                if code == getattr(mt5,"TRADE_RETCODE_DONE",10009):
                    self.log(
                        f"AUTO EXIT ACTIVE [{profile.get('effective_mode','-')}]: ENTRY={plan['entry']} | "
                        f"SL={plan['sl']} | TP={plan['tp']} | RR={plan['rr']:.2f} | "
                        f"TP move={plan.get('tp_move_pct',0.0):.2f}%"
                    )
                    try:
                        snapshot_key=int(getattr(result,"order",0) or 0)
                        self.entry_risk_snapshot[snapshot_key]={
                            "symbol":self.symbol,
                            "side":final_action,
                            "entry_time":int(time.time()),
                            "entry_price":float(plan["entry"]),
                            "sl":float(plan["sl"]),
                            "tp":float(plan["tp"]),
                            "rr":float(plan["rr"]),
                            "risk_pct":float(profile["risk_pct"]),
                            "quality":float(profile["quality"]),
                            "entry_cap":int(profile["max_entries"]),
                            "session_profit_pct":float(profile["session_profit_pct"]),
                            "session_loss_pct":float(profile["session_loss_pct"]),
                            "final_score":float(final_score),
                            "mode":str(getattr(self,"mode","AUTO")),
                            "managed_sl":float(plan["sl"]),
                            "managed_tp":float(plan["tp"]),
                            "exit_stage":"INITIAL",
                            "last_r_multiple":0.0,
                            "partial_stage1":False,
                            "partial_stage2":False,
                            "partial_stage1_volume":0.0,
                            "partial_stage2_volume":0.0,
                            "account_type":str(live_meta.get("account_type","UNKNOWN")),
                            "live_readiness_passed":bool(live_ready),
                            "consensus_calibration_grade":str(profile.get("consensus_calibration",{}).get("grade","LEARNING")),
                            "consensus_calibration_score":float(profile.get("consensus_calibration",{}).get("score",0.5) or 0.5),
                            "consensus_calibration_risk_multiplier":float(profile.get("consensus_calibration_risk_multiplier",1.0) or 1.0),
                        }
                        self.log(
                            f"ENTRY SNAPSHOT SAVED: entry={plan['entry']} | quality={profile['quality']:.2f} | "
                            f"risk={profile['risk_pct']:.2f}% | RR={plan['rr']:.2f}"
                        )
                    except Exception as e:
                        self.log(f"ENTRY SNAPSHOT WARNING: {e}")

            except Exception as e:
                self.log("ERROR: "+str(e))
                time.sleep(5)
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# V3.10.26 — Historical Replay / Backtest Engine
# ---------------------------------------------------------------------------

class HistoricalReplayEngine:
    """Deterministic candle-by-candle replay for validating the trading logic.

    FAST mode never sends orders and never calls Ollama. It reuses add_indicators(),
    snapshot() and technical_score() so results are tied to the same technical
    pipeline as the live engine. FULL_AI is deliberately a bounded validation mode:
    a caller may provide an ai_decider callback; otherwise it falls back to FAST.
    """

    def __init__(self, starting_balance=10000.0, risk_pct=0.50, rr=2.0,
                 warmup=220, fee_bps=0.0, slippage_bps=0.0):
        self.starting_balance=float(starting_balance)
        self.risk_pct=max(0.01,min(5.0,float(risk_pct)))
        self.rr=max(1.0,float(rr))
        self.warmup=max(60,int(warmup))
        self.fee_bps=max(0.0,float(fee_bps))
        self.slippage_bps=max(0.0,float(slippage_bps))

    @staticmethod
    def _max_drawdown(equity):
        if not equity:
            return 0.0
        peak=float(equity[0]); worst=0.0
        for x in equity:
            x=float(x); peak=max(peak,x)
            if peak>0:
                worst=max(worst,(peak-x)/peak)
        return worst*100.0

    @staticmethod
    def _profit_factor(trades):
        gp=sum(max(0.0,float(t["pnl"])) for t in trades)
        gl=sum(abs(min(0.0,float(t["pnl"]))) for t in trades)
        return gp/gl if gl>1e-12 else (999.0 if gp>0 else 0.0)

    @staticmethod
    def _longest_loss_streak(trades):
        best=cur=0
        for t in trades:
            if float(t["pnl"])<0:
                cur+=1; best=max(best,cur)
            else:
                cur=0
        return best

    def _signal(self, hist, ai_decider=None, mode="FAST"):
        base=snapshot(hist)
        side,conf=technical_score(base)
        side=str(side).upper()
        conf=float(conf or 0.0)
        if str(mode).upper()=="FULL_AI" and callable(ai_decider):
            try:
                ai=ai_decider(dict(base),side,conf)
                if isinstance(ai,dict):
                    a=str(ai.get("action",side) or side).upper()
                    c=float(ai.get("confidence",conf) or conf)
                    if a in {"BUY","SELL","HOLD"}:
                        side=a
                    conf=max(0.0,min(1.0,c))
            except Exception:
                pass
        return base,side,conf

    def run(self, df, symbol="BACKTEST", timeframe="M15", mode="FAST",
            ai_decider=None, min_confidence=0.60, max_bars_in_trade=240):
        if df is None or len(df)<self.warmup+10:
            raise ValueError("Not enough historical candles for replay.")

        data=df.copy().reset_index(drop=True)
        if "time" not in data:
            data["time"]=range(len(data))
        for c in ("open","high","low","close"):
            data[c]=pd.to_numeric(data[c],errors="coerce")
        if "tick_volume" not in data:
            data["tick_volume"]=1.0
        data=data.dropna(subset=["open","high","low","close"]).reset_index(drop=True)

        balance=self.starting_balance
        equity=[balance]
        trades=[]
        open_trade=None

        # Cache indicators once; snapshots still consume only data available up to i.
        enriched=add_indicators(data)

        for i in range(self.warmup,len(data)-1):
            row=data.iloc[i]
            hi=float(row["high"]); lo=float(row["low"]); close=float(row["close"])

            if open_trade is not None:
                side=open_trade["side"]
                hit_sl=(lo<=open_trade["sl"]) if side=="BUY" else (hi>=open_trade["sl"])
                hit_tp=(hi>=open_trade["tp"]) if side=="BUY" else (lo<=open_trade["tp"])
                exit_price=None; reason=None

                # Conservative same-candle rule: if SL and TP are both touched,
                # assume SL happened first rather than granting optimistic hindsight.
                if hit_sl:
                    exit_price=open_trade["sl"]; reason="SL"
                elif hit_tp:
                    exit_price=open_trade["tp"]; reason="TP"
                elif i-open_trade["entry_i"]>=int(max_bars_in_trade):
                    exit_price=close; reason="TIME"

                if exit_price is not None:
                    direction=1.0 if side=="BUY" else -1.0
                    raw_r=direction*(float(exit_price)-open_trade["entry"])/open_trade["risk_distance"]
                    costs=(self.fee_bps+self.slippage_bps)/10000.0
                    cost_cash=open_trade["risk_cash"]*costs*10.0
                    pnl=open_trade["risk_cash"]*raw_r-cost_cash
                    balance+=pnl
                    trades.append({
                        **open_trade,
                        "exit_i":i,
                        "exit_time":str(row["time"]),
                        "exit":float(exit_price),
                        "exit_reason":reason,
                        "r":float(raw_r),
                        "pnl":float(pnl),
                        "balance":float(balance),
                    })
                    equity.append(balance)
                    open_trade=None

            if open_trade is not None:
                continue

            # Snapshot() intentionally selects the last completed candle (-2), so
            # pass data through i+2; this avoids future leakage while matching live behavior.
            hist=data.iloc[:min(i+2,len(data))].copy()
            try:
                base,side,conf=self._signal(hist,ai_decider=ai_decider,mode=mode)
            except Exception:
                continue
            if side not in {"BUY","SELL"} or conf<float(min_confidence):
                continue

            atr=max(float(base.get("atr14",0.0) or 0.0),close*0.001)
            risk_distance=max(atr*1.5,close*0.001)
            entry=close
            sl=entry-risk_distance if side=="BUY" else entry+risk_distance
            tp=entry+risk_distance*self.rr if side=="BUY" else entry-risk_distance*self.rr
            risk_cash=max(0.01,balance*self.risk_pct/100.0)

            open_trade={
                "symbol":str(symbol),
                "timeframe":str(timeframe).upper(),
                "mode":str(mode).upper(),
                "side":side,
                "confidence":conf,
                "regime":str(base.get("regime","UNKNOWN")),
                "structure":str(base.get("structure","NEUTRAL")),
                "entry_i":i,
                "entry_time":str(row["time"]),
                "entry":float(entry),
                "sl":float(sl),
                "tp":float(tp),
                "risk_distance":float(risk_distance),
                "risk_cash":float(risk_cash),
            }

        # Mark any final open position to market.
        if open_trade is not None:
            row=data.iloc[-1]; exit_price=float(row["close"])
            direction=1.0 if open_trade["side"]=="BUY" else -1.0
            raw_r=direction*(exit_price-open_trade["entry"])/open_trade["risk_distance"]
            pnl=open_trade["risk_cash"]*raw_r
            balance+=pnl
            trades.append({
                **open_trade,"exit_i":len(data)-1,"exit_time":str(row["time"]),
                "exit":exit_price,"exit_reason":"END","r":raw_r,"pnl":pnl,"balance":balance
            })
            equity.append(balance)

        wins=[t for t in trades if t["pnl"]>0]
        losses=[t for t in trades if t["pnl"]<0]
        avg_r=(sum(t["r"] for t in trades)/len(trades)) if trades else 0.0

        by_regime={}
        for t in trades:
            k=t["regime"]
            b=by_regime.setdefault(k,{"trades":0,"wins":0,"pnl":0.0,"r":0.0})
            b["trades"]+=1; b["wins"]+=int(t["pnl"]>0); b["pnl"]+=t["pnl"]; b["r"]+=t["r"]
        for b in by_regime.values():
            n=max(1,b["trades"])
            b["win_rate"]=b["wins"]/n
            b["avg_r"]=b["r"]/n

        result={
            "symbol":str(symbol),"timeframe":str(timeframe).upper(),"mode":str(mode).upper(),
            "starting_balance":self.starting_balance,"ending_balance":balance,
            "net_profit":balance-self.starting_balance,
            "return_pct":((balance/self.starting_balance)-1.0)*100.0 if self.starting_balance else 0.0,
            "trades":len(trades),"wins":len(wins),"losses":len(losses),
            "win_rate":len(wins)/len(trades) if trades else 0.0,
            "profit_factor":self._profit_factor(trades),
            "expectancy_r":avg_r,
            "max_drawdown_pct":self._max_drawdown(equity),
            "longest_loss_streak":self._longest_loss_streak(trades),
            "by_regime":by_regime,
            "trade_log":trades,
            "equity_curve":equity,
        }
        return result

    @staticmethod
    def format_report(r):
        lines=[
            "BACKTEST / REPLAY RESULT",
            f"Symbol      : {r.get('symbol')}",
            f"Timeframe   : {r.get('timeframe')}",
            f"Mode        : {r.get('mode')}",
            f"Trades      : {r.get('trades',0)}",
            f"Win Rate    : {float(r.get('win_rate',0))*100:.1f}%",
            f"Profit Fact.: {float(r.get('profit_factor',0)):.2f}",
            f"Expectancy  : {float(r.get('expectancy_r',0)):+.2f}R",
            f"Net Profit  : {float(r.get('net_profit',0)):+.2f}",
            f"Return      : {float(r.get('return_pct',0)):+.2f}%",
            f"Max DD      : {float(r.get('max_drawdown_pct',0)):.2f}%",
            f"Longest L   : {int(r.get('longest_loss_streak',0))} trade(s)",
            "",
            "BY REGIME",
        ]
        for k,v in sorted((r.get("by_regime") or {}).items()):
            lines.append(
                f"{k}: n={v['trades']} | WR={v['win_rate']*100:.1f}% | "
                f"AvgR={v['avg_r']:+.2f} | PnL={v['pnl']:+.2f}"
            )
        return "\n".join(lines)


def replay_from_mt5(symbol, timeframe="M15", bars=5000, mode="FAST",
                    starting_balance=10000.0, risk_pct=0.50, rr=2.0,
                    ai_decider=None):
    """Convenience entry point. Reads historical candles only; never sends orders."""
    tf=str(timeframe).upper()
    code=TIMEFRAMES.get(tf)
    if code is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    arr=mt5.copy_rates_from_pos(str(symbol),code,0,max(300,int(bars)))
    if arr is None or len(arr)<300:
        raise RuntimeError(f"MT5 returned insufficient history for {symbol} {tf}")
    df=pd.DataFrame(arr)
    if "time" in df:
        df["time"]=pd.to_datetime(df["time"],unit="s",errors="coerce")
    engine=HistoricalReplayEngine(
        starting_balance=starting_balance,risk_pct=risk_pct,rr=rr
    )
    return engine.run(
        df,symbol=symbol,timeframe=tf,mode=mode,ai_decider=ai_decider
    )
