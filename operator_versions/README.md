# Operator Lifecycle Checker

Checks installed OpenShift operators against the [Red Hat Product Life Cycle API](https://access.redhat.com/product-life-cycles/api/v1/) to determine support status, remaining support time, and OpenShift version compatibility.

## Prerequisites

- `oc` CLI authenticated to a cluster (`oc login`)
- Python 3.6+

## Setup

```bash
cd operator_versions

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
# Activate the virtual environment (if not already active)
source venv/bin/activate

# Standard colored table output
python operator_lifecycle_check.py

# JSON output (for piping to other tools)
python operator_lifecycle_check.py --json

# Disable colors (for logging or non-TTY)
python operator_lifecycle_check.py --no-color

# Force refresh of cached API data
python operator_lifecycle_check.py --refresh-cache

# Deactivate when done
deactivate
```

## How It Works

1. **Fetches all operator lifecycle data** from the Red Hat API in a single call and caches it locally (`~/.cache/operator-lifecycle/products.json`, 24-hour TTL). Subsequent runs skip the API call entirely.
2. **Queries the cluster** for installed operators (`oc get csv -A`) and the cluster version (`oc get clusterversion`).
3. **Auto-matches** each operator's CSV `displayName` against cached API product names and former names using case-insensitive fuzzy matching (with/without "Red Hat " prefix, suffix stripping, etc.).
4. **Reports** for each matched operator:
   - **Support status** — Full Support, Maintenance Support, or End of Life
   - **Full support end date** with days remaining (color-coded: green > 180d, yellow < 180d, red < 90d)
   - **Maintenance support end date** with days remaining
   - **OCP compatibility** — whether the installed operator version lists your cluster's OCP version as supported
   - **Lifecycle tier** — Platform Aligned, Platform Agnostic, or Rolling Stream

Results are sorted by urgency: expired operators first, then those ending soonest.

## Name Matching

The Red Hat API includes a `package` field on each product that matches the CSV package name in the cluster (e.g., `rhods-operator`, `gatekeeper-operator-product`). The script matches on this field first for an exact hit.

As a fallback, it also compares the CSV `displayName` against all product names and former names (case-insensitive, with/without "Red Hat " prefix). Between the `package` field and the display name matching, no manual mapping or configuration is needed.

## Cache

API data is cached at `~/.cache/operator-lifecycle/products.json` with a 24-hour TTL. Use `--refresh-cache` to force a fresh pull. If the API is unreachable, the script falls back to a stale cache with a warning.

## Data Source

All lifecycle data comes from the [Red Hat Product Life Cycle API v1](https://access.redhat.com/product-life-cycles/api/v1/products). The operator lifecycle classifications are documented at [OpenShift Operator Life Cycles](https://access.redhat.com/support/policy/updates/openshift_operators).
