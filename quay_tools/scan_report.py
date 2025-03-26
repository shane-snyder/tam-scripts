import requests
import json
import os
import csv
import argparse

# Initialize API_BASE_URL and ACCESS_TOKEN as None
API_BASE_URL = None
ACCESS_TOKEN = None

# Validating that the url put in has proper formatting
def validate_api_base_url(url):
    # Ensure the URL starts with 'https://'
    if not url.startswith("https://"):
        url = "https://" + url
    
    # Ensure the URL contains '/api'
    if "/api" not in url:
        url = url.rstrip('/') + "/api"
    
    # Ensure the URL ends with '/v1'
    if not url.endswith("/v1"):
        url = url.rstrip('/') + "/v1"
    
    return url

#Lookup used to find the manifest_digest that get passed into the scan
def get_manifest_digest(org_name, repository, tag):
    url = f"{API_BASE_URL}/repository/{org_name}/{repository}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    
    # Print the URL being requested
    print(f"Requesting repository information with URL: {url}")
    
    response = requests.get(url, headers=headers)
    
    # Print the response status and data
    print(f"Response Status Code: {response.status_code}")
    print(f"Response Data: {response.text}")
    
    if response.status_code == 200:
        data = response.json()
        tags = data.get("tags", {})
        tag_info = tags.get(tag)
        
        if tag_info:
            return tag_info.get("manifest_digest")
        else:
            print(f"Tag '{tag}' not found in repository '{repository}'.")
            return None
    else:
        print(f"Failed to retrieve repository information: {response.status_code} {response.text}")
        return None

#Run the security vulnerabilities output
def get_security_report(org_name, repository, manifest_digest, tag):
    url = (f"{API_BASE_URL}/repository/{org_name}/{repository}/"
           f"manifest/{manifest_digest}/security?vulnerabilities=true")
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json"
    }
    
    #Print the URL being requested
    print(f"Requesting security report with URL: {url}")
    
    response = requests.get(url, headers=headers)
    
    #Print the response status and data
    print(f"Response Status Code: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        features_with_vulnerabilities = [
            feature for feature in data.get("data", {}).get("Layer", {}).get("Features", [])
            if feature.get("Vulnerabilities")
        ]
        
        # Print the JSON version of the filtered data
        print("Security Report (JSON):")
        print(json.dumps(features_with_vulnerabilities, indent=2))
        
        # Save the filtered data to a CSV file
        save_to_csv(features_with_vulnerabilities, org_name, repository, tag)
    else:
        print(f"Failed to retrieve security report. "
              f"Status code: {response.status_code}, Response: {response.text}")
# Create the csv output
def save_to_csv(features, org_name, repository, tag):
    # Construct the filename based on inputs
    filename = f"{org_name}-{repository}-{tag}.csv"
    
    # Open the file in write mode
    with open(filename, mode='w', newline='') as file:
        writer = csv.writer(file)
        
        # Write the header
        writer.writerow(['Vulnerability Name', 'Severity', 'CVSS Score', 'Package', 'Package version', 'Fixed by', 'Links', 'Description'])
        
        # Write the data
        for feature in features:
            for vulnerability in feature['Vulnerabilities']:
                # Format links with line breaks
                links = vulnerability['Link'].replace(' ', '\n')
                
                # Get CVSS score if it exists
                cvss_score = vulnerability.get('Metadata', {}).get('NVD', {}).get('CVSSv3', {}).get('Score', '')
                
                # Get Fixed By if it exists
                fixed_by = vulnerability.get('FixedBy', '')
                
                writer.writerow([
                    vulnerability['Name'],
                    vulnerability['Severity'],
                    cvss_score,
                    feature['Name'],
                    feature['Version'],
                    fixed_by,
                    links,
                    vulnerability['Description']
                ])

def main():
    global API_BASE_URL, ACCESS_TOKEN
    
    # Parse through arguments or prompt to supply those arguments
    parser = argparse.ArgumentParser(description="Run a security scan on a Quay repository.")
    parser.add_argument('--api-url', help='The API base URL for Quay.')
    parser.add_argument('--access-token', help='Your access token for Quay.')
    parser.add_argument('--org', help='Your Quay organization name.')
    parser.add_argument('--repo', help='The repository name you want to scan.')
    parser.add_argument('--tag', help='The tag you want to scan.')

    args = parser.parse_args()

    # Use command-line arguments or prompt for input
    API_BASE_URL = args.api_url or os.getenv('API_BASE_URL') or input("Enter the API base URL: ")
    ACCESS_TOKEN = args.access_token or os.getenv('ACCESS_TOKEN') or input("Enter your access token: ")
    org_name = args.org or input("Enter your Quay organization name: ")
    repository = args.repo or input("Enter the repository name you'd like to scan: ")
    tag = args.tag or input("Enter the tag you'd like to scan: ")

    # Validate and update the API_BASE_URL
    API_BASE_URL = validate_api_base_url(API_BASE_URL)

    manifest_digest = get_manifest_digest(org_name, repository, tag)
    if manifest_digest:
        get_security_report(org_name, repository, manifest_digest, tag)

if __name__ == "__main__":
    main()
