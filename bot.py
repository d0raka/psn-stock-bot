#!/usr/bin/env python3
"""psn-stock-bot: Telegram restock alerts for Sony PlayStation Store (PSN) gift cards on Amazon India.

Polls the Amazon.in product pages listed in products.json and notifies every subscribed Telegram chat when a
denomination comes back in stock (and when it sells out again). Configuration is via environment variables;
see .env.example and README.md.
"""
import html, json, logging, os, random, re, sys, threading, time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo
from curl_cffi import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Minimal .env loader (KEY=VALUE lines, # comments). Real environment variables always win."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(BASE_DIR, ".env"))

URL = "https://www.amazon.in/dp/B07K6RYVJ5"  # legacy single-product URL; used only as a Referer fallback
LEGACY_ASIN = "B07K6RYVJ5"
PRODUCT_FAMILY = "Sony PlayStation Store Gift Card (Email Delivery – Digital Voucher Code) – אמזון הודו"
__version__ = "1.0.0"
TZ_NAME = os.environ.get("BOT_TIMEZONE", "").strip() or "Asia/Jerusalem"
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # unknown zone name -> fall back to the default
    TZ_NAME = "Asia/Jerusalem"
    TZ = ZoneInfo(TZ_NAME)
DATA = os.environ.get("DATA_DIR", "").strip() or os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA, "state.json")
PRODUCTS_FILE = os.environ.get("PRODUCTS_FILE", "").strip() or os.path.join(DATA, "products.json")
# Public Amazon.in listings (product title "Rs.<N> Sony PlayStation Store Gift Card (Email Delivery in 1 hour- Digital Voucher Code)")
DEFAULT_PRODUCTS = [
    {"asin": "B07K6RYVHR", "amount": 1000, "label": "1000 ₹", "enabled": True},
    {"asin": "B093QF35KZ", "amount": 2000, "label": "2000 ₹", "enabled": True},
    {"asin": "B07K6PVR8B", "amount": 3000, "label": "3000 ₹", "enabled": True},
    {"asin": "B07K6RYVJ5", "amount": 4000, "label": "4000 ₹", "enabled": True},
    {"asin": "B0B3DDW7TB", "amount": 5000, "label": "5000 ₹", "enabled": True},
]
STAGGER = (10, 18)          # seconds between product fetches within a cycle
CYCLE_RETRY_BUDGET = 240    # seconds: only retry BLOCKED products while the cycle is younger than this
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ENV_CHAT_ID = os.environ.get("CHAT_ID", "").strip()


