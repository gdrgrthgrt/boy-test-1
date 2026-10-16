#!/usr/bin/env python3
"""Admin mini-app: HTTP server (runs in a thread inside the bot process).

Serves the owner-only admin panel for the Telegram bot. State is file-based
so the bot and the panel always stay in sync.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOT_DIR = Path(os.environ.get("BOT_DIR", Path(__file__).resolve().parent))
PANEL_HTML = BOT_DIR / "panel.html"
APPROVED_FILE = BOT_DIR / ".approved"
SETTINGS_FILE = BOT_DIR / ".settings.json"
PENDING_FILE = BOT_DIR / ".pending.json"
STATS_FILE = BOT_DIR / ".stats.json"
TUNNEL_FILE = BOT_DIR / ".tunnel_url"
USER_LIMITS_FILE = BOT_DIR / ".user_limits.json"
ALERTS_FILE = BOT_DIR / ".alerts.json"
USER_MSGS_FILE = BOT_DIR / ".user_msgs.json"
PLANS_FILE = BOT_DIR / ".plans.json"
PROFILES_FILE = BOT_DIR / ".profiles.json"
WATCHLIST_FILE = BOT_DIR / ".watchlists.json"
JOURNAL_FILE = BOT_DIR / ".journals.json"
PANEL_ACCESS_TOKEN = os.environ.get("PANEL_ACCESS_TOKEN", "").strip()
_JSON_LOCK = threading.RLock()

DEFAULT_PLANS = {
    "free": {"label": "Free", "daily_messages": 5, "market_tools": True, "alerts": 2, "journal": True, "watchlist": 5},
    "pro": {"label": "Pro", "daily_messages": 30, "market_tools": True, "alerts": 10, "journal": True, "watchlist": 15},
    "vip": {"label": "VIP", "daily_messages": 0, "market_tools": True, "alerts": 50, "journal": True, "watchlist": 30},
}

DEFAULT_SETTINGS = {
    "mode": "closed",
    "maintenance": False,
    "features": {
        "market_analysis": True,
        "news_trading": True,
        "market_scanner": True,
        "risk_calculator": True,
        "smart_alerts": True,
        "daily_brief": True,
    },
    "digest": {
        "enabled": True,
        "time_utc": "05:30",
        "news_count": 7,
        "schedule_id": "4bbb16bf",
        "sources": {
            "myfxbook": True,
            "dailyfx": True,
            "fxstreet": True,
            "tradingview": True,
            "tradingeconomics": True,
            "investing": True,
            "forexfactory": True,
        },
    },
}


# ---------- State helpers (file-based, shared with the bot) ----------

def _load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _save_json(path, data):
    """Atomically persist JSON so a crash cannot leave a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with _JSON_LOCK:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass


def load_settings():
    s = _load_json(SETTINGS_FILE, {})
    # merge with defaults so new keys appear
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    for k, v in s.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def save_settings(s):
    _save_json(SETTINGS_FILE, s)


def load_approved():
    try:
        if APPROVED_FILE.exists():
            return {int(u) for u in APPROVED_FILE.read_text().split() if u.strip().isdigit()}
    except Exception:
        pass
    return set()


def save_approved(users):
    APPROVED_FILE.write_text("\n".join(str(u) for u in sorted(users)) + "\n")
    try:
        APPROVED_FILE.chmod(0o600)
    except Exception:
        pass


def load_pending():
    return _load_json(PENDING_FILE, {})


def save_pending(p):
    _save_json(PENDING_FILE, p)


def load_stats():
    return _load_json(STATS_FILE, {"messages": 0, "users_seen": [], "last_digest": None})


def save_stats(s):
    _save_json(STATS_FILE, s)


def bump_stat(key, value=None):
    s = load_stats()
    if value is not None:
        s[key] = value
    elif key in s and isinstance(s[key], int):
        s[key] += 1
    save_stats(s)


