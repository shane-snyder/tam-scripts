# OpenShift Upgrade Planner

Plans an OpenShift upgrade with a focus on operators. It reads the cluster version and installed operators from the cluster you're logged into, or from a **must-gather**. For a target OCP version, it tells you **which exact operator versions to install, in which channel, and when**: before or after each OCP update.

## What it reports

1. **OCP update path**: every intermediate release, from the [OpenShift update graph API](https://api.openshift.com/api/upgrades_info/v1/graph). If the target can't be reached (for example, it's older than your current z-stream), it lists the releases in that minor that you *can* reach. It also notes the EUS-to-EUS worker-pause option.
2. **Pre-flight checks**:
   - ClusterVersion or OLM reporting `Upgradeable=False`
   - `olm.maxOpenShiftVersion` set on installed CSVs
   - InstallPlans waiting for approval
   - Subscriptions with no installed CSV, and channels missing from the catalog
   - Newer versions already available in your current channel
3. **Support on current OCP**: for each operator, its support phase (Full, Maintenance or Extended, with end dates), whether the installed version is supported on the current OCP version, and the newest version supported there, with the exact route to get to it.
4. **Support matrix**: whether the installed version is supported on each OCP minor from current to target, and whether it still ships in that OCP version's catalog.
5. **Upgrade runbook**: a chronological list of stages. Each stage lists the operator steps to do on that OCP version, followed by the OCP update and the operators that must be finished before you start it.

   | Tag | Meaning |
   |---|---|
   | `NOW` | Not supported on the current OCP version. Fix this first. |
   | `GATE` | Required before the next OCP update; the installed version doesn't support the next minor. |
   | `AFTER` | Required once the cluster reaches this OCP version, for example NMState, MetalLB and CNV, which only support their own minor. |
   | `ALIGN` | Recommended: match a platform-aligned operator to the cluster minor (for example, ODF 4.21 → 4.22 after OCP 4.22). |
   | `OPTIONAL` | A newer version that's also supported on the target. |
   | `BLOCKED` | No version supports that OCP version. |

   Each step shows the **exact InstallPlans OLM will create**, including channel switches. For example:
   - GitOps `1.20.1 → 1.22.2` installs `gitops-1.22: 1.22.0 → 1.22.2`. 1.22.2's skipRange starts at 1.22.0, so 1.22.0 has to be installed first.
   - ACM `2.15.4 → 2.17.3` is a single InstallPlan. 2.17.3's skipRange is `>=2.15.0`.
   - ACM `2.14.1 → 2.17.3` installs `release-2.15: 2.15.8 ⇒ release-2.17: 2.17.3`.
6. **Summary at target**: the required and newest versions on the target OCP version, with the channel for each.

## Prerequisites

- Either `oc` logged in to the cluster (read access to ClusterVersion, Subscriptions, CSVs, InstallPlans, CatalogSources, PackageManifests and ClusterOperators), or a must-gather (see [Running from a must-gather](#running-from-a-must-gather))
- Python 3.8+
- Internet access to `api.openshift.com`, `access.redhat.com` and `catalog.redhat.com`. No Red Hat login or registry credentials are needed.

## Setup

```bash
cd operator_versions
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt    # requests, plus pyyaml for --must-gather
```

## Usage

```bash
# Plan to the newest release in a minor, and also write an HTML report
python ocp_upgrade_planner.py --target 4.22 --html plan.html

# Plan to a specific release
python ocp_upgrade_planner.py --target 4.22.18

# Plan from a must-gather instead of a live cluster (directory or .tar.gz)
python ocp_upgrade_planner.py --target 4.22 --must-gather ~/cases/must-gather.tar.gz --html plan.html

# Required operator moves go straight to the newest supported version (fewer later moves)
python ocp_upgrade_planner.py --target 4.22 --prefer-latest

# What-if: pretend the cluster is on a different OCP version (operators still come from the cluster)
python ocp_upgrade_planner.py --target 4.22 --from 4.20.10

# What-if for operators that aren't installed (repeatable), with or without the cluster's own
python ocp_upgrade_planner.py --target 4.22 --from 4.20.10 --only-simulated \
  --simulate-operator odf-operator=4.20.1-rhodf:stable-4.20 \
  --simulate-operator advanced-cluster-management=2.14.1:release-2.14

# Include conditional (known-risk) update edges if no recommended path exists
python ocp_upgrade_planner.py --target 4.22.16 --allow-conditional

# Machine-readable output, or no color for logs
python ocp_upgrade_planner.py --target 4.22 --json
python ocp_upgrade_planner.py --target 4.22 --no-color > plan.txt

# Other switches
#   --lifecycle-only   skip the Ecosystem Catalog and suggest version streams only
#   --no-catalog       skip per-operator PackageManifest lookups on the cluster
#   --refresh-cache    ignore cached API data (cache: ~/.cache/ocp-upgrade-planner, 24h)
```

## Running from a must-gather

Pass `--must-gather` with a must-gather directory, the `must-gather.local.<id>` folder, or the `.tar`/`.tar.gz` a customer uploaded. No cluster access or `oc` login is needed. The planner reads the same objects it would read live:

- ClusterVersion and the `operator-lifecycle-manager` ClusterOperator
- Subscriptions, ClusterServiceVersions, InstallPlans and CatalogSources
- Nodes, for the architecture

A must-gather can't answer "is a newer version available in my channel right now", because PackageManifests are only queried on a live cluster. Everything else works, and the header shows when the must-gather was collected.

If the must-gather is missing the OLM objects, the planner warns and prints a collection command. A full must-gather can also refuse to run when the node it lands on is over 70% disk (`Disk usage exceeds the volume percentage`). In both cases you can collect just what the planner needs with `oc adm inspect`. This runs from your workstation and creates no pods on the cluster:

```bash
oc adm inspect clusterversion/version clusteroperator/operator-lifecycle-manager --dest-dir=planner-data
oc adm inspect subscriptions.operators.coreos.com,clusterserviceversions.operators.coreos.com,\
installplans.operators.coreos.com,catalogsources.operators.coreos.com -A --dest-dir=planner-data
python ocp_upgrade_planner.py --target 4.22 --must-gather planner-data
```

The output is the same layout as a must-gather, so it also works when a customer collects it and sends it to you. Add `--arch` if nodes weren't collected and the cluster isn't amd64.

## How it works

**When to move** comes from the [Red Hat Product Life Cycle API](https://access.redhat.com/product-life-cycles/api/v1/products) (each version's `openshift_compatibility`). For each OCP stage the cluster passes through:

1. If the operator doesn't support the OCP version it's on, it moves (`NOW` on the current version, `AFTER` on later stops).
2. Platform-aligned operators that are versioned like OCP itself (ODF, LVMS, NMState, MetalLB, CNV) are kept on the cluster's minor. ODF 4.20 supports OCP 4.20 and 4.21, so on an EUS 4.20 → 4.22 upgrade the planner **gates OCP 4.22 on ODF 4.21**, then recommends ODF 4.22 afterwards.
3. Before each OCP update, any operator that doesn't support the next minor gets a `GATE` step. Required moves go to the lowest version that covers every remaining OCP version through the target, so each operator moves once. With `--prefer-latest` they go to the newest supported version instead.
4. Finally, it lists the newest version supported on the target as `OPTIONAL`.

**What to install** comes from the [Red Hat Ecosystem Catalog API](https://catalog.redhat.com/api/containers/v1/ui/), which publishes the contents of each operator index image (`redhat-operator-index:v4.x`, certified, community, marketplace) per OCP version:

- Each CatalogSource is mapped to its index by image name, which also works for mirrored copies. Bundles are fetched for every OCP minor on the path, with one API call per catalog and minor.
- Each step uses the catalog the cluster will actually have at that point.
- OLM's choice is modeled exactly. From the installed CSV, OLM installs the newest bundle in the subscribed channel that `replaces` it, `skips` it, or has a `skipRange` covering it. The planner searches across channels for the fewest InstallPlans to the target version, and never routes through a channel whose next bundle would overshoot it.
- It keeps your channel style. A versioned channel such as `gitops-1.20` moves to `gitops-1.22`; a rolling channel such as `stable` or `latest` stays as it is.
- Pre-release channels (`candidate`, `fast`, `preview`, `alpha`, `beta`) are only used if you're already subscribed to one or it's the package default.
- With Automatic approval, it warns when OLM would carry on past the target version to the channel head.

## Caveats

- Catalogs built with OLM's semver template (for example, Web Terminal, Grafana and DevWorkspace) don't expose upgrade edges through the API. Their install sequence is inferred as "latest z of each newer minor" and marked `inferred from semver order`.
- Operators missing from the lifecycle API (community, certified, or versions that are too old) get catalog-only guidance: channel availability and the newest version at each stage. Their support status shows as `?`.
- Custom CatalogSources whose image isn't a Red Hat index fall back to lifecycle-only stream suggestions.
- Confirm against operator release notes before changing production.
