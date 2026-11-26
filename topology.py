"""Network topology with live reachability — optional hwdash module.

The [topology] section describes a tree of nodes: one root (usually the router
that terminates the tunnels) and everything connected through it. A background
thread pings each node from this machine, so a node's state means "reachable
from here": it covers the path and the node together.

Optionally, Keenetic/Netcraze routers with an `api` address are read over their
web API, with GET requests only: tunnel state for the links (the link's colour then
shows what the tunnel reports, not the ping) and CPU, memory and uptime for the node.

Exits ([[topology.exits]]) are public VPNs and proxies, shown in a table: each is checked
with a real request through it, so a tunnel that is up but useless still shows as a problem.
"""
import hashlib
import http.cookiejar
import json
import logging
import os
import re
import shlex
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("hwdash")

TOPOLOGY_DEFAULTS = {
    "enabled": False,
    "interval": 15,      # seconds between checks
    "count": 3,          # echo requests per check
    "rtt_warn_ms": 150,  # a slower average counts as degraded
    "api_interval": 30,  # seconds between router API reads
    "handshake_max": 300,  # a WireGuard handshake older than this means the tunnel is down
    "credentials": "~/.config/hwdash/topology.cred",  # "user:password" for router APIs, mode 600
    "nodes": [],
    "exits": [],
    "exit_interval": 60,    # seconds between checks through each exit
    "speed_interval": 3600,  # seconds between speed samples
    "geo_interval": 900,    # seconds between exit location lookups
    "geo_url": "https://ipinfo.io/json",
    "state_file": "",  # keeps speed history across restarts, e.g. "~/.local/share/hwdash/exits.json"
    "ip_url": "https://api.ipify.org",  # plain-text IP echo, used to see which exit a shared port uses
}
EXIT_FIELDS = {"id", "title", "at", "proxy", "ssh", "probe", "check", "speed", "speed_streams", "speed_bytes",
               "lag_warn_ms", "speed_warn_mbit", "active_via", "backup", "purpose"}
NODE_FIELDS = {"id", "title", "subtitle", "group", "ping", "parent", "link", "optional",
               "api", "tunnel_if", "tunnel_user"}
_URL = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+$")
_PROXY = re.compile(r"^(socks5h?|https?)://[A-Za-z0-9.-]+:\d+$")
_SSH_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_API = re.compile(r"^https?://[A-Za-z0-9.:\[\]-]+$")
FAILS_FOR_DOWN = 2       # full-loss checks in a row before a node is reported down
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
_LOSS = re.compile(r"([\d.]+)% packet loss")
_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)/")


def clean_nodes(nodes):
    """Validated copies of the configured nodes; raises ValueError on a broken tree."""
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("[topology] needs at least one [[topology.nodes]]")
    out, seen = [], set()
    for n in nodes:
        unknown = set(n) - NODE_FIELDS
        if unknown:
            raise ValueError(f"unknown node fields: {', '.join(sorted(unknown))}")
        nid = str(n.get("id", ""))
        if not nid or nid in seen:
            raise ValueError(f"node id is empty or repeated: {nid!r}")
        seen.add(nid)
        target = str(n.get("ping", ""))
        if target and not _TARGET.match(target):
            raise ValueError(f"node {nid}: bad ping target {target!r}")
        api = str(n.get("api", "")).rstrip("/")
        if api and not _API.match(api):
            raise ValueError(f"node {nid}: bad api address {api!r}")
        out.append({"id": nid, "title": str(n.get("title", nid)), "subtitle": str(n.get("subtitle", "")),
                    "group": str(n.get("group", "")), "ping": target, "parent": str(n.get("parent", "")),
                    "link": str(n.get("link", "")), "optional": bool(n.get("optional", False)),
                    "api": api, "tunnel_if": str(n.get("tunnel_if", "")), "tunnel_user": str(n.get("tunnel_user", ""))})
    by_id = {n["id"]: n for n in out}
    if sum(1 for n in out if not n["parent"]) != 1:
        raise ValueError("exactly one node must have no parent")
    for n in out:
        walked, cur = set(), n
        while cur["parent"]:
            if cur["id"] in walked or cur["parent"] not in by_id:
                raise ValueError(f"node {n['id']}: parent chain is broken")
            walked.add(cur["id"])
            cur = by_id[cur["parent"]]
    return out


