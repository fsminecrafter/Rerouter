#!/usr/bin/env python3
"""Rerouter - a small website viewer with built-in menus.

* Serves index.html + config.json (and nothing else from disk).
* Speaks plain HTTP *and* HTTPS on the same port (default 2060): the first
  byte of every connection is peeked, a TLS ClientHello (0x16) is wrapped,
  anything else is handled as ordinary HTTP.
* /api/system        -> device status
* /api/feeds/<group> -> merged RSS/Atom feeds for a group from config.json
* POST /api/pages    -> add/edit/remove webpage entries (needs the password whose
                        PBKDF2 hash is stored as "password_hash" in config.json)
* POST /api/system-items      -> add/edit/remove your own messages/commands on the
                                 System page (same password)
* POST /api/system-items/run  -> run a stored command and return its output
* /api/postits       -> the post-it wall (data/postits.json)

Set the password with:  ./server.py --set-password

Environment / flags: HOST, PORT, CERT, KEY, NO_TLS=1 (or --no-tls)
"""
import argparse
import getpass
import hashlib
import hmac
import html
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import uuid
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STARTED = time.time()

# Only these files are ever served from disk (certs/, .git, server.py, ... are not).
STATIC = {"/": "index.html", "/index.html": "index.html"}   # /config.json is served filtered, see below

FEED_TTL_DEFAULT = 600          # seconds, override with "refresh_seconds" in config.json
FEED_FORCE_MIN_AGE = 30         # a manual refresh can't hammer sources more often than this
FEED_RETRY_AFTER = 60           # a failed source is retried after this many seconds
FEED_TIMEOUT = 15               # per-source network timeout (slow devices / slow feeds)
CACHE_FILE = ROOT / "cache" / "feeds.json"
MAX_FEED_BYTES = 2 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; Rerouter/1.2; +https://github.com/fsminecrafter/Rerouter)"



# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def load_config():
    with open(ROOT / "config.json", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# RSS / Atom
# --------------------------------------------------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _safe_url(url):
    parts = urllib.parse.urlsplit(url or "")
    return url if parts.scheme in ("http", "https") and parts.netloc else ""


def _parse_date(value):
    if not value:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _clean(text, limit=220):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_feed(data, source):
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        f = {}
        for child in el:
            name = _local(child.tag)
            if name == "link":
                href = (child.text or "").strip() or child.get("href") or ""
                if href and (not f.get("link") or child.get("rel") in (None, "alternate")):
                    f["link"] = href
            elif name in ("title", "description", "summary", "content",
                          "pubDate", "published", "updated", "date"):
                f.setdefault(name, "".join(child.itertext()).strip())
        title = _clean(f.get("title"), 200)
        if not title:
            continue
        stamp = next((f[k] for k in ("pubDate", "published", "updated", "date") if f.get(k)), None)
        items.append({
            "title": title,
            "link": _safe_url(f.get("link")),
            "source": source,
            "published": _parse_date(stamp),
            "summary": _clean(f.get("description") or f.get("summary") or f.get("content")),
        })
    return items


def _describe(exc):
    """Short, human-readable reason a source failed (shown in the UI)."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, ET.ParseError):
        return "not a valid RSS/Atom feed"
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS certificate check failed (wrong device clock or missing CA certificates?)"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
        return "timed out"
    return (str(reason) or type(exc).__name__)[:100]


def fetch_source(src, verify_tls=True):
    url = _safe_url(src.get("url"))
    if not url:
        raise ValueError("missing or invalid url")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    ctx = None if verify_tls else ssl._create_unverified_context()
    last = None
    for attempt in range(2):  # one retry for flaky/slow feeds
        try:
            with urllib.request.urlopen(req, timeout=FEED_TIMEOUT, context=ctx) as resp:
                data = resp.read(MAX_FEED_BYTES + 1)
            if len(data) > MAX_FEED_BYTES:
                raise ValueError("feed too large")
            name = src.get("name") or urllib.parse.urlsplit(url).netloc
            return parse_feed(data, name)
        except ET.ParseError:
            raise
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise last


# ---- per-source cache: in memory + persisted to cache/feeds.json -------------
# entry = {"name", "items", "fetched", "expires", "error"}
_sources = {}
_inflight = set()
_state_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="feed")


def _load_disk_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _sources.update({k: v for k, v in data.items() if isinstance(v, dict) and "items" in v})
    except (OSError, ValueError):
        pass


def _save_disk_cache():
    try:
        with _state_lock:
            snapshot = json.dumps(_sources)
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(snapshot, encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _refresh_source(src, cfg):
    url = src.get("url", "")
    name = src.get("name") or url
    try:
        items = fetch_source(src, cfg["verify_tls"])[: cfg["per_source"]]
        entry = {"name": name, "items": items, "fetched": time.time(),
                 "expires": time.time() + cfg["ttl"], "error": None}
    except Exception as exc:
        with _state_lock:
            old = _sources.get(url) or {}
        # keep the last good items; retry sooner than a normal refresh
        entry = {"name": name, "items": old.get("items", []), "fetched": old.get("fetched", 0),
                 "expires": time.time() + min(cfg["ttl"], FEED_RETRY_AFTER), "error": _describe(exc)}
    with _state_lock:
        _sources[url] = entry
        _inflight.discard(url)
    _save_disk_cache()


def _kick(src, cfg):
    url = src.get("url", "")
    with _state_lock:
        if url in _inflight:
            return
        _inflight.add(url)
    _pool.submit(_refresh_source, src, cfg)


def _feed_settings():
    cfg = load_config()
    return cfg, {
        "ttl": int(cfg.get("refresh_seconds", FEED_TTL_DEFAULT)),
        "per_source": int(cfg.get("items_per_source", 15)),
        "max_items": int(cfg.get("max_items", 80)),
        "verify_tls": cfg.get("feed_verify_tls", True) is not False,
    }


def get_feed_group(group, force=False):
    """Returns immediately with whatever is cached; stale/missing sources refresh in the background."""
    cfg, st = _feed_settings()
    sources = (cfg.get("feeds") or {}).get(group)
    if not isinstance(sources, list):
        return None
    now = time.time()
    items, errors, pending, fetched = [], [], [], 0
    for src in sources:
        url = src.get("url", "")
        name = src.get("name") or url
        with _state_lock:
            entry = _sources.get(url)
        due = (entry is None or now >= entry["expires"]
               or (force and now - entry.get("fetched", 0) > FEED_FORCE_MIN_AGE))
        if due:
            _kick(src, st)
        if entry is None:
            pending.append(name)
            continue
        items.extend(entry["items"])
        fetched = max(fetched, entry.get("fetched", 0))
        if entry.get("error"):
            errors.append({"source": name, "error": entry["error"]})
    with _state_lock:
        loading = any(s.get("url", "") in _inflight for s in sources)
    items.sort(key=lambda i: i["published"] or 0, reverse=True)
    return {"group": group, "fetched": fetched, "items": items[: st["max_items"]],
            "sources": [s.get("name") or s.get("url") for s in sources],
            "errors": errors, "pending": pending, "loading": loading}


def warm_feeds():
    """At startup: load the disk cache (instant first page) and refresh anything expired."""
    _load_disk_cache()
    try:
        for group in (load_config().get("feeds") or {}):
            get_feed_group(group)
    except Exception:
        pass


# --------------------------------------------------------------------------
# system info
# --------------------------------------------------------------------------
def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is actually sent
            return s.getsockname()[0]
    except OSError:
        return None


def system_info():
    info = {
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "ip": local_ip(),
        "service_uptime": time.time() - STARTED,
        "tls": TLS_ENABLED,
    }
    try:
        with open("/proc/uptime") as f:
            info["uptime"] = float(f.read().split()[0])
    except (OSError, ValueError):
        pass
    try:
        info["load"] = list(os.getloadavg())
    except (OSError, AttributeError):
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k] = int(v.split()[0]) * 1024
        info["mem_total"] = mem["MemTotal"]
        info["mem_available"] = mem.get("MemAvailable", mem.get("MemFree", 0))
    except (OSError, ValueError, KeyError, IndexError):
        pass
    try:
        du = shutil.disk_usage(ROOT)
        info["disk_total"], info["disk_used"] = du.total, du.used
    except OSError:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            info["cpu_temp"] = int(f.read().strip()) / 1000
    except (OSError, ValueError):
        pass
    return info


# --------------------------------------------------------------------------
# config editing + password (PBKDF2-HMAC-SHA256, stdlib only)
# --------------------------------------------------------------------------
PBKDF2_ITERATIONS = 200_000
ADDRESS_RE = re.compile(r"^(https?://\S+|:\d{1,5}(/\S*)?|[\w.-]+:\d{1,5}(/\S*)?|/(?!/)\S*)$")
MAX_BODY = 8192
_config_lock = threading.Lock()
_failures = {}   # client ip -> [count, first_failure, locked_until]
MAX_FAILURES, FAIL_WINDOW, LOCKOUT = 5, 600, 300


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def hash_password(password, iterations=PBKDF2_ITERATIONS):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                    bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(check.hex(), digest)
    except (ValueError, AttributeError):
        return False


def write_config(cfg):
    tmp = ROOT / "config.json.tmp"
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, ROOT / "config.json")


def public_config():
    """config.json as sent to browsers: never includes the password hash."""
    cfg = load_config()
    editable = bool(cfg.pop("password_hash", ""))
    cfg["editable"] = editable
    return cfg


def pages_key(cfg):
    for key in ("webapages", "webpages"):
        if key in cfg:
            return key
    return "webapages"


def validate_page(page):
    if not isinstance(page, dict):
        raise ApiError(400, "missing page data")
    title = str(page.get("title", "")).strip()
    webpage = str(page.get("webpage", "")).strip()
    protocol = str(page.get("protocol", "")).strip().lower()
    if not title or len(title) > 60:
        raise ApiError(400, "title is required (max 60 characters)")
    if not webpage or len(webpage) > 300 or not ADDRESS_RE.match(webpage):
        raise ApiError(400, "address must look like :3000/path, /path, host:3000/path or http(s)://...")
    if protocol not in ("", "http", "https"):
        raise ApiError(400, "protocol must be empty (auto), http or https")
    entry = {"webpage": webpage, "title": title}
    if protocol:
        entry["protocol"] = protocol
    return entry


def check_lockout(ip):
    rec = _failures.get(ip)
    if rec and rec[2] > time.time():
        raise ApiError(429, f"too many wrong passwords - try again in {int(rec[2] - time.time())}s")


def record_failure(ip):
    now = time.time()
    rec = _failures.get(ip)
    if not rec or now - rec[1] > FAIL_WINDOW:
        rec = _failures[ip] = [0, now, 0]
    rec[0] += 1
    if rec[0] >= MAX_FAILURES:
        rec[2] = now + LOCKOUT
        rec[0], rec[1] = 0, now


def authenticate(cfg, body, ip):
    """Raises ApiError unless body["password"] matches the stored hash."""
    password = body.get("password")
    if not isinstance(password, str) or not password:
        raise ApiError(401, "password required")
    stored = cfg.get("password_hash", "")
    if not stored:
        raise ApiError(403, "editing is disabled: run './server.py --set-password' on the device first")
    if not verify_password(password, stored):
        record_failure(ip)
        raise ApiError(401, "wrong password")
    _failures.pop(ip, None)


def edit_pages(body, ip):
    """add / edit / remove a webpage entry. Always requires the password."""
    check_lockout(ip)
    action = body.get("action")
    if action not in ("add", "edit", "remove"):
        raise ApiError(400, "unknown action")
    with _config_lock:
        cfg = load_config()
        authenticate(cfg, body, ip)
        key = pages_key(cfg)
        pages = cfg.setdefault(key, [])
        if action == "add":
            pages.append(validate_page(body.get("page")))
        else:
            index = body.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(pages):
                raise ApiError(400, "entry no longer exists - reload the page")
            if action == "edit":
                pages[index] = validate_page(body.get("page"))
            else:
                pages.pop(index)
        write_config(cfg)
        return {"ok": True, "pages": pages}


# --------------------------------------------------------------------------
# custom System-page items: your own messages and commands
# --------------------------------------------------------------------------
MAX_ITEMS = 40
RUN_TIMEOUT = 10          # seconds a command may run
RUN_MIN_GAP = 2           # seconds between two runs of the same item
MAX_OUTPUT = 8000
_run_last = {}


def validate_item(item):
    if not isinstance(item, dict):
        raise ApiError(400, "missing item data")
    title = str(item.get("title", "")).strip()
    kind = str(item.get("type", "message")).strip().lower()
    if not title or len(title) > 60:
        raise ApiError(400, "title is required (max 60 characters)")
    if kind == "message":
        text = str(item.get("text", "")).strip()
        if not text or len(text) > 500:
            raise ApiError(400, "message is required (max 500 characters)")
        return {"title": title, "type": "message", "text": text}
    if kind == "command":
        command = str(item.get("command", "")).strip()
        if not command or len(command) > 300 or "\n" in command or "\r" in command:
            raise ApiError(400, "command is required (one line, max 300 characters)")
        return {"title": title, "type": "command", "command": command}
    raise ApiError(400, "type must be message or command")


def edit_system_items(body, ip):
    """add / edit / remove a custom System-page item. Always requires the password."""
    check_lockout(ip)
    action = body.get("action")
    if action not in ("add", "edit", "remove"):
        raise ApiError(400, "unknown action")
    with _config_lock:
        cfg = load_config()
        authenticate(cfg, body, ip)
        items = cfg.setdefault("system_items", [])
        if action == "add":
            if len(items) >= MAX_ITEMS:
                raise ApiError(400, f"limit of {MAX_ITEMS} items reached")
            items.append(validate_item(body.get("item")))
        else:
            index = body.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(items):
                raise ApiError(400, "entry no longer exists - reload the page")
            if action == "edit":
                items[index] = validate_item(body.get("item"))
            else:
                items.pop(index)
        write_config(cfg)
        return {"ok": True, "items": items}


def run_system_item(body):
    """Runs a command that is already stored in config.json (never one sent by the client)."""
    index = body.get("index")
    items = load_config().get("system_items") or []
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(items):
        raise ApiError(400, "entry no longer exists - reload the page")
    item = items[index]
    if item.get("type") != "command" or not item.get("command"):
        raise ApiError(400, "this entry is not a command")
    now = time.time()
    if now - _run_last.get(index, 0) < RUN_MIN_GAP:
        raise ApiError(429, "slow down - try again in a moment")
    _run_last[index] = now
    try:
        proc = subprocess.run(item["command"], shell=True, cwd=ROOT, capture_output=True,
                              text=True, errors="replace", timeout=RUN_TIMEOUT, stdin=subprocess.DEVNULL)
        out, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired:
        out, code = f"(timed out after {RUN_TIMEOUT}s)", None
    return {"output": out[:MAX_OUTPUT], "code": code}


# --------------------------------------------------------------------------
# post-it wall (open to anyone who can reach the page, like the original Notey)
# --------------------------------------------------------------------------
POSTIT_FILE = ROOT / "data" / "postits.json"
POSTIT_MAX, POSTIT_CHARS = 100, 500
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_postit_lock = threading.Lock()


def load_postits():
    try:
        with open(POSTIT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_postits(notes):
    POSTIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POSTIT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(notes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, POSTIT_FILE)


def edit_postits(body):
    action = body.get("action")
    if action not in ("add", "update", "remove"):
        raise ApiError(400, "unknown action")
    content = body.get("content")
    color = body.get("color")
    if content is not None and (not isinstance(content, str) or len(content) > POSTIT_CHARS):
        raise ApiError(400, f"note text must be at most {POSTIT_CHARS} characters")
    if color is not None and (not isinstance(color, str) or not COLOR_RE.match(color)):
        raise ApiError(400, "invalid color")
    with _postit_lock:
        notes = load_postits()
        if action == "add":
            if len(notes) >= POSTIT_MAX:
                raise ApiError(400, f"limit of {POSTIT_MAX} notes reached")
            notes.append({"id": uuid.uuid4().hex[:10], "content": content or "",
                          "color": color or "#f7f1a8"})
        else:
            note = next((n for n in notes if n.get("id") == body.get("id")), None)
            if note is None:
                raise ApiError(404, "note no longer exists")
            if action == "remove":
                notes.remove(note)
            else:
                if content is not None:
                    note["content"] = content
                if color is not None:
                    note["color"] = color
        save_postits(notes)
        return {"notes": notes}


def set_password_cli():
    if sys.stdin.isatty():
        first = getpass.getpass("New Rerouter password: ")
        if first != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match.")
    else:
        first = sys.stdin.readline().rstrip("\r\n")
    if len(first) < 6:
        sys.exit("Password must be at least 6 characters.")
    with _config_lock:
        cfg = load_config()
        cfg["password_hash"] = hash_password(first)
        write_config(cfg)
    print("Password saved to config.json (stored as a PBKDF2 hash).")


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    timeout = 30  # don't let idle/half-open connections pin a thread forever

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api(self, parsed):
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/system":
            return self._send_json(system_info())
        if path == "/api/postits":
            with _postit_lock:
                return self._send_json({"notes": load_postits()})
        if path.startswith("/api/feeds/"):
            group = urllib.parse.unquote(path[len("/api/feeds/"):])
            try:
                data = get_feed_group(group, force=query.get("force") == ["1"])
            except (OSError, ValueError) as exc:
                return self._send_json({"error": f"bad config.json: {exc}"}, 500)
            if data is None:
                return self._send_json({"error": f"unknown feed group '{group}'"}, 404)
            return self._send_json(data)
        return self._send_json({"error": "not found"}, 404)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            return self._api(parsed)
        if parsed.path == "/config.json":
            try:
                return self._send_json(public_config())
            except (OSError, ValueError) as exc:
                return self._send_json({"error": f"bad config.json: {exc}"}, 500)
        if parsed.path in STATIC:
            self.path = "/" + STATIC[parsed.path]
            return super().do_GET()
        self.send_error(404, "Not found")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        routes = {
            "/api/pages": lambda body: edit_pages(body, self.client_address[0]),
            "/api/system-items": lambda body: edit_system_items(body, self.client_address[0]),
            "/api/system-items/run": run_system_item,
            "/api/postits": edit_postits,
        }
        if path not in routes:
            return self._send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                raise ApiError(413, "request body too large or empty")
            raw = self.rfile.read(length)   # always drain the body before replying (clean TLS close)
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ApiError(415, "expected application/json")
            try:
                body = json.loads(raw)
            except ValueError:
                raise ApiError(400, "invalid JSON")
            if not isinstance(body, dict):
                raise ApiError(400, "invalid JSON")
            self._send_json(routes[path](body))
        except ApiError as err:
            self._send_json({"error": err.message}, err.status)
        except (OSError, ValueError) as exc:
            self._send_json({"error": f"could not save: {exc}"}, 500)

    def do_HEAD(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in STATIC:
            self.path = "/" + STATIC[parsed.path]
            return super().do_HEAD()
        self.send_error(404, "Not found")


# --------------------------------------------------------------------------
# HTTP + HTTPS on one port
# --------------------------------------------------------------------------
class DualProtocolServer(ThreadingHTTPServer):
    """Peeks at the first byte of each connection: 0x16 = TLS handshake."""

    ssl_context = None

    def finish_request(self, request, client_address):
        # Runs in the per-connection worker thread, so a slow client can't block accept().
        conn = request
        try:
            if self.ssl_context is not None:
                request.settimeout(10)
                first = request.recv(1, socket.MSG_PEEK)
                if not first:
                    return
                if first[0] == 0x16:
                    conn = self.ssl_context.wrap_socket(request, server_side=True)
            conn.settimeout(Handler.timeout)
            self.RequestHandlerClass(conn, client_address, self)
        except (ssl.SSLError, ConnectionError, socket.timeout, TimeoutError):
            pass  # browsers rejecting a self-signed cert, resets, idle clients...
        finally:
            if conn is not request:
                try:
                    conn.close()
                except OSError:
                    pass


def ensure_cert(cert, key):
    """Use existing cert/key, or generate a self-signed pair with openssl."""
    if cert.exists() and key.exists():
        return True
    if not shutil.which("openssl"):
        return False
    cert.parent.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    san = f"subjectAltName=DNS:{host},DNS:localhost,IP:127.0.0.1"
    ip = local_ip()
    if ip:
        san += f",IP:{ip}"
    base = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
            "-keyout", str(key), "-out", str(cert), "-subj", f"/CN={host}"]
    try:
        subprocess.run(base + ["-addext", san], check=True, capture_output=True)
    except subprocess.CalledProcessError:  # very old openssl without -addext
        subprocess.run(base, check=True, capture_output=True)
    os.chmod(key, 0o600)
    print(f"Generated self-signed certificate: {cert}")
    return True


TLS_ENABLED = False


def main():
    global TLS_ENABLED
    parser = argparse.ArgumentParser(description="Serve the Rerouter website viewer.")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "2060")))
    parser.add_argument("--cert", default=os.environ.get("CERT", str(ROOT / "certs" / "cert.pem")))
    parser.add_argument("--key", default=os.environ.get("KEY", str(ROOT / "certs" / "key.pem")))
    parser.add_argument("--no-tls", action="store_true",
                        default=os.environ.get("NO_TLS", "") not in ("", "0"))
    parser.add_argument("--set-password", action="store_true",
                        help="set the password that protects adding/editing/removing entries")
    parser.add_argument("--check-password", action="store_true",
                        help="exit 0 if a password is configured, 1 otherwise")
    args = parser.parse_args()

    if args.set_password:
        return set_password_cli()
    if args.check_password:
        sys.exit(0 if load_config().get("password_hash") else 1)

    server = DualProtocolServer((args.host, args.port), Handler)

    if not args.no_tls:
        try:
            cert, key = Path(args.cert), Path(args.key)
            if ensure_cert(cert, key):
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.load_cert_chain(str(cert), str(key))
                server.ssl_context = ctx
                TLS_ENABLED = True
            else:
                print("WARNING: openssl not found and no certificate present - HTTPS disabled.")
        except (OSError, ssl.SSLError, subprocess.CalledProcessError) as exc:
            print(f"WARNING: HTTPS disabled ({exc})")

    warm_feeds()
    shown = args.host if args.host != "0.0.0.0" else "localhost"
    print(f"Serving Rerouter at http://{shown}:{args.port}")
    if TLS_ENABLED:
        print(f"                and https://{shown}:{args.port}  (same port)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
