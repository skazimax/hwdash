"""Moonlight sessions and access schedules for Sunshine — optional hwdash module.

Three parts share one SQLite database and one policy file:

- StreamManager (inside hwdash): tails sunshine.log for CLIENT CONNECTED /
  DISCONNECTED, polls the Sunshine Web API, counts each device's streaming time,
  switches paired devices on and off by schedule (Sunshine's own "enabled" flag,
  checked on every request, resume included), warns before time runs out and
  ends the stream when it does. The game itself keeps running for a short grace
  period, so that adding time does not lose it; then it is closed too.
- The launch gate (`hwdash.py gate-start` / `gate-end`), run by Sunshine as
  global_prep_cmd: records who launched what (SUNSHINE_CLIENT_NAME) and refuses
  a launch outside the allowed time — without needing hwdash to be running.
- Policies: per device mode (unmanaged | always | schedule | blocked), weekly
  windows, daily limits and temporary overrides; changing them needs a PIN.
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SUNSHINE_DEFAULTS = {
    "enabled": False,
    "api": "https://localhost:47990",  # Sunshine Web UI
    "serverinfo": "http://localhost:47989/serverinfo",  # Moonlight HTTP endpoint, no auth
    "credentials": "~/.config/hwdash/sunshine.cred",  # "user:password" of the Web UI, mode 600
    "log": "~/.config/sunshine/sunshine.log",
    "policy": "~/.config/hwdash/sunshine-policy.json",
    "pin": "~/.config/hwdash/pin",
    "warn_minutes": [10, 2],  # notifications before the time runs out
    "close_after_disconnect_minutes": 5,  # close a managed device's app left running after "Disconnect"
    "game_close_grace_minutes": 5,  # after the stream is ended: time to add minutes before the game is closed
    "game_process": r"^\S*reaper SteamLaunch AppId=(\d+)",  # regex on the cmdline; empty = never close games
    "notify": ["notify-send", "-u", "critical", "-a", "Moonlight", "Moonlight", "{message}"],
    "tick": 5,  # seconds between checks
    "lang": "en",  # language of the on-screen notifications: en | ru
}
MESSAGES = {
    "en": {"left": "{n} min of streaming left", "up": "Streaming time is up"},
    "ru": {"left": "Осталось {n} мин стрима", "up": "Время стрима вышло"},
}
MODES = ("unmanaged", "always", "schedule", "blocked")
TOKEN_TTL = 15 * 60
LAUNCH_GRACE = 60  # a launch with no running app after this long is treated as ended
GAME_TERM_WAIT = 15  # seconds between SIGTERM and SIGKILL when closing a game
GAME_KILL_WAIT = 10  # seconds after SIGKILL before reporting that the game survived
HISTORY_DAYS = 90

log = logging.getLogger("hwdash.sunshine")


# ---- policies ---------------------------------------------------------------

def empty_policy():
    return {"mode": "unmanaged", "days": [[] for _ in range(7)], "limit_weekday": None, "limit_weekend": None,
            "allow_until": None, "block_until": None}


def clean_policy(obj):
    """Validates a policy from the UI: mode, 7 lists of [from, to] minutes (Monday first), limits."""
    if not isinstance(obj, dict) or obj.get("mode") not in MODES:
        raise ValueError("bad mode")
    days = obj.get("days") or [[] for _ in range(7)]
    if not isinstance(days, list) or len(days) != 7:
        raise ValueError("days must be a list of 7 lists")
    clean_days = []
    for windows in days:
        spans = []
        for w in windows:
            if (not isinstance(w, list) or len(w) != 2 or not all(isinstance(x, int) for x in w)
                    or not 0 <= w[0] < w[1] <= 1440):
                raise ValueError("a window is [from, to] in minutes, 0 <= from < to <= 1440")
            spans.append(w)
        merged = []
        for a, b in sorted(spans):  # overlapping or touching windows become one
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        clean_days.append(merged)
    limits = {}
    for key in ("limit_weekday", "limit_weekend"):
        v = obj.get(key)
        if v is not None and (not isinstance(v, int) or not 0 <= v <= 1440):
            raise ValueError(f"{key} must be minutes 0..1440 or null")
        limits[key] = v
    return {"mode": obj["mode"], "days": clean_days, **limits}


def _midnight(ts):
    d = dt.datetime.fromtimestamp(ts)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def day_limit(policy, weekday):
    return policy["limit_weekend"] if weekday >= 5 else policy["limit_weekday"]


def next_open(policy, now, skip_today=False):
    """Start of the next allowed window after `now` (within a week), or None."""
    mid = _midnight(now)
    for d in range(8):
        day = mid + dt.timedelta(days=d)
        if (d == 0 and skip_today) or day_limit(policy, day.weekday()) == 0:
            continue
        for a, _ in policy["days"][day.weekday()]:
            start = (day + dt.timedelta(minutes=a)).timestamp()
            if start > now:
                return start
    return None


def evaluate(policy, now, used_s):
    """Whether a device may stream now, and until when.

    Order: a pause (block_until) wins, then an extension (allow_until), then the
    mode; "schedule" needs the current time inside a window and the daily limit
    not used up. Returns {managed, allowed, reason, ends_at, opens_at, limit_s, used_s}.
    """
    p = {**empty_policy(), **(policy or {})}
    weekday = _midnight(now).weekday()
    limit = day_limit(p, weekday)
    res = {"managed": p["mode"] != "unmanaged", "allowed": True, "reason": p["mode"], "ends_at": None,
           "opens_at": None, "limit_s": None if limit is None or p["mode"] != "schedule" else limit * 60,
           "used_s": int(used_s)}
    if p["mode"] == "unmanaged":
        return res
    if p["block_until"] and now < p["block_until"]:
        return {**res, "allowed": False, "reason": "paused", "opens_at": p["block_until"]}

    if p["mode"] == "always":
        regular = {"allowed": True, "reason": "always", "ends_at": None, "opens_at": None}
    elif p["mode"] == "blocked":
        regular = {"allowed": False, "reason": "blocked", "ends_at": None, "opens_at": None}
    else:
        minute = (now - _midnight(now).timestamp()) / 60
        window = next(((a, b) for a, b in p["days"][weekday] if a <= minute < b), None)
        if limit is not None and used_s >= limit * 60:
            regular = {"allowed": False, "reason": "limit", "ends_at": None,
                       "opens_at": next_open(p, now, skip_today=True)}
        elif window is None:
            regular = {"allowed": False, "reason": "closed", "ends_at": None, "opens_at": next_open(p, now)}
        else:
            ends = _midnight(now).timestamp() + window[1] * 60
            if limit is not None:
                ends = min(ends, now + limit * 60 - used_s)
            regular = {"allowed": True, "reason": "window", "ends_at": ends, "opens_at": None}

    if p["allow_until"] and now < p["allow_until"]:
        ends = p["allow_until"]
        if regular["allowed"]:
            ends = None if regular["ends_at"] is None else max(ends, regular["ends_at"])
        return {**res, "allowed": True, "reason": "extra", "ends_at": ends}
    return {**res, **regular}


def today_windows(policy, now):
    """Allowed windows of today as [start_ts, end_ts] for the day timeline."""
    p = {**empty_policy(), **(policy or {})}
    mid = _midnight(now).timestamp()
    if p["mode"] in ("unmanaged", "always"):
        return [[mid, mid + 86400]]
    if p["mode"] == "blocked":
        return []
    return [[mid + a * 60, mid + b * 60] for a, b in p["days"][_midnight(now).weekday()]]


# ---- PIN ----------------------------------------------------------------------

def hash_pin(pin, salt=None, rounds=200_000):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_pin(stored, pin):
    try:
        _, rounds, salt, digest = stored.strip().split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", pin.encode(), base64.b64decode(salt), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


# ---- storage ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS stream_launches (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, end_ts INTEGER, uuid TEXT, name TEXT, app TEXT,
    width INTEGER, height INTEGER, fps INTEGER, denied INTEGER NOT NULL DEFAULT 0, reason TEXT);
CREATE TABLE IF NOT EXISTS stream_usage (
    id INTEGER PRIMARY KEY, launch_id INTEGER, uuid TEXT, start INTEGER NOT NULL, end INTEGER);
CREATE TABLE IF NOT EXISTS stream_actions (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, uuid TEXT, action TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS stream_usage_uuid ON stream_usage (uuid, start);
"""