def clean_exits(exits):
    """Validated copies of the configured exits; their values end up on command lines."""
    out, seen = [], set()
    for e in exits:
        unknown = set(e) - EXIT_FIELDS
        if unknown:
            raise ValueError(f"unknown exit fields: {', '.join(sorted(unknown))}")
        eid = str(e.get("id", ""))
        if not eid or eid in seen:
            raise ValueError(f"exit id is empty or repeated: {eid!r}")
        seen.add(eid)
        check, speed, proxy, ssh = (str(e.get(k, "")) for k in ("check", "speed", "proxy", "ssh"))
        if not _URL.match(check) or (speed and not _URL.match(speed)):
            raise ValueError(f"exit {eid}: bad check or speed url")
        if (proxy and not _PROXY.match(proxy)) or (ssh and not _SSH_HOST.match(ssh)):
            raise ValueError(f"exit {eid}: bad proxy or ssh host")
        active_via = str(e.get("active_via", ""))
        if active_via and not _PROXY.match(active_via):
            raise ValueError(f"exit {eid}: bad active_via")
        probe = str(e.get("probe", ""))
        if probe and (not ssh or not _SSH_HOST.match(probe)):
            raise ValueError(f"exit {eid}: probe needs an ssh host and a plain command name")
        out.append({"id": eid, "title": str(e.get("title", eid)), "at": str(e.get("at", "")), "proxy": proxy, "ssh": ssh, "probe": probe,
                    "active_via": active_via, "backup": bool(e.get("backup", False)), "purpose": str(e.get("purpose", "")),
                    "check": check, "speed": speed, "speed_streams": max(1, int(e.get("speed_streams", 1))),
                    "speed_bytes": max(0, int(e.get("speed_bytes", 0))), "lag_warn_ms": float(e.get("lag_warn_ms", 1500)),
                    "speed_warn_mbit": float(e.get("speed_warn_mbit", 2))})
    return out


def exit_curl(ex, url, seconds, write="%{http_code} %{time_starttransfer} %{speed_download} %{size_download}"):
    """curl through an exit, locally or on its ssh host: stdout, or None when it could not run.

    With `probe`, the remote side is a fixed script (a forced command for a restricted key)
    that takes only "check", "speed" or "geo" and has its own urls.
    """
    args = ["curl", "-s", "-m", str(seconds)]
    args += ["--proxy", ex["proxy"]] if ex["proxy"] else ["--noproxy", "*"]
    if url == ex["speed"] and ex["speed_bytes"]:
        args += ["-r", f"0-{ex['speed_bytes'] - 1}"]
    args += ["-o", "/dev/null", "-w", write, url] if write else [url]
    if ex["probe"]:
        kind = "check" if url == ex["check"] else "speed" if url == ex["speed"] else "geo"
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ex["ssh"], f"{ex['probe']} {kind}"]
    elif ex["ssh"]:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ex["ssh"], shlex.join(args)]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=seconds + 15)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("exit %s: %s", ex["id"], type(e).__name__)
        return None
    return res.stdout


def parse_curl(text):
    """(http code, seconds to first byte, Mbit/s, bytes); code 0 means the request failed."""
    try:
        code, ttfb, speed, size = (text or "").split()[:4]
        return int(code), float(ttfb), float(speed) * 8 / 1e6, int(float(size))
    except ValueError:
        return 0, None, None, 0


def usual_speed(st):
    """Median of earlier samples, once there are enough of them to mean something."""
    history = st["speeds"][:-1]
    return statistics.median(history) if len(history) >= 6 else None