def get_tunnel_url():
    try:
        if TUNNEL_FILE.exists():
            return TUNNEL_FILE.read_text().strip()
    except Exception:
        pass
    return ""


# ---------- Per-user message limits ----------

def load_user_limits():
    """{user_id: daily_limit (int, 0 = unlimited)}"""
    return _load_json(USER_LIMITS_FILE, {})


def save_user_limits(d):
    _save_json(USER_LIMITS_FILE, d)


def set_user_limit(uid: int, limit: int):
    d = load_user_limits()
    limit = max(0, int(limit))
    if limit == 0:
        d.pop(str(uid), None)
    else:
        d[str(uid)] = limit
    save_user_limits(d)


def get_user_limit(uid: int) -> int:
    return int(load_user_limits().get(str(uid), 0))


# ---------- Per-user daily message counts ----------

def _today_tehran() -> str:
    # Tehran = UTC+3:30
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3.5 * 3600))


def load_user_msgs():
    return _load_json(USER_MSGS_FILE, {})


def save_user_msgs(d):
    _save_json(USER_MSGS_FILE, d)


def record_user_msg(uid: int) -> int:
    """Increment today's count for a user. Returns the new count."""
    d = load_user_msgs()
    today = _today_tehran()
    entry = d.get(str(uid), {})
    if entry.get("date") != today:
        entry = {"date": today, "count": 0}
    entry["count"] = int(entry.get("count", 0)) + 1
    d[str(uid)] = entry
    # prune old days
    for k in list(d.keys()):
        if d[k].get("date") != today:
            d.pop(k, None)
    save_user_msgs(d)
    return entry["count"]


def get_user_msg_count(uid: int) -> int:
    entry = load_user_msgs().get(str(uid), {})
    if entry.get("date") == _today_tehran():
        return int(entry.get("count", 0))
    return 0


# ---------- Plans / XP / Watchlist / Trading Journal ----------

def load_plans():
    return _load_json(PLANS_FILE, json.loads(json.dumps(DEFAULT_PLANS)))

def save_plans(d):
    _save_json(PLANS_FILE, d)

def load_profiles():
    return _load_json(PROFILES_FILE, {})

def save_profiles(d):
    _save_json(PROFILES_FILE, d)

def get_profile(uid: int):
    d=load_profiles(); k=str(uid); p=d.get(k, {"plan":"free","xp":0,"level":1,"streak":0,"last_active":None,"trades":0,"wins":0,"losses":0})
    return p

def add_xp(uid: int, amount: int, reason: str = "activity"):
    d=load_profiles(); k=str(uid); p=get_profile(uid); today=_today_tehran()
    if p.get("last_active") == today:
        p["streak"] = max(1, int(p.get("streak",1)))
    else:
        # A simple consecutive-day streak; exact yesterday handling is intentionally UTC+3:30 based.
        p["streak"] = max(1, int(p.get("streak",0))+1)
        p["last_active"] = today
    p["xp"] = int(p.get("xp",0))+max(0,int(amount))
    p["level"] = 1 + p["xp"]//250
    p["last_xp_reason"] = reason
    d[k]=p; save_profiles(d); return p

def set_user_plan(uid: int, plan: str):
    plans=load_plans(); plan=plan if plan in plans else "free"
    d=load_profiles(); p=get_profile(uid); p["plan"]=plan; d[str(uid)]=p; save_profiles(d); return p

def get_user_plan(uid: int):
    p=get_profile(uid); plans=load_plans(); name=p.get("plan","free"); return name, plans.get(name, plans["free"])

def load_watchlists(): return _load_json(WATCHLIST_FILE, {})
def save_watchlists(d): _save_json(WATCHLIST_FILE,d)
def get_watchlist(uid: int): return load_watchlists().get(str(uid),[])
def set_watchlist(uid: int, items):
    d=load_watchlists(); d[str(uid)]=[str(x).upper().replace('/','') for x in items][:30]; save_watchlists(d); return d[str(uid)]