class StreamDB:
    """Launches (from the gate), connected intervals (from the log) and an audit trail."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def w(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    def add_launch(self, ts, uuid, name, app, width, height, fps, denied, reason):
        return self.w("INSERT INTO stream_launches (ts, uuid, name, app, width, height, fps, denied, reason)"
                      " VALUES (?,?,?,?,?,?,?,?,?)", (ts, uuid, name, app, width, height, fps, int(denied), reason))

    def active_launch(self):
        rows = self.q("SELECT id, ts, uuid, name, app, width, height, fps FROM stream_launches"
                      " WHERE end_ts IS NULL AND denied = 0 ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        return dict(zip(("id", "ts", "uuid", "name", "app", "width", "height", "fps"), rows[0]))

    def end_launches(self, ts):
        self.w("UPDATE stream_launches SET end_ts = ? WHERE end_ts IS NULL AND denied = 0", (ts,))

    def open_usage(self):
        rows = self.q("SELECT id, launch_id, uuid, start FROM stream_usage WHERE end IS NULL ORDER BY id DESC LIMIT 1")
        return dict(zip(("id", "launch_id", "uuid", "start"), rows[0])) if rows else None

    def start_usage(self, ts, launch):
        self.w("INSERT INTO stream_usage (launch_id, uuid, start) VALUES (?,?,?)",
               (launch and launch["id"], launch and launch["uuid"], ts))

    def end_usage(self, ts):
        self.w("UPDATE stream_usage SET end = MAX(start, ?) WHERE end IS NULL", (ts,))

    def intervals(self, start, end, now):
        """{uuid: [[start, end], ...]} of connected time overlapping [start, end)."""
        out = {}
        for uuid, s, e in self.q("SELECT uuid, start, end FROM stream_usage"
                                 " WHERE start < ? AND (end IS NULL OR end > ?) ORDER BY start", (end, start)):
            out.setdefault(uuid, []).append([max(s, start), min(e if e is not None else now, end)])
        return out

    def used_today(self, uuid, now):
        mid = _midnight(now).timestamp()
        return sum(e - s for s, e in self.intervals(mid, mid + 86400, now).get(uuid, []))

    def action(self, ts, uuid, action, detail=""):
        self.w("INSERT INTO stream_actions (ts, uuid, action, detail) VALUES (?,?,?,?)", (ts, uuid, action, detail))

    def recent(self, since):
        launches = self.q("SELECT ts, end_ts, uuid, name, app, denied, reason FROM stream_launches"
                          " WHERE ts >= ? ORDER BY ts DESC LIMIT 50", (since,))
        actions = self.q("SELECT ts, uuid, action, detail FROM stream_actions WHERE ts >= ? ORDER BY ts DESC LIMIT 50",
                         (since,))
        return ([dict(zip(("ts", "end_ts", "uuid", "name", "app", "denied", "reason"), r)) for r in launches],
                [dict(zip(("ts", "uuid", "action", "detail"), r)) for r in actions])

    def prune(self, before):
        for table, col in (("stream_launches", "ts"), ("stream_usage", "start"), ("stream_actions", "ts")):
            self.w(f"DELETE FROM {table} WHERE {col} < ?", (before,))


# ---- Sunshine -------------------------------------------------------------------

class SunshineAPI:
    """Sunshine Web UI API (basic auth, self-signed certificate) and Moonlight serverinfo."""

    def __init__(self, cfg):
        self.base = cfg["api"].rstrip("/")
        self.serverinfo_url = cfg["serverinfo"]
        self.cred_path = Path(cfg["credentials"]).expanduser()
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE  # Sunshine's own self-signed certificate on localhost

    def _call(self, method, path, body=None):
        cred = self.cred_path.read_text().strip()
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode())
        req.add_header("Authorization", "Basic " + base64.b64encode(cred.encode()).decode())
        req.add_header("Origin", self.base)  # Sunshine's CSRF check
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5, context=self.ctx) as r:
            return json.loads(r.read() or b"{}")

    def clients(self):
        data = self._call("GET", "/api/clients/list")
        return [{"uuid": c["uuid"], "name": c.get("name", ""), "enabled": c.get("enabled", True) in (True, "true")}
                for c in data.get("named_certs", [])]

    def set_enabled(self, uuid, enabled):
        self._call("POST", "/api/clients/update", {"uuid": uuid, "enabled": enabled})

    def close_app(self):
        self._call("POST", "/api/apps/close", {})

    def server_state(self):
        """{"busy": bool, "app_id": int} from serverinfo (0 = nothing running)."""
        with urllib.request.urlopen(self.serverinfo_url, timeout=5) as r:
            xml = r.read().decode(errors="replace")
        game = re.search(r"<currentgame>(\d+)</currentgame>", xml)
        return {"busy": "SUNSHINE_SERVER_BUSY" in xml, "app_id": int(game[1]) if game else 0}


LOG_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)?\]: \w+: (.*)$")


def parse_log_line(line):
    """(ts, kind, value) for lines hwdash cares about, else None."""
    m = LOG_LINE.match(line.strip())
    if not m:
        return None
    ts = dt.datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").timestamp()
    text = m[2]
    if text.startswith("CLIENT CONNECTED"):
        return ts, "connected", None
    if text.startswith("CLIENT DISCONNECTED"):
        return ts, "disconnected", None
    if text.startswith("Creating encoder ["):
        return ts, "codec", text[len("Creating encoder ["):].rstrip("]").split("_")[0].upper()
    if text.startswith("Streaming bitrate is "):
        return ts, "bitrate", int(text.split()[-1])
    return None


class LogTail:
    """Reads new lines of a log that Sunshine rotates on start (sunshine.log -> .1)."""

    def __init__(self, path):
        self.path = path
        self.inode = None
        self.pos = 0

    def read(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        if st.st_ino != self.inode or st.st_size < self.pos:
            self.inode, self.pos = st.st_ino, 0
        with open(self.path, errors="replace") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        return data.splitlines()


class SteamGames:
    """Running games: processes whose cmdline matches `pattern` (Steam's reaper), and their trees."""

    def __init__(self, pattern):
        self.pattern = re.compile(pattern) if pattern else None

    @staticmethod
    def _table():
        """{pid: (ppid, cmdline)} of this user's processes."""
        table, uid = {}, os.getuid()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                if os.stat(f"/proc/{entry}").st_uid != uid:
                    continue
                with open(f"/proc/{entry}/stat") as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            except (OSError, ValueError, IndexError):
                continue  # the process just exited
            table[int(entry)] = (ppid, cmd)
        return table

    def _roots(self, table):
        if not self.pattern:
            return {}
        return {pid: m for pid, (_, cmd) in table.items() if (m := self.pattern.match(cmd))}

    def running(self):
        games = []
        for pid, m in self._roots(self._table()).items():
            app_id = m[1] if m.re.groups else None
            games.append({"pid": pid, "app_id": app_id, "name": self._name(app_id)})
        return games

    @staticmethod
    def _name(app_id):
        manifest = read_or_none(Path.home() / f".local/share/Steam/steamapps/appmanifest_{app_id}.acf")
        m = re.search(r'"name"\s+"([^"]+)"', manifest or "")
        return m[1] if m else None

    def signal(self, sig):
        """SIGTERM goes to what runs under the roots; SIGKILL to the roots as well."""
        table = self._table()
        roots = self._roots(table)
        children = {}
        for pid, (ppid, _) in table.items():
            children.setdefault(ppid, []).append(pid)
        targets, stack = [], list(roots)
        while stack:
            for child in children.get(stack.pop(), []):
                targets.append(child)
                stack.append(child)
        if sig == signal.SIGKILL:
            targets += list(roots)
        for pid in targets:
            try:
                os.kill(pid, sig)
            except OSError:
                pass


# ---- manager -------------------------------------------------------------------

class StreamManager:
    """Session state, schedule enforcement and the API behind the Streaming tab."""

    def __init__(self, cfg, db_path, api=None, games=None):
        self.cfg = cfg
        self.db = StreamDB(db_path)
        self.api = api or SunshineAPI(cfg)
        self.game_procs = games or SteamGames(cfg["game_process"])
        self.games = []
        self.policy_path = Path(cfg["policy"]).expanduser()
        self.clients_cache = self.policy_path.with_name("sunshine-clients.json")
        self.game_close_path = self.policy_path.with_name("sunshine-game-close.json")
        self.game_close = self._load_game_close()  # survives a restart of hwdash
        self.pin_path = Path(cfg["pin"]).expanduser()
        self.tail = LogTail(Path(cfg["log"]).expanduser())
        self.lock = threading.RLock()
        self.policies = self._load_policies()
        self.clients = []
        self.clients_at = 0
        self.server = {"busy": False, "app_id": 0}
        self.connected = False
        self.connected_since = None
        self.disconnected_since = None
        self.codec = None
        self.bitrate = None
        self.error = None
        self.warned = {}  # launch id -> thresholds already announced
        self.last_close = 0
        self.tokens = {}
        self.fails = 0
        self.locked_until = 0
        self._replay_log()

    # -- state ----------------------------------------------------------------

    def _load_policies(self):
        try:
            data = json.loads(self.policy_path.read_text())
            return {u: {**empty_policy(), **p} for u, p in data.get("clients", {}).items()}
        except (OSError, ValueError):
            return {}

    def _save_policies(self):
        write_private(self.policy_path, json.dumps({"clients": self.policies}, indent=1))

    def policy(self, uuid):
        return self.policies.get(uuid) or empty_policy()

    def _load_game_close(self):
        try:
            return json.loads(self.game_close_path.read_text())
        except (OSError, ValueError):
            return None

    def _set_game_close(self, value):
        self.game_close = value
        if value:
            write_private(self.game_close_path, json.dumps(value))
        else:
            self.game_close_path.unlink(missing_ok=True)

    def _replay_log(self):
        """Catch up after a restart: is a client connected right now, since when, with what codec."""
        for line in self.tail.read():
            ev = parse_log_line(line)
            if ev:
                self._on_log(*ev, record=False)
        try:
            if self.connected and not self.api.server_state()["busy"]:  # see tick(): no DISCONNECTED in the log
                self.connected, self.disconnected_since = False, self.connected_since
        except (OSError, ValueError, urllib.error.URLError):
            pass
        open_usage = self.db.open_usage()
        if self.connected and not open_usage:
            self.db.start_usage(int(self.connected_since), self.db.active_launch())
        elif not self.connected and open_usage:
            self.db.end_usage(int(self.disconnected_since or time.time()))

    def _on_log(self, ts, kind, value, record=True):
        if kind == "connected":
            self.connected, self.connected_since, self.codec, self.bitrate = True, ts, None, None
            if record:
                self.db.end_usage(int(ts))  # a missed DISCONNECTED
                self.db.start_usage(int(ts), self.db.active_launch())
        elif kind == "disconnected":
            self.connected, self.disconnected_since = False, ts
            if record:
                self.db.end_usage(int(ts))
        elif kind == "codec":
            self.codec = value
        elif kind == "bitrate":
            self.bitrate = value

    # -- loop -----------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="sunshine").start()

    def _loop(self):
        last_prune = 0
        while True:
            now = time.time()
            try:
                self.tick(now)
                if now - last_prune > 86400:
                    self.db.prune(int(now - HISTORY_DAYS * 86400))
                    last_prune = now
            except Exception:
                log.exception("sunshine tick failed")
            time.sleep(self.cfg["tick"])

    def tick(self, now):
        with self.lock:
            for line in self.tail.read():
                ev = parse_log_line(line)
                if ev:
                    self._on_log(*ev)
            try:
                self.server = self.api.server_state()
                if self.connected and not self.server["busy"]:
                    self._on_log(now, "disconnected", None)  # /api/apps/close logs no CLIENT DISCONNECTED
                if not self.clients or now - self.clients_at > 30:
                    self._refresh_clients(now)
                launch = self.db.active_launch()
                if launch and not self.server["busy"] and now - launch["ts"] > LAUNCH_GRACE:
                    self.db.end_launches(int(now))  # app gone without the gate's undo (crash, Sunshine restart)
                    self.db.end_usage(int(now))
                    launch = None
                self._enforce(now, launch)
                self.error = None
            except (OSError, ValueError, urllib.error.URLError) as e:  # Sunshine down, bad credentials
                self.error = str(e)
            self._games_tick(now)  # runs even when Sunshine is down

    def _games_tick(self, now):
        """The grace period after the stream ended: wait, then SIGTERM, then SIGKILL, then report."""
        self.games = self.game_procs.running()
        pending = self.game_close
        if not pending:
            return
        uuid = pending["uuid"]
        if not self.games:
            self.db.action(int(now), uuid, "game_closed" if pending.get("term_at") else "game_gone")
            self._set_game_close(None)
        elif pending.get("kill_at"):
            if now - pending["kill_at"] >= GAME_KILL_WAIT:
                self.db.action(int(now), uuid, "game_failed")
                log.warning("the game of %s survived SIGKILL", pending["name"])
                self._set_game_close(None)
        elif pending.get("term_at"):
            if now - pending["term_at"] >= GAME_TERM_WAIT:
                self.game_procs.signal(signal.SIGKILL)
                self._set_game_close({**pending, "kill_at": now})
        elif self.connected and self.connected_since > pending["armed_at"]:
            self._keep_game(now, "reconnected")  # someone is back at the machine
        elif pending["why"] != "end" and self._eval(uuid, now)["allowed"]:
            self._keep_game(now, "allowed")  # a schedule window opened or the rules changed
        elif now >= pending["close_at"]:
            self._term_game(now, uuid, pending)

    def _term_game(self, now, uuid, pending):
        self.game_procs.signal(signal.SIGTERM)
        self.db.action(int(now), uuid, "game_term", ", ".join(g["name"] or str(g["app_id"]) for g in self.games))
        log.info("closing the game: %s", pending["name"])
        self._set_game_close({**pending, "term_at": now})

    def _keep_game(self, now, why):
        if self.game_close and not self.game_close.get("term_at"):
            self.db.action(int(now), self.game_close["uuid"], "game_keep", why)
            self._set_game_close(None)

    def _refresh_clients(self, now):
        self.clients = self.api.clients()
        self.clients_at = now
        cache = json.dumps([{"uuid": c["uuid"], "name": c["name"]} for c in self.clients])
        if read_or_none(self.clients_cache) != cache:
            write_private(self.clients_cache, cache)

    def _eval(self, uuid, now):
        return evaluate(self.policy(uuid), now, self.db.used_today(uuid, now))

    def _enforce(self, now, launch):
        for c in self.clients:
            ev = self._eval(c["uuid"], now)
            if ev["managed"] and c["enabled"] != ev["allowed"]:
                self.api.set_enabled(c["uuid"], ev["allowed"])
                c["enabled"] = ev["allowed"]
                self.db.action(int(now), c["uuid"], "enable" if ev["allowed"] else "disable", ev["reason"])
                log.info("%s %s (%s)", "enabled" if ev["allowed"] else "disabled", c["name"], ev["reason"])
        if not launch or not launch["uuid"] or not self.server["busy"]:
            return
        ev = self._eval(launch["uuid"], now)
        if not ev["managed"]:
            return
        if not ev["allowed"]:
            self._close(now, launch, "time is up" if self.connected else ev["reason"], close_game=True)
            return
        if ev["ends_at"] is not None and self.connected:
            left = ev["ends_at"] - now
            done = self.warned.setdefault(launch["id"], set())
            for m in sorted(self.cfg["warn_minutes"]):
                if left <= m * 60 and m not in done:
                    done.update(x for x in self.cfg["warn_minutes"] if x >= m)
                    self.notify("left", n=max(1, round(left / 60)))
                    self.db.action(int(now), launch["uuid"], "warn", f"{round(left / 60)} min")
                    break
        limit = self.cfg["close_after_disconnect_minutes"]
        if (not self.connected and self.policy(launch["uuid"])["mode"] != "always" and limit is not None
                and self.disconnected_since
                and now - self.disconnected_since > limit * 60):
            self._close(now, launch, "left running after disconnect")

    def _close(self, now, launch, why, close_game=False):
        """Ends the stream. Closing the app does not stop a game started from it (Big Picture is
        detached), so for a managed device the game gets a grace period and is closed after it."""
        if now - self.last_close < 30:  # give Sunshine time to close before retrying
            return
        self.last_close = now
        if self.connected:
            self.notify("up")
        self.api.close_app()
        self.db.action(int(now), launch["uuid"], "close", why)
        log.info("closed the app of %s: %s", launch["name"], why)
        if (close_game and not self.game_close and self.game_procs.running()
                and self.policy(launch["uuid"])["mode"] in ("schedule", "blocked")):
            grace = self.cfg["game_close_grace_minutes"]
            self._set_game_close({"uuid": launch["uuid"], "name": launch["name"], "why": why, "armed_at": now,
                                  "close_at": now + grace * 60})
            self.db.action(int(now), launch["uuid"], "game_wait", f"{grace} min")

    def notify(self, key, **kw):
        message = MESSAGES.get(self.cfg["lang"], MESSAGES["en"])[key].format(**kw)
        cmd = [part.replace("{message}", message) for part in self.cfg["notify"]]
        try:
            subprocess.run(cmd, timeout=5, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            log.warning("notification failed: %s", cmd[0])

    # -- API ------------------------------------------------------------------

    def snapshot(self, token=None):
        now = time.time()
        with self.lock:
            launch = self.db.active_launch()
            mid = _midnight(now).timestamp()
            usage = self.db.intervals(mid, mid + 86400, now)
            launches, actions = self.db.recent(int(mid))
            clients = []
            for c in self.clients:
                ev = self._eval(c["uuid"], now)
                online = bool(launch and launch["uuid"] == c["uuid"] and self.connected)
                clients.append({**c, "policy": self.policy(c["uuid"]), "eval": ev, "online": online,
                                "windows": today_windows(self.policy(c["uuid"]), now),
                                "usage": usage.get(c["uuid"], [])})
            session = None
            if launch and self.server["busy"]:
                ev = self._eval(launch["uuid"], now) if launch["uuid"] else None
                session = {**launch, "connected": self.connected, "connected_since": self.connected_since,
                           "disconnected_since": None if self.connected else self.disconnected_since,
                           "codec": self.codec, "bitrate": self.bitrate, "eval": ev}
            elif self.server["busy"]:  # running, but launched before the gate was installed
                session = {"name": None, "uuid": None, "app": None, "ts": self.connected_since,
                           "connected": self.connected, "connected_since": self.connected_since,
                           "codec": self.codec, "bitrate": self.bitrate, "eval": None}
            return {"available": self.error is None, "error": self.error, "now": now, "day_start": mid,
                    "pin_set": self.pin_path.exists(), "authorized": self.authorized(token),
                    "session": session, "clients": clients, "launches": launches, "actions": actions,
                    "games": self.games, "game_close": self.game_close}

    def login(self, pin):
        now = time.time()
        with self.lock:
            if now < self.locked_until:
                return None, int(self.locked_until - now)
            stored = read_or_none(self.pin_path)
            if stored and check_pin(stored, pin):
                self.fails = 0
                token = secrets.token_urlsafe(24)
                self.tokens = {t: exp for t, exp in self.tokens.items() if exp > now}
                self.tokens[token] = now + TOKEN_TTL
                return token, 0
            self.fails += 1
            if self.fails >= 5:  # 30 s, 1 min, 2 min, ... up to an hour
                self.locked_until = now + min(3600, 30 * 2 ** (self.fails - 5))
            return None, max(0, int(self.locked_until - now))

    def authorized(self, token):
        return bool(token) and self.tokens.get(token, 0) > time.time()

    def set_policy(self, uuid, policy):
        with self.lock:
            old = self.policy(uuid)
            self.policies[uuid] = {**clean_policy(policy), "allow_until": old["allow_until"],
                                   "block_until": old["block_until"]}
            self._save_policies()
            self.db.action(int(time.time()), uuid, "policy", self.policies[uuid]["mode"])
            now = time.time()
            if self.game_close and self.game_close["uuid"] == uuid and self._eval(uuid, now)["allowed"]:
                self._keep_game(now, "rules changed")
            self.tick(now)

    def apply_action(self, uuid, action, minutes=None):
        """extend (minutes: from the current end, or from now when closed; keeps a waiting game),
        pause (until midnight or 07:00), end (end the stream now), clear (drop extensions and
        pauses), close_game (close the running game now, whoever started it)."""
        now = time.time()
        with self.lock:
            if action == "close_game":
                if not self.game_procs.running():
                    raise ValueError("no game is running")
                pending = self.game_close or {"uuid": uuid or None, "name": None, "why": "manual", "armed_at": now,
                                              "close_at": now}
                if not pending.get("term_at"):
                    self.games = self.game_procs.running()
                    self._term_game(now, pending["uuid"], pending)
                return
            p = dict(self.policy(uuid))
            if p["mode"] == "unmanaged" and action in ("extend", "pause_today", "pause_morning"):
                raise ValueError("device is not managed")
            if action == "extend":
                if not isinstance(minutes, int) or not 1 <= minutes <= 720:
                    raise ValueError("minutes must be 1..720")
                ev = self._eval(uuid, now)
                base = ev["ends_at"] if ev["allowed"] and ev["ends_at"] else now
                p.update(allow_until=max(base, p["allow_until"] or 0) + minutes * 60, block_until=None)
            elif action in ("pause_today", "pause_morning"):
                until = _midnight(now) + dt.timedelta(days=1, hours=7 if action == "pause_morning" else 0)
                p.update(block_until=until.timestamp(), allow_until=None)
            elif action == "clear":
                p.update(allow_until=None, block_until=None)
            elif action != "end":
                raise ValueError("unknown action")
            if action != "end":
                self.policies[uuid] = p
                self._save_policies()
            self.db.action(int(now), uuid, action, str(minutes or ""))
            launch = self.db.active_launch()
            if action in ("end", "pause_today", "pause_morning") and launch and launch["uuid"] == uuid \
                    and self.server["busy"]:
                self.last_close = 0
                self._close(now, launch, action, close_game=True)
            if action == "extend" and self.game_close and self.game_close["uuid"] == uuid:
                self._keep_game(now, "extended")
            self.tick(now)


def read_or_none(path):
    try:
        return path.read_text()
    except OSError:
        return None


# ---- launch gate (Sunshine global_prep_cmd) -----------------------------------

def run_gate(cfg, db_path, phase, env=None, now=None):
    """Exit code for Sunshine: 1 refuses the launch. Any internal error lets it through."""
    env = os.environ if env is None else env
    now = now or time.time()
    try:
        db = StreamDB(db_path)
        if phase == "end":
            db.end_launches(int(now))
            db.end_usage(int(now))
            return 0
        name = env.get("SUNSHINE_CLIENT_NAME", "")
        policy_path = Path(cfg["policy"]).expanduser()
        clients = json.loads(read_or_none(policy_path.with_name("sunshine-clients.json")) or "[]")
        uuid = next((c["uuid"] for c in clients if c["name"] == name), None)
        policies = json.loads(read_or_none(policy_path) or "{}").get("clients", {})
        ev = evaluate(policies.get(uuid), now, db.used_today(uuid, now) if uuid else 0)
        num = lambda k: int(env[k]) if env.get(k, "").isdigit() else None
        db.add_launch(int(now), uuid, name, env.get("SUNSHINE_APP_NAME"), num("SUNSHINE_CLIENT_WIDTH"),
                      num("SUNSHINE_CLIENT_HEIGHT"), num("SUNSHINE_CLIENT_FPS"), not ev["allowed"], ev["reason"])
        if not ev["allowed"]:
            print(f"hwdash: {name} is not allowed to stream now ({ev['reason']})")
            return 1
        return 0
    except Exception as e:  # never lock everyone out because of a bug here
        print(f"hwdash gate error, allowing: {e!r}")
        return 0


# ---- demo --------------------------------------------------------------------

class DemoGames:
    """A pretend game for --demo; it goes away on the first signal."""

    def __init__(self):
        self.list = [{"pid": 1, "app_id": "228380", "name": "Racing Game"}]

    def running(self):
        return list(self.list)

    def signal(self, sig):
        self.list = []


class DemoSunshineAPI:
    """In-memory Sunshine for --demo: five devices, one of them streaming."""

    def __init__(self, clients):
        self._clients = clients
        self.busy = True

    def clients(self):
        return [dict(c) for c in self._clients]

    def set_enabled(self, uuid, enabled):
        for c in self._clients:
            if c["uuid"] == uuid:
                c["enabled"] = enabled

    def close_app(self):
        self.busy = False

    def server_state(self):
        return {"busy": self.busy, "app_id": 1 if self.busy else 0}


def make_demo(tmp, cfg):
    """A StreamManager over a temporary database with a believable day, built around now."""
    now = time.time()
    cfg = {**SUNSHINE_DEFAULTS, **cfg, "enabled": True, "notify": ["true"], "log": str(tmp / "none.log"),
           "policy": str(tmp / "sunshine-policy.json"), "pin": str(tmp / "pin")}
    write_private(Path(cfg["pin"]), hash_pin("1234"))
    ids = {name: f"DEMO-{i:04d}" for i, name in enumerate(("Living room TV", "Kids tablet", "Steam Deck",
                                                            "Old laptop", "Phone"))}
    minute = int((now - _midnight(now).timestamp()) / 60)
    kid_window = [max(0, (minute - 180) // 30 * 30), min(1440, (minute + 90) // 30 * 30)]
    week = lambda *w: [list(map(list, w)) for _ in range(7)]
    policies = {
        ids["Living room TV"]: {**empty_policy(), "mode": "always"},
        ids["Kids tablet"]: {**empty_policy(), "mode": "schedule", "days": week(kid_window),
                             "limit_weekday": 90, "limit_weekend": 90},
        ids["Steam Deck"]: {**empty_policy(), "mode": "schedule",
                            "days": [[[960, 1200]]] * 5 + [[[600, 1260]]] * 2, "limit_weekday": 60,
                            "limit_weekend": 120},
        ids["Old laptop"]: {**empty_policy(), "mode": "blocked"},
    }
    write_private(Path(cfg["policy"]), json.dumps({"clients": policies}))
    clients = [{"uuid": u, "name": n, "enabled": n != "Old laptop"} for n, u in ids.items()]
    mgr = StreamManager(cfg, tmp / "metrics.db", api=DemoSunshineAPI(clients), games=DemoGames())
    db, kid = mgr.db, ids["Kids tablet"]
    t = int(now)
    lid = db.add_launch(t - 6000, ids["Living room TV"], "Living room TV", "Desktop", 3840, 2160, 60, False, "always")
    db.w("INSERT INTO stream_usage (launch_id, uuid, start, end) VALUES (?,?,?,?)", (lid, ids["Living room TV"], t - 6000, t - 4200))
    db.end_launches(t - 4100)
    lid = db.add_launch(t - 5400, kid, "Kids tablet", "Steam Big Picture", 1920, 1080, 60, False, "window")
    db.w("INSERT INTO stream_usage (launch_id, uuid, start, end) VALUES (?,?,?,?)", (lid, kid, t - 5400, t - 3600))
    db.end_launches(t - 3500)
    db.add_launch(t - 3000, ids["Old laptop"], "Old laptop", "Desktop", 1920, 1080, 60, True, "blocked")
    db.add_launch(t - 2400, kid, "Kids tablet", "Steam Big Picture", 1920, 1080, 60, False, "window")
    db.start_usage(t - 2280, db.active_launch())
    mgr.connected, mgr.connected_since, mgr.codec, mgr.bitrate = True, t - 2280, "HEVC", 20_000_000
    mgr.tick(now)
    return mgr
