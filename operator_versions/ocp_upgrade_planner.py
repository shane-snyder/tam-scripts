#!/usr/bin/env python3
"""
OpenShift Upgrade Planner (operator focused)

Reads the cluster version and installed operators from the cluster you are
logged into and, for a target OpenShift version, reports:

  1. The OCP update path (every intermediate release) from the OpenShift
     update graph (Cincinnati) API.
  2. Whether each installed operator is supported on the current OCP version,
     and the exact route to the newest version supported on it.
  3. A support matrix of installed operator versions across every OCP minor
     between current and target.
  4. A chronological runbook: which exact operator versions to install, in
     which channel, and when (before/after each OCP update), including the
     exact sequence of InstallPlans OLM will create (replaces / skips /
     skipRange edges, channel switches).

Data sources (no authentication required):
  - OpenShift update graph:          api.openshift.com
  - Red Hat Product Life Cycle API:  access.redhat.com   (support / OCP compatibility)
  - Red Hat Ecosystem Catalog API:   catalog.redhat.com  (operator index contents per OCP version)

Usage:
    python ocp_upgrade_planner.py --target 4.22.18
    python ocp_upgrade_planner.py --target 4.22 --html plan.html
    python ocp_upgrade_planner.py --target 4.22 --from 4.20.10
    python ocp_upgrade_planner.py --target 4.22 --must-gather ./must-gather.local.1234/
    python ocp_upgrade_planner.py --target 4.22 --simulate-operator odf-operator=4.20.5-rhodf --only-simulated

Requirements:
    - oc CLI authenticated to a cluster, or a must-gather (--must-gather)
    - requests (and pyyaml for --must-gather): pip install -r requirements.txt
"""

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' package required. Install with: pip install requests")
    sys.exit(1)

LIFECYCLE_API_URL = "https://access.redhat.com/product-life-cycles/api/v1/products"
GRAPH_API_URL = "https://api.openshift.com/api/upgrades_info/v1/graph"
# Red Hat Ecosystem Catalog: the contents of every operator index image per
# OCP version (channels, versions, replaces/skips/skipRange). No auth needed.
CATALOG_API_URL = "https://catalog.redhat.com/api/containers/v1/operators/bundles"
CATALOG_PAGE_SIZE = 500
# CatalogSource image name -> Ecosystem Catalog organization. Matching on the
# image name also covers mirrored catalogs (mirror.example.com/redhat/redhat-operator-index:v4.21).
INDEX_ORGS = {
    "redhat-operator-index": "redhat-operators",
    "certified-operator-index": "certified-operators",
    "community-operator-index": "community-operators",
    "redhat-marketplace-index": "redhat-marketplace",
}
CACHE_DIR = Path.home() / ".cache" / "ocp-upgrade-planner"
CACHE_TTL_HOURS = 24

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"

USE_COLOR = True


def c(text, color):
    return f"{color}{text}{RESET}" if USE_COLOR and color else str(text)


def log(msg):
    print(c(msg, DIM), file=sys.stderr)


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def vkey(version):
    """Numeric sort key for dotted versions; ignores build suffixes ('4.21.0-2026...')."""
    nums = re.findall(r"\d+", version.split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums)


def _ident_key(parts):
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts.split(".")) if parts else ()


def semkey(version):
    """Semver sort key: prereleases ('4.21.0-2026...', '2.19.0-4') sort before the release.

    Build metadata ('2.7.0+0.1784907685') is a final tiebreaker so that rebuilds
    of the same version still order deterministically.
    """
    main, _, build = version.lstrip("v").partition("+")
    core, _, pre = main.partition("-")
    nums = tuple((list(map(int, re.findall(r"\d+", core))) + [0, 0, 0])[:3])
    return (nums, 0 if pre else 1, _ident_key(pre), _ident_key(build))


def in_semver_range(version, rng):
    """Evaluate an OLM skipRange such as '>=4.2.0 <4.22.0' or '>=1.0.0 <1.2.0 || >=2.0.0'."""
    ops = {
        ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b, "<": lambda a, b: a < b,
        "=": lambda a, b: a == b, "==": lambda a, b: a == b,
    }
    v = semkey(version)[:3]  # ranges ignore build metadata
    for alternative in rng.split("||"):
        comparators = re.findall(r"(>=|<=|==|>|<|=)?\s*v?(\d[^\s]*)", alternative)
        if comparators and all(ops[op or "="](v, semkey(ver)[:3]) for op, ver in comparators):
            return True
    return False


def minor_of(version):
    """'4.21.35' -> '4.21'; '1.4.9-1' -> '1.4'."""
    parts = vkey(version)
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else version


def minor_num(ocp_minor):
    return vkey(ocp_minor)[1]


def ocp_minor_range(start_minor, end_minor):
    major = vkey(start_minor)[0]
    return [f"{major}.{m}" for m in range(minor_num(start_minor), minor_num(end_minor) + 1)]


def stream_match(version, stream):
    """True if '4.22.0-2026...' belongs to stream '4.22' (or '1.5.8' to '1.5')."""
    want = vkey(stream)[:2]
    return vkey(version)[: len(want)] == want


def summarize(ocp_list):
    if not ocp_list:
        return "unknown"
    s = sorted(ocp_list, key=vkey)
    return s[0] if len(s) == 1 else f"{s[0]}-{s[-1]}"


# ---------------------------------------------------------------------------
# Cached HTTP
# ---------------------------------------------------------------------------

def cached(cache_name, fetch, force_refresh=False):
    """Return fetch() output, cached on disk for CACHE_TTL_HOURS."""
    cache_file = CACHE_DIR / cache_name
    if not force_refresh and cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_hours < CACHE_TTL_HOURS:
            with open(cache_file) as f:
                return json.load(f)
    try:
        data = fetch()
    except requests.RequestException as e:
        if cache_file.exists():
            log(f"Warning: fetch for {cache_name} failed ({e}); using stale cache.")
            with open(cache_file) as f:
                return json.load(f)
        raise
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(data, f)
    return data


