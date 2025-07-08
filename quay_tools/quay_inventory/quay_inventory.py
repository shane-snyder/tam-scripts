import requests
import json
import os
import getpass
import csv
from datetime import datetime

# --- Configuration ---
QUAY_HOSTNAME = os.getenv('QUAY_HOSTNAME')
if not QUAY_HOSTNAME:
    QUAY_HOSTNAME = input("Enter your Quay hostname (e.g., quay.example.com): ")

ACCESS_TOKEN = os.getenv('ACCESS_TOKEN')
if not ACCESS_TOKEN:
    ACCESS_TOKEN = getpass.getpass("Enter your Quay OAuth2 Access Token: ")

def validate_and_build_api_base_url(hostname):
    """Ensures the hostname is clean and builds the full v1 API base URL."""
    if hostname.startswith("https://"):
        hostname = hostname[8:]
    elif hostname.startswith("http://"):
        hostname = hostname[7:]
    hostname = hostname.rstrip('/')
    return f"https://{hostname}/api/v1"

API_BASE_URL = validate_and_build_api_base_url(QUAY_HOSTNAME)

def get_namespaces():
    """Gets all namespaces (user's own and their organizations)."""
    print("Fetching user and organization namespaces...")
    url = f"{API_BASE_URL}/user/"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    namespaces = [data.get('username')] if 'username' in data else []
    namespaces.extend([org['name'] for org in data.get('organizations', [])])
    print(f"Found namespaces: {namespaces}")
    return namespaces

def get_repositories_for_namespace(namespace):
    """Fetches all repositories for a given namespace, handling pagination."""
    repos = []
    url = f"{API_BASE_URL}/repository"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    params = {'namespace': namespace}
    while True:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        repos.extend(data.get('repositories', []))
        if 'next_page' in data:
            params['next_page'] = data['next_page']
        else:
            break
    return repos

def get_repository_details(repo_name):
    """Fetches repository details, including all tags."""
    url = f"{API_BASE_URL}/repository/{repo_name}"
    params = {'includeTags': 'true'}
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()

def get_repository_logs(repo_name):
    """Fetches usage logs for a given repository."""
    url = f"{API_BASE_URL}/repository/{repo_name}/logs"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    params = {"limit": 50}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json().get('logs', [])

def main():
    """Main function to find repos, list images, and show usage."""
    print(f"--- Connecting to API Base: {API_BASE_URL} ---\n")
    try:
        namespaces = get_namespaces()
        all_repositories = []
        print("\nFetching repositories for each namespace...")
        for ns in namespaces:
            print(f"  - Checking namespace: {ns}")
            repos_in_ns = get_repositories_for_namespace(ns)
            if repos_in_ns:
                all_repositories.extend(repos_in_ns)
        
        print(f"\nSuccessfully found a total of {len(all_repositories)} repositories.")
        
        inventory_data = []

        for repo in all_repositories:
            full_repo_name = f"{repo['namespace']}/{repo['name']}"
            print(f"Processing repository: {full_repo_name}...")

            try:
                repo_details = get_repository_details(full_repo_name)
                tags_dict = repo_details.get('tags', {})
                tag_count = len(tags_dict)
                latest_push_time_str = "N/A"
                unique_images_count = 0

                if tags_dict:
                    unique_images_count = len({tag_data['manifest_digest'] for tag_data in tags_dict.values() if 'manifest_digest' in tag_data})
                    timestamps = [datetime.strptime(tag_data['last_modified'].split(' -')[0], '%a, %d %b %Y %H:%M:%S') for tag_data in tags_dict.values()]
                    if timestamps:
                        latest_push_time = max(timestamps)
                        latest_push_time_str = latest_push_time.strftime('%Y-%m-%d %H:%M:%S')
                
                push_history_str = "No recent push activity found."
                pull_history_str = "No recent pull activity found."
                logs = get_repository_logs(full_repo_name)
                if logs:
                    push_activity = {}
                    pull_activity = {}
                    for log in logs:
                        log_kind = log.get('kind')
                        performer = log.get('performer', {}).get('name', 'System/Automation')
                        
                        # Capture push history
                        if log_kind == 'push_repo':
                            if performer not in push_activity:
                                timestamp_str = log['datetime'].split('-0000')[0].strip()
                                timestamp = datetime.strptime(timestamp_str, "%a, %d %b %Y %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
                                push_activity[performer] = timestamp
                        
                        # Capture pull history
                        if log_kind == 'pull_repo':
                            if performer not in pull_activity:
                                timestamp_str = log['datetime'].split('-0000')[0].strip()
                                timestamp = datetime.strptime(timestamp_str, "%a, %d %b %Y %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
                                pull_activity[performer] = timestamp
                    
                    if push_activity:
                        push_history_str = "\n".join([f"User: {user}, Timestamp: {timestamp}" for user, timestamp in push_activity.items()])
                    
                    if pull_activity:
                        pull_history_str = "\n".join([f"User: {user}, Timestamp: {timestamp}" for user, timestamp in pull_activity.items()])

                inventory_data.append({
                    'Namespace': repo['namespace'],
                    'Repository': repo['name'],
                    'Tag Count': tag_count,
                    'Unique Image Count': unique_images_count,
                    'Latest Push Timestamp': latest_push_time_str,
                    'Push History': push_history_str,
                    'Pull History': pull_history_str
                })

            except requests.exceptions.HTTPError as e:
                print(f"  ERROR processing repository {full_repo_name}: {e}")
            
        output_filename = 'quay_repository_inventory.csv'
        print(f"\nProcessing complete. Writing data to {output_filename}...")
        
        if inventory_data:
            with open(output_filename, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['Namespace', 'Repository', 'Tag Count', 'Unique Image Count', 'Latest Push Timestamp', 'Push History', 'Pull History']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(inventory_data)
            print("Successfully saved inventory to CSV.")
        else:
            print("No repository data found to write to CSV.")

    except requests.exceptions.RequestException as e:
        print(f"An API error occurred: {e}")
    except KeyError as e:
        print(f"An unexpected error occurred. Missing key in data: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()