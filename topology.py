"""Network topology with live reachability — optional hwdash module.

The [topology] section describes a tree of nodes: one root (usually the router
that terminates the tunnels) and everything connected through it. A background
thread pings each node from this machine, so a node's state means "reachable
from here": it covers the path and the node together.

Optionally, Keenetic/Netcraze routers with an `api` address are read over their
web API, with GET requests only: tunnel state for the links (the link's colour then
shows what the tunnel reports, not the ping) and CPU, memory and uptime for the node.
"""
import hashlib
import http.cookiejar
import json
import logging
import os
import re
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
}
NODE_FIELDS = {"id", "title", "subtitle", "group", "ping", "parent", "link", "optional",
               "api", "tunnel_if", "tunnel_user"}
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
    def __init__(self, cfg, probe=ping, apis=None):
        self.cfg = {**TOPOLOGY_DEFAULTS, **cfg}
        self.nodes = clean_nodes(self.cfg["nodes"])
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

    def snapshot(self):
        by_id = {n["id"]: n for n in self.nodes}
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
                    "interval": int(self.cfg["interval"]), "api_interval": int(self.cfg["api_interval"]), "nodes": nodes}

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

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
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


def make_demo(cfg):
    """A monitor over made-up nodes and results; never touches the network."""
    mon = TopologyMonitor({**cfg, "enabled": True, "nodes": DEMO_NODES}, probe=lambda target, count: _DEMO_RESULTS[target], apis={})
    for _ in range(FAILS_FOR_DOWN):
        mon.check_once()
    mon.routers = {"hub": {"cpu": 12, "mem": 41, "uptime": 950000}, "branch": {"cpu": 38, "mem": 55, "uptime": 86400}}
    mon.tunnels = {"vps": {"state": "up", "handshake_age": 40, "rx_bps": 12000, "tx_bps": 9000},
                   "branch": {"state": "up", "handshake_age": 75, "rx_bps": 2300000, "tx_bps": 80000},
                   "laptop": {"state": "idle", "session": False}}
    mon.api_ts = int(time.time())
    return mon