def cached_get_json(url, cache_name, params=None, force_refresh=False, headers=None):
    def fetch():
        resp = requests.get(url, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.json()

    return cached(cache_name, fetch, force_refresh)


def fetch_lifecycle_products(force_refresh=False):
    log("Fetching Red Hat product lifecycle data...")
    data = cached_get_json(LIFECYCLE_API_URL, "products.json", force_refresh=force_refresh)
    return [p for p in data.get("data", []) if p.get("is_operator")]


def fetch_graph(channel, arch, force_refresh=False):
    """Fetch one update channel. Returns None if the channel does not exist."""
    try:
        return cached_get_json(
            GRAPH_API_URL,
            f"graph-{channel}-{arch}.json",
            params={"channel": channel, "arch": arch},
            headers={"Accept": "application/json"},
            force_refresh=force_refresh,
        )
    except requests.RequestException:
        return None


def fetch_catalog(org, ocp_minor, packages, force_refresh=False):
    """Bundles for `packages` in the `org` index image for one OCP minor.

    Returns {package: {"default": channel, "channels": {channel: [bundle, ...]}}}
    with each channel's bundles sorted oldest -> newest.
    """
    pkgs = sorted(set(packages))
    digest = hashlib.sha1(",".join(pkgs).encode()).hexdigest()[:10]
    fields = ["package", "channel_name", "version", "csv_name", "replaces", "skips",
              "skip_range", "is_default_channel"]

    def fetch():
        rows, page = [], 0
        while True:
            resp = requests.get(
                CATALOG_API_URL,
                params={
                    "filter": f"package=in=({','.join(pkgs)});ocp_version=={ocp_minor};organization=={org}",
                    "page_size": CATALOG_PAGE_SIZE,
                    "page": page,
                    "include": ",".join(["total"] + [f"data.{f}" for f in fields]),
                },
                timeout=60,
            )
            resp.raise_for_status()
            body = resp.json()
            rows.extend(body.get("data", []))
            page += 1
            if len(rows) >= body.get("total", 0) or not body.get("data"):
                return rows

    rows = cached(f"bundles-{org}-{ocp_minor}-{digest}.json", fetch, force_refresh)

    catalog, seen = {}, set()
    for b in rows:
        key = (b["package"], b["channel_name"], b["csv_name"])
        if key in seen:
            continue
        seen.add(key)
        pkg = catalog.setdefault(b["package"], {"default": "", "channels": {}})
        if b.get("is_default_channel"):
            pkg["default"] = b["channel_name"]
        pkg["channels"].setdefault(b["channel_name"], []).append(
            {
                "version": b["version"],
                "csv": b["csv_name"],
                "replaces": b.get("replaces"),
                "skips": b.get("skips") or [],
                "skip_range": b.get("skip_range"),
            }
        )
    for pkg in catalog.values():
        for bundles in pkg["channels"].values():
            bundles.sort(key=lambda x: semkey(x["version"]))
    return catalog


def fetch_catalogs(operators, minors, catalog_orgs, force_refresh=False):
    """Returns {(org, minor): catalog} for every org in use and every minor."""
    wanted = {}
    for op in operators:
        org = catalog_orgs.get((op["source_namespace"], op["source"]))
        if org and op["package"]:
            wanted.setdefault(org, set()).add(op["package"])
    jobs = [(org, m, pkgs) for org, pkgs in wanted.items() for m in minors]
    if not jobs:
        return {}
    log(f"Fetching operator catalog contents for {', '.join(minors)} from the Red Hat Ecosystem Catalog...")

    def run(job):
        org, m, pkgs = job
        try:
            return (org, m), fetch_catalog(org, m, pkgs, force_refresh)
        except requests.RequestException as e:
            log(f"Warning: catalog lookup failed for {org} {m}: {e}")
            return (org, m), None

    with ThreadPoolExecutor(max_workers=6) as pool:
        return {k: v for k, v in pool.map(run, jobs) if v is not None}


# ---------------------------------------------------------------------------
# Cluster data
# ---------------------------------------------------------------------------

def run_oc(*args):
    cmd = ["oc"] + list(args) + ["-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


# kind -> (API group as must-gather names its directory, namespaced)
KINDS = {
    "clusterversions": ("config.openshift.io", False),
    "clusteroperators": ("config.openshift.io", False),
    "nodes": ("core", False),
    "subscriptions": ("operators.coreos.com", True),
    "clusterserviceversions": ("operators.coreos.com", True),
    "installplans": ("operators.coreos.com", True),
    "catalogsources": ("operators.coreos.com", True),
}

INSPECT_HINT = (
    "oc adm inspect clusterversion/version clusteroperator/operator-lifecycle-manager --dest-dir=<dir> && "
    "oc adm inspect subscriptions.operators.coreos.com,clusterserviceversions.operators.coreos.com,"
    "installplans.operators.coreos.com,catalogsources.operators.coreos.com -A --dest-dir=<dir>"
)


class OcSource:
    """Cluster objects from the cluster `oc` is logged in to."""

    live = True
    label = "live cluster (oc)"

    def __init__(self):
        self._csvs = None

    def list(self, kind):
        group, namespaced = KINDS[kind]
        resource = kind if group == "core" else f"{kind}.{group}"
        return run_oc("get", resource, *(["-A"] if namespaced else []))["items"]

    def get(self, kind, name, namespace=None):
        group, _ = KINDS[kind]
        resource = kind if group == "core" else f"{kind}.{group}"
        try:
            return run_oc("get", resource, name, *(["-n", namespace] if namespace else []))
        except RuntimeError:
            return None

    def csv(self, namespace, name):
        if self._csvs is None:
            self._csvs = {(x["metadata"]["namespace"], x["metadata"]["name"]): x
                          for x in self.list("clusterserviceversions")}
        return self._csvs.get((namespace, name))


class MustGatherSource:
    """The same objects read from a must-gather or `oc adm inspect` output.

    Accepts the directory, a directory containing the per-image gather
    directories, or a .tar / .tar.gz / .tgz of either.
    """

    live = False

    def __init__(self, path):
        try:
            import yaml
        except ImportError:
            raise RuntimeError("Reading a must-gather needs PyYAML: pip install pyyaml")
        self.yaml = yaml
        self.loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        path = Path(path).expanduser()
        if not path.exists():
            raise RuntimeError(f"{path} does not exist")
        if path.is_file():
            path = self._extract(path)
        candidates = [path] + sorted(p for p in path.iterdir() if p.is_dir())
        candidates += [q for p in candidates[1:] for q in sorted(p.iterdir()) if q.is_dir()]
        self.roots = [d for d in candidates
                      if (d / "namespaces").is_dir() or (d / "cluster-scoped-resources").is_dir()]
        if not self.roots:
            raise RuntimeError(f"No must-gather data (namespaces/ or cluster-scoped-resources/) under {path}")
        self.label = f"must-gather {path.name}"
        for d in [path] + self.roots:
            ts = d / "timestamp"
            if ts.is_file():
                first = ts.read_text().strip().splitlines()[:1]
                if first:
                    self.label += f" (collected {first[0][:19]})"
                break

    @staticmethod
    def _extract(archive):
        import atexit
        import shutil
        import tarfile
        import tempfile

        dest = Path(tempfile.mkdtemp(prefix="ocp-upgrade-planner-mg-"))
        atexit.register(shutil.rmtree, dest, ignore_errors=True)
        log(f"Extracting {archive.name}...")
        with tarfile.open(archive) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(dest, filter="data")
            else:
                safe = [m for m in tar.getmembers()
                        if (m.isfile() or m.isdir()) and not m.name.startswith("/") and ".." not in Path(m.name).parts]
                tar.extractall(dest, members=safe)
        # Name the source after the archive, not the temp directory.
        top = [p for p in dest.iterdir() if p.is_dir()]
        return top[0] if len(top) == 1 else dest

    def _load(self, f):
        try:
            with open(f) as fh:
                doc = self.yaml.load(fh, Loader=self.loader)
        except (OSError, self.yaml.YAMLError):
            return []
        if not isinstance(doc, dict):
            return []
        return (doc.get("items") or []) if "items" in doc else [doc]

    def _files(self, kind, namespace="*", name="*"):
        group, namespaced = KINDS[kind]
        base = f"namespaces/{namespace}/{group}" if namespaced else f"cluster-scoped-resources/{group}"
        for root in self.roots:
            yield from root.glob(f"{base}/{kind}/{name}.yaml")
            if name == "*":
                yield from root.glob(f"{base}/{kind}.yaml")

    def list(self, kind):
        seen, out = set(), []
        for f in self._files(kind):
            for obj in self._load(f):
                meta = obj.get("metadata", {})
                key = (meta.get("namespace"), meta.get("name"))
                if key not in seen:
                    seen.add(key)
                    out.append(obj)
        return out

    def get(self, kind, name, namespace=None):
        for f in self._files(kind, namespace or "*", name):
            for obj in self._load(f):
                if obj.get("metadata", {}).get("name") == name:
                    return obj
        # Some gathers store a whole list in one <kind>.yaml file instead.
        group, namespaced = KINDS[kind]
        base = f"namespaces/{namespace or '*'}/{group}" if namespaced else f"cluster-scoped-resources/{group}"
        for root in self.roots:
            for f in root.glob(f"{base}/{kind}.yaml"):
                for obj in self._load(f):
                    meta = obj.get("metadata", {})
                    if meta.get("name") == name and (namespace is None or meta.get("namespace") == namespace):
                        return obj
        return None

    def csv(self, namespace, name):
        # Load just the one CSV: AllNamespaces operators have a copy in every namespace.
        return self.get("clusterserviceversions", name, namespace) if name else None


def get_cluster_info(src):
    cv = src.get("clusterversions", "version")
    if not cv:
        raise RuntimeError("ClusterVersion 'version' not found.")
    version = cv["status"]["desired"]["version"]
    channel = cv["spec"].get("channel", "")
    # desired.architecture is only set ("Multi") for multi-arch payloads.
    arch = (cv["status"].get("desired", {}).get("architecture") or "").lower()
    if not arch:
        try:
            arch = src.list("nodes")[0]["status"]["nodeInfo"]["architecture"]
        except (RuntimeError, IndexError, KeyError):
            arch = "amd64"
            log("Architecture not found; assuming amd64 (override with --arch).")
    conditions = {x["type"]: x for x in cv["status"].get("conditions", [])}
    return {"version": version, "channel": channel, "arch": arch, "conditions": conditions}


def parse_max_ocp(csv):
    """Return olm.maxOpenShiftVersion from a CSV's olm.properties, if set."""
    raw = csv["metadata"].get("annotations", {}).get("olm.properties")
    if not raw:
        return None
    try:
        for prop in json.loads(raw):
            if prop.get("type") == "olm.maxOpenShiftVersion":
                return str(prop.get("value"))
    except (ValueError, TypeError):
        pass
    return None


def get_installed_operators(src):
    """Return one entry per Subscription, enriched with its installed CSV."""
    subs = src.list("subscriptions")
    if not subs and not src.live:
        log("Warning: no Subscriptions in the must-gather. Collect them with:\n  " + INSPECT_HINT)

    operators = []
    for sub in subs:
        ns = sub["metadata"]["namespace"]
        spec = sub.get("spec", {})
        status = sub.get("status", {})
        installed = status.get("installedCSV") or ""
        csv = src.csv(ns, installed)
        operators.append(
            {
                "namespace": ns,
                "package": spec.get("name", ""),
                "channel": spec.get("channel", ""),
                "source": spec.get("source", ""),
                "source_namespace": spec.get("sourceNamespace", "openshift-marketplace"),
                "approval": spec.get("installPlanApproval", "Automatic"),
                "installed_csv": installed,
                "display_name": csv["spec"].get("displayName", "") if csv else "",
                "version": csv["spec"].get("version", "") if csv else "",
                "csv_phase": csv.get("status", {}).get("phase", "") if csv else "",
                "max_ocp": parse_max_ocp(csv) if csv else None,
                "simulated": False,
            }
        )
    return operators


def simulated_operator(spec, source):
    """'pkg=version[:channel]' -> an operator entry that is not on the cluster."""
    if "=" not in spec:
        raise ValueError(f"--simulate-operator expects PKG=VERSION[:CHANNEL], got '{spec}'")
    pkg, rest = spec.split("=", 1)
    version, _, channel = rest.partition(":")
    return {
        "namespace": "(simulated)",
        "package": pkg.strip(),
        "channel": channel.strip(),
        "source": source,
        "source_namespace": "openshift-marketplace",
        "approval": "Manual",
        "installed_csv": "",
        "display_name": "",
        "version": version.strip(),
        "csv_phase": "Succeeded",
        "max_ocp": None,
        "simulated": True,
    }


def get_catalog_orgs(src):
    """Map (namespace, CatalogSource name) -> Ecosystem Catalog organization."""
    try:
        items = src.list("catalogsources")
    except RuntimeError:
        return {}
    orgs = {}
    for cs in items:
        image = cs.get("spec", {}).get("image", "")
        for index_name, org in INDEX_ORGS.items():
            if index_name in image:
                orgs[(cs["metadata"]["namespace"], cs["metadata"]["name"])] = org
    return orgs


def get_catalog_channels(op):
    """Channels for this package in the cluster's own catalog (PackageManifest; live only)."""
    try:
        data = run_oc(
            "get", "packagemanifests", "-n", op["source_namespace"],
            "-l", f"catalog={op['source']}",
            "--field-selector", f"metadata.name={op['package']}",
        )
    except RuntimeError:
        return None
    if not data.get("items"):
        return None
    status = data["items"][0]["status"]
    return {
        "default": status.get("defaultChannel", ""),
        "channels": {
            ch["name"]: ch.get("currentCSVDesc", {}).get("version") or ch.get("currentCSV", "")
            for ch in status.get("channels", [])
        },
    }


def get_olm_upgradeable(src):
    co = src.get("clusteroperators", "operator-lifecycle-manager")
    if not co:
        return None
    for cond in co.get("status", {}).get("conditions", []):
        if cond["type"] == "Upgradeable":
            return cond
    return None


def get_pending_installplans(src):
    try:
        ips = src.list("installplans")
    except RuntimeError:
        return []
    pending, seen = [], set()
    for ip in sorted(ips, key=lambda x: x["metadata"].get("creationTimestamp", ""), reverse=True):
        key = (ip["metadata"]["namespace"], tuple(sorted(ip["spec"].get("clusterServiceVersionNames", []))))
        if key in seen:
            continue
        if not ip["spec"].get("approved") and ip.get("status", {}).get("phase") == "RequiresApproval":
            seen.add(key)
            pending.append(
                {
                    "namespace": ip["metadata"]["namespace"],
                    "name": ip["metadata"]["name"],
                    "csvs": ip["spec"].get("clusterServiceVersionNames", []),
                }
            )
    return pending


# ---------------------------------------------------------------------------
# OCP update path
# ---------------------------------------------------------------------------

def build_update_graph(current, target_minor, arch, force_refresh=False):
    """Union the stable/eus channels covering current..target.

    Returns (edges: dict from -> set(to), conditional: dict (from,to) -> [risks],
    versions: set, channels_used: list).
    """
    edges, conditional, versions, used = {}, {}, set(), []
    minors = ocp_minor_range(minor_of(current), target_minor)
    channels = [f"stable-{m}" for m in minors]
    channels += [f"eus-{m}" for m in minors if minor_num(m) % 2 == 0]

    for ch in channels:
        g = fetch_graph(ch, arch, force_refresh)
        if not g or not g.get("nodes"):
            continue
        used.append(ch)
        nodes = [n["version"] for n in g["nodes"]]
        versions.update(nodes)
        for a, b in g.get("edges", []):
            edges.setdefault(nodes[a], set()).add(nodes[b])
        for ce in g.get("conditionalEdges", []):
            risks = [r.get("name", "") for r in ce.get("risks", [])]
            for e in ce.get("edges", []):
                conditional.setdefault((e["from"], e["to"]), set()).update(risks)
    # A conditional edge may also appear as a plain edge in another channel;
    # only treat it as risky if no unconditional edge exists.
    conditional = {k: sorted(v) for k, v in conditional.items() if k[1] not in edges.get(k[0], set())}
    return edges, conditional, versions, used


def shortest_update_path(edges, conditional, start, goal, allow_conditional):
    """BFS for the fewest hops; ties prefer higher intermediate versions."""
    adj = {k: set(v) for k, v in edges.items()}
    if allow_conditional:
        for (a, b) in conditional:
            adj.setdefault(a, set()).add(b)
    prev = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            break
        for nxt in sorted(adj.get(node, ()), key=vkey, reverse=True):
            if nxt not in prev:
                prev[nxt] = node
                queue.append(nxt)
    if goal not in prev:
        return None
    path, node = [], goal
    while node is not None:
        path.append(node)
        node = prev[node]
    return list(reversed(path))


def reachable_in_minor(edges, conditional, start, minor):
    """Versions in `minor` reachable from start: {'recommended': [...], 'conditional': [...]}."""
    def reach(allow_cond):
        adj = {k: set(v) for k, v in edges.items()}
        if allow_cond:
            for (a, b) in conditional:
                adj.setdefault(a, set()).add(b)
        seen, queue = {start}, deque([start])
        while queue:
            for nxt in adj.get(queue.popleft(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return {v for v in seen if minor_of(v) == minor}

    rec = reach(False)
    cond = reach(True) - rec
    return {"recommended": sorted(rec, key=vkey), "conditional": sorted(cond, key=vkey)}


def resolve_target(target, versions):
    """Expand '4.22' to the newest 4.22.z known to the graph."""
    if target.count(".") >= 2:
        return target
    candidates = [v for v in versions if minor_of(v) == target]
    return max(candidates, key=vkey) if candidates else None


def plan_ocp_path(current, target, arch, allow_conditional, force_refresh=False):
    target_minor = minor_of(target)
    log(f"Fetching OpenShift update graph ({arch})...")
    edges, conditional, versions, channels = build_update_graph(
        current, target_minor, arch, force_refresh
    )
    resolved = resolve_target(target, versions)
    result = {
        "channels_queried": channels,
        "target": resolved or target,
        "path": None,
        "risks": [],
        "error": None,
        "reachable": None,
    }
    if not resolved or resolved not in versions:
        result["error"] = f"Target {target} not found in channels {', '.join(channels) or '(none)'}."
        return result
    if current not in versions:
        result["error"] = f"Current version {current} not found in channels {', '.join(channels)}."
        return result

    path = shortest_update_path(edges, conditional, current, resolved, allow_conditional=False)
    if path is None and allow_conditional:
        path = shortest_update_path(edges, conditional, current, resolved, allow_conditional=True)
    if path is None:
        result["error"] = f"No update path from {current} to {resolved}."
        result["reachable"] = reachable_in_minor(edges, conditional, current, target_minor)
        return result

    result["path"] = path
    for a, b in zip(path, path[1:]):
        if (a, b) in conditional:
            result["risks"].append({"from": a, "to": b, "risks": conditional[(a, b)]})
    return result


def hop_list(path):
    """Minor hops crossed by an OCP path, e.g. [('4.20','4.21'), ('4.21','4.22')]."""
    hops = []
    for a, b in zip(path, path[1:]):
        if minor_of(a) != minor_of(b):
            hops.append((minor_of(a), minor_of(b)))
    return hops


# ---------------------------------------------------------------------------
# Lifecycle data
# ---------------------------------------------------------------------------

def build_index(products):
    index = {}
    for p in products:
        # 'package' may list several packages: "odf-operator,ocs-operator,...".
        for pkg in (p.get("package") or "").split(","):
            if pkg.strip():
                index.setdefault(pkg.strip(), p)
        index[p["name"].lower()] = p
        for former in p.get("former_names", []):
            index[former.lower()] = p
    return index


def match_product(op, index):
    for pkg in (op["package"], re.sub(r"\d+$", "", op["package"])):
        if pkg in index:
            return index[pkg]
    dn = (op["display_name"] or "").strip().lower()
    if not dn:
        return None
    candidates = [dn, f"red hat {dn}"]
    if dn.startswith("red hat "):
        candidates.append(dn[len("red hat "):])
    for suffix in (" operator", " for red hat openshift"):
        if dn.endswith(suffix):
            candidates.append(dn[: -len(suffix)])
    candidates.append(op["package"].replace("-", " ").lower())
    for cand in candidates:
        if cand in index:
            return index[cand]
    return None


def parse_date(s):
    if not s or s == "N/A":
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def phase_end(version_data, phase_name):
    for phase in version_data.get("phases", []):
        if phase["name"].lower() == phase_name.lower():
            return parse_date(phase.get("end_date"))
    return None


def lifecycle_versions(product):
    """Return [{name, ocp:set, type, tier, full_end, maint_end}] sorted ascending."""
    out = []
    for v in product.get("versions", []):
        if not vkey(v["name"]):
            continue
        ocp = {x.strip() for x in (v.get("openshift_compatibility") or "").split(",") if x.strip()}
        full, maint = phase_end(v, "Full support"), phase_end(v, "Maintenance support")
        out.append(
            {
                "name": v["name"].strip(),
                "ocp": ocp,
                "type": v.get("type", ""),
                "tier": v.get("tier", ""),
                "full_end": full.strftime("%Y-%m-%d") if full else None,
                "maint_end": maint.strftime("%Y-%m-%d") if maint else None,
            }
        )
    return sorted(out, key=lambda v: vkey(v["name"]))


def find_lifecycle_version(versions, installed):
    """Match a version ('1.4.9', '4.21.0-2026...', '4.20.5-rhodf') to a lifecycle stream."""
    if not installed:
        return None
    clean = installed.split("-")[0]
    for v in versions:
        if v["name"] == clean:
            return v
    mm = minor_of(clean)
    for v in versions:
        if minor_of(v["name"]) == mm:
            return v
    major = str(vkey(clean)[0])
    for v in versions:
        if v["name"] == major:
            return v
    return None


# ---------------------------------------------------------------------------
# OLM upgrade routing
# ---------------------------------------------------------------------------

def channel_next(bundles, csv, version):
    """The bundle OLM installs next from csv/version when subscribed to this channel.

    OLM picks the newest bundle in the channel that replaces, skips, or has a
    skipRange covering what is installed. Example (GitOps): 1.22.0 has
    skipRange '>=1.0.0 <1.22.0' but 1.22.2 only '>=1.22.0 <1.22.2', so 1.20.x
    must install 1.22.0 first and then 1.22.2.
    """
    newer = [b for b in bundles if semkey(b["version"]) > semkey(version)]
    if not newer:
        return None
    if not any(b["replaces"] or b["skips"] or b["skip_range"] for b in bundles):
        # Semver-template catalogs carry no edge data in the API; assume OLM
        # moves to the latest z of the current minor, then of each next minor.
        same = [b for b in newer if minor_of(b["version"]) == minor_of(version)]
        pool = same or [b for b in newer if minor_of(b["version"]) == minor_of(newer[0]["version"])]
        return dict(pool[-1], inferred=True)
    cands = [
        b for b in newer
        if b["replaces"] == csv or csv in b["skips"]
        or (b["skip_range"] and in_semver_range(version, b["skip_range"]))
    ]
    return max(cands, key=lambda b: semkey(b["version"])) if cands else None


def route(pkg, state, goal):
    """Fewest InstallPlans from state to version `goal`, allowing channel switches.

    Each hop is what OLM would really install for the chosen channel, so a
    channel whose next bundle overshoots the goal cannot be used to stop on it.
    Returns [{"version", "csv", "channel", "inferred"}] or None.
    """
    channels = pkg["channels"]

    goal_minor = minor_of(goal)
    has_goal = {ch for ch, bs in channels.items() if any(b["version"] == goal for b in bs)}

    def ordered(cur_ch):
        # Keep the subscription's channel style: versioned (gitops-1.20 -> gitops-1.22)
        # or rolling (latest / stable).
        versioned = bool(re.search(r"\d+\.\d+", cur_ch or ""))
        return sorted(channels, key=lambda ch: (
            ch != cur_ch,
            ch not in has_goal,
            versioned and goal_minor not in ch,
            ch != pkg["default"],
            ch,
        ))

    start = state["csv"] or f"@{state['version']}"
    prev = {start: None}
    queue = deque([(start, state["version"], state["channel"])])
    while queue:
        csv, ver, cur_ch = queue.popleft()
        if ver == goal:
            hops, node = [], csv
            while prev[node]:
                parent, hop = prev[node]
                hops.append(hop)
                node = parent
            return list(reversed(hops))
        for ch in ordered(cur_ch):
            nxt = channel_next(channels[ch], csv, ver)
            if not nxt or semkey(nxt["version"]) > semkey(goal) or nxt["csv"] in prev:
                continue
            prev[nxt["csv"]] = (csv, {"version": nxt["version"], "csv": nxt["csv"], "channel": ch,
                                      "inferred": bool(nxt.get("inferred"))})
            queue.append((nxt["csv"], nxt["version"], ch))
    return None


def resolve(pkg, state, stream=None, goal=None):
    """Exact route from state to the newest bundle of `stream` (or to `goal`).

    With neither, the target is the head of the current channel (or the
    default channel if the current one no longer exists).
    Returns {version, csv, channel, hops, reachable, overshoot} or None.
    """
    channels = pkg["channels"]
    if goal is None:
        if stream:
            pool = [b for bs in channels.values() for b in bs if stream_match(b["version"], stream)]
        else:
            pool = channels.get(state["channel"]) or channels.get(pkg["default"]) or []
        if not pool:
            return None
        goal = max(pool, key=lambda b: semkey(b["version"]))["version"]
    if goal == state["version"]:
        return {"version": goal, "csv": state["csv"], "channel": state["channel"], "hops": [],
                "reachable": True, "overshoot": None}

    hops = route(pkg, state, goal)
    if hops is None and stream:
        # Newest bundle not reachable: settle for the newest one in the stream that is.
        for b in sorted({b["version"]: b for bs in channels.values() for b in bs
                         if stream_match(b["version"], stream)}.values(),
                        key=lambda b: semkey(b["version"]), reverse=True):
            if semkey(b["version"]) <= semkey(state["version"]):
                break
            hops = route(pkg, state, b["version"])
            if hops:
                goal = b["version"]
                break
    if not hops:
        ch = next((ch for ch, bs in channels.items() if any(b["version"] == goal for b in bs)), "")
        csv = next((b["csv"] for b in channels.get(ch, []) if b["version"] == goal), "")
        return {"version": goal, "csv": csv, "channel": ch, "hops": [], "reachable": False,
                "overshoot": None}

    last = hops[-1]
    # With Automatic approval OLM keeps going to the channel head.
    tail, cur = [], last
    while True:
        nxt = channel_next(channels[last["channel"]], cur["csv"], cur["version"])
        if not nxt:
            break
        tail.append(nxt["version"])
        cur = nxt
    return {"version": last["version"], "csv": last["csv"], "channel": last["channel"], "hops": hops,
            "reachable": True, "overshoot": tail[-1] if tail else None}


PRERELEASE_CHANNEL = re.compile(r"candidate|fast|preview|alpha|beta|nightly|dev-|tech", re.I)


def eligible(pkg, current_channel):
    """Package view without pre-release channels (candidate, fast, preview...),
    unless that channel is the one subscribed to or the package default."""
    return {
        "default": pkg["default"],
        "channels": {ch: bs for ch, bs in pkg["channels"].items()
                     if ch in (current_channel, pkg["default"]) or not PRERELEASE_CHANNEL.search(ch)},
    }


def find_csv(catalogs, org, package, version, minors):
    """Locate the CSV name and a channel for a simulated operator version."""
    for m in minors:
        pkg = (catalogs.get((org, m)) or {}).get(package)
        if not pkg:
            continue
        for ch in [pkg["default"]] + sorted(pkg["channels"]):
            for b in pkg["channels"].get(ch, []):
                if b["version"] == version:
                    return b["csv"], ch
    return "", ""


# ---------------------------------------------------------------------------
# Operator planning
# ---------------------------------------------------------------------------

KIND_ORDER = {"now": 0, "after": 1, "align": 2, "gate": 3, "blocked": 4, "optional": 5}


class OperatorPlanner:
    """Plans one operator across the OCP stages using lifecycle + catalog data."""

    def __init__(self, op, product, catalogs, org, minors, approval, prefer_latest=False):
        self.op = op
        self.lc = lifecycle_versions(product) if product else []
        self.catalogs = catalogs
        self.org = org
        self.minors = minors
        self.approval = approval
        self.prefer_latest = prefer_latest
        self.warnings = []
        installed = find_lifecycle_version(self.lc, op["version"])
        # Operators versioned like OCP itself (NMState 4.21, ODF 4.21, LVMS, MetalLB, CNV...).
        self.tracks_ocp = bool(
            installed and installed["tier"] == "Aligned"
            and re.fullmatch(r"\d+\.\d+", installed["name"])
            and installed["name"] in installed["ocp"]
        )

    # -- data helpers -------------------------------------------------------

    def pkg_at(self, minor):
        return (self.catalogs.get((self.org, minor)) or {}).get(self.op["package"])

    def has_catalog(self, minor):
        return (self.org, minor) in self.catalogs

    def lc_for(self, version):
        return find_lifecycle_version(self.lc, version)

    def supported(self, version, minor):
        """True/False from lifecycle data; None when unknown."""
        lv = self.lc_for(version)
        if not lv or not lv["ocp"]:
            return None
        return minor in lv["ocp"]

    def in_catalog(self, version, minor):
        pkg = self.pkg_at(minor)
        if pkg is None:
            return False if self.has_catalog(minor) else None
        return any(b["version"] == version for bs in pkg["channels"].values() for b in bs)

    def lowest_stream(self, state_version, need, minor):
        """Lowest lifecycle stream newer than state that supports every minor in `need`
        and ships in the `minor` catalog (when known). Highest with --prefer-latest."""
        cur = self.lc_for(state_version)
        for v in (reversed(self.lc) if self.prefer_latest else self.lc):
            if cur and vkey(v["name"]) <= vkey(cur["name"]):
                continue
            if not all(m in v["ocp"] for m in need):
                continue
            pkg = self.pkg_at(minor)
            if pkg is not None and not any(stream_match(b["version"], v["name"])
                                           for bs in pkg["channels"].values() for b in bs):
                continue
            return v["name"]
        return None

    # -- step construction --------------------------------------------------

    def pick_stream(self, version, stage, remaining):
        """Stream for a required move at `stage`: one that covers every remaining OCP
        minor through the target if possible (one move), else the next hop, else `stage`."""
        for need in (remaining, remaining[:2], remaining[:1]):
            stream = self.lowest_stream(version, need, stage)
            if stream:
                return stream
        return None

    def make_step(self, state, kind, stage, index_minor, reason, stream=None, goal=None, required=True):
        step = {
            "kind": kind, "stage": stage, "required": required, "reason": reason,
            "from": state["version"], "from_channel": state["channel"],
            "to": goal or stream, "channel": state["channel"], "hops": [],
            "exact": False, "reachable": None, "overshoot": None, "notes": [],
        }
        pkg = self.pkg_at(index_minor)
        if pkg:
            res = resolve(eligible(pkg, state["channel"]), state, stream=stream, goal=goal)
            if res:
                step.update(to=res["version"], channel=res["channel"], hops=res["hops"],
                            exact=True, reachable=res["reachable"])
                if res["overshoot"] and self.approval == "Automatic":
                    step["overshoot"] = res["overshoot"]
                state.update(version=res["version"], csv=res["csv"], channel=res["channel"])
                return step
            step["notes"].append(f"No {stream or 'newer'} bundle in the v{index_minor} catalog.")
        elif self.has_catalog(index_minor):
            step["notes"].append(f"Package not found in the v{index_minor} catalog.")
        if stream:
            # Lifecycle-only: keep a stream-level suggestion.
            state["version"] = stream
            ch = state["channel"]
            prev_minor = minor_of(step["from"])
            if prev_minor in ch:
                state["channel"] = ch.replace(prev_minor, stream)
            step["channel"] = state["channel"]
        return step

    # -- planning -----------------------------------------------------------

    def plan(self, hops, current_minor, target_minor):
        """Steps for each OCP stage. Stage = OCP minor the cluster is on at the time."""
        op = self.op
        state = {"version": op["version"], "csv": op["installed_csv"], "channel": op["channel"]}
        steps = []
        stages = [current_minor] + [b for _, b in hops]
        nxt_of = {a: b for a, b in hops}

        for stage in stages:
            first = stage == current_minor
            arrived_kind = "now" if first else "after"
            pkg = self.pkg_at(stage)

            # 1. Must support the OCP version we are on.
            sup = self.supported(state["version"], stage)
            nxt = nxt_of.get(stage)
            remaining = stages[stages.index(stage):]
            if sup is False:
                stream = stage if self.tracks_ocp else self.pick_stream(state["version"], stage, remaining)
                if stream:
                    lv = self.lc_for(op["version"] if first else state["version"])
                    steps.append(self.make_step(
                        state, arrived_kind, stage, stage,
                        f"{minor_of(state['version'])} is not supported on OCP {stage}"
                        + (f" (supports {summarize(lv['ocp'])})" if lv else "") + ".",
                        stream=stream))
                elif pkg:
                    # Lifecycle data lists no version for this OCP yet; go by the catalog.
                    before = state["version"]
                    s = self.make_step(state, arrived_kind, stage, stage,
                                       f"Lifecycle data lists no version for OCP {stage} yet; "
                                       f"newest in the v{stage} catalog.")
                    if s["to"] != before:
                        steps.append(s)
                    else:
                        self.warnings.append(
                            f"Lifecycle data lists no version for OCP {stage}; {before} ships in the v{stage} "
                            "catalog. Confirm support in the release notes.")
                else:
                    steps.append(self.blocked(state, stage, f"No released version supports OCP {stage}."))
                    break
            elif sup is None and self.has_catalog(stage):
                if pkg is None:
                    steps.append(self.blocked(state, stage, f"Package is not in the v{stage} catalog."))
                    break
                if state["channel"] not in pkg["channels"]:
                    steps.append(self.make_step(state, arrived_kind, stage, stage,
                                                f"Channel '{state['channel']}' is not in the v{stage} catalog."))

            # 2. Platform-aligned operators follow the cluster (ODF 4.21 on OCP 4.21).
            if self.tracks_ocp and vkey(minor_of(state["version"])) < vkey(stage):
                nxt = nxt_of.get(stage)
                need_gate = nxt and self.supported(state["version"], nxt) is False
                if need_gate:
                    steps.append(self.make_step(
                        state, "gate", stage, stage,
                        f"Required before OCP {stage} -> {nxt}: {minor_of(state['version'])} does not support {nxt}.",
                        stream=stage))
                else:
                    steps.append(self.make_step(
                        state, "align", stage, stage,
                        f"Platform-aligned: match the operator to OCP {stage}.",
                        stream=stage, required=False))

            # 3. Gate: must support the next OCP minor before the cluster moves.
            if nxt and self.supported(state["version"], nxt) is False:
                stream = self.lowest_stream(state["version"], remaining, stage) \
                    or self.lowest_stream(state["version"], [stage, nxt], stage)
                if stream:
                    lv = self.lc_for(stream)
                    steps.append(self.make_step(
                        state, "gate", stage, stage,
                        f"Required before OCP {stage} -> {nxt}: {minor_of(state['version'])} does not support {nxt}; "
                        f"{stream} supports {summarize(lv['ocp']) if lv else nxt}.",
                        stream=stream))
                # else: no version spans both, so it moves right after the OCP update.

        # 4. Optional: newest version supported on the target.
        if not any(s["kind"] == "blocked" for s in steps):
            best = self.best_on(target_minor, state)
            if best and semkey(best) > semkey(state["version"]) and best != state["version"]:
                steps.append(self.make_step(
                    state, "optional", target_minor, target_minor,
                    f"Newest version supported on OCP {target_minor}.", goal=best, required=False))
        return steps

    def blocked(self, state, stage, reason):
        return {"kind": "blocked", "stage": stage, "required": True, "reason": reason,
                "from": state["version"], "from_channel": state["channel"], "to": None,
                "channel": state["channel"], "hops": [], "exact": False, "reachable": None,
                "overshoot": None, "notes": []}

    def best_on(self, minor, state=None):
        """Newest version in the `minor` catalog that is supported on `minor`.

        Without lifecycle data, stay within the subscribed channel.
        """
        pkg = self.pkg_at(minor)
        if not pkg:
            ok = [v["name"] for v in self.lc if minor in v["ocp"]]
            return ok[-1] if ok else None
        pkg = eligible(pkg, (state or {}).get("channel") or self.op["channel"])
        if not self.lc:
            channel = (state or {}).get("channel") or self.op["channel"]
            bundles = pkg["channels"].get(channel) or pkg["channels"].get(pkg["default"]) or []
            return bundles[-1]["version"] if bundles else None
        versions = {b["version"] for bs in pkg["channels"].values() for b in bs}
        if self.tracks_ocp:
            versions = {v for v in versions if minor_of(v) == minor}
        known = [v for v in versions if self.supported(v, minor) is True]
        unknown = [v for v in versions if self.supported(v, minor) is None]
        pool = known or unknown
        return max(pool, key=semkey) if pool else None

    def latest_on_current(self, current_minor):
        """Exact route from the installed version to the newest supported on current OCP."""
        state = {"version": self.op["version"], "csv": self.op["installed_csv"], "channel": self.op["channel"]}
        best = self.best_on(current_minor)
        if not best:
            return None
        if self.pkg_at(current_minor) is None:
            return {"to": best, "exact": False, "hops": [], "channel": "", "reachable": None,
                    "supported": self.supported(best, current_minor)}
        if semkey(best) <= semkey(state["version"]):
            return {"to": state["version"], "exact": True, "hops": [], "channel": state["channel"],
                    "reachable": True, "supported": self.supported(state["version"], current_minor)}
        step = self.make_step(state, "optional", current_minor, current_minor, "", goal=best, required=False)
        step["supported"] = self.supported(best, current_minor)
        return step


def analyze(operators, products, hops, current_minor, target_minor, with_packagemanifests,
            catalogs, catalog_orgs, minors, prefer_latest=False):
    index = build_index(products)
    hop_targets = [b for _, b in hops]
    results = []
    for i, op in enumerate(operators, 1):
        log(f"  [{i}/{len(operators)}] {op['display_name'] or op['package']}")
        org = catalog_orgs.get((op["source_namespace"], op["source"]))
        if op["simulated"]:
            org = org or (op["source"] if op["source"] in INDEX_ORGS.values() else None)
            csv, ch = find_csv(catalogs, org, op["package"], op["version"], minors)
            op["installed_csv"] = csv or f"{op['package']}.v{op['version']}"
            op["channel"] = op["channel"] or ch

        product = match_product(op, index) if op["version"] else None
        entry = dict(op)
        entry["name"] = op["display_name"] or (product["name"] if product else op["package"])
        entry["issues"] = []

        if not op["version"]:
            entry["issues"].append("Subscription has no installed CSV (failed or pending install).")
        elif op["csv_phase"] and op["csv_phase"] != "Succeeded":
            entry["issues"].append(f"CSV phase is {op['csv_phase']}.")
        if op["max_ocp"]:
            blocked = [m for m in hop_targets if vkey(m) > vkey(minor_of(op["max_ocp"]))]
            if blocked:
                entry["issues"].append(
                    f"olm.maxOpenShiftVersion={op['max_ocp']} blocks the cluster update to "
                    f"{blocked[0]} until this operator is updated."
                )
        cat = get_catalog_channels(op) if with_packagemanifests and not op["simulated"] else None
        if cat and op["channel"] in cat["channels"] and op["version"]:
            head = cat["channels"][op["channel"]]
            if head and semkey(head) > semkey(op["version"]):
                entry["issues"].append(
                    f"Newer version {head} available now in channel '{op['channel']}'"
                    + (" (Manual approval)" if op["approval"] == "Manual" else "") + "."
                )
        elif cat and op["channel"] and op["channel"] not in cat["channels"]:
            entry["issues"].append(f"Channel '{op['channel']}' no longer exists in catalog '{op['source']}'.")

        has_catalog = any(k[0] == org for k in catalogs) if org else False
        if not op["version"] or (not product and not has_catalog):
            entry.update(data="none", steps=[], matrix={}, support=None, latest_current=None,
                         tracks_ocp=False, tier="")
            results.append(entry)
            continue

        planner = OperatorPlanner(op, product, catalogs if has_catalog else {}, org, minors, op["approval"],
                                  prefer_latest)
        lv = planner.lc_for(op["version"])
        entry["data"] = "+".join(x for x in ("lifecycle" if lv else "", "catalog" if has_catalog else "") if x)
        entry["tier"] = lv["tier"] if lv else (planner.lc[-1]["tier"] if planner.lc else "")
        entry["tracks_ocp"] = planner.tracks_ocp
        entry["support"] = {
            "stream": lv["name"], "type": lv["type"], "full_end": lv["full_end"],
            "maint_end": lv["maint_end"], "ocp": sorted(lv["ocp"], key=vkey),
        } if lv else None
        entry["matrix"] = {
            m: {"supported": planner.supported(op["version"], m), "in_catalog": planner.in_catalog(op["version"], m)}
            for m in minors
        }
        entry["supported_now"] = planner.supported(op["version"], current_minor)
        entry["latest_current"] = planner.latest_on_current(current_minor)
        entry["steps"] = planner.plan(hops, current_minor, target_minor)
        entry["issues"] += planner.warnings
        if lv is None and planner.lc:
            entry["issues"].append(f"Installed version {op['version']} is not in lifecycle data; support status unknown.")

        required = [s for s in entry["steps"] if s["kind"] not in ("optional", "blocked") and s["to"]]
        entry["final"] = {"version": required[-1]["to"], "channel": required[-1]["channel"]} if required \
            else {"version": op["version"], "channel": op["channel"]}
        opt = [s for s in entry["steps"] if s["kind"] == "optional"]
        entry["newest"] = {"version": opt[-1]["to"], "channel": opt[-1]["channel"]} if opt else entry["final"]
        fl = planner.lc_for(entry["final"]["version"]) if entry["final"]["version"] else None
        entry["final_support"] = planner.supported(entry["final"]["version"], target_minor)
        entry["final_support_type"] = fl["type"] if fl else None
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------

def build_report(cluster, current, ocp, hops, results, olm_cond, pending_ips, what_if, minors):
    current_minor, target_minor = minor_of(current), minor_of(ocp["target"])
    checks = []
    up = cluster["conditions"].get("Upgradeable")
    if up and up.get("status") == "False":
        checks.append(("error", f"ClusterVersion Upgradeable=False: {up.get('message', '').strip()}"))
    if olm_cond and olm_cond.get("status") == "False":
        checks.append(("error", f"OLM Upgradeable=False: {olm_cond.get('message', '').strip()}"))
    for ip in pending_ips:
        checks.append(("warn", f"InstallPlan {ip['namespace']}/{ip['name']} awaiting approval: {', '.join(ip['csvs'])}"))
    if not checks:
        checks.append(("ok", "No cluster-level blockers (ClusterVersion / OLM Upgradeable, pending InstallPlans)."))

    # OCP stops: the last release of each minor on the path.
    path = ocp["path"] or [current, ocp["target"]]
    stop_of = {}
    for v in path:
        stop_of[minor_of(v)] = v
    stop_of[current_minor] = current

    eus_note = None
    if len(hops) >= 2 and minor_num(hops[0][0]) % 2 == 0 and minor_num(hops[-1][1]) % 2 == 0:
        eus_note = ("EUS-to-EUS: you can pause worker MachineConfigPools so workers reboot once, but the "
                    "control plane and platform-aligned operators must still stop at "
                    + ", ".join(b for _, b in hops[:-1]) + ".")

    planned = [r for r in results if r.get("steps") is not None and r["data"] != "none"]

    def stage_items(stage, kinds):
        items = []
        for r in planned:
            for s in r["steps"]:
                if s["stage"] == stage and s["kind"] in kinds:
                    items.append({"operator": r["name"], "namespace": r["namespace"],
                                  "approval": r["approval"], **s})
        return sorted(items, key=lambda x: (KIND_ORDER[x["kind"]], x["operator"].lower()))

    runbook = []
    stages = [current_minor] + [b for _, b in hops]
    for i, stage in enumerate(stages):
        kinds = ("now", "after", "align", "gate", "blocked")
        runbook.append({
            "type": "operators",
            "minor": stage,
            "ocp": stop_of.get(stage, f"{stage}.z"),
            "current": i == 0,
            "items": stage_items(stage, kinds),
        })
        if i + 1 < len(stages):
            nxt = stages[i + 1]
            start, end = stop_of.get(stage, stage), stop_of.get(nxt, f"{nxt}.z")
            via = []
            if ocp["path"] and start in path and end in path:
                via = path[path.index(start) + 1: path.index(end)]
            gates = list(dict.fromkeys(x["operator"] for x in runbook[-1]["items"]
                                       if x["kind"] in ("gate", "now", "blocked")))
            runbook.append({"type": "ocp", "from": start, "to": end, "via": via, "gates": gates})
    optional = stage_items(target_minor, ("optional",))

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "cluster": {"version": cluster["version"], "channel": cluster["channel"], "arch": cluster["arch"],
                    "source": cluster.get("source", "")},
        "current": current,
        "current_minor": current_minor,
        "target": ocp["target"],
        "target_minor": target_minor,
        "what_if": what_if,
        "ocp": ocp,
        "eus_note": eus_note,
        "minors": minors,
        "checks": checks,
        "operators": results,
        "runbook": runbook,
        "optional": optional,
    }


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------

KIND_LABEL = {
    "now": ("NOW", RED, "Required now: not supported on the current OCP version"),
    "gate": ("GATE", RED, "Required before the next OCP update"),
    "after": ("AFTER", YELLOW, "Required once the cluster is on this OCP version"),
    "align": ("ALIGN", BLUE, "Recommended: match platform-aligned operator to OCP"),
    "optional": ("OPTIONAL", CYAN, "Newer version also supported on the target"),
    "blocked": ("BLOCKED", RED, "No supported version available"),
}


def phase_text(sup):
    if not sup:
        return "unknown"
    short = {"Full Support": "Full", "Maintenance Support": "Maintenance", "Extended Support": "Extended"}
    kind = short.get(sup["type"], sup["type"])
    end = sup["full_end"] if kind == "Full" else (sup["maint_end"] if kind == "Maintenance" else None)
    if not end:
        return kind
    return f"{kind} ended {end}" if end < datetime.now().strftime("%Y-%m-%d") else f"{kind} until {end}"


def tick(v):
    return {True: "yes", False: "NO", None: "?"}[v]


def hops_text(step):
    """'gitops-1.22: 1.22.0 -> 1.22.2' grouped by channel, with channel switches marked."""
    if not step["hops"]:
        return ""
    groups, cur = [], None
    for h in step["hops"]:
        if not groups or h["channel"] != cur:
            groups.append((h["channel"], []))
            cur = h["channel"]
        groups[-1][1].append(h["version"])
    parts = []
    for ch, versions in groups:
        parts.append(f"{ch}: {' -> '.join(versions)}")
    text = "  =>  ".join(parts)
    inferred = any(h["inferred"] for h in step["hops"])
    return text + (" (inferred from semver order)" if inferred else "")


def print_text(rep):
    w = 120
    hr = c("=" * w, BOLD)
    print("\n" + hr)
    print(c("  OpenShift Upgrade Plan", BOLD))
    print(hr)
    print(f"  Current version : {c(rep['current'], CYAN)}" + (c("  (--from override)", YELLOW) if rep["what_if"] else ""))
    print(f"  Target version  : {c(rep['target'], CYAN)}")
    print(f"  Cluster channel : {rep['cluster']['channel'] or '(none)'}   arch: {rep['cluster']['arch']}")
    print(f"  Cluster data    : {rep['cluster']['source']}")
    print(f"  Report date     : {rep['generated']}")
    print(hr)

    ocp = rep["ocp"]
    print(c("\n  OCP UPDATE PATH", BOLD))
    if ocp["error"]:
        print(c(f"  {ocp['error']}", RED))
        reach = ocp.get("reachable")
        if reach and reach["recommended"]:
            print(f"  Reachable {rep['target_minor']}.z (recommended): {c(', '.join(reach['recommended']), GREEN)}")
        if reach and reach["conditional"]:
            print(f"  Reachable {rep['target_minor']}.z (conditional): {c(', '.join(reach['conditional']), YELLOW)}")
        print(c("  The operator plan below assumes one stop per OCP minor.", DIM))
    else:
        print("  " + c(" -> ".join(ocp["path"]), CYAN))
        for r in ocp["risks"]:
            print(c(f"  ! Conditional edge {r['from']} -> {r['to']}: {', '.join(r['risks'])}", YELLOW))
    if rep["eus_note"]:
        print(c(f"  {rep['eus_note']}", DIM))

    print(c("\n  PRE-FLIGHT CHECKS", BOLD))
    for level, msg in rep["checks"]:
        print("  " + c("• ", {"error": RED, "warn": YELLOW, "ok": GREEN}[level]) + msg)

    ops = [r for r in rep["operators"] if r["data"] != "none"]
    ops.sort(key=lambda r: (not r["tracks_ocp"], r["tier"] != "Aligned", r["name"].lower()))

    # ---- Current OCP support ----
    cm = rep["current_minor"]
    print(c(f"\n  SUPPORT ON CURRENT OCP {cm}", BOLD))
    hdr = (f"  {'Operator':<32} {'Installed':<22} {'Channel':<14} {'Support phase':<27} "
           f"{'On ' + cm:<6} {'Latest for ' + cm:<24}")
    print(c(hdr, BOLD))
    print("  " + "─" * (w - 2))
    for r in ops:
        phase = phase_text(r["support"])
        lc = r["latest_current"]
        latest = lc["to"] if lc else "-"
        on_color = {True: GREEN, False: RED, None: DIM}[r["supported_now"]]
        latest_color = GREEN if lc and lc["to"] == r["version"] else YELLOW
        print(f"  {r['name'][:32]:<32} {r['version'][:22]:<22} {r['channel'][:14]:<14} {phase[:27]:<27} "
              + c(f"{tick(r['supported_now']):<6}", on_color) + " " + c(f"{latest[:24]:<24}", latest_color))
        if lc and lc.get("hops"):
            print(c(f"  {'':<32} route: {hops_text(lc)}", DIM))
        elif lc and lc.get("reachable") is False:
            print(c(f"  {'':<32} no OLM upgrade edge from {r['version']} to {lc['to']}; see release notes", RED))

    # ---- Matrix ----
    minors = rep["minors"]
    print(c(f"\n  INSTALLED VERSION SUPPORT ACROSS OCP {minors[0]} - {minors[-1]}", BOLD))
    print(c("  yes/NO from lifecycle data; '+cat' = installed version still shipped in that OCP catalog", DIM))
    hdr = f"  {'Operator':<34} {'Installed':<22} {'Supports':<12}" + "".join(f" {m:<10}" for m in minors)
    print(c(hdr, BOLD))
    print("  " + "─" * (w - 2))
    for r in ops:
        cells = ""
        for m in minors:
            cell = r["matrix"].get(m, {})
            s = cell.get("supported")
            label = tick(s) + ("+cat" if cell.get("in_catalog") else "")
            cells += " " + c(f"{label:<10}", {True: GREEN, False: RED, None: DIM}[s])
        supports = summarize(r["support"]["ocp"]) if r["support"] else "unknown"
        print(f"  {r['name'][:34]:<34} {r['version'][:22]:<22} {supports:<12}{cells}")

    # ---- Runbook ----
    print(c("\n  UPGRADE RUNBOOK", BOLD))
    for kind in ("now", "gate", "after", "align", "optional", "blocked"):
        tag, color, desc = KIND_LABEL[kind]
        print(c(f"  {tag:<9}", color) + c(desc, DIM))
    n = 0
    for block in rep["runbook"]:
        n += 1
        if block["type"] == "ocp":
            via = f" (via {', '.join(block['via'])})" if block["via"] else ""
            print(c(f"\n  {n}. Update OCP {block['from']} -> {block['to']}{via}", BOLD + CYAN))
            if block["gates"]:
                print(c(f"     Do not start until done: {', '.join(block['gates'])}", YELLOW))
            continue
        where = f"{block['ocp']} (current)" if block["current"] else block["ocp"]
        print(c(f"\n  {n}. Operators on OCP {where}", BOLD))
        if not block["items"]:
            print(c("     No operator changes required.", DIM))
        for it in block["items"]:
            print_step(it)
    if rep["optional"]:
        n += 1
        print(c(f"\n  {n}. Optional on OCP {rep['target_minor']}: newest supported versions", BOLD))
        for it in rep["optional"]:
            print_step(it)

    # ---- Target summary ----
    tm = rep["target_minor"]
    print(c(f"\n  SUMMARY AT TARGET OCP {tm}", BOLD))
    hdr = (f"  {'Operator':<34} {'Installed':<22} {'Required @' + tm:<24} {'Channel':<16} "
           f"{'Supported':<10} {'Newest @' + tm:<22}")
    print(c(hdr, BOLD))
    print("  " + "─" * (w - 2))
    for r in ops:
        blocked = any(s["kind"] == "blocked" for s in r["steps"])
        final = "BLOCKED" if blocked else (r["final"]["version"] or "?")
        color = RED if blocked else (GREEN if final == r["version"] else YELLOW)
        sup = r.get("final_support")
        print(f"  {r['name'][:34]:<34} {r['version'][:22]:<22} " + c(f"{final[:24]:<24}", color)
              + f" {(r['final']['channel'] or '')[:16]:<16} "
              + c(f"{tick(sup):<10}", {True: GREEN, False: RED, None: DIM}[sup])
              + f" {(r['newest']['version'] or '-')[:22]:<22}")

    flagged = [r for r in rep["operators"] if r["issues"]]
    if flagged:
        print(c("\n  OPERATOR WARNINGS", BOLD))
        for r in flagged:
            for m in r["issues"]:
                color = RED if ("blocks" in m or "no installed" in m.lower()) else YELLOW
                print("  " + c("• ", color) + f"{r['name']} ({r['namespace']}): {m}")

    nodata = [r for r in rep["operators"] if r["data"] == "none"]
    if nodata:
        print(c("\n  NO VERSION DATA (verify manually)", BOLD))
        for r in nodata:
            print(f"  {c('•', YELLOW)} {r['name']:<40} pkg={r['package']} ver={r['version'] or '-'} src={r['source']}")

    print(c("\n  When: Red Hat Product Life Cycle API (OCP compatibility). What: Red Hat Ecosystem Catalog (index"
            " contents per OCP version, OLM replaces/skips/skipRange).", DIM))
    print(c("  Confirm against operator release notes before changing production.\n", DIM))


def print_step(it):
    tag, color, _ = KIND_LABEL[it["kind"]]
    name = it["operator"] + (" (simulated)" if it["namespace"] == "(simulated)" else "")
    pad = " " * 15
    if it["kind"] == "blocked":
        print(f"     {c(f'{tag:<9}', color)} {name[:40]:<40} {it['reason']}")
        return
    chan = ""
    if it["channel"] and it["channel"] != it["from_channel"]:
        chan = c(f"   channel {it['from_channel'] or '?'} -> {it['channel']}", CYAN)
    elif it["channel"]:
        chan = c(f"   [{it['channel']}]", DIM)
    print(f"     {c(f'{tag:<9}', color)} {name[:40]:<40} {it['from']} -> {c(it['to'], BOLD)}{chan}")
    if it["hops"]:
        count = len(it["hops"])
        plans = f"{count} InstallPlan{'s' if count > 1 else ''}"
        print(c(f"{pad}installs: {hops_text(it)}  ({plans})", DIM))
    if it["exact"] and it["reachable"] is False:
        print(c(f"{pad}No OLM upgrade edge from {it['from']}; check release notes for the supported path.", RED))
    if it.get("overshoot"):
        print(c(f"{pad}Automatic approval: OLM will continue to {it['overshoot']} in '{it['channel']}'. "
                "Set Manual approval to stop here.", YELLOW))
    if it["reason"]:
        print(c(f"{pad}why: {it['reason']}", DIM))
    for note in it["notes"]:
        print(c(f"{pad}{note}", YELLOW))


# ---------------------------------------------------------------------------
# HTML output
# ---------------------------------------------------------------------------

HTML_CSS = """
:root{--bg:#f7f7f8;--card:#fff;--fg:#1d1d1f;--muted:#6b6b73;--line:#e3e3e8;--accent:#0b62c4;
--ok:#1a7f37;--ok-bg:#e6f4ea;--bad:#c62828;--bad-bg:#fdecea;--warn:#9a6700;--warn-bg:#fff4d6;
--info:#0b62c4;--info-bg:#e7f0fb;--opt:#5b5b66;--opt-bg:#efeff3;--code:#f0f0f3}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121214;--card:#1c1c1f;--fg:#ececf0;
--muted:#9a9aa3;--line:#2e2e33;--accent:#6aa8ff;--ok:#5fd17a;--ok-bg:#16301d;--bad:#ff7b72;--bad-bg:#3a1a1a;
--warn:#e3b341;--warn-bg:#352b10;--info:#6aa8ff;--info-bg:#15263d;--opt:#b4b4bd;--opt-bg:#26262b;--code:#26262b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.sub{color:var(--muted)}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:16px 0}
.meta div{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.meta b{display:block;font-size:12px;color:var(--muted);font-weight:500}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:15px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}
th{font-weight:600;color:var(--muted);font-size:12px;background:var(--card);position:sticky;top:0}
tr:last-child td{border-bottom:0}
td.wrapcell{white-space:normal;min-width:260px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;background:var(--code);padding:1px 5px;border-radius:4px}
.b{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;letter-spacing:.02em}
.ok{color:var(--ok);background:var(--ok-bg)}.bad{color:var(--bad);background:var(--bad-bg)}
.warn{color:var(--warn);background:var(--warn-bg)}.info{color:var(--info);background:var(--info-bg)}
.opt{color:var(--opt);background:var(--opt-bg)}
.stage{margin:0 0 12px}.stage h3{margin:0 0 8px;font-size:15px}
.ocpstep{border-left:4px solid var(--accent)}
.step{display:grid;grid-template-columns:90px minmax(180px,260px) 1fr;gap:4px 12px;padding:8px 0;border-top:1px solid var(--line)}
.step:first-of-type{border-top:0}
.detail{grid-column:3;color:var(--muted);font-size:12.5px}
.legend span{margin-right:12px}
ul.checks{margin:0;padding-left:18px}
.muted{color:var(--muted)}
@media (max-width:700px){.step{grid-template-columns:80px 1fr}.step>.ver,.detail{grid-column:1/-1}}
"""

KIND_CLASS = {"now": "bad", "gate": "bad", "after": "warn", "align": "info", "optional": "opt", "blocked": "bad"}


def e(x):
    return html.escape(str(x if x is not None else ""))


def badge(text, cls):
    return f'<span class="b {cls}">{e(text)}</span>'


def sup_badge(v, extra=""):
    return badge({True: "yes", False: "no", None: "?"}[v] + extra, {True: "ok", False: "bad", None: "opt"}[v])


def hops_html(step):
    if not step.get("hops"):
        return ""
    groups, cur = [], None
    for h in step["hops"]:
        if not groups or h["channel"] != cur:
            groups.append((h["channel"], []))
            cur = h["channel"]
        groups[-1][1].append(h["version"])
    parts = [f"<code>{e(ch)}</code> {' &rarr; '.join(e(v) for v in vs)}" for ch, vs in groups]
    inferred = any(h["inferred"] for h in step["hops"])
    n = len(step["hops"])
    return (" &rArr; ".join(parts) + f" <span class='muted'>({n} InstallPlan{'s' if n > 1 else ''}"
            + ("; inferred from semver order" if inferred else "") + ")</span>")


def step_html(it):
    tag = KIND_LABEL[it["kind"]][0]
    name = e(it["operator"]) + (" <span class='muted'>(simulated)</span>" if it["namespace"] == "(simulated)" else "")
    if it["kind"] == "blocked":
        return (f'<div class="step">{badge(tag, "bad")}<div><b>{name}</b></div>'
                f'<div class="ver">{e(it["reason"])}</div></div>')
    chan = ""
    if it["channel"] and it["channel"] != it["from_channel"]:
        chan = f' &nbsp;channel <code>{e(it["from_channel"] or "?")}</code> &rarr; <code>{e(it["channel"])}</code>'
    elif it["channel"]:
        chan = f' &nbsp;<code>{e(it["channel"])}</code>'
    details = []
    if it["hops"]:
        details.append("Installs: " + hops_html(it))
    if it["exact"] and it["reachable"] is False:
        details.append(f"<span class='b bad'>no OLM upgrade edge</span> from {e(it['from'])}; check release notes.")
    if it.get("overshoot"):
        details.append(f"<span class='b warn'>Automatic approval</span> OLM will continue to "
                       f"{e(it['overshoot'])}; set Manual approval to stop here.")
    if it["reason"]:
        details.append(e(it["reason"]))
    details += [f"<span class='b warn'>note</span> {e(n)}" for n in it["notes"]]
    return (f'<div class="step">{badge(tag, KIND_CLASS[it["kind"]])}<div><b>{name}</b></div>'
            f'<div class="ver"><code>{e(it["from"])}</code> &rarr; <code><b>{e(it["to"])}</b></code>{chan}</div>'
            + "".join(f'<div class="detail">{d}</div>' for d in details) + "</div>")


def render_html(rep):
    ocp = rep["ocp"]
    out = []
    a = out.append
    a(f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
      f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
      f"<title>OCP Upgrade Plan</title><style>{HTML_CSS}</style></head><body><main>")
    a(f"<h1>OpenShift upgrade plan</h1><div class='sub'>{e(rep['current'])} &rarr; {e(rep['target'])}"
      f" &middot; generated {e(rep['generated'])}</div>")
    a("<div class='meta'>")
    for k, v in (("Current version", rep["current"] + (" (what-if)" if rep["what_if"] else "")),
                 ("Target version", rep["target"]), ("Cluster channel", rep["cluster"]["channel"] or "(none)"),
                 ("Architecture", rep["cluster"]["arch"]), ("Cluster data", rep["cluster"]["source"])):
        a(f"<div><b>{e(k)}</b>{e(v)}</div>")
    a("</div>")

    a("<h2>OCP update path</h2><div class='card'>")
    if ocp["error"]:
        a(f"<div>{badge('error', 'bad')} {e(ocp['error'])}</div>")
        reach = ocp.get("reachable")
        if reach and reach["recommended"]:
            a(f"<div>Reachable {e(rep['target_minor'])}.z (recommended): <b>{e(', '.join(reach['recommended']))}</b></div>")
        if reach and reach["conditional"]:
            a(f"<div>Reachable {e(rep['target_minor'])}.z (conditional): {e(', '.join(reach['conditional']))}</div>")
    else:
        a(f"<div class='path'>{' &rarr; '.join(e(v) for v in ocp['path'])}</div>")
        for r in ocp["risks"]:
            a(f"<div>{badge('risk', 'warn')} {e(r['from'])} &rarr; {e(r['to'])}: {e(', '.join(r['risks']))}</div>")
    if rep["eus_note"]:
        a(f"<div class='muted' style='margin-top:6px'>{e(rep['eus_note'])}</div>")
    a("</div>")

    a("<h2>Pre-flight checks</h2><div class='card'><ul class='checks'>")
    for level, msg in rep["checks"]:
        a(f"<li>{badge(level, {'error': 'bad', 'warn': 'warn', 'ok': 'ok'}[level])} {e(msg)}</li>")
    for r in rep["operators"]:
        for m in r["issues"]:
            a(f"<li>{badge('operator', 'warn')} <b>{e(r['name'])}</b> ({e(r['namespace'])}): {e(m)}</li>")
    a("</ul></div>")

    ops = [r for r in rep["operators"] if r["data"] != "none"]
    ops.sort(key=lambda r: (not r["tracks_ocp"], r["tier"] != "Aligned", r["name"].lower()))
    cm = rep["current_minor"]

    a(f"<h2>Support on current OCP {e(cm)}</h2><div class='wrap'><table><thead><tr>"
      f"<th>Operator</th><th>Installed</th><th>Channel</th><th>Tier</th><th>Support phase</th>"
      f"<th>Supported on {e(cm)}</th><th>Latest for {e(cm)}</th><th>Route from installed</th></tr></thead><tbody>")
    for r in ops:
        phase = phase_text(r["support"])
        lc = r["latest_current"]
        latest = lc["to"] if lc else "-"
        latest_cell = (badge("current", "ok") if lc and lc["to"] == r["version"] else f"<b>{e(latest)}</b>")
        route_cell = hops_html(lc) if lc and lc.get("hops") else (
            "<span class='b bad'>no OLM upgrade edge</span>" if lc and lc.get("reachable") is False else "")
        a(f"<tr><td>{e(r['name'])}</td><td><code>{e(r['version'])}</code></td><td><code>{e(r['channel'])}</code></td>"
          f"<td>{e(r['tier'] or '-')}</td><td>{e(phase)}</td><td>{sup_badge(r['supported_now'])}</td>"
          f"<td>{latest_cell}</td><td class='wrapcell'>{route_cell}</td></tr>")
    a("</tbody></table></div>")

    minors = rep["minors"]
    a(f"<h2>Installed version support across OCP {e(minors[0])} &ndash; {e(minors[-1])}</h2>"
      "<div class='sub' style='margin-bottom:8px'>From lifecycle data. A <i>cat</i> marker means the installed "
      "version still ships in that OCP version's catalog.</div>")
    a("<div class='wrap'><table><thead><tr><th>Operator</th><th>Installed</th><th>Lifecycle supports</th>"
      + "".join(f"<th>OCP {e(m)}</th>" for m in minors) + "</tr></thead><tbody>")
    for r in ops:
        cells = ""
        for m in minors:
            cell = r["matrix"].get(m, {})
            cells += f"<td>{sup_badge(cell.get('supported'), ' · cat' if cell.get('in_catalog') else '')}</td>"
        supports = summarize(r["support"]["ocp"]) if r["support"] else "unknown"
        a(f"<tr><td>{e(r['name'])}</td><td><code>{e(r['version'])}</code></td><td>{e(supports)}</td>{cells}</tr>")
    a("</tbody></table></div>")

    a("<h2>Upgrade runbook</h2><div class='legend sub' style='margin-bottom:10px'>")
    for kind in ("now", "gate", "after", "align", "optional", "blocked"):
        tag, _, desc = KIND_LABEL[kind]
        a(f"<span>{badge(tag, KIND_CLASS[kind])} {e(desc)}</span>")
    a("</div>")
    n = 0
    for block in rep["runbook"]:
        n += 1
        if block["type"] == "ocp":
            via = f" <span class='muted'>(via {e(', '.join(block['via']))})</span>" if block["via"] else ""
            gates = (f"<div class='muted'>Do not start until done: {e(', '.join(block['gates']))}</div>"
                     if block["gates"] else "")
            a(f"<div class='card stage ocpstep'><h3>{n}. Update OCP {e(block['from'])} &rarr; {e(block['to'])}{via}</h3>{gates}</div>")
            continue
        where = f"{block['ocp']} (current)" if block["current"] else block["ocp"]
        a(f"<div class='card stage'><h3>{n}. Operators on OCP {e(where)}</h3>")
        if not block["items"]:
            a("<div class='muted'>No operator changes required.</div>")
        a("".join(step_html(it) for it in block["items"]))
        a("</div>")
    if rep["optional"]:
        n += 1
        a(f"<div class='card stage'><h3>{n}. Optional on OCP {e(rep['target_minor'])}: newest supported versions</h3>")
        a("".join(step_html(it) for it in rep["optional"]))
        a("</div>")

    tm = rep["target_minor"]
    a(f"<h2>Summary at target OCP {e(tm)}</h2><div class='wrap'><table><thead><tr><th>Operator</th>"
      f"<th>Installed</th><th>Required @ {e(tm)}</th><th>Channel</th><th>Supported on {e(tm)}</th>"
      f"<th>Newest @ {e(tm)}</th></tr></thead><tbody>")
    for r in ops:
        blocked = any(s["kind"] == "blocked" for s in r["steps"])
        final = r["final"]["version"] or "?"
        final_cell = badge("blocked", "bad") if blocked else (
            badge("no change", "ok") if final == r["version"] else f"<b>{e(final)}</b>")
        a(f"<tr><td>{e(r['name'])}</td><td><code>{e(r['version'])}</code></td><td>{final_cell}</td>"
          f"<td><code>{e(r['final']['channel'])}</code></td><td>{sup_badge(r.get('final_support'))}</td>"
          f"<td>{e(r['newest']['version'])}</td></tr>")
    a("</tbody></table></div>")

    nodata = [r for r in rep["operators"] if r["data"] == "none"]
    if nodata:
        a("<h2>No version data</h2><div class='card'><ul class='checks'>")
        for r in nodata:
            a(f"<li><b>{e(r['name'])}</b> &middot; pkg <code>{e(r['package'])}</code> &middot; "
              f"version {e(r['version'] or '-')} &middot; source {e(r['source'])}</li>")
        a("</ul></div>")

    a("<p class='sub' style='margin-top:28px'>When to update comes from the Red Hat Product Life Cycle API "
      "(OCP compatibility). Exact versions, channels and OLM upgrade edges come from the Red Hat Ecosystem Catalog "
      "(operator index contents per OCP version). Confirm against operator release notes before changing production.</p>")
    a("</main></body></html>")
    return "".join(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global USE_COLOR
    parser = argparse.ArgumentParser(
        description="Plan an OpenShift upgrade: OCP update path plus exact operator versions per stage."
    )
    parser.add_argument("--target", "-t", required=True, help="Target OCP version, e.g. 4.22.15 or 4.22 (latest z)")
    parser.add_argument("--must-gather", metavar="PATH",
                        help="Read cluster data from a must-gather (or oc adm inspect) directory or tarball "
                             "instead of the live cluster")
    parser.add_argument("--from", dest="from_version", help="Override the current OCP version (what-if planning)")
    parser.add_argument("--arch", help="Override architecture for the update graph (default: from cluster)")
    parser.add_argument("--allow-conditional", action="store_true",
                        help="Allow conditional (known-risk) update edges if no recommended path exists")
    parser.add_argument("--simulate-operator", action="append", default=[], metavar="PKG=VERSION[:CHANNEL]",
                        help="Add an operator that is not installed (repeatable), e.g. odf-operator=4.20.5-rhodf")
    parser.add_argument("--simulate-source", default="redhat-operators",
                        help="Catalog for --simulate-operator (default: redhat-operators)")
    parser.add_argument("--only-simulated", action="store_true",
                        help="Ignore the cluster's operators; plan only --simulate-operator entries")
    parser.add_argument("--prefer-latest", action="store_true",
                        help="Required operator moves go to the newest supported version instead of the minimum")
    parser.add_argument("--no-catalog", action="store_true",
                        help="Skip per-operator PackageManifest lookups on the cluster (faster)")
    parser.add_argument("--lifecycle-only", action="store_true",
                        help="Skip the Red Hat Ecosystem Catalog lookup; suggest version streams only")
    parser.add_argument("--html", metavar="FILE", help="Also write an HTML report to FILE")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    parser.add_argument("--refresh-cache", action="store_true", help="Ignore cached API data")
    args = parser.parse_args()

    USE_COLOR = not args.json and not args.no_color and sys.stdout.isatty()

    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", args.target):
        parser.error("--target must look like 4.22 or 4.22.15")

    try:
        src = MustGatherSource(args.must_gather) if args.must_gather else OcSource()
        log(f"Reading cluster version and operators from {src.label}...")
        cluster = get_cluster_info(src)
        operators = [] if args.only_simulated else get_installed_operators(src)
        olm_cond = get_olm_upgradeable(src)
        pending_ips = [] if args.only_simulated else get_pending_installplans(src)
    except Exception as ex:
        print(c(f"Error: {ex}", RED), file=sys.stderr)
        if not args.must_gather:
            print("Make sure 'oc' is installed and you are logged in, or use --must-gather.", file=sys.stderr)
        sys.exit(1)
    cluster["source"] = src.label
    try:
        operators += [simulated_operator(s, args.simulate_source) for s in args.simulate_operator]
    except ValueError as ex:
        parser.error(str(ex))

    current = args.from_version or cluster["version"]
    arch = args.arch or cluster["arch"]
    if vkey(args.target) <= vkey(current)[: len(vkey(args.target))]:
        parser.error(f"--target {args.target} is not newer than current version {current}")

    ocp = plan_ocp_path(current, args.target, arch, args.allow_conditional, args.refresh_cache)
    if ocp["path"]:
        hops = hop_list(ocp["path"])
    else:
        minors = ocp_minor_range(minor_of(current), minor_of(ocp["target"]))
        hops = list(zip(minors, minors[1:]))
    minors = ocp_minor_range(minor_of(current), minor_of(ocp["target"]))

    products = fetch_lifecycle_products(args.refresh_cache)
    catalogs, catalog_orgs = {}, {}
    if not args.lifecycle_only:
        catalog_orgs = get_catalog_orgs(src)
        for op in operators:
            if op["simulated"] and op["source"] in INDEX_ORGS.values():
                catalog_orgs.setdefault((op["source_namespace"], op["source"]), op["source"])
        catalogs = fetch_catalogs(operators, minors, catalog_orgs, args.refresh_cache)
    log(f"Analyzing {len(operators)} operators...")
    results = analyze(operators, products, hops, minor_of(current), minor_of(ocp["target"]),
                      not args.no_catalog and src.live, catalogs, catalog_orgs, minors, args.prefer_latest)
    rep = build_report(cluster, current, ocp, hops, results, olm_cond, pending_ips,
                       bool(args.from_version), minors)

    if args.html:
        Path(args.html).write_text(render_html(rep))
        log(f"Wrote HTML report to {Path(args.html).resolve()}")
    if args.json:
        print(json.dumps(rep, indent=2, default=lambda o: sorted(o) if isinstance(o, set) else str(o)))
    else:
        print_text(rep)


if __name__ == "__main__":
    main()