def exit_state(ex, st):
    """(state, reason) for an exit: "speed" is below the threshold, "drop" is below half of its usual."""
    if st["fails"]:
        return ("down" if st["fails"] >= FAILS_FOR_DOWN else "degraded"), "no_answer"
    if st["lag_ms"] is not None and st["lag_ms"] > ex["lag_warn_ms"]:
        return "degraded", "lag"
    speed = st["speed_mbit"]
    if speed is not None:
        usual = usual_speed(st)
        if speed < ex["speed_warn_mbit"]:
            return "degraded", "speed"
        if usual and speed < usual / 2:
            return "degraded", "drop"
    return "up", ""


def parse_ping(text):
    """(loss %, min, avg, max) from ping's summary; RTTs are None when nothing came back."""
    m = _LOSS.search(text)
    loss = float(m.group(1)) if m else 100.0
    r = _RTT.search(text)
    return (loss, float(r.group(1)), float(r.group(2)), float(r.group(3))) if r else (loss, None, None, None)


def ping(target, count):
    env = {**os.environ, "LC_ALL": "C"}
    try:
        res = subprocess.run(["ping", "-n", "-q", "-c", str(count), "-i", "0.2", "-W", "1", "--", target],
                             capture_output=True, text=True, timeout=count + 5, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("ping %s failed: %s", target, e)
        return 100.0, None, None, None
    return parse_ping(res.stdout)


def classify(node, loss, avg, fails, rtt_warn):
    """State for one finished check, given how many full-loss checks came in a row."""
    if loss >= 100:
        if fails < FAILS_FOR_DOWN:
            return "degraded"
        return "idle" if node["optional"] else "down"
    return "degraded" if loss > 0 or (avg is not None and avg > rtt_warn) else "up"


class KeeneticApi:
    """Read-only client for the router's web API; keeps one session and logs in again when it expires."""

    def __init__(self, base, user, password):
        self.base, self.user, self.password = base, user, password
        # Direct on purpose: a proxy in the environment must not see router traffic or its login.
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                                  urllib.request.ProxyHandler({}))

    def _login(self):
        try:
            self.opener.open(f"{self.base}/auth", timeout=8)
            return
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            realm, challenge = e.headers["X-NDM-Realm"], e.headers["X-NDM-Challenge"]
        inner = hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest()
        body = json.dumps({"login": self.user, "password": hashlib.sha256((challenge + inner).encode()).hexdigest()}).encode()
        req = urllib.request.Request(f"{self.base}/auth", body, {"Content-Type": "application/json"})
        self.opener.open(req, timeout=8)

    def get(self, path):
        url = f"{self.base}/rci/{path}"
        try:
            return json.load(self.opener.open(url, timeout=8))
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
        self._login()
        return json.load(self.opener.open(url, timeout=8))


def read_credentials(path):
    try:
        user, _, password = Path(path).expanduser().read_text().strip().partition(":")
    except OSError:
        return None
    return (user, password) if user and password else None


def tunnel_status(node, interfaces, sessions, handshake_max):
    """What the router at the far end reports about this node's tunnel, or None if it says nothing."""
    off = "idle" if node["optional"] else "down"
    if node["tunnel_if"]:
        peers = ((interfaces.get(node["tunnel_if"]) or {}).get("wireguard") or {}).get("peer") or []
        if not peers:
            return None
        peer = peers[0]
        age = peer.get("last-handshake")
        fresh = bool(peer.get("online")) and isinstance(age, (int, float)) and age <= handshake_max
        return {"state": "up" if fresh else off, "handshake_age": age, "rx": peer.get("rxbytes"), "tx": peer.get("txbytes")}
    if node["tunnel_user"]:
        session = next((t for t in sessions if t.get("username") == node["tunnel_user"]), None)
        if session is None:
            return {"state": off, "session": False}
        stat = session.get("statistic") or {}
        return {"state": "up", "session": True, "uptime": session.get("uptime"), "rx": stat.get("rxbytes"), "tx": stat.get("txbytes")}
    return None