def load_journals(): return _load_json(JOURNAL_FILE, {})
def save_journals(d): _save_json(JOURNAL_FILE,d)
def get_journal(uid: int): return load_journals().get(str(uid),[])
def add_trade(uid: int, trade: dict):
    d = load_journals()
    rows = d.get(str(uid), [])
    trade = dict(trade)
    # Millisecond timestamps can collide for rapid submissions; guarantee per-user uniqueness.
    existing = {int(t.get("id", 0)) for t in rows}
    trade_id = int(time.time() * 1000)
    while trade_id in existing:
        trade_id += 1
    trade["id"] = trade_id
    trade["created"] = time.strftime("%Y-%m-%d %H:%M")
    trade["result"] = "open"
    rows.append(trade)
    d[str(uid)] = rows[-100:]
    save_journals(d)
    return trade

def update_trade_result(uid: int, trade_id: int, result: str):
    """Update a trade idempotently and keep profile counters consistent on corrections."""
    aliases = {"win": "win", "profit": "win", "برد": "win", "loss": "loss", "losses": "loss", "باخت": "loss"}
    normalized = aliases.get(str(result).strip().lower())
    if normalized is None:
        return None
    d = load_journals()
    rows = d.get(str(uid), [])
    changed = None
    previous = None
    for trade in rows:
        if int(trade.get("id", 0)) == int(trade_id):
            previous = str(trade.get("result", "open")).lower()
            if previous == normalized:
                return None  # Repeating the same result must not inflate statistics or XP.
            trade["result"] = normalized
            changed = trade
            break
    if changed:
        profile_data = load_profiles()
        profile = get_profile(uid)
        if previous in ("win", "profit", "برد"):
            profile["wins"] = max(0, int(profile.get("wins", 0)) - 1)
        elif previous in ("loss", "losses", "باخت"):
            profile["losses"] = max(0, int(profile.get("losses", 0)) - 1)
        if normalized == "win":
            profile["wins"] = int(profile.get("wins", 0)) + 1
        else:
            profile["losses"] = int(profile.get("losses", 0)) + 1
        # Trades means completed trades, not every result-edit operation.
        profile["trades"] = max(0, int(profile.get("trades", 0)) + (0 if previous in ("win", "loss", "profit", "losses", "برد", "باخت") else 1))
        profile_data[str(uid)] = profile
        save_profiles(profile_data)
        save_journals(d)
    return changed

# ---------- Price alerts ----------

def load_alerts():
    """[{id, symbol, direction, price, active, created}]"""
    return _load_json(ALERTS_FILE, [])


def save_alerts(a):
    _save_json(ALERTS_FILE, a)


def update_digest_schedule(time_utc: str, enabled: bool = True):
    """Standalone scheduler: persist the schedule; bot process executes it."""
    try:
        hh, mm = time_utc.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        return False, "bad time format"
    s = load_settings()
    d = s.setdefault("digest", {})
    d["time_utc"] = f"{hh:02d}:{mm:02d}"
    d["enabled"] = bool(enabled)
    d["schedule_id"] = "standalone"
    save_settings(s)
    return True, f"saved {d['time_utc']} UTC"


def toggle_source(key: str, on: bool):
    s = load_settings()
    d = s.setdefault("digest", {})
    src = d.setdefault("sources", {})
    if key in src:
        src[key] = bool(on)
        save_settings(s)
        return True
    return False


# ---------- Telegram WebApp initData verification ----------

def verify_init_data(bot_token: str, init_data: str) -> dict | None:
    """Verify Telegram WebApp initData. Returns the user dict if valid, else None."""
    if not init_data:
        return None
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    params = dict(pairs)
    if len(params) != len(pairs):
        return None  # Reject duplicate keys to avoid ambiguous signature validation.
    user_raw = params.get("user")
    try:
        auth_date = int(params.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 60 or now - auth_date > 86400:
            return None
    except (TypeError, ValueError):
        return None
    if not user_raw:
        return None
    # Telegram requires all fields except hash (including auth_date) in the check string.
    check_items = sorted(
        (k, v) for k, v in params.items() if k != "hash"
    )
    data_check_string = "\n".join(f"{k}={v}" for k, v in check_items)
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, params.get("hash", "")):
        return None
    try:
        return json.loads(user_raw)
    except Exception:
        return None


