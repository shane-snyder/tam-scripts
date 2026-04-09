import requests
import json
import os
import getpass
import csv
import argparse
from datetime import datetime, timezone
from urllib.parse import quote

# --- CLI Arguments ---
parser = argparse.ArgumentParser(
    description="Generate a Quay repository inventory CSV with tag, usage, and cleanup data.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
examples:
  python3 quay_inventory.py
  python3 quay_inventory.py --stale-days 30
  python3 quay_inventory.py --stale-days 365

environment variables:
  QUAY_HOSTNAME   Quay hostname (e.g. quay.example.com) — prompted if not set
  ACCESS_TOKEN    Quay OAuth2 access token — prompted if not set
    """
)
parser.add_argument(
    '--stale-days',
    type=int,
    default=180,
    metavar='DAYS',
    help='Number of days without a push after which a tag is considered stale (default: 180)'
)
args = parser.parse_args()
STALE_DAYS_THRESHOLD = args.stale_days

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

def get_repository_size(repo_name):
    """Fetches the total compressed size of a repository in bytes."""
    url = f"{API_BASE_URL}/repository/{repo_name}/quota"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            quotas = resp.json()
            if quotas:
                return quotas[0].get('limit_bytes') or quotas[0].get('used_bytes', 0)
        return None
    except requests.exceptions.RequestException:
        return None

def get_repository_logs(repo_name):
    """Fetches usage logs for a given repository."""
    url = f"{API_BASE_URL}/repository/{repo_name}/logs"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    params = {"limit": 50}
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 501:
        return []  # Splunk logs model does not support log lookups
    response.raise_for_status()
    return response.json().get('logs', [])

def get_repository_user_permissions(repo_name):
    """Returns dict[user] = role for direct user permissions on the repository."""
    url = f"{API_BASE_URL}/repository/{repo_name}/permissions/user/"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        perms = data.get('permissions', {})
        return {user: details.get('role') for user, details in perms.items()}
    except requests.exceptions.RequestException as e:
        print(f"    Warning: Could not fetch user permissions for {repo_name}: {e}")
        return {}

def get_repository_team_permissions(repo_name):
    """Returns dict[team_name] = role for team permissions on the repository."""
    url = f"{API_BASE_URL}/repository/{repo_name}/permissions/team/"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        perms = data.get('permissions', {})
        return {team: details.get('role') for team, details in perms.items()}
    except requests.exceptions.RequestException as e:
        print(f"    Warning: Could not fetch team permissions for {repo_name}: {e}")
        return {}

def get_team_members(org_name, team_name):
    """Returns list of usernames for a given team in an org."""
    url = f"{API_BASE_URL}/organization/{org_name}/team/{quote(team_name, safe='')}/members"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        members = data.get('members', data if isinstance(data, list) else [])
        users = []
        for m in members:
            username = (m.get('name') if isinstance(m, dict) else None) or (m.get('username') if isinstance(m, dict) else None)
            if username:
                users.append(username)
        return users
    except requests.exceptions.RequestException as e:
        print(f"    Warning: Could not fetch members for team {org_name}/{team_name}: {e}")
        return []

def format_size(size_bytes):
    """Converts bytes to a human-readable string."""
    if size_bytes is None:
        return "N/A"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"

def parse_tag_datetime(raw):
    """Parses a Quay tag last_modified string into a naive UTC datetime."""
    try:
        return datetime.strptime(raw.split(' -')[0].strip(), '%a, %d %b %Y %H:%M:%S')
    except (ValueError, AttributeError):
        return None

def main():
    """Main function to find repos, list images, show usage, and permissions."""
    print(f"--- Connecting to API Base: {API_BASE_URL} ---\n")
    now = datetime.utcnow()
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
                stale_tag_count = 0
                duplicate_tags_str = "None"

                tags_str = "None"
                if tags_dict:
                    digests = [tag_data['manifest_digest'] for tag_data in tags_dict.values() if 'manifest_digest' in tag_data]
                    unique_images_count = len(set(digests))

                    # Find duplicate tags (multiple tag names pointing to the same digest)
                    digest_to_tags = {}
                    for tag_name, tag_data in tags_dict.items():
                        digest = tag_data.get('manifest_digest')
                        if digest:
                            digest_to_tags.setdefault(digest, []).append(tag_name)
                    duplicates = {d: names for d, names in digest_to_tags.items() if len(names) > 1}
                    if duplicates:
                        dup_lines = [f"{', '.join(sorted(names))} → same image" for names in duplicates.values()]
                        duplicate_tags_str = "\n".join(dup_lines)

                    tag_lines = []
                    for tag_name in sorted(tags_dict.keys()):
                        tag_data = tags_dict[tag_name]
                        modified_raw = tag_data.get('last_modified', '')
                        modified_dt = parse_tag_datetime(modified_raw)
                        if modified_dt:
                            age_days = (now - modified_dt).days
                            date_str = modified_dt.strftime('%d %b %Y')
                            age_str = f"{age_days}d ago"
                            if age_days >= STALE_DAYS_THRESHOLD:
                                stale_tag_count += 1
                        else:
                            date_str = 'N/A'
                            age_str = 'N/A'
                        tag_lines.append(f"{tag_name} ({date_str}, {age_str})")
                    tags_str = "\n".join(tag_lines)

                    timestamps = [parse_tag_datetime(td.get('last_modified', '')) for td in tags_dict.values()]
                    timestamps = [t for t in timestamps if t]
                    if timestamps:
                        latest_push_time_str = max(timestamps).strftime('%Y-%m-%d %H:%M:%S')

                push_history_str = "No recent push activity found."
                pull_history_str = "No recent pull activity found."
                logs = get_repository_logs(full_repo_name)
                if logs:
                    push_activity = {}
                    pull_activity = {}
                    for log in logs:
                        log_kind = log.get('kind')
                        performer = log.get('performer', {}).get('name', 'System/Automation')

                        if log_kind == 'push_repo':
                            if performer not in push_activity:
                                timestamp_str = log['datetime'].split('-0000')[0].strip()
                                timestamp = datetime.strptime(timestamp_str, "%a, %d %b %Y %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
                                push_activity[performer] = timestamp

                        if log_kind == 'pull_repo':
                            if performer not in pull_activity:
                                timestamp_str = log['datetime'].split('-0000')[0].strip()
                                timestamp = datetime.strptime(timestamp_str, "%a, %d %b %Y %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
                                pull_activity[performer] = timestamp

                    if push_activity:
                        push_history_str = "\n".join([f"User: {user}, Timestamp: {timestamp}" for user, timestamp in push_activity.items()])

                    if pull_activity:
                        pull_history_str = "\n".join([f"User: {user}, Timestamp: {timestamp}" for user, timestamp in pull_activity.items()])

                repo_size = get_repository_size(full_repo_name)

                # Permissions: direct users + users via teams
                direct_user_perms = get_repository_user_permissions(full_repo_name)
                team_perms = get_repository_team_permissions(full_repo_name)

                effective_user_sources = {}
                for user, role in direct_user_perms.items():
                    if not role:
                        continue
                    effective_user_sources.setdefault(user, []).append(f"direct:{role}")

                org_name = repo.get('namespace')
                if org_name and team_perms:
                    for team, role in team_perms.items():
                        if not role:
                            continue
                        members = get_team_members(org_name, team)
                        for user in members:
                            effective_user_sources.setdefault(user, []).append(f"team:{team}:{role}")

                users_with_permissions_str = "None"
                if effective_user_sources:
                    parts = []
                    for user in sorted(effective_user_sources.keys()):
                        parts.append(f"{user}: {' | '.join(sorted(effective_user_sources[user]))}")
                    users_with_permissions_str = " ".join(parts)

                inventory_data.append({
                    'Namespace': repo['namespace'],
                    'Repository': repo['name'],
                    'Repo Size': format_size(repo_size),
                    'Tag Count': tag_count,
                    'Unique Image Count': unique_images_count,
                    f'Stale Tags (>{STALE_DAYS_THRESHOLD}d)': stale_tag_count,
                    'Duplicate Tags': duplicate_tags_str,
                    'Latest Push Timestamp': latest_push_time_str,
                    'Tags': tags_str,
                    'Push History': push_history_str,
                    'Pull History': pull_history_str,
                    'Users with Permissions': users_with_permissions_str
                })

            except requests.exceptions.HTTPError as e:
                print(f"  ERROR processing repository {full_repo_name}: {e}")

        output_filename = 'quay_repository_inventory.csv'
        print(f"\nProcessing complete. Writing data to {output_filename}...")

        if inventory_data:
            with open(output_filename, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = [
                    'Namespace', 'Repository', 'Repo Size', 'Tag Count', 'Unique Image Count',
                    f'Stale Tags (>{STALE_DAYS_THRESHOLD}d)', 'Duplicate Tags',
                    'Latest Push Timestamp', 'Tags', 'Push History', 'Pull History',
                    'Users with Permissions'
                ]
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