def _int_env(name, default=0):
    v = os.environ.get(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default


# Telegram user id of the bot admin (for a private chat, chat id == user id). 0/unset = nobody can change settings.
OWNER_ID = _int_env("OWNER_ID", 0)
PROXY = os.environ.get("AMAZON_PROXY", "").strip()
POLL = max(60, _int_env("POLL_SECONDS", 300))
JITTER = 30
# Datacenter IPs get Amazon's robot page intermittently; retry a few times per poll.
ATTEMPTS = max(1, _int_env("CHECK_ATTEMPTS", 3))
ATTEMPT_GAP = 20
MANUAL_COOLDOWN = 60
_products = {"list": []}
MAX_REMINDERS = 6
REMINDER_CHOICES = (0, 10, 30)
DEFAULT_REMINDERS = 10
HISTORY_MAX = 50
TG = f"https://api.telegram.org/bot{TOKEN}"
# Filled from getMe at startup; BOT_USERNAME is only a fallback used before/without getMe.
BOT = {"username": os.environ.get("BOT_USERNAME", "").strip().lstrip("@")}
# ALLOWED_GROUPS (comma-separated chat ids):
#   empty  -> open mode: any group and any private user may subscribe.
#   set    -> restricted mode: the bot stays only in these groups (leaves any other group/channel), and private
#             users must be members of one of them (the owner is always allowed).
ALLOWED_GROUPS = set()
for _g in os.environ.get("ALLOWED_GROUPS", "").replace(";", ",").split(","):
    _g = _g.strip()
    if _g.lstrip("-").isdigit():
        ALLOWED_GROUPS.add(int(_g))
RESTRICTED = bool(ALLOWED_GROUPS)
PRIVATE_TEXT = "הבוט הזה פרטי 🔒"
# Optional invite link to your alerts group; enables the "share the group" buttons and /share. Empty = no share UI.
GROUP_INVITE = os.environ.get("GROUP_INVITE", "").strip()
from urllib.parse import quote as _q


def tracked_amounts_text():
    """Amounts enabled in products.json, used in user-facing group promo text."""
    if not products():
        load_products()
    amounts = [str(p["amount"]) for p in products() if p.get("enabled")]
    return " · ".join(amounts) if amounts else "—"


def promo_text():
    return ("🎮 מחפשים כרטיסי PlayStation (PSN) באמזון הודו?\n"
            "הצטרפו לקבוצה ותקבלו התראה אוטומטית ברגע שכרטיס חוזר למלאי 🚨\n"
            f"💵 {tracked_amounts_text()} רופי\n👇")


def share_enabled():
    return bool(GROUP_INVITE)


def share_group_url():
    return f"https://t.me/share/url?url={_q(GROUP_INVITE, safe='')}&text={_q(promo_text(), safe='')}"


def promo_full():
    return promo_text() + "\n" + GROUP_INVITE
_copy_ok = {"v": True}  # flips to False if Telegram rejects copy_text buttons
MEMBER_TTL = 300
LOCKED_TOAST = "🔒 רק המנהל יכול לשנות הגדרות"

os.makedirs(DATA, exist_ok=True)


def redact(s):
    s = str(s)
    if TOKEN:
        s = s.replace(TOKEN, "<TOKEN>")
    if PROXY:
        s = s.replace(PROXY, "<PROXY>")
    return s


class RedactFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


log = logging.getLogger("psn")
log.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
for h in (RotatingFileHandler(os.path.join(DATA, "bot.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
          logging.StreamHandler(sys.stdout)):
    h.setFormatter(fmt)
    h.addFilter(RedactFilter())
    log.addHandler(h)
logging.Formatter.converter = staticmethod(lambda t: datetime.fromtimestamp(t, TZ).timetuple())

_lock = threading.RLock()        # state.json
_check_lock = threading.Lock()   # one Amazon check at a time
_manual_lock = threading.Lock()
_manual = {"last": 0.0}


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.error("state.json unreadable (%s); starting fresh", redact(e))
        return {}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def now_il():
    return datetime.now(TZ)


def now_iso():
    return now_il().isoformat(timespec="seconds")


def parse_iso(s):
    try:
        return datetime.fromisoformat(s).astimezone(TZ)
    except Exception:
        return None


def fmt_il(iso, date=True):
    d = parse_iso(iso) if isinstance(iso, str) else iso
    if not d:
        return "—"
    return d.strftime("%H:%M %d/%m/%Y" if date else "%H:%M")


def minutes_between(a_iso, b_iso=None):
    a = parse_iso(a_iso)
    b = parse_iso(b_iso) if b_iso else now_il()
    if not a or not b:
        return None
    return max(0, int(round((b - a).total_seconds() / 60)))


def new_sub():
    return {"muted": False, "reminders": DEFAULT_REMINDERS, "rem": {}, "since": now_iso()}


def migrate_state():
    """Legacy state upgrade: single chat_id -> subscribers{}, add history/checks. Also seeds CHAT_ID on first run."""
    with _lock:
        st = load_state()
        if "subscribers" not in st:
            subs = {}
            for cid in (st.get("chat_id"), ENV_CHAT_ID):
                if cid:
                    s = new_sub()
                    s["type"] = "private" if int(cid) > 0 else "group"
                    subs[str(int(cid))] = s
            st["subscribers"] = subs
            log.info("initialised subscribers from legacy state / CHAT_ID: %s", list(subs))
        if "history" not in st and "products" not in st:
            st["history"] = ([{"ts": st["last_change"], "status": st["last_status"]}]
                             if st.get("last_status") and st.get("last_change") else [])
        if "products" not in st:
            st.setdefault("checks", [])
        save_state(st)
        return st


def get_sub(cid):
    return load_state().get("subscribers", {}).get(str(cid))


def subscribe(chat):
    cid = str(chat["id"])
    with _lock:
        st = load_state()
        subs = st.setdefault("subscribers", {})
        is_new = cid not in subs
        s = subs.setdefault(cid, new_sub())
        s["type"] = chat.get("type", s.get("type", ""))
        s["title"] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        save_state(st)
    if is_new:
        log.info("subscribed chat %s (type=%s) - total %d", cid, s["type"], len(subs))
    return is_new


def update_sub(cid, **kw):
    with _lock:
        st = load_state()
        s = st.setdefault("subscribers", {}).setdefault(str(cid), new_sub())
        s.update(kw)
        save_state(st)
        return s


def unsubscribe(cid, reason):
    with _lock:
        st = load_state()
        removed = st.get("subscribers", {}).pop(str(cid), None)
        save_state(st)
    if removed is not None:
        log.info("unsubscribed chat %s (%s)", cid, redact(reason))


def migrate_chat(old, new):
    with _lock:
        st = load_state()
        subs = st.setdefault("subscribers", {})
        s = subs.pop(str(old), None)
        if s is None and str(new) in subs:
            return
        s = s or new_sub()
        s["type"] = "supergroup"
        subs[str(new)] = {**s, **subs.get(str(new), {})}
        save_state(st)
    log.info("chat %s migrated to supergroup %s", old, new)


# ---------------------------------------------------------------- products
def load_products():
    """products.json: [{"asin","amount","label","enabled"}]. Created with DEFAULT_PRODUCTS if missing."""
    try:
        with open(PRODUCTS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = DEFAULT_PRODUCTS
        try:
            with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            log.info("created %s with %d default products", PRODUCTS_FILE, len(raw))
        except OSError as e:
            log.warning("could not write %s: %s", PRODUCTS_FILE, e)
    except Exception as e:
        log.error("products.json unreadable (%s); using defaults", redact(e))
        raw = DEFAULT_PRODUCTS
    out, seen = [], set()
    for p in raw:
        a = str(p.get("asin", "")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", a) or a in seen:
            continue
        seen.add(a)
        amt = int(p.get("amount") or 0)
        out.append({"asin": a, "amount": amt, "label": p.get("label") or f"{amt} ₹",
                    "enabled": bool(p.get("enabled", True))})
    out.sort(key=lambda x: x["amount"])
    _products["list"] = out
    return out


def products():
    return _products["list"]


def product(asin):
    return next((p for p in products() if p["asin"] == asin), None)


def purl(asin):
    return f"https://www.amazon.in/dp/{asin}"


def pstate(st, asin):
    return st.setdefault("products", {}).setdefault(asin, {})


def is_enabled(st, asin):
    ov = st.get("product_enabled", {}).get(asin)
    if ov is not None:
        return bool(ov)
    p = product(asin)
    return bool(p and p["enabled"])


def enabled_products(st=None):
    st = st or load_state()
    return [p for p in products() if is_enabled(st, p["asin"])]


def migrate_v06():
    """Legacy state upgrade: top-level single-product state moves to products[B07K6RYVJ5]; rem_sent -> rem{asin:n}."""
    with _lock:
        st = load_state()
        if "products" in st:
            return st
        ps = {}
        for k in ("last_status", "last_change", "last_result", "in_since", "consecutive_blocked", "history", "checks"):
            if k in st:
                ps[k] = st.pop(k)
        st["products"] = {LEGACY_ASIN: ps}
        for sub in st.get("subscribers", {}).values():
            sub["rem"] = {LEGACY_ASIN: sub.pop("rem_sent", 0)}
        st.setdefault("cycles", [])
        st["consecutive_blocked_cycles"] = ps.get("consecutive_blocked", 0)
        save_state(st)
        log.info("migrated legacy single-product state -> products[%s] (history=%d checks=%d)",
                 LEGACY_ASIN, len(ps.get("history", [])), len(ps.get("checks", [])))
        return st


# ---------------------------------------------------------------- allowlist
_member_cache = {}  # user_id -> (ts, bool)


def load_allowed_from_state(st):
    """Allowed-group ids learned from migrations persist in state.json (allowed_groups_migrated)."""
    if not RESTRICTED:
        return
    for g in st.get("allowed_groups_migrated", []):
        try:
            ALLOWED_GROUPS.add(int(g))
        except (TypeError, ValueError):
            pass


def is_allowed_group(cid):
    if not RESTRICTED:
        return True
    try:
        return int(cid) in ALLOWED_GROUPS
    except (TypeError, ValueError):
        return False


def chat_ok(chat):
    """Private chats pass (gated separately); groups/supergroups/channels must be allowlisted (restricted mode)."""
    if chat.get("type") == "private":
        return True
    return is_allowed_group(chat.get("id"))


def follow_migration(old, new):
    """If an allowed group migrates to a new id, allow the new id too (persisted)."""
    if not RESTRICTED:
        return
    if is_allowed_group(old) and not is_allowed_group(new):
        ALLOWED_GROUPS.add(int(new))
        with _lock:
            st = load_state()
            lst = st.setdefault("allowed_groups_migrated", [])
            if int(new) not in lst:
                lst.append(int(new))
            save_state(st)
        log.info("allowed group %s migrated -> %s; new id added to allowlist", old, new)


def leave_chat(chat, reason, call_api=True):
    cid = chat.get("id") if isinstance(chat, dict) else chat
    ctype = chat.get("type") if isinstance(chat, dict) else "?"
    title = (chat.get("title") or "") if isinstance(chat, dict) else ""
    ok = None
    if call_api and TOKEN:
        j = tg_call("leaveChat", chat_id=cid)
        ok = bool(j and j.get("ok"))
    unsubscribe(cid, f"not allowlisted: {reason}")
    log.warning("ALLOWLIST: left non-allowed chat %s (type=%s title=%r) reason=%s leaveChat_ok=%s",
                cid, ctype, title, reason, ok)
    return ok


def is_group_member(user_id):
    """True if user is a member of any allowed group (getChatMember, cached MEMBER_TTL s). Always True in open mode."""
    if not RESTRICTED:
        return True
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if OWNER_ID and uid == OWNER_ID:
        return True
    hit = _member_cache.get(uid)
    if hit and time.time() - hit[0] < MEMBER_TTL:
        return hit[1]
    member, definitive = False, False
    for g in sorted(ALLOWED_GROUPS):
        j = tg_call("getChatMember", chat_id=g, user_id=uid)
        if j and j.get("ok"):
            definitive = True
            r = j.get("result") or {}
            st_ = r.get("status")
            if st_ in ("member", "administrator", "creator") or (st_ == "restricted" and r.get("is_member")):
                member = True
                break
        elif j and j.get("error_code") == 400:
            definitive = True  # user not found / not a participant
    if definitive:
        _member_cache[uid] = (time.time(), member)
    log.info("membership check user %s -> %s%s", uid, member, "" if definitive else " (api error, not cached)")
    return member


def private_allowed(chat, uid):
    """Private chat gate: owner always; in restricted mode others only if members of an allowed group."""
    if chat.get("type") != "private":
        return True
    return is_owner(uid) or is_group_member(uid)


def startup_cleanup():
    """Unsubscribe (and try to leave) any non-allowlisted group in state; drop private non-members."""
    subs = dict(load_state().get("subscribers", {}))
    for k, s in subs.items():
        try:
            cid = int(k)
        except ValueError:
            continue
        if cid < 0 and not is_allowed_group(cid):
            leave_chat({"id": cid, "type": s.get("type", "group"), "title": s.get("title", "")}, "startup cleanup")
        elif RESTRICTED and cid > 0 and cid != OWNER_ID and TOKEN and not is_group_member(cid):
            unsubscribe(cid, "startup: private user not in allowed group")
    log.info("allowlist: %s", sorted(ALLOWED_GROUPS) if RESTRICTED else "open mode (ALLOWED_GROUPS empty)")


# ---------------------------------------------------------------- amazon
def classify(t):
    if "automated access" in t or len(t) < 50000:
        return "BLOCKED"
    if 'id="outOfStock"' in t or "Currently unavailable" in t:
        return "OUT"
    if 'id="add-to-cart-button"' in t or 'id="buy-now-button"' in t:
        return "IN"
    return "UNKNOWN"


AMZ_BASE = "https://www.amazon.in"
COOKIE_FILE = os.path.join(DATA, "amazon_cookies.json")
AMZ_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
}
AMZ_REFERER = AMZ_BASE + "/s?k=playstation+gift+card+4000"
_amz = {"s": None}
AMZ_STATS = {"fetch": 0, "robot": 0, "continue_ok": 0, "continue_fail": 0, "image_captcha": 0}


def _amz_session():
    """One persistent Chrome-impersonated session (consistent UA/TLS) with a cookie jar persisted to disk.
    A persistent session is far less likely to hit the robot page than fresh one-off requests.
    The jar only holds anonymous cookies Amazon sets for this session - never log in or import account cookies."""
    if _amz["s"] is None:
        s = requests.Session(impersonate="chrome")
        s.headers.update(AMZ_HEADERS)
        if PROXY:
            s.proxies = {"http": PROXY, "https": PROXY}
        try:
            with open(COOKIE_FILE, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    s.cookies.set(k, v, domain=".amazon.in")
            log.info("amazon session: loaded %d cookies from disk", len(s.cookies))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("amazon cookie jar unreadable (%s); starting clean", redact(e))
        _amz["s"] = s
    return _amz["s"]


def _amz_save_cookies():
    s = _amz["s"]
    if s is None:
        return
    try:
        jar = {}
        for c in s.cookies.jar:
            if "amazon" in (c.domain or ""):
                jar[c.name] = c.value
        tmp = COOKIE_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(jar, f)
        os.replace(tmp, COOKIE_FILE)
    except Exception as e:
        log.warning("could not save amazon cookies: %s", redact(e))


def _amz_reset():
    """Drop session + cookie jar (used when the session itself seems burnt)."""
    _amz["s"] = None
    try:
        os.remove(COOKIE_FILE)
    except FileNotFoundError:
        pass


def _auto_continue(s, t, url=URL):
    """Amazon's robot page is often only a 'Continue shopping' button (GET form to /errors_page/validateCaptcha,
    hidden amzn / amzn-r / field-keywords, no image). Submitting it sets fresh cookies. Never solves image captchas."""
    m = re.search(r'<form[^>]*action="([^"]*validateCaptcha[^"]*)"(.*?)</form>', t, re.S)
    if not m:
        return False
    body = m.group(1), m.group(2)
    if "captchacharacters" in t or "<img" in body[1]:
        AMZ_STATS["image_captcha"] += 1
        log.warning("amazon robot page is an IMAGE captcha variant - not auto-solvable")
        return False
    params = {n: html.unescape(v) for n, v in re.findall(r'name="([^"]+)" value="([^"]*)"', body[1])}
    action = body[0] if body[0].startswith("http") else AMZ_BASE + body[0]
    time.sleep(random.uniform(2, 5))
    s.get(action, params=params, timeout=30, headers={"Referer": url})
    return True


def fetch_once(asin=LEGACY_ASIN):
    url = purl(asin)
    s = _amz_session()
    AMZ_STATS["fetch"] += 1
    r = s.get(url, timeout=30, headers={"Referer": AMZ_REFERER})
    status = classify(r.text)
    if status == "BLOCKED" and "automated access" in r.text:
        AMZ_STATS["robot"] += 1
        if _auto_continue(s, r.text, url):
            time.sleep(random.uniform(4, 8))
            r = s.get(url, timeout=30, headers={"Referer": AMZ_REFERER})
            status = classify(r.text)
            if status == "BLOCKED":
                AMZ_STATS["continue_fail"] += 1
                log.info("robot page: auto-continue did NOT help (size=%d); resetting session", len(r.text))
                _amz_reset()
            else:
                AMZ_STATS["continue_ok"] += 1
                log.info("robot page: auto-continue OK -> %s", status)
        else:
            _amz_reset()
    _amz_save_cookies()
    return r.status_code, len(r.text), status


def fetch_safe(asin):
    try:
        return fetch_once(asin)
    except Exception as e:
        log.warning("fetch %s error: %s", asin, redact(e))
        return ("ERROR", 0, "UNKNOWN")


def check_all(prods=None):
    """One cycle: each enabled product once (staggered), then retry BLOCKED ones while within budget.
    Returns {asin: ((code, size, status), attempts)}."""
    prods = prods if prods is not None else enabled_products()
    t0 = time.time()
    res = {}
    for i, p in enumerate(prods):
        if i:
            time.sleep(random.uniform(*STAGGER))
        res[p["asin"]] = (fetch_safe(p["asin"]), 1)
    for attempt in range(2, ATTEMPTS + 1):
        todo = [a for a, (r, _) in res.items() if r[2] == "BLOCKED"]
        if not todo or time.time() - t0 > CYCLE_RETRY_BUDGET:
            break
        for a in todo:
            if time.time() - t0 > CYCLE_RETRY_BUDGET:
                break
            time.sleep(ATTEMPT_GAP + random.uniform(0, 10))
            res[a] = (fetch_safe(a), attempt)
    log.info("cycle: %d products in %ds", len(prods), int(time.time() - t0))
    return res


# ---------------------------------------------------------------- telegram api
QUIET_ERRORS = ("message is not modified", "query is too old", "query id is invalid")


def tg_call(method, **params):
    if not TOKEN:
        return None
    try:
        r = requests.post(f"{TG}/{method}", json=params, timeout=params.get("timeout", 0) + 20)
        j = r.json()
        if not j.get("ok"):
            desc = str(j.get("description", ""))
            if not any(q in desc for q in QUIET_ERRORS):
                log.error("Telegram %s failed: %s", method, redact(desc))
        return j
    except Exception as e:
        log.error("Telegram %s exception: %s", method, redact(e))
        return None


def _strip_copy(markup):
    """Fallback: replace copy_text buttons with a callback that sends the promo message."""
    if not markup or "inline_keyboard" not in markup:
        return markup
    return {"inline_keyboard": [[({"text": b["text"], "callback_data": "m:promo"} if "copy_text" in b else b)
                                 for b in row] for row in markup["inline_keyboard"]]}


def _has_copy(markup):
    return bool(markup) and any("copy_text" in b for row in markup.get("inline_keyboard", []) for b in row)


def _copy_rejected(j):
    d = str((j or {}).get("description", "")).lower()
    return bool(j) and not j.get("ok") and j.get("error_code") == 400 and ("button" in d or "copy" in d
                                                                           or "reply markup" in d or "keyboard" in d)


def send_to(cid, text, markup=None, html_mode=False, preview=False, _retry=True):
    if not TOKEN:
        log.info("(checker-only, no token) would send to %s: %s", cid, text.replace("\n", " | "))
        return None
    p = {"chat_id": cid, "text": text, "disable_web_page_preview": not preview}
    if markup:
        p["reply_markup"] = markup
    if html_mode:
        p["parse_mode"] = "HTML"
    if _has_copy(markup) and not _copy_ok["v"]:
        p["reply_markup"] = markup = _strip_copy(markup)
    j = tg_call("sendMessage", **p)
    if _has_copy(markup) and _copy_rejected(j):
        log.warning("copy_text button rejected (%s); falling back to promo message button", j.get("description"))
        _copy_ok["v"] = False
        p["reply_markup"] = _strip_copy(markup)
        j = tg_call("sendMessage", **p)
    if j and not j.get("ok"):
        new = (j.get("parameters") or {}).get("migrate_to_chat_id")
        if new and _retry:
            follow_migration(cid, new)
            migrate_chat(cid, new)
            return send_to(new, text, markup, html_mode, preview, _retry=False)
        if j.get("error_code") == 403:
            unsubscribe(cid, j.get("description"))
    return j


def edit(cid, mid, text, markup=None):
    p = {"chat_id": cid, "message_id": mid, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = _strip_copy(markup) if (_has_copy(markup) and not _copy_ok["v"]) else markup
    j = tg_call("editMessageText", **p)
    if _has_copy(markup) and _copy_ok["v"] and _copy_rejected(j):
        log.warning("copy_text button rejected on edit (%s); falling back", j.get("description"))
        _copy_ok["v"] = False
        p["reply_markup"] = _strip_copy(markup)
        j = tg_call("editMessageText", **p)
    return j


def answer(cq_id, text=None, alert=False):
    p = {"callback_query_id": cq_id}
    if text:
        p["text"] = text
        p["show_alert"] = alert
    return tg_call("answerCallbackQuery", **p)


def broadcast(text, markup=None, preview=False, kind="msg"):
    subs = load_state().get("subscribers", {})
    ok = total = 0
    for cid, s in list(subs.items()):
        if s.get("muted"):
            continue
        icid = int(cid)
        if icid < 0 and not is_allowed_group(icid):
            leave_chat({"id": icid, "type": s.get("type"), "title": s.get("title", "")}, "broadcast guard")
            continue
        if RESTRICTED and icid > 0 and icid != OWNER_ID and not is_group_member(icid):
            unsubscribe(icid, "private user no longer in allowed group")
            continue
        total += 1
        j = send_to(int(cid), text, markup, preview=preview)
        if j and j.get("ok"):
            ok += 1
        time.sleep(0.05)
    log.info("broadcast %s: delivered %d/%d (muted/skipped %d)", kind, ok, total, len(subs) - total)
    return ok, total


def is_owner(user_id):
    """ONLY the configured OWNER_ID may change settings - anywhere. No group-admin exception."""
    try:
        return bool(OWNER_ID) and int(user_id) == OWNER_ID
    except (TypeError, ValueError):
        return False


def owner_panel(cid):
    """Settings/mute controls are shown only in the owner's private chat."""
    try:
        return bool(OWNER_ID) and int(cid) == OWNER_ID
    except (TypeError, ValueError):
        return False


COMMANDS = [("menu", "📋 תפריט ראשי"), ("status", "🛒 מה המצב עכשיו?"), ("check", "🔄 בדוק עכשיו"),
            ("history", "📜 היסטוריית מלאי"), ("share", "📢 הודעת שיתוף לקבוצה"), ("help", "ℹ️ איך זה עובד")]
if not GROUP_INVITE:
    COMMANDS = [c for c in COMMANDS if c[0] != "share"]
OWNER_COMMANDS = COMMANDS + [("settings", "⚙️ הגדרות (מנהל)"), ("mute", "🔕 השתק התראות בצ'אט הזה"),
                             ("unmute", "🔔 הפעל התראות בצ'אט הזה")]


def set_commands():
    public = [{"command": c, "description": d} for c, d in COMMANDS]
    owner = [{"command": c, "description": d} for c, d in OWNER_COMMANDS]
    scopes = [({"type": "default"}, public), ({"type": "all_group_chats"}, public)]
    if OWNER_ID:
        scopes.append(({"type": "chat", "chat_id": OWNER_ID}, owner))
    for scope, cmds in scopes:
        j = tg_call("setMyCommands", commands=cmds, scope=scope)
        log.info("setMyCommands scope=%s ok=%s", scope["type"], bool(j and j.get("ok")))


# ---------------------------------------------------------------- texts & keyboards
STATUS_HE = {"IN": "✅ במלאי!", "OUT": "❌ לא במלאי", None: "❓ עדיין לא ידוע"}
RESULT_HE = {"IN": "במלאי", "OUT": "לא במלאי", "BLOCKED": "🚫 אמזון חסמה את הבדיקה",
             "UNKNOWN": "❓ לא ברורה", None: "—"}


def btn(text, data):
    return {"text": text, "callback_data": data}


BACK = [btn("⬅️ חזרה", "m:main")]
AMAZON_BTN = {"text": "🛒 לעמוד המוצר באמזון", "url": URL}


def kb_buy(p):
    return {"inline_keyboard": [[{"text": f"🛒 לקנייה באמזון ({p['amount']} ₹)", "url": purl(p["asin"])}]]}


def alert_text(p):
    return (f"🚨🚨🚨 במלאי עכשיו!!!!🚨🚨🚨\n💵 {p['amount']} רופי\n🛒 {purl(p['asin'])}\n"
            f"שעה: {now_il().strftime('%H:%M %d/%m/%Y')}")


def out_text(p, dur_min):
    t = f"❌ אזל מהמלאי - {p['amount']} רופי - {now_il().strftime('%H:%M %d/%m/%Y')}"
    if dur_min is not None:
        t += f"\n⏱️ נמשך {dur_min} דקות"
    return t


def reminder_text(p, n):
    return f"🔁 תזכורת #{n}: {p['amount']} רופי עדיין במלאי!"


# ---- combined messages (2+ products in the same cycle / reminder tick)
def _by_amount(ps):
    return sorted(ps, key=lambda p: p["amount"])


def kb_buy_multi(ps):
    btns = [{"text": f"🛒 {p['amount']}", "url": purl(p["asin"])} for p in _by_amount(ps)]
    return {"inline_keyboard": [btns[i:i + 3] for i in range(0, len(btns), 3)]}


def alert_text_multi(ps):
    body = "".join(f"💵 {p['amount']} רופי\n🛒 {purl(p['asin'])}\n" for p in _by_amount(ps))
    return f"🚨🚨🚨 במלאי עכשיו!!!!🚨🚨🚨\n{body}שעה: {now_il().strftime('%H:%M %d/%m/%Y')}"


def out_text_multi(items):
    """items: [(p, dur_min)]"""
    lines = [f"❌ אזל מהמלאי - {now_il().strftime('%H:%M %d/%m/%Y')}"]
    for p, dur in sorted(items, key=lambda x: x[0]["amount"]):
        lines.append(f"• {p['amount']} רופי" + (f" - ⏱️ נמשך {dur} דקות" if dur is not None else ""))
    return "\n".join(lines)


def reminder_text_multi(ps, n):
    amts = ", ".join(str(p["amount"]) for p in _by_amount(ps))
    return f"🔁 תזכורת #{n}: עדיין במלאי - {amts} רופי!"


def restock_message(ps):
    """-> (text, markup, preview). A single product uses the single-product format."""
    if len(ps) == 1:
        return alert_text(ps[0]), kb_buy(ps[0]), True
    return alert_text_multi(ps), kb_buy_multi(ps), False


def out_message(items):
    if len(items) == 1:
        return out_text(*items[0]), None
    return out_text_multi(items), None


def reminder_message(ps, n):
    if len(ps) == 1:
        return reminder_text(ps[0], n), kb_buy(ps[0])
    return reminder_text_multi(ps, n), kb_buy_multi(ps)


def view_main(cid):
    st = load_state()
    s = st.get("subscribers", {}).get(str(cid)) or {}
    muted = s.get("muted")
    ens = enabled_products(st)
    ins = [p for p in ens if pstate(st, p["asin"]).get("last_status") == "IN"]
    summary = (f"✅ במלאי: {', '.join(str(p['amount']) for p in ins)} ₹" if ins
               else f"❌ אין מלאי ({len(ens)} סכומים במעקב)")
    lines = ["🎮 <b>התראות מלאי – כרטיסי PSN</b>",
             "כרטיסי מתנה PlayStation Store באמזון הודו", "",
             f"{summary} · נבדק {fmt_il(st.get('last_check'), date=False) if st.get('last_check') else '—'}"]
    if muted:
        lines.append("🔕 ההתראות בצ'אט הזה מושתקות")
    lines += ["", "בחרו מה לעשות 👇"]
    if owner_panel(cid):
        rows = [[btn("🛒 מה המצב עכשיו?", "m:status"), btn("🔄 בדוק עכשיו", "m:check")],
                [btn("📜 היסטוריה", "m:hist"), btn("🔔 הפעל התראות" if muted else "🔕 השתק התראות", "m:mute")],
                [btn("⚙️ הגדרות", "m:settings"), btn("ℹ️ איך זה עובד", "m:help")]]
    else:  # groups / other users: read-only menu, no mute toggle
        rows = [[btn("🛒 מה המצב עכשיו?", "m:status"), btn("🔄 בדוק עכשיו", "m:check")],
                [btn("📜 היסטוריה", "m:hist"), btn("ℹ️ איך זה עובד", "m:help")],
                [btn("⚙️ הגדרות (צפייה)", "m:settings")]]
    rows += [[{"text": f"🛒 קנה {p['amount']} ₹", "url": purl(p["asin"])}] for p in ins[:4]]
    rows += share_rows()
    return "\n".join(lines), {"inline_keyboard": rows}


def share_rows():
    if not share_enabled():
        return []
    return [[{"text": "📢 שתף את הקבוצה", "url": share_group_url()}],
            [{"text": "📋 העתק הודעת שיתוף", "copy_text": {"text": promo_full()[:256]}},
             btn("📝 הודעת שיתוף", "m:promo")]]


def view_promo():
    return html.escape(promo_full()), {"inline_keyboard": [[{"text": "📢 שתף את הקבוצה", "url": share_group_url()}], BACK]}


def product_line(st, p, show_since=True):
    ps = pstate(st, p["asin"])
    last = ps.get("last_status")
    if not is_enabled(st, p["asin"]):
        return f"⏸️ <b>{p['amount']} ₹</b> – מעקב כבוי"
    icon = {"IN": "✅", "OUT": "❌"}.get(last, "❓")
    txt = {"IN": "במלאי!", "OUT": "לא במלאי"}.get(last, "עדיין לא ידוע")
    extra = ""
    if last == "IN" and ps.get("in_since") and show_since:
        extra = f" · מאז {fmt_il(ps['in_since'], date=False)} ({minutes_between(ps['in_since'])} דק׳)"
    if ps.get("last_result") in ("BLOCKED", "UNKNOWN"):
        extra += " · <i>(אמזון חסמה את הבדיקה האחרונה)</i>"
    return f"{icon} <b>{p['amount']} ₹</b> – {txt}{extra}"


def buy_rows(st):
    return [[{"text": f"🛒 קנה {p['amount']} ₹", "url": purl(p["asin"])}] for p in enabled_products(st)
            if pstate(st, p["asin"]).get("last_status") == "IN"]


def view_status():
    st = load_state()
    lines = ["🛒 <b>מה המצב עכשיו?</b>", f"📦 {html.escape(PRODUCT_FAMILY)}", ""]
    lines += [product_line(st, p) for p in products()]
    lines += ["", f"🕒 בדיקה אחרונה: {fmt_il(st.get('last_check'))}"]
    if st.get("next_check"):
        lines.append(f"⏭️ בדיקה הבאה: בערך {fmt_il(st['next_check'], date=False)}")
    lines.append("<i>(שעון ישראל)</i>" if TZ_NAME == "Asia/Jerusalem" else f"<i>({html.escape(TZ_NAME)})</i>")
    return "\n".join(lines), {"inline_keyboard": buy_rows(st) + [BACK]}


def all_checks(st):
    t = time.time()
    return [c for p in products() for c in pstate(st, p["asin"]).get("checks", []) if t - c[0] < 86400]


def check_stats(st=None):
    """Return total individual requests, real answers, and failed requests in 24h."""
    st = st or load_state()
    total = successes = 0
    for c in all_checks(st):
        try:
            attempts = max(1, int(c[2]))
        except (IndexError, TypeError, ValueError):
            attempts = 1
        total += attempts
        # A check stops after its first real IN/OUT answer; earlier attempts were failures.
        if len(c) > 1 and c[1] in ("IN", "OUT"):
            successes += 1
    return total, successes, total - successes


def view_history():
    st = load_state()
    hist = []
    for p in products():
        for h in pstate(st, p["asin"]).get("history", []):
            hist.append((parse_iso(h.get("ts", "")) or now_il(), p, h))
    hist = sorted(hist, key=lambda x: x[0])[-12:][::-1]
    lines = ["📜 <b>היסטוריה</b> – השינויים האחרונים (כל הסכומים)", ""]
    if not hist:
        lines.append("עדיין אין שינויים רשומים.")
    for _, p, h in hist:
        if h.get("status") == "IN":
            lines.append(f"✅ {p['amount']} ₹ חזר למלאי · {fmt_il(h['ts'])}")
        else:
            extra = f" (היה במלאי {h['dur_min']} דק׳)" if h.get("dur_min") is not None else ""
            lines.append(f"❌ {p['amount']} ₹ לא במלאי · {fmt_il(h['ts'])}{extra}")
    total, successes, failures = check_stats(st)
    lines.append("")
    if total:
        success_pct = round(100 * successes / total)
        failure_pct = 100 - success_pct
        lines.append(f"📊 ב-24 השעות האחרונות: {total} ניסיונות · ✅ {successes} עברו ({success_pct}%) · "
                     f"🚫 {failures} נכשלו ({failure_pct}%)")
    else:
        lines.append("📊 ב-24 השעות האחרונות: אין עדיין ניסיונות")
    return "\n".join(lines), {"inline_keyboard": [BACK]}


def reminders_label(v):
    return "כבוי" if not v else f"כל {v} דקות (עד {MAX_REMINDERS})"


def chat_label(cid, s):
    t = (s.get("title") or "").strip()
    if OWNER_ID and int(cid) == OWNER_ID:
        t = "פרטי (אתה)"
    return (t or str(cid))[:22]


def next_rem(v):
    i = REMINDER_CHOICES.index(v) if v in REMINDER_CHOICES else 0
    return REMINDER_CHOICES[(i + 1) % len(REMINDER_CHOICES)]


def view_settings(cid, admin=None):
    admin = owner_panel(cid) if admin is None else admin
    st = load_state()
    subs = st.get("subscribers", {})
    s = subs.get(str(cid)) or {}
    rem = s.get("reminders", DEFAULT_REMINDERS)
    lines = ["⚙️ <b>הגדרות</b>", "",
             f"📦 מוצר: {html.escape(PRODUCT_FAMILY)}",
             "🎯 סכומים במעקב: " + (" · ".join(("✅" if is_enabled(st, p["asin"]) else "⏸️") + f"{p['amount']}"
                                               for p in products()) or "—"),
             f"⏱️ תדירות בדיקה: כל {POLL // 60} דקות (±{JITTER} שניות), כל הסכומים ברצף",
             f"🔁 ניסיונות בכל בדיקה: עד {ATTEMPTS} (כשאמזון חוסמת)",
             f"👥 צ'אטים מנויים: {len(subs)}",
             f"🔔 התראות בצ'אט הזה: {'מושתקות 🔕' if s.get('muted') else 'פעילות ✅'}",
             f"🔁 תזכורות בזמן שיש מלאי: {reminders_label(rem)}"]
    rows = []
    if admin:
        lines += ["", "בחרו תדירות תזכורות לצ'אט הזה:"]
        opts = [btn(("✅ " if rem == v else "") + ("כבוי" if not v else f"כל {v} דק׳"), f"set:rem:{v}")
                for v in REMINDER_CHOICES]
        rows += [opts[:2], opts[2:]]
        lines += ["", "🎯 הפעלה/כיבוי מעקב לפי סכום:"]
        tg = [btn(("✅ " if is_enabled(st, p["asin"]) else "⏸️ ") + str(p["amount"]), f"p:tog:{p['asin']}")
              for p in products()]
        rows += [tg[i:i + 3] for i in range(0, len(tg), 3)]
        others = [(k, v) for k, v in subs.items() if str(k) != str(cid)]
        if others:
            lines += ["", "👥 <b>צ'אטים אחרים</b> (השתקה · תזכורות):"]
            for k, o in others[:10]:
                lines.append(f"• {html.escape(chat_label(k, o))}: "
                             f"{'🔕 מושתק' if o.get('muted') else '🔔 פעיל'} · "
                             f"{reminders_label(o.get('reminders', DEFAULT_REMINDERS))}")
                orem = o.get("reminders", DEFAULT_REMINDERS)
                rows.append([btn(("🔕 " if o.get("muted") else "🔔 ") + chat_label(k, o), f"o:mute:{k}"),
                             btn("🔁 " + ("כבוי" if not orem else f"{orem} דק׳"), f"o:rem:{k}")])
    else:
        lines += ["", "🔒 <i>ההגדרות לצפייה בלבד – רק המנהל יכול לשנות אותן.</i>"]
    rows.append(BACK)
    return "\n".join(lines), {"inline_keyboard": rows}


def view_help():
    text = ("ℹ️ <b>איך זה עובד</b>\n\n"
            f"• כל ~{POLL // 60} דקות הבוט בודק את כרטיסי ה-PSN באמזון הודו ("
            + ", ".join(str(p["amount"]) for p in enabled_products()) + " רופי).\n"
            "• ברגע שסכום כלשהו חוזר למלאי – נשלחת התראה 🚨 לכל הצ'אטים, עם כפתור קנייה.\n"
            "• כשהוא אוזל שוב – הודעת ❌ עם כמה זמן הוא היה במלאי.\n"
            "• בזמן שיש מלאי נשלחות תזכורות 🔁 (לפי ההגדרות).\n"
            f"• אמזון חוסמת לפעמים את הבדיקה – הבוט מנסה שוב עד {ATTEMPTS} פעמים, "
            "אז התראה יכולה להתאחר בכמה דקות.\n"
            "• 🔒 את ההגדרות (השתקה, תזכורות, סכומים במעקב) רק המנהל יכול לשנות.\n"
            + ("• 📢 רוצים לצרף חברים? כפתור \"שתף את הקבוצה\" בתפריט שולח להם הזמנה לקבוצה.\n"
               if share_enabled() else "") +
            "• המלאי חוזר באקראיות ונגמר מהר – כשיש התראה, רוצו לקנות! 🏃\n\n"
            "<b>פקודות:</b>\n" + "\n".join(f"/{c} – {d}" for c, d in COMMANDS))
    return text, {"inline_keyboard": [BACK]}


def welcome_text(group):
    if group:
        return (f"👋 היי לכולם!\nמעכשיו אתריע כאן ברגע שכרטיס PlayStation ({tracked_amounts_text()} רופי) "
                "חוזר למלאי באמזון הודו 🎮\n📌 כדאי להצמיד את ההודעה הזו – הכפתורים למטה עובדים לכולם 👇")
    return "👋 ברוכים הבאים! מחובר ✅ אתריע כאן כשכרטיס PSN חוזר למלאי.\nהתפריט למטה 👇"


def check_result_view(results):
    """results: {asin: (status, attempts)}"""
    st = load_state()
    lines = [f"🔄 <b>תוצאת בדיקה</b> · {now_il().strftime('%H:%M:%S')}", ""]
    for p in enabled_products(st):
        r, _att = results.get(p["asin"], ("UNKNOWN", 0))
        if r == "IN":
            lines.append(f"✅ <b>{p['amount']} ₹</b> – במלאי עכשיו! רוצו 🏃")
        elif r == "OUT":
            lines.append(f"❌ <b>{p['amount']} ₹</b> – לא במלאי")
        else:
            known = STATUS_HE.get(pstate(st, p["asin"]).get("last_status"), "—")
            lines.append(f"🚫 <b>{p['amount']} ₹</b> – אמזון חסמה את הבדיקה · ידוע: {known}")
    if not results:
        lines.append("אין סכומים במעקב.")
    return "\n".join(lines), {"inline_keyboard": buy_rows(st) + [BACK]}


# ---------------------------------------------------------------- core logic
def process_product(p, code, size, status, attempts, source="poll"):
    asin = p["asin"]
    ts = now_iso()
    t = time.time()
    events = []
    with _lock:
        st = load_state()
        ps = pstate(st, asin)
        first_run = "last_status" not in ps
        prev = ps.get("last_status")
        ps["last_check"] = ts
        ps["last_result"] = status
        checks = [c for c in ps.get("checks", []) if t - c[0] < 86400]
        checks.append([int(t), status, attempts])
        ps["checks"] = checks
        ps["consecutive_blocked"] = ps.get("consecutive_blocked", 0) + 1 if status == "BLOCKED" else 0
        if status in ("IN", "OUT"):
            if status != prev:
                ps["last_change"] = ts
                entry = {"ts": ts, "status": status}
                if status == "OUT" and prev == "IN":
                    entry["dur_min"] = minutes_between(ps.get("in_since"), ts)
                hist = ps.setdefault("history", [])
                hist.append(entry)
                del hist[:-HISTORY_MAX]
            if status == "IN" and prev != "IN":
                events.append(("IN", None))
                ps["in_since"] = ts
                for s in st.get("subscribers", {}).values():
                    s.setdefault("rem", {})[asin] = 0
            elif status == "OUT" and prev == "IN":
                events.append(("OUT", minutes_between(ps.get("in_since"), ts)))
                ps["in_since"] = None
            ps["last_status"] = status
        save_state(st)
    log.info("check(%s) %s [%s]: http=%s size=%s result=%s attempts=%d last_status=%s%s",
             source, p["amount"], asin, code, size, status, attempts, ps.get("last_status"),
             " (first run)" if first_run else "")
    for kind, dur in events:
        if kind == "IN":
            log.warning("RESTOCK detected %s [%s] (prev=%s)", p["amount"], asin, prev)
        else:
            log.warning("OUT of stock again %s after %s min", p["amount"], dur)
    return status, events


def notify_transitions(ins, outs):
    """ins: [p]; outs: [(p, dur)] collected over ONE cycle -> one message per kind per chat."""
    if ins:
        text, kb, preview = restock_message(ins)
        tag = "+".join(str(p["amount"]) for p in _by_amount(ins))
        ok, total = broadcast(text, kb, preview=preview, kind=f"RESTOCK {tag}")
        if total and not ok:
            log.error("restock alert %s was NOT delivered to any chat", tag)
    if outs:
        text, kb = out_message(outs)
        tag = "+".join(str(p["amount"]) for p, _ in sorted(outs, key=lambda x: x[0]["amount"]))
        broadcast(text, kb, kind=f"OUT {tag}")


def process_cycle(results, source="poll"):
    """results: {asin: ((code,size,status), attempts)} -> {asin: (status, attempts)}"""
    out = {}
    ins, outs = [], []
    for asin, ((code, size, status), attempts) in results.items():
        p = product(asin)
        if p:
            status, events = process_product(p, code, size, status, attempts, source)
            out[asin] = (status, attempts)
            for kind, dur in events:
                (ins.append(p) if kind == "IN" else outs.append((p, dur)))
    try:
        notify_transitions(ins, outs)
    except Exception as e:
        log.error("notify error: %s", redact(e))
    with _lock:
        st = load_state()
        st["last_check"] = now_iso()
        all_blocked = bool(out) and all(v[0] == "BLOCKED" for v in out.values())
        st["consecutive_blocked_cycles"] = st.get("consecutive_blocked_cycles", 0) + 1 if all_blocked else 0
        blocked = st["consecutive_blocked_cycles"]
        save_state(st)
    nb = sum(1 for v in out.values() if v[0] == "BLOCKED")
    log.info("cycle(%s) summary: %s | blocked %d/%d", source,
             " ".join(f"{product(a)['amount']}={v[0]}" for a, v in out.items()), nb, len(out))
    if blocked >= 3:
        log.warning("Amazon BLOCKED all products %d cycles in a row", blocked)
    if blocked == 6 and OWNER_ID:
        send_to(OWNER_ID, f"⚠️ אמזון חוסמת את כל הבדיקות כבר {blocked} סבבים ברצף. הבוט ממשיך לנסות.")
    return out


def run_check(source):
    with _check_lock:
        return process_cycle(check_all(), source)


def reserve_manual():
    with _manual_lock:
        left = MANUAL_COOLDOWN - (time.time() - _manual["last"])
        if left > 0:
            return int(left) + 1
        _manual["last"] = time.time()
        return 0


def poll_loop():
    while True:
        t0 = time.time()
        try:
            run_check("poll")
        except Exception as e:
            log.error("poll error: %s", redact(e))
        # keep a ~5 min start-to-start cycle regardless of how long the staggered fetches took
        delay = max(30, POLL + random.uniform(-JITTER, JITTER) - (time.time() - t0))
        with _lock:
            st = load_state()
            st["next_check"] = datetime.fromtimestamp(time.time() + delay, TZ).isoformat(timespec="seconds")
            save_state(st)
        time.sleep(delay)


def reminder_loop():
    while True:
        time.sleep(30)
        try:
            to_send = []
            with _lock:
                st = load_state()
                live = []  # (since, p) for every enabled product currently IN
                for p in enabled_products(st):
                    ps = pstate(st, p["asin"])
                    since = parse_iso(ps.get("in_since") or "")
                    if ps.get("last_status") == "IN" and since:
                        live.append((since, p))
                live.sort(key=lambda x: x[0])
                for cid, s in st.get("subscribers", {}).items():
                    iv = s.get("reminders", DEFAULT_REMINDERS)
                    if not live or not iv or s.get("muted"):
                        continue
                    rem = s.setdefault("rem", {})
                    dues = {p["asin"]: min(MAX_REMINDERS, int((now_il() - since).total_seconds() // (iv * 60)))
                            for since, p in live}
                    anchor = live[0][1]["asin"]  # earliest restock window drives timing + numbering
                    if dues[anchor] > rem.get(anchor, 0):
                        for a, d in dues.items():  # one message covers all still-in-stock amounts
                            rem[a] = max(rem.get(a, 0), d)
                        to_send.append((cid, [p for _, p in live], dues[anchor]))
                if to_send:
                    save_state(st)
            for cid, ps_, n in to_send:
                log.info("reminder #%d %s -> chat %s", n, "+".join(str(p["amount"]) for p in _by_amount(ps_)), cid)
                text, kb = reminder_message(ps_, n)
                send_to(int(cid), text, kb)
        except Exception as e:
            log.error("reminder error: %s", redact(e))


# ---------------------------------------------------------------- handlers
def do_manual_check(cid, mid):
    try:
        text, kb = check_result_view(run_check("manual"))
    except Exception as e:
        log.error("manual check error: %s", redact(e))
        text, kb = "⚠️ הבדיקה נכשלה, נסו שוב בעוד דקה.", {"inline_keyboard": [BACK]}
    edit(cid, mid, text, kb)


def toggle_mute(cid, want=None):
    s = get_sub(cid) or {}
    muted = (not s.get("muted")) if want is None else want
    update_sub(cid, muted=muted)
    log.info("chat %s muted=%s", cid, muted)
    return muted


def set_reminders(cid, v):
    s = get_sub(cid) or {}
    st = load_state()
    rem = dict(s.get("rem", {}))
    if v:
        for p in products():  # don't fire a burst of catch-up reminders after a change mid-window
            ps = pstate(st, p["asin"])
            if ps.get("last_status") == "IN" and ps.get("in_since"):
                rem[p["asin"]] = max(rem.get(p["asin"], 0), min(MAX_REMINDERS, int(
                    (now_il() - parse_iso(ps["in_since"])).total_seconds() // (v * 60))))
    update_sub(cid, reminders=v, rem=rem)


def owner_chat_setting(cq_id, cid, mid, data):
    """Owner's private panel: change mute/reminders of another subscribed chat (e.g. the group)."""
    parts = data.split(":")
    target = parts[2] if len(parts) == 3 else ""
    if parts[1] not in ("mute", "rem") or target not in load_state().get("subscribers", {}):
        answer(cq_id, "הצ'אט לא נמצא")
        edit(cid, mid, *view_settings(cid))
        return
    if parts[1] == "mute":
        muted = toggle_mute(target)
        answer(cq_id, "🔕 הושתק" if muted else "🔔 הופעל")
    else:
        v = next_rem((get_sub(target) or {}).get("reminders", DEFAULT_REMINDERS))
        set_reminders(target, v)
        log.info("chat %s reminders=%s (by owner panel)", target, v)
        answer(cq_id, f"✅ תזכורות: {reminders_label(v)}")
    edit(cid, mid, *view_settings(cid))


def handle_callback(cq):
    cq_id = cq["id"]
    data = cq.get("data", "")
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    cid, mid = chat.get("id"), msg.get("message_id")
    uid = cq.get("from", {}).get("id")
    if not cid or not mid:
        answer(cq_id)
        return
    if not chat_ok(chat):
        answer(cq_id)
        leave_chat(chat, "callback_query")
        return
    if not private_allowed(chat, uid):
        log.info("private bot: denied callback %s from user %s", data, uid)
        answer(cq_id, PRIVATE_TEXT, alert=True)
        return
    subscribe(chat)
    group = chat.get("type") != "private"
    if data == "m:main":
        answer(cq_id)
        edit(cid, mid, *view_main(cid))
    elif data == "m:status":
        answer(cq_id)
        edit(cid, mid, *view_status())
    elif data == "m:hist":
        answer(cq_id)
        edit(cid, mid, *view_history())
    elif data == "m:help":
        answer(cq_id)
        edit(cid, mid, *view_help())
    elif data == "m:promo":
        answer(cq_id)
        if not share_enabled():
            edit(cid, mid, *view_main(cid))
        elif group:  # don't let members spam the group: show it in place
            edit(cid, mid, *view_promo())
        else:      # private: a standalone message that is easy to forward/copy
            send_to(cid, promo_full(), {"inline_keyboard": [[{"text": "📢 שתף את הקבוצה", "url": share_group_url()}]]},
                    preview=True)
    elif data == "m:settings":
        answer(cq_id)
        edit(cid, mid, *view_settings(cid, owner_panel(cid) and is_owner(uid)))
    elif data == "m:check":
        left = reserve_manual()
        if left:
            answer(cq_id, f"⏳ אפשר לבדוק פעם בדקה – נסו שוב בעוד {left} שניות")
            return
        answer(cq_id, "בודק...")
        edit(cid, mid, "⏳ <b>בודק עכשיו באמזון...</b>\nזה יכול לקחת עד דקה אם אמזון חוסמת.",
             {"inline_keyboard": [BACK]})
        threading.Thread(target=do_manual_check, args=(cid, mid), name="manual-check", daemon=True).start()
    elif data == "m:mute":
        if not is_owner(uid):
            log.info("denied mute button: user %s chat %s", uid, cid)
            answer(cq_id, LOCKED_TOAST)
            return
        muted = toggle_mute(cid)
        answer(cq_id, "🔕 ההתראות הושתקו בצ'אט הזה" if muted else "🔔 ההתראות הופעלו בצ'אט הזה")
        edit(cid, mid, *view_main(cid))
    elif data.startswith("p:tog:"):
        if not (is_owner(uid) and owner_panel(cid)):
            log.info("denied product toggle %s: user %s chat %s", data, uid, cid)
            answer(cq_id, LOCKED_TOAST)
            return
        asin = data.split(":", 2)[2]
        p = product(asin)
        if not p:
            answer(cq_id, "הסכום לא נמצא")
            return
        with _lock:
            st = load_state()
            new = not is_enabled(st, asin)
            st.setdefault("product_enabled", {})[asin] = new
            save_state(st)
        log.info("product %s [%s] tracking enabled=%s (by owner)", p["amount"], asin, new)
        answer(cq_id, f"{'✅ מעקב הופעל' if new else '⏸️ מעקב כובה'}: {p['amount']} ₹")
        edit(cid, mid, *view_settings(cid))
    elif data.startswith("set:") or data.startswith("o:"):
        if not is_owner(uid):
            log.info("denied settings button %s: user %s chat %s", data, uid, cid)
            answer(cq_id, LOCKED_TOAST)
            return
        if data.startswith("o:"):
            owner_chat_setting(cq_id, cid, mid, data)
            return
        try:
            v = int(data.split(":")[2])
        except ValueError:
            answer(cq_id)
            return
        if v not in REMINDER_CHOICES:
            answer(cq_id)
            return
        set_reminders(cid, v)
        log.info("chat %s reminders=%s (by user %s)", cid, v, uid)
        answer(cq_id, f"✅ תזכורות: {reminders_label(v)}")
        edit(cid, mid, *view_settings(cid))
    else:
        answer(cq_id)


def send_view(cid, view):
    text, kb = view
    return send_to(cid, text, kb, html_mode=True)


def handle_message(m):
    chat = m.get("chat", {})
    cid = chat.get("id")
    if m.get("migrate_to_chat_id"):
        follow_migration(cid, m["migrate_to_chat_id"])
        migrate_chat(cid, m["migrate_to_chat_id"])
        return
    if m.get("migrate_from_chat_id"):
        follow_migration(m["migrate_from_chat_id"], cid)
        migrate_chat(m["migrate_from_chat_id"], cid)
        return
    if not chat_ok(chat):
        leave_chat(chat, "message")
        return
    text = (m.get("text") or "").strip()
    if not text.startswith("/"):
        return
    head = text.split()[0]
    cmd, _, at = head.partition("@")
    if at and BOT["username"] and at.lower() != BOT["username"].lower():
        return
    cmd = cmd.lower()
    group = chat.get("type") != "private"
    uid = m.get("from", {}).get("id")
    if not private_allowed(chat, uid):
        log.info("private bot: denied %s from user %s (not in allowed group)", cmd, uid)
        send_to(cid, PRIVATE_TEXT)
        return
    if cmd == "/start":
        subscribe(chat)
        log.info("/start from chat_id=%s (type=%s user=%s)", cid, chat.get("type"),
                 m.get("from", {}).get("username"))
        print(f"CHAT_ID={cid}", flush=True)
        s = get_sub(cid) or {}
        if group and time.time() - s.get("welcomed", 0) < 60:
            return  # welcome already posted by my_chat_member a moment ago
        update_sub(cid, welcomed=time.time())
        send_view(cid, (welcome_text(group) + "\n\n" + view_main(cid)[0], view_main(cid)[1]))
        return
    if cmd == "/share":
        if not share_enabled():
            return
        subscribe(chat)
        send_to(cid, promo_full(), {"inline_keyboard": [[{"text": "📢 שתף את הקבוצה", "url": share_group_url()}]]},
                preview=True)
        return
    if cmd in ("/menu", "/status", "/history", "/help", "/settings", "/check", "/mute", "/unmute"):
        subscribe(chat)
    if cmd == "/menu":
        send_view(cid, view_main(cid))
    elif cmd == "/status":
        send_view(cid, view_status())
    elif cmd == "/history":
        send_view(cid, view_history())
    elif cmd == "/help":
        send_view(cid, view_help())
    elif cmd == "/settings":
        send_view(cid, view_settings(cid, owner_panel(cid) and is_owner(uid)))
    elif cmd == "/check":
        left = reserve_manual()
        if left:
            send_to(cid, f"⏳ אפשר לבדוק פעם בדקה – נסו שוב בעוד {left} שניות")
            return
        j = send_to(cid, "⏳ <b>בודק עכשיו באמזון...</b>", {"inline_keyboard": [BACK]}, html_mode=True)
        if j and j.get("ok"):
            threading.Thread(target=do_manual_check, args=(j["result"]["chat"]["id"], j["result"]["message_id"]),
                             name="manual-check", daemon=True).start()
    elif cmd in ("/mute", "/unmute"):
        if not is_owner(uid):
            log.info("denied %s: user %s chat %s", cmd, uid, cid)
            if not group:  # stay silent in groups
                send_to(cid, LOCKED_TOAST)
            return
        muted = toggle_mute(cid, want=(cmd == "/mute"))
        send_to(cid, "🔕 ההתראות הושתקו בצ'אט הזה. /unmute להפעלה." if muted
                else "🔔 ההתראות פעילות בצ'אט הזה.")


def handle_my_member(u):
    chat = u.get("chat", {})
    cid = chat.get("id")
    new = (u.get("new_chat_member") or {}).get("status")
    old = (u.get("old_chat_member") or {}).get("status")
    log.info("my_chat_member: chat %s (%s) %s -> %s", cid, chat.get("type"), old, new)
    if not chat_ok(chat):
        if new in ("left", "kicked"):
            unsubscribe(cid, f"bot status {new} (non-allowed chat)")
            log.info("ALLOWLIST: non-allowed chat %s removed the bot (%s); nothing to leave", cid, new)
        else:
            leave_chat(chat, f"my_chat_member {old}->{new} by user {(u.get('from') or {}).get('id')}")
        return
    if new in ("member", "administrator", "creator", "restricted") and old in ("left", "kicked", None):
        if new == "restricted" and not (u.get("new_chat_member") or {}).get("is_member"):
            return
        subscribe(chat)
        if chat.get("type") != "private":
            update_sub(cid, welcomed=time.time())
            send_view(cid, (welcome_text(True) + "\n\n" + view_main(cid)[0], view_main(cid)[1]))
    elif new in ("left", "kicked"):
        unsubscribe(cid, f"bot status {new}")


def telegram_loop():
    offset = None
    me = tg_call("getMe")
    if me and me.get("ok"):
        BOT["username"] = me["result"].get("username") or BOT["username"]
        log.info("Telegram connected as @%s", BOT["username"])
    set_commands()
    while True:
        params = {"timeout": 50, "allowed_updates": ["message", "callback_query", "my_chat_member"]}
        if offset is not None:
            params["offset"] = offset
        j = tg_call("getUpdates", **params)
        if not j or not j.get("ok"):
            time.sleep(10)
            continue
        for u in j.get("result", []):
            offset = u["update_id"] + 1
            try:
                if u.get("message"):
                    handle_message(u["message"])
                elif u.get("callback_query"):
                    handle_callback(u["callback_query"])
                elif u.get("my_chat_member"):
                    handle_my_member(u["my_chat_member"])
            except Exception as e:
                log.error("handler error: %s", redact(e))


def main():
    migrate_state()
    st = migrate_v06()
    load_products()
    load_allowed_from_state(st)
    if TOKEN:
        try:
            startup_cleanup()
        except Exception as e:
            log.error("startup cleanup error: %s", redact(e))
    st = load_state()
    log.info("products: %s", ", ".join(f"{p['amount']}={p['asin']}{'' if is_enabled(st, p['asin']) else '(off)'}"
                                        for p in products()))
    log.info("starting psn-stock-bot v%s (allowlist=%s, owner=%s): poll=%ss±%ss attempts=%d proxy=%s telegram=%s subscribers=%d",
             __version__, sorted(ALLOWED_GROUPS) if RESTRICTED else "open", "set" if OWNER_ID else "none", POLL, JITTER, ATTEMPTS, "yes" if PROXY else "no",
             "yes" if TOKEN else "NO (checker-only)", len(st.get("subscribers", {})))
    if not OWNER_ID:
        log.warning("OWNER_ID is not set: settings are read-only for everyone and admin warnings are only logged")
    if TOKEN:
        threading.Thread(target=telegram_loop, name="telegram", daemon=True).start()
        threading.Thread(target=reminder_loop, name="reminders", daemon=True).start()
    poll_loop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.critical("fatal: %s", redact(e))
        sys.exit(1)
