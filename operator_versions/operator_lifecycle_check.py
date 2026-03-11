#!/usr/bin/env python3
"""
OpenShift Operator Lifecycle Checker

Queries installed operators from an OpenShift cluster and cross-references
them against the Red Hat Product Life Cycle API to determine support status,
remaining support time, and OpenShift version compatibility.

On first run, fetches all operator lifecycle data from the API (single call)
and caches it locally. Subsequent runs reuse the cache until it expires.

Usage:
    python operator_lifecycle_check.py [--json] [--no-color] [--refresh-cache]

Requirements:
    - oc CLI authenticated to a cluster
    - requests Python package (pip install requests)
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' package required. Install with: pip install requests")
    sys.exit(1)

API_URL = "https://access.redhat.com/product-life-cycles/api/v1/products"
CACHE_DIR = Path.home() / ".cache" / "operator-lifecycle"
CACHE_FILE = CACHE_DIR / "products.json"
CACHE_TTL_HOURS = 24

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"


# ---------------------------------------------------------------------------
# Cache & API
# ---------------------------------------------------------------------------

def fetch_operator_products(force_refresh=False):
    """Fetch all operator lifecycle data from the API, with local caching.

    Returns a list of product dicts filtered to only those with
    openshift_compatibility data (i.e., OpenShift operators).
    """
    if not force_refresh and CACHE_FILE.exists():
        age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
        if age_hours < CACHE_TTL_HOURS:
            with open(CACHE_FILE) as f:
                return json.load(f)
            
    print(
        f"{DIM}Fetching lifecycle data from Red Hat API (cached for {CACHE_TTL_HOURS}h)...{RESET}",
        file=sys.stderr,
    )

    try:
        resp = requests.get(API_URL, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as e:
        if CACHE_FILE.exists():
            print(
                f"{YELLOW}Warning: API fetch failed ({e}), using stale cache.{RESET}",
                file=sys.stderr,
            )
            with open(CACHE_FILE) as f:
                return json.load(f)
        print(f"{RED}Error: Could not fetch lifecycle data: {e}{RESET}", file=sys.stderr)
        sys.exit(1)

    all_products = resp.json().get("data", [])

    operators = [p for p in all_products if p.get("is_operator")]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(operators, f)

    print(
        f"{DIM}Cached {len(operators)} operator products.{RESET}",
        file=sys.stderr,
    )
    return operators


def build_index(products):
    """Build lookup indexes from product data.

    Returns a dict keyed by:
      - package field (exact CSV package name match)
      - product name (case-insensitive)
      - all former names (case-insensitive)
    """
    index = {}
    for p in products:
        pkg = p.get("package", "")
        if pkg:
            index[pkg] = p
        index[p["name"].lower()] = p
        for former in p.get("former_names", []):
            index[former.lower()] = p
    return index


def match_operator(display_name, package, index):
    """Match a CSV operator against the product index.

    Attempts in order:
      1. Package name (exact match via API 'package' field)
      2. Display name (case-insensitive)
      3. "Red Hat " + display name
      4. Display name without "Red Hat " prefix
      5. Display name without trailing " Operator"
      6. Package name with hyphens as spaces
    """
    if package in index:
        return index[package]

    if not display_name:
        return None

    dn_lower = display_name.strip().lower()

    if dn_lower in index:
        return index[dn_lower]

    if not dn_lower.startswith("red hat "):
        candidate = f"red hat {dn_lower}"
        if candidate in index:
            return index[candidate]

    if dn_lower.startswith("red hat "):
        candidate = dn_lower[len("red hat "):]
        if candidate in index:
            return index[candidate]

    for suffix in (" operator", " for red hat openshift"):
        if dn_lower.endswith(suffix):
            candidate = dn_lower[: -len(suffix)]
            if candidate in index:
                return index[candidate]

    pkg_as_name = package.replace("-", " ").lower()
    if pkg_as_name in index:
        return index[pkg_as_name]

    return None


# ---------------------------------------------------------------------------
# Cluster data
# ---------------------------------------------------------------------------

def ctext(text, width, color=""):
    """Pad text to a fixed visible width, then wrap in ANSI color."""
    padded = str(text)[:width].ljust(width)
    return f"{color}{padded}{RESET}" if color else padded


def run_oc(*args):
    """Run an oc command and return parsed JSON."""
    cmd = ["oc"] + list(args) + ["-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"oc command failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def get_cluster_version():
    """Return (major_minor, full_version) for the cluster."""
    data = run_oc("get", "clusterversion")
    version = data["items"][0]["status"]["desired"]["version"]
    parts = version.split(".")
    return f"{parts[0]}.{parts[1]}", version


def get_installed_operators():
    """Return a dict of unique installed operators keyed by package name."""
    data = run_oc("get", "csv", "-A")
    operators = {}
    for item in data.get("items", []):
        csv_name = item["metadata"]["name"]
        display_name = item["spec"].get("displayName", "")
        version = item["spec"].get("version", "")
        package = csv_name.rsplit(".v", 1)[0] if ".v" in csv_name else csv_name

        if package not in operators:
            operators[package] = {
                "csv_name": csv_name,
                "display_name": display_name,
                "version": version,
                "package": package,
            }
    return operators


# ---------------------------------------------------------------------------
# Version & lifecycle helpers
# ---------------------------------------------------------------------------

def major_minor(version_str):
    """Extract major.minor from a version string like '4.18.3' -> '4.18'."""
    parts = version_str.strip().split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return version_str.strip()


def parse_date(s):
    """Parse an ISO date string. Returns datetime or None."""
    if not s or s == "N/A":
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def find_version(api_data, installed_version):
    """Find the API version entry matching the installed version."""
    installed_clean = installed_version.split("-")[0].strip()
    installed_mm = major_minor(installed_clean)

    for v in api_data.get("versions", []):
        if v["name"].strip() == installed_clean:
            return v

    for v in api_data.get("versions", []):
        if major_minor(v["name"]) == installed_mm:
            return v

    return None


def phase_end(version_data, phase_name):
    """Return (raw_string, parsed_datetime) for a lifecycle phase end date."""
    for phase in version_data.get("phases", []):
        if phase["name"].lower() == phase_name.lower():
            raw = phase.get("end_date", "N/A")
            return raw, parse_date(raw)
    return "N/A", None


def days_remaining(dt):
    """Days from now until dt. Negative means expired."""
    if dt is None:
        return None
    return (dt - datetime.now()).days


def format_end_date(raw, dt, use_color):
    """Format a support end date for display (unpadded)."""
    if dt:
        days = days_remaining(dt)
        date_str = dt.strftime("%Y-%m-%d")
        if days < 0:
            label = f"{date_str} (EXPIRED)"
            return (label, RED if use_color else "")
        elif days < 90:
            label = f"{date_str} ({days}d)"
            return (label, RED if use_color else "")
        elif days < 180:
            label = f"{date_str} ({days}d)"
            return (label, YELLOW if use_color else "")
        else:
            label = f"{date_str} ({days}d)"
            return (label, GREEN if use_color else "")

    if raw and raw != "N/A":
        return (raw, CYAN if use_color else "")

    return ("N/A", DIM if use_color else "")


def status_color(status, use_color):
    if not use_color:
        return ""
    s = status.lower()
    if "end of life" in s or "retired" in s:
        return RED
    if "maintenance" in s:
        return YELLOW
    if "full" in s:
        return GREEN
    return ""


def ocp_compat_info(version_data, cluster_minor):
    """Check OCP compatibility. Returns (is_compatible, compat_versions_list)."""
    raw = version_data.get("openshift_compatibility", "")
    if not raw:
        return None, []
    versions = [v.strip() for v in raw.split(",") if v.strip()]
    return cluster_minor in versions, versions


def summarize_ocp_versions(versions):
    """Condense a list like ['4.14','4.16','4.17','4.18'] into '4.14-4.18'."""
    if not versions:
        return "N/A"
    if len(versions) == 1:
        return versions[0]
    try:
        parsed = sorted(versions, key=lambda v: [int(x) for x in v.split(".")])
        return f"{parsed[0]}-{parsed[-1]}"
    except (ValueError, IndexError):
        return ", ".join(versions)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_operators(operators, products, cluster_minor, use_color):
    """Match each installed operator against cached lifecycle data."""
    index = build_index(products)
    results = []
    unmatched = []
    total = len(operators)

    for i, (package, op) in enumerate(operators.items(), 1):
        name_for_log = op["display_name"] or package
        print(f"{DIM}  [{i}/{total}] {name_for_log}{RESET}", file=sys.stderr)

        api_data = match_operator(op["display_name"], package, index)
        if api_data is None:
            unmatched.append(op)
            continue

        version_match = find_version(api_data, op["version"])
        if version_match is None:
            unmatched.append(op)
            continue

        status = version_match.get("type", "Unknown")
        tier = version_match.get("tier", "Unknown")

        full_raw, full_dt = phase_end(version_match, "Full support")
        maint_raw, maint_dt = phase_end(version_match, "Maintenance support")
        eus_raw, eus_dt = phase_end(version_match, "Extended update support")

        compatible, compat_versions = ocp_compat_info(version_match, cluster_minor)
        ocp_summary = summarize_ocp_versions(compat_versions)

        full_text, full_color = format_end_date(full_raw, full_dt, use_color)
        maint_text, maint_color = format_end_date(maint_raw, maint_dt, use_color)

        results.append(
            {
                "display_name": op["display_name"] or api_data["name"],
                "package": package,
                "version": major_minor(op["version"]),
                "installed_version": op["version"],
                "api_name": api_data["name"],
                "tier": tier,
                "status": status,
                "full_support_end_raw": full_raw,
                "full_support_end_date": full_dt.isoformat() if full_dt else None,
                "full_support_days": days_remaining(full_dt),
                "maintenance_end_raw": maint_raw,
                "maintenance_end_date": maint_dt.isoformat() if maint_dt else None,
                "maintenance_days": days_remaining(maint_dt),
                "eus_end_raw": eus_raw,
                "eus_end_date": eus_dt.isoformat() if eus_dt else None,
                "ocp_compatible": compatible,
                "ocp_compatible_versions": compat_versions,
                "ocp_summary": ocp_summary,
                "_full_text": full_text,
                "_full_color": full_color,
                "_maint_text": maint_text,
                "_maint_color": maint_color,
            }
        )

    return results, unmatched


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def sort_by_urgency(results):
    """Sort: expired first, then ending soonest, then alphabetical."""
    def key(r):
        days = r.get("full_support_days")
        if days is None:
            return (2, 9999, r["display_name"].lower())
        if days < 0:
            return (0, days, r["display_name"].lower())
        return (1, days, r["display_name"].lower())
    return sorted(results, key=key)


def print_report(results, unmatched, cluster_minor, cluster_full, use_color):
    """Print the formatted lifecycle report."""
    c_bold = BOLD if use_color else ""
    c_dim = DIM if use_color else ""
    c_cyan = CYAN if use_color else ""
    c_green = GREEN if use_color else ""
    c_red = RED if use_color else ""
    c_yellow = YELLOW if use_color else ""
    c_reset = RESET if use_color else ""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{c_bold}{'=' * 120}{c_reset}")
    print(f"{c_bold}  OpenShift Operator Lifecycle Report{c_reset}")
    print(f"{c_bold}{'=' * 120}{c_reset}")
    print(
        f"  Cluster Version : {c_cyan}{cluster_full}{c_reset}"
        f"  (minor: {c_cyan}{cluster_minor}{c_reset})"
    )
    print(f"  Report Date     : {c_cyan}{now_str}{c_reset}")
    print(f"  Data Source     : Red Hat Product Life Cycle API v1")
    print(f"{c_bold}{'=' * 120}{c_reset}\n")

    if not results and not unmatched:
        print("  No operators found.\n")
        return

    W_NAME = 42
    W_VER = 8
    W_TIER = 10
    W_STATUS = 22
    W_FULL = 30
    W_MAINT = 30
    W_OCP = 18

    if results:
        results = sort_by_urgency(results)

        hdr = (
            f"  {'Operator':<{W_NAME}} {'Ver':<{W_VER}} {'Tier':<{W_TIER}} "
            f"{'Status':<{W_STATUS}} {'Full Support Ends':<{W_FULL}} "
            f"{'Maintenance Ends':<{W_MAINT}} {'OCP Compat':<{W_OCP}}"
        )
        print(f"{c_bold}{hdr}{c_reset}")
        print(f"  {'─' * 160}")

        for r in results:
            s_color = status_color(r["status"], use_color)

            if r["ocp_compatible"] is True:
                ocp_str = f"{r['ocp_summary']} ✓"
                ocp_color = c_green
            elif r["ocp_compatible"] is False:
                ocp_str = f"{r['ocp_summary']} ✗"
                ocp_color = c_red
            else:
                ocp_str = r["ocp_summary"]
                ocp_color = c_dim

            row = (
                f"  {ctext(r['display_name'], W_NAME)}"
                f" {ctext(r['version'], W_VER)}"
                f" {ctext(r['tier'], W_TIER)}"
                f" {ctext(r['status'], W_STATUS, s_color)}"
                f" {ctext(r['_full_text'], W_FULL, r['_full_color'])}"
                f" {ctext(r['_maint_text'], W_MAINT, r['_maint_color'])}"
                f" {ctext(ocp_str, W_OCP, ocp_color)}"
            )
            print(row)

    if unmatched:
        print(f"\n{c_bold}  Operators Not Found in Lifecycle API:{c_reset}")
        print(f"  {'─' * 80}")
        for u in sorted(unmatched, key=lambda x: x["display_name"].lower()):
            print(
                f"  {c_yellow}•{c_reset} {u['display_name']:<40}"
                f"  (pkg: {u['package']}, ver: {u['version']})"
            )
        print(
            f"\n  {c_dim}These may be community operators or not yet tracked "
            f"in the Red Hat Product Life Cycle API.{c_reset}"
        )

    matched = len(results)
    total = matched + len(unmatched)
    print(f"\n  {c_dim}Matched {matched}/{total} operators.{c_reset}\n")


def print_json(results, unmatched, cluster_minor, cluster_full):
    """Output machine-readable JSON to stdout."""
    for r in results:
        for k in list(r.keys()):
            if k.startswith("_"):
                del r[k]

    output = {
        "cluster_version": cluster_full,
        "cluster_minor": cluster_minor,
        "report_date": datetime.now().isoformat(),
        "operators": results,
        "unmatched": [
            {
                "package": u["package"],
                "display_name": u["display_name"],
                "version": u["version"],
            }
            for u in unmatched
        ],
    }
    print(json.dumps(output, indent=2, default=str))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Check OpenShift operator lifecycle status against the Red Hat API."
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force refresh of cached lifecycle data from the API",
    )
    args = parser.parse_args()

    use_color = not args.json and not args.no_color and sys.stdout.isatty()

    products = fetch_operator_products(force_refresh=args.refresh_cache)

    print(f"{DIM}Fetching cluster version...{RESET}", file=sys.stderr)
    try:
        cluster_minor, cluster_full = get_cluster_version()
    except Exception as e:
        print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
        print("Make sure 'oc' is installed and you are logged in.", file=sys.stderr)
        sys.exit(1)

    print(f"{DIM}Fetching installed operators...{RESET}", file=sys.stderr)
    try:
        operators = get_installed_operators()
    except Exception as e:
        print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)

    print(
        f"{DIM}Found {len(operators)} operators. Matching against "
        f"{len(products)} known products...{RESET}",
        file=sys.stderr,
    )

    results, unmatched = analyze_operators(operators, products, cluster_minor, use_color)

    if args.json:
        print_json(results, unmatched, cluster_minor, cluster_full)
    else:
        print_report(results, unmatched, cluster_minor, cluster_full, use_color)


if __name__ == "__main__":
    main()