def router_status(system):
    total, free = system.get("memtotal"), system.get("memfree")
    used = None
    if total and free is not None:
        used = round(100 * (total - free - (system.get("memcache") or 0) - (system.get("membuffers") or 0)) / total)
    try:
        uptime = int(system.get("uptime"))
    except (TypeError, ValueError):
        uptime = None
    return {"cpu": system.get("cpuload"), "mem": used, "uptime": uptime}


class TopologyMonitor:
    def __init__(self, cfg, probe=ping, apis=None, curl=exit_curl):
        self.cfg = {**TOPOLOGY_DEFAULTS, **cfg}
        self.nodes = clean_nodes(self.cfg["nodes"])
        self.exits = clean_exits(self.cfg["exits"])
        self.curl = curl
        self.exit_status = {e["id"]: {"state": "unknown", "reason": "", "fails": 0, "lag_ms": None, "speed_mbit": None,
                                      "speeds": [], "speed_ts": 0, "geo": None, "geo_ts": 0, "checked": 0, "since": None, "active": None, "last_ok": None}
                            for e in self.exits}
        self.probe = probe
        self.lock = threading.Lock()
        self.ts = 0
        self.apis = apis if apis is not None else self._make_apis()
        self.tunnels, self.routers = {}, {}
        self.api_ts = 0
        self._counters = {}
        self.status = {n["id"]: {"state": "unknown", "since": None, "fails": 0, "loss": None,
                                 "rtt_min": None, "rtt_avg": None, "rtt_max": None, "last_ok": None}
                       for n in self.nodes}
        self._load_speeds()

    def _state_path(self):
        return Path(self.cfg["state_file"]).expanduser() if self.cfg["state_file"] else None

    def _load_speeds(self):
        path = self._state_path()
        try:
            saved = json.loads(path.read_text()) if path else {}
        except (OSError, ValueError):
            return
        for eid, s in saved.items():
            if eid in self.exit_status and isinstance(s, dict):
                speeds = [float(x) for x in s.get("speeds", []) if isinstance(x, (int, float))][-24:]
                self.exit_status[eid].update(speeds=speeds, speed_mbit=speeds[-1] if speeds else None,
                                             speed_ts=int(s.get("speed_ts", 0)))
                if isinstance(s.get("last_ok"), (int, float)):
                    self.exit_status[eid]["last_ok"] = int(s["last_ok"])

    def _save_speeds(self):
        path = self._state_path()
        if not path:
            return
        data = {eid: {k: st[k] for k in ("speeds", "speed_ts", "last_ok")} for eid, st in self.exit_status.items()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(path)
        except OSError as e:
            log.warning("topology: cannot save %s: %s", path, e)

    def check_once(self):
        targets = [n for n in self.nodes if n["ping"]]
        count = int(self.cfg["count"])
        if targets:
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                results = list(pool.map(lambda n: self.probe(n["ping"], count), targets))
        else:
            results = []
        now = int(time.time())
        with self.lock:
            for node, (loss, low, avg, high) in zip(targets, results):
                st = self.status[node["id"]]
                st["fails"] = st["fails"] + 1 if loss >= 100 else 0
                state = classify(node, loss, avg, st["fails"], self.cfg["rtt_warn_ms"])
                if state != st["state"]:
                    st["since"] = now
                st.update(state=state, loss=loss, rtt_min=low, rtt_avg=avg, rtt_max=high)
                if loss < 100:
                    st["last_ok"] = now
            self.ts = now

    def _make_apis(self):
        wanted = [n for n in self.nodes if n["api"]]
        if not wanted:
            return {}
        cred = read_credentials(self.cfg["credentials"])
        if cred is None:
            log.warning("topology: no credentials in %s, routers will not be read", self.cfg["credentials"])
            return {}
        return {n["id"]: KeeneticApi(n["api"], *cred) for n in wanted}

    def read_routers(self):
        """One API round: load of every router, tunnels of the children of each router."""
        routers, tunnels = {}, {}
        now = time.time()
        for node in self.nodes:
            api = self.apis.get(node["id"])
            if api is None:
                continue
            try:
                routers[node["id"]] = router_status(api.get("show/system"))
                children = [c for c in self.nodes if c["parent"] == node["id"] and (c["tunnel_if"] or c["tunnel_user"])]
                if not children:
                    continue
                interfaces = api.get("show/interface") if any(c["tunnel_if"] for c in children) else {}
                sessions = []
                if any(c["tunnel_user"] for c in children):
                    sessions = api.get("show/sstp-server").get("tunnel") or []
                for child in children:
                    status = tunnel_status(child, interfaces, sessions, self.cfg["handshake_max"])
                    if status:
                        tunnels[child["id"]] = self._add_rates(child["id"], status, now)
            except Exception as e:  # a router being down is a normal state here, not a bug
                log.info("topology: cannot read router %s: %s", node["id"], type(e).__name__)
        with self.lock:
            self.routers, self.tunnels = routers, tunnels
            self.api_ts = int(now)

    def _add_rates(self, nid, status, now):
        rx, tx = status.get("rx"), status.get("tx")
        prev = self._counters.get(nid)
        if isinstance(rx, int) and isinstance(tx, int):
            self._counters[nid] = (now, rx, tx)
            if prev and now > prev[0] and rx >= prev[1] and tx >= prev[2]:
                status["rx_bps"] = round((rx - prev[1]) * 8 / (now - prev[0]))
                status["tx_bps"] = round((tx - prev[2]) * 8 / (now - prev[0]))
        return status

    def check_exit(self, ex, now):
        """One round for one exit: the check, and the location when it is due."""
        st = self.exit_status[ex["id"]]
        code, ttfb, _, _ = parse_curl(self.curl(ex, ex["check"], 15))
        updates = {"checked": int(now), "fails": 0 if code else st["fails"] + 1, "lag_ms": round(ttfb * 1000) if code else None}
        if code:
            updates["last_ok"] = int(now)
        if ex["active_via"]:
            updates["active"] = self._is_active(ex)
        # An exit applied by routing only covers its own domains, so a lookup would show this machine instead.
        if code and (ex["proxy"] or ex["probe"]) and now - st["geo_ts"] >= self.cfg["geo_interval"]:
            try:
                info = json.loads(self.curl(ex, self.cfg["geo_url"], 15, write="") or "")
                updates.update(geo={k: info.get(k) for k in ("ip", "city", "country", "org")}, geo_ts=int(now))
            except ValueError:
                pass  # no location this round: keep the last one
        with self.lock:
            st.update(updates)
            self._update_exit_state(ex, st, now)

    def _is_active(self, ex):
        """True if the shared port leaves through this exit, None if either IP could not be read."""
        ours = (self.curl(ex, self.cfg["ip_url"], 12, write="") or "").strip()
        shared = (self.curl({**ex, "proxy": ex["active_via"]}, self.cfg["ip_url"], 12, write="") or "").strip()
        return ours == shared if ours and shared else None

    def _update_exit_state(self, ex, st, now):
        state, reason = exit_state(ex, st)
        if state != st["state"]:
            st["since"] = int(now)
        st.update(state=state, reason=reason)

    def measure_speed(self, ex, now):
        # Several connections at once, like speed test sites: one connection through a VPN is often capped.
        with ThreadPoolExecutor(max_workers=ex["speed_streams"]) as pool:
            samples = list(pool.map(lambda _: parse_curl(self.curl(ex, ex["speed"], 25)), range(ex["speed_streams"])))
        speed = sum(mbit for got, _, mbit, size in samples if got and size)
        with self.lock:
            st = self.exit_status[ex["id"]]
            st.update(speed_mbit=speed, speed_ts=int(now), speeds=(st["speeds"] + [speed])[-24:])
            if speed > 0:
                st["last_ok"] = max(st["last_ok"] or 0, int(now))
            self._update_exit_state(ex, st, now)
            self._save_speeds()

    def check_exits(self):
        if not self.exits:
            return
        now = time.time()
        with ThreadPoolExecutor(max_workers=len(self.exits)) as pool:
            list(pool.map(lambda ex: self.check_exit(ex, now), self.exits))
        with self.lock:
            self._save_speeds()  # keeps the last answer across restarts
        # One at a time: exits often share an uplink, and parallel downloads would measure each other.
        for ex in self.exits:
            st = self.exit_status[ex["id"]]
            if ex["speed"] and not st["fails"] and time.time() - st["speed_ts"] >= self.cfg["speed_interval"]:
                self.measure_speed(ex, time.time())

    def snapshot(self):
        by_id = {n["id"]: n for n in self.nodes}
        now = int(time.time())
        fresh_for = 3 * int(self.cfg["exit_interval"])
        with self.lock:
            nodes = []
            for n in self.nodes:
                st = self.status[n["id"]]
                state = st["state"]
                parent = by_id.get(n["parent"])
                # Behind a dead parent a failed ping says nothing about the node itself.
                blocked = bool(parent) and self.status[parent["id"]]["state"] == "down" and state in ("down", "idle", "degraded")
                tunnel = self.tunnels.get(n["id"])
                node_state = "unknown" if blocked else state
                # Router addresses and SSTP logins stay on the server: the page does not need them.
                public = {k: v for k, v in n.items() if k not in ("api", "tunnel_if", "tunnel_user")}
                nodes.append({**public, **{k: v for k, v in st.items() if k != "fails"},
                              "state": node_state, "blocked": blocked, "tunnel": tunnel, "router": self.routers.get(n["id"]),
                              "link_state": tunnel["state"] if tunnel else node_state})
            return {"ts": self.ts, "api_ts": self.api_ts, "now": int(time.time()),
                    "interval": int(self.cfg["interval"]), "api_interval": int(self.cfg["api_interval"]), "nodes": nodes,
                    # Proxy addresses, ssh hosts and check urls stay on the server.
                    "exits": [self._public_exit(e, now, fresh_for) for e in self.exits]}

    def _public_exit(self, ex, now, fresh_for):
        raw = self.exit_status[ex["id"]]
        st = {k: v for k, v in raw.items() if k not in ("fails", "speeds", "geo_ts")}
        st["speed_usual"] = usual_speed(raw)
        if raw["fails"]:  # an old sample is not the speed of a channel that does not answer now
            st["speed_mbit"] = None
        if st["checked"] and now - st["checked"] > fresh_for:  # the checks stopped: do not keep showing the last state
            st.update(state="unknown", reason="")
        lag, speed = st["lag_ms"], st["speed_mbit"]
        return {"id": ex["id"], "title": ex["title"], "at": ex["at"], "backup": ex["backup"], "purpose": ex["purpose"], **st,
                # The last time the link worked: a check that answered or a speed test that got data.
                "last_good": max(raw["last_ok"] or 0, raw["speed_ts"] if raw["speeds"] and raw["speeds"][-1] > 0 else 0) or None,
                "lag_bad": lag is not None and lag > ex["lag_warn_ms"],
                "speed_bad": speed is not None and speed < ex["speed_warn_mbit"]}

    def _loop(self):
        while True:
            started = time.monotonic()
            try:
                self.check_once()
            except Exception:
                log.exception("topology check failed")
            time.sleep(max(1.0, float(self.cfg["interval"]) - (time.monotonic() - started)))

    def _api_loop(self):
        while True:
            try:
                self.read_routers()
            except Exception:
                log.exception("topology router read failed")
            time.sleep(max(5.0, float(self.cfg["api_interval"])))

    def _exit_loop(self):
        while True:
            started = time.monotonic()
            try:
                self.check_exits()
            except Exception:
                log.exception("topology exit check failed")
            time.sleep(max(5.0, float(self.cfg["exit_interval"]) - (time.monotonic() - started)))

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        if self.exits:
            threading.Thread(target=self._exit_loop, daemon=True).start()
        if self.apis:
            threading.Thread(target=self._api_loop, daemon=True).start()


DEMO_NODES = [
    {"id": "hub", "title": "Main office router", "subtitle": "192.0.2.1", "group": "Hub", "ping": "hub"},
    {"id": "vps", "title": "Cloud server", "subtitle": "192.0.2.10", "group": "Server", "ping": "vps", "parent": "hub", "link": "WireGuard"},
    {"id": "branch", "title": "Branch router", "subtitle": "192.0.2.20", "group": "Site", "ping": "branch", "parent": "hub", "link": "WireGuard"},
    {"id": "cabin", "title": "Cabin router", "subtitle": "192.0.2.30", "group": "Site", "ping": "cabin", "parent": "hub", "link": "LTE"},
    {"id": "phone", "title": "Phone", "subtitle": "not probed", "group": "Client", "parent": "hub", "link": "WireGuard"},
    {"id": "laptop", "title": "Laptop", "subtitle": "192.0.2.40", "group": "Client", "ping": "laptop", "parent": "hub", "link": "WireGuard", "optional": True},
]
_DEMO_RESULTS = {"hub": (0.0, 0.4, 0.6, 0.9), "vps": (0.0, 41.0, 44.0, 52.0), "branch": (0.0, 7.0, 8.0, 12.0),
                 "cabin": (33.3, 60.0, 80.0, 140.0), "laptop": (100.0, None, None, None)}


DEMO_EXITS = [
    {"id": "vpn1", "title": "VPN one", "at": "vps", "check": "https://example.com/"},
    {"id": "vpn2", "title": "VPN two", "at": "vps", "check": "https://example.com/"},
    {"id": "hub-vpn", "title": "Proxy", "at": "hub", "check": "https://example.com/"},
]
_DEMO_EXITS = {
    "vpn1": {"state": "up", "lag_ms": 210, "speed_mbit": 61.0, "geo": {"city": "Frankfurt", "country": "DE", "org": "Example Net"}},
    "vpn2": {"state": "degraded", "reason": "speed", "lag_ms": 700, "speed_mbit": 0.6, "geo": {"city": "Stockholm", "country": "SE", "org": "Example Net"}},
    "hub-vpn": {"state": "up", "lag_ms": 320, "speed_mbit": 18.0, "geo": {"city": "Warsaw", "country": "PL", "org": "Example Net"}},
}


def make_demo(cfg):
    """A monitor over made-up nodes and results; never touches the network."""
    mon = TopologyMonitor({**cfg, "enabled": True, "nodes": DEMO_NODES, "exits": DEMO_EXITS},
                          probe=lambda target, count: _DEMO_RESULTS[target], apis={},
                          curl=lambda ex, url, seconds, write="x": None)
    for eid, extra in _DEMO_EXITS.items():
        mon.exit_status[eid].update(checked=int(time.time()), **extra)
    for _ in range(FAILS_FOR_DOWN):
        mon.check_once()
    mon.routers = {"hub": {"cpu": 12, "mem": 41, "uptime": 950000}, "branch": {"cpu": 38, "mem": 55, "uptime": 86400}}
    mon.tunnels = {"vps": {"state": "up", "handshake_age": 40, "rx_bps": 12000, "tx_bps": 9000},
                   "branch": {"state": "up", "handshake_age": 75, "rx_bps": 2300000, "tx_bps": 80000},
                   "laptop": {"state": "idle", "session": False}}
    mon.api_ts = int(time.time())
    return mon