# ---------- Actions ----------

class AdminAPI:
    def __init__(self, bot_token: str, owner_id: int, tg_send=None):
        self.bot_token = bot_token
        self.owner_id = owner_id
        self.tg_send = tg_send  # callable(chat_id, text, parse_mode)

    def state(self):
        settings = load_settings()
        approved = sorted(load_approved())
        pending = load_pending()
        stats = load_stats()
        limits = load_user_limits()
        alerts = load_alerts()
        plans = load_plans(); profiles = load_profiles()
        users = []
        for uid in approved:
            prof=profiles.get(str(uid), get_profile(uid)); plan_name=prof.get("plan","free"); plan=plans.get(plan_name, plans["free"])
            effective_limit=int(limits.get(str(uid), 0)) or int(plan.get("daily_messages",0))
            users.append({
                "id": uid, "limit": effective_limit, "custom_limit": int(limits.get(str(uid),0)),
                "today": get_user_msg_count(uid), "plan": plan_name, "xp": int(prof.get("xp",0)),
                "level": int(prof.get("level",1)), "streak": int(prof.get("streak",0)),
            })
        all_profiles=list(profiles.values())
        total_trades=sum(int(x.get("trades",0)) for x in all_profiles)
        total_wins=sum(int(x.get("wins",0)) for x in all_profiles)
        return {
            "ok": True,
            "owner_id": self.owner_id,
            "settings": settings,
            "approved": users,
            "pending": pending,
            "stats": stats,
            "limits": limits,
            "alerts": alerts,
            "plans": plans,
            "profiles": profiles,
            "trading_stats": {"trades": total_trades, "wins": total_wins, "win_rate": round(total_wins/total_trades*100,1) if total_trades else 0},
            "tunnel_url": get_tunnel_url(),
            "now_utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        }

    def _run_manage_schedule(self, args):
        return False, "manage-schedule is unavailable in standalone mode"

    def action(self, name, payload):
        s = load_settings()
        if name == "set_mode":
            s["mode"] = payload.get("mode", "closed") if payload.get("mode") in ("open", "closed") else "closed"
            save_settings(s)
            return {"ok": True, "msg": "حالت تغییر کرد"}

        elif name == "approve_user":
            uid = int(payload.get("user_id"))
            approved = load_approved(); approved.add(uid); save_approved(approved)
            p = load_pending(); p.pop(uid, None); save_pending(p)
            if self.tg_send:
                self.tg_send(uid, "✅ دسترسی‌ات توسط مالک ربات فعال شد. خوش آمدی! 🙂")
            return {"ok": True, "msg": f"کاربر {uid} مجاز شد"}

        elif name == "deny_user":
            uid = int(payload.get("user_id"))
            p = load_pending(); p.pop(uid, None); save_pending(p)
            if self.tg_send:
                self.tg_send(uid, "❌ متأسفانه درخواستت توسط مالک ربات رد شد.")
            return {"ok": True, "msg": f"کاربر {uid} رد شد"}

        elif name == "remove_user":
            uid = int(payload.get("user_id"))
            approved = load_approved(); approved.discard(uid); save_approved(approved)
            return {"ok": True, "msg": f"کاربر {uid} حذف شد"}

        elif name == "add_user":
            uid = int(payload.get("user_id"))
            approved = load_approved(); approved.add(uid); save_approved(approved)
            if self.tg_send:
                self.tg_send(uid, "✅ دسترسی‌ات توسط مالک ربات فعال شد. خوش آمدی! 🙂")
            return {"ok": True, "msg": f"کاربر {uid} اضافه شد"}

        elif name == "set_feature":
            key = str(payload.get("key", ""))
            features = s.setdefault("features", {})
            if key not in features:
                return {"ok": False, "msg": "قابلیت نامعتبر است"}
            features[key] = bool(payload.get("enabled"))
            save_settings(s)
            return {"ok": True, "msg": "قابلیت به‌روزرسانی شد"}

        elif name == "set_maintenance":
            s["maintenance"] = bool(payload.get("enabled"))
            save_settings(s)
            return {"ok": True, "msg": "حالت نگهداری تغییر کرد"}

        elif name == "set_digest":
            d = s.setdefault("digest", {})
            if "enabled" in payload:
                d["enabled"] = bool(payload["enabled"])
            if payload.get("time_utc"):
                d["time_utc"] = payload["time_utc"]
            if "news_count" in payload:
                d["news_count"] = max(1, min(20, int(payload["news_count"])))
            if isinstance(payload.get("sources"), dict):
                d["sources"].update({k: bool(v) for k, v in payload["sources"].items() if k in d["sources"]})
            save_settings(s)
            # update cron if time changed
            if payload.get("time_utc"):
                self._update_cron(d["time_utc"], d["enabled"])
            return {"ok": True, "msg": "تنظیمات گزارش ذخیره شد"}

        elif name == "run_digest_now":
            return {"ok": False, "msg": "اجرای دستی گزارش در نسخه مستقل در حال اتصال است؛ زمان‌بندی خودکار فعال است."}

        elif name == "broadcast":
            text = (payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "msg": "متن خالی است"}
            targets = sorted(load_approved() | {self.owner_id})
            sent = 0
            if self.tg_send:
                for uid in targets:
                    try:
                        self.tg_send(uid, text)
                        sent += 1
                    except Exception:
                        pass
            return {"ok": True, "msg": f"برای {sent} نفر ارسال شد"}

        elif name == "set_user_limit":
            uid = int(payload.get("user_id"))
            limit = int(payload.get("limit", 0))
            set_user_limit(uid, limit)
            msg = f"محدودیت کاربر {uid} حذف شد (نامحدود)" if limit == 0 else f"محدودیت کاربر {uid} روی {limit} پیام/روز تنظیم شد"
            return {"ok": True, "msg": msg}

        elif name == "add_alert":
            symbol = (payload.get("symbol") or "").strip().upper()
            direction = payload.get("direction", "above")
            try:
                price = float(payload.get("price"))
            except (TypeError, ValueError):
                return {"ok": False, "msg": "قیمت باید عدد معتبر باشد"}
            if not re.fullmatch(r"[A-Z0-9._=/-]{2,20}", symbol) or direction not in ("above", "below") or not (price > 0 and price < float("inf")):
                return {"ok": False, "msg": "نماد، جهت یا قیمت معتبر نیست"}
            alerts = load_alerts()
            aid = int(time.time() * 1000)
            alerts.append({
                "id": aid, "user_id": self.owner_id, "symbol": symbol, "direction": direction,
                "price": price, "active": True, "created": time.strftime("%Y-%m-%d %H:%M"),
            })
            save_alerts(alerts)
            return {"ok": True, "msg": f"هشدار {symbol} در {price} فعال شد"}

        elif name == "remove_alert":
            aid = int(payload.get("alert_id"))
            alerts = load_alerts()
            alerts = [a for a in alerts if a.get("id") != aid]
            save_alerts(alerts)
            return {"ok": True, "msg": "هشدار حذف شد"}

        elif name == "set_user_plan":
            uid=int(payload.get("user_id")); plan=str(payload.get("plan","free")); set_user_plan(uid, plan)
            return {"ok": True, "msg": f"پلن کاربر {uid} روی {plan.upper()} تنظیم شد"}

        elif name == "set_plan_limit":
            plan=str(payload.get("plan","free")); daily=max(0,int(payload.get("daily_messages",0)))
            plans=load_plans();
            if plan not in plans: return {"ok":False,"msg":"پلن نامعتبر است"}
            plans[plan]["daily_messages"]=daily; plans[plan]["alerts"]=max(0,int(payload.get("alerts",plans[plan].get("alerts",2)))); plans[plan]["watchlist"]=max(1,int(payload.get("watchlist",plans[plan].get("watchlist",5))))
            save_plans(plans); return {"ok":True,"msg":"تنظیمات پلن ذخیره شد"}

        elif name == "reset_xp":
            uid=int(payload.get("user_id")); d=load_profiles(); p=get_profile(uid); p.update({"xp":0,"level":1,"streak":0}); d[str(uid)]=p; save_profiles(d); return {"ok":True,"msg":"XP کاربر ریست شد"}

        elif name == "reset_settings":
            save_settings(json.loads(json.dumps(DEFAULT_SETTINGS)))
            return {"ok": True, "msg": "تنظیمات به حالت پیش‌فرض برگشت"}

        else:
            return {"ok": False, "msg": "عملیات نامعتبر"}

    def _update_cron(self, time_utc, enabled):
        # Standalone mode: the bot process owns the scheduler. Settings are
        # persisted here and digest_scheduler() picks them up automatically.
        return None


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    api: AdminAPI = None
    owner_id: int = 0
    bot_token: str = ""

    def log_message(self, *a):
        pass

    def _cors(self):
        # Same-origin requests need no CORS header. Explicitly allow only a configured origin.
        allowed = os.environ.get("PANEL_ALLOWED_ORIGIN", "").strip().rstrip("/")
        origin = self.headers.get("Origin", "").rstrip("/")
        if allowed and origin == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        """Return True if the request is from the owner (initData or token)."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        init_data = self.headers.get("X-Telegram-Init-Data", "")
        user = verify_init_data(self.bot_token, init_data)
        if user and int(user.get("id", 0)) == self.owner_id:
            return True
        # Never derive a management credential from the public bot token.
        # For browser access outside Telegram, configure a separate PANEL_ACCESS_TOKEN.
        h = self.headers.get("Authorization", "")
        configured = os.environ.get("PANEL_ACCESS_TOKEN", "").strip()
        if configured and h.startswith("Bearer "):
            return hmac.compare_digest(h[7:], configured)
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Telegram-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"ok": True, "service": "fx-telegram-bot"})
            return
        if parsed.path == "/":
            try:
                html = PANEL_HTML.read_text()
            except Exception:
                self._json(500, {"ok": False, "msg": "panel.html not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.send_header("Content-Length", str(len(html.encode())))
            self.end_headers()
            self.wfile.write(html.encode())
            return
        if parsed.path == "/api/state":
            if not self._authed():
                self._json(401, {"ok": False, "msg": "unauthorized"})
                return
            self._json(200, self.api.state())
            return
        self._json(404, {"ok": False, "msg": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/action":
            if not self._authed():
                self._json(401, {"ok": False, "msg": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json(400, {"ok": False, "msg": "bad json"})
                return
            name = body.get("action")
            payload = body.get("payload", {})
            try:
                result = self.api.action(name, payload if isinstance(payload, dict) else {})
            except (ValueError, TypeError, OverflowError) as exc:
                self._json(400, {"ok": False, "msg": "ورودی نامعتبر است"})
                return
            except Exception:
                self._json(500, {"ok": False, "msg": "خطای داخلی در اجرای عملیات"})
                return
            self._json(200, result)
            return
        self._json(404, {"ok": False, "msg": "not found"})


def start_admin_panel(bot_token: str, owner_id: int, port: int = 8099, tg_send=None) -> ThreadingHTTPServer:
    api = AdminAPI(bot_token, owner_id, tg_send=tg_send)
    Handler.api = api
    Handler.owner_id = owner_id
    Handler.bot_token = bot_token
    server = ThreadingHTTPServer((os.environ.get("PANEL_HOST", "0.0.0.0"), port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server