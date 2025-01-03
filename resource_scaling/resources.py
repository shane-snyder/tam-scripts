import subprocess
import json

# ANSI color codes for output
RED = '\033[91m'
RESET = '\033[0m'


def get_resource_quota(namespace):
    """Retrieve resource quota for the namespace."""
    result = subprocess.run(
        ["kubectl", "get", "resourcequota", "-n", namespace, "-o", "json"],
        capture_output=True, text=True
    )
    quotas = json.loads(result.stdout)
    if "items" in quotas and quotas["items"]:
        return quotas["items"][0]["spec"]["hard"]
    return {}

def get_deployments(namespace):
    """Retrieve all deployments in a given namespace."""
    result = subprocess.run(
        ["kubectl", "get", "deployments", "-n", namespace, "-o", "json"],
        capture_output=True, text=True
    )
    deployments = json.loads(result.stdout)
    return deployments.get("items", [])

def get_hpa(namespace):
    """Retrieve all HPAs in a given namespace."""
    result = subprocess.run(
        ["kubectl", "get", "hpa", "-n", namespace, "-o", "json"],
        capture_output=True, text=True
    )
    hpas = json.loads(result.stdout)
    return hpas.get("items", [])

def convert_memory_to_mi(memory_str):
    """Convert memory string to MiB value."""
    if isinstance(memory_str, int):
        return memory_str
    
    memory_str = str(memory_str)
    if memory_str.endswith('Gi'):
        return int(memory_str.replace('Gi', '')) * 1024
    elif memory_str.endswith('Mi'):
        return int(memory_str.replace('Mi', ''))
    elif memory_str.endswith('Ki'):
        return int(memory_str.replace('Ki', '')) // 1024
    else:
        return int(memory_str)

def calculate_total_resources(requests, limits, max_replicas):
    """Calculate total resources for max replicas."""
    total_requests = {
        "cpu": int(requests.get("cpu", "0m").replace("m", "")) * max_replicas,
        "memory": convert_memory_to_mi(requests.get("memory", "0Mi")) * max_replicas
    }
    total_limits = {
        "cpu": int(limits.get("cpu", "0m").replace("m", "")) * max_replicas,
        "memory": convert_memory_to_mi(limits.get("memory", "0Mi")) * max_replicas
    }
    return total_requests, total_limits

def get_all_namespaces():
    """Retrieve all namespaces in the cluster."""
    result = subprocess.run(
        ["kubectl", "get", "namespaces", "-o", "json"],
        capture_output=True, text=True
    )
    namespaces = json.loads(result.stdout)
    return [ns["metadata"]["name"] for ns in namespaces.get("items", [])]

def get_namespaces_with_quotas():
    """Retrieve all namespaces that have resource quotas."""
    result = subprocess.run(
        ["kubectl", "get", "resourcequota", "--all-namespaces", "-o", "json"],
        capture_output=True, text=True
    )
    quotas = json.loads(result.stdout)
    # Get unique namespaces that have quotas
    return list(set(item["metadata"]["namespace"] for item in quotas.get("items", [])))

def main(namespace):
    if namespace.lower() == "all":
        namespaces = get_namespaces_with_quotas()
        if not namespaces:
            print("No namespaces with resource quotas found.")
            return
    else:
        namespaces = [namespace]

    for ns in namespaces:
        print(f"\n{'-'*80}")
        print(f"Analyzing namespace: {ns}")
        print(f"{'-'*80}")
        
        deployments = get_deployments(ns)
        hpas = get_hpa(ns)
        resource_quota = get_resource_quota(ns)

        hpa_map = {hpa["spec"]["scaleTargetRef"]["name"]: hpa for hpa in hpas}

        total_cpu_requests = 0
        total_memory_requests = 0
        total_cpu_limits = 0
        total_memory_limits = 0

        print("\nDeployment Resource Usage:")

        for deployment in deployments:
            name = deployment["metadata"]["name"]
            containers = deployment["spec"]["template"]["spec"]["containers"]

            if name not in hpa_map:
                print(f"  Deployment: {name} has no corresponding HPA")
                continue

            hpa = hpa_map[name]
            max_replicas = hpa["spec"].get("maxReplicas", 1)

            deployment_cpu_requests = 0
            deployment_memory_requests = 0
            deployment_cpu_limits = 0
            deployment_memory_limits = 0

            for container in containers:
                requests = container.get("resources", {}).get("requests", {})
                limits = container.get("resources", {}).get("limits", {})
                total_requests, total_limits = calculate_total_resources(requests, limits, max_replicas)

                deployment_cpu_requests += total_requests["cpu"]
                deployment_memory_requests += total_requests["memory"]
                deployment_cpu_limits += total_limits["cpu"]
                deployment_memory_limits += total_limits["memory"]

            total_cpu_requests += deployment_cpu_requests
            total_memory_requests += deployment_memory_requests
            total_cpu_limits += deployment_cpu_limits
            total_memory_limits += deployment_memory_limits

            print(f"  Deployment: {name}")
            print(f"    Max Replicas: {max_replicas}")
            print(f"    Total CPU Requests: {deployment_cpu_requests}m")
            print(f"    Total Memory Requests: {deployment_memory_requests}Mi")
            print(f"    Total CPU Limits: {deployment_cpu_limits}m")
            print(f"    Total Memory Limits: {deployment_memory_limits}Mi")

        # Convert quota to milliCPU and MiB for comparison
        quota_cpu_requests = int(float(resource_quota.get("requests.cpu", "0").replace("m", "")) * 1000)
        quota_memory_requests = int(resource_quota.get("requests.memory", "0").replace("Gi", "")) * 1024
        quota_cpu_limits = int(float(resource_quota.get("limits.cpu", "0").replace("m", "")) * 1000)
        quota_memory_limits = int(resource_quota.get("limits.memory", "0").replace("Gi", "")) * 1024

        print("\nResource Quota:")
        print(f"  CPU Requests: {quota_cpu_requests}m")
        print(f"  Memory Requests: {quota_memory_requests}Mi")
        print(f"  CPU Limits: {quota_cpu_limits}m")
        print(f"  Memory Limits: {quota_memory_limits}Mi")

        # Check if resources exceed the quota
        exceeded = False

        if total_cpu_requests > quota_cpu_requests:
            print(f"  {RED}CPU Requests exceed quota by {total_cpu_requests - quota_cpu_requests}m{RESET}")
            exceeded = True
        if total_memory_requests > quota_memory_requests:
            print(f"  {RED}Memory Requests exceed quota by {total_memory_requests - quota_memory_requests}Mi{RESET}")
            exceeded = True
        if total_cpu_limits > quota_cpu_limits:
            print(f"  {RED}CPU Limits exceed quota by {total_cpu_limits - quota_cpu_limits}m{RESET}")
            exceeded = True
        if total_memory_limits > quota_memory_limits:
            print(f"  {RED}Memory Limits exceed quota by {total_memory_limits - quota_memory_limits}Mi{RESET}")
            exceeded = True

        if not exceeded:
            print("  All deployments fit within the resource quota.")

if __name__ == "__main__":
    namespace = input("Enter the namespace: ")
    main(namespace)
