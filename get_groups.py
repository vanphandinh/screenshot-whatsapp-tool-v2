import requests
import json
import os
import sys

def log(message, type="INFO"):
    colors = {
        "INFO": "\033[94m",    # Blue
        "SUCCESS": "\033[92m", # Green
        "ERROR": "\033[91m",   # Red
        "ACTION": "\033[93m",  # Yellow
        "DEBUG": "\033[95m",   # Magenta
        "END": "\033[0m"
    }
    prefixes = {
        "INFO": "[*]",
        "SUCCESS": "[+]",
        "ERROR": "[-]",
        "ACTION": "[>]",
        "DEBUG": "[#]"
    }
    print(f"{colors.get(type, '')}{prefixes.get(type, '[ ]')} {message}{colors.get('END', '')}")

def get_groups():
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    if not os.path.exists(config_path):
        log("config.json not found!", "ERROR")
        return

    with open(config_path, 'r') as f:
        config = json.load(f)

    base_url = config.get('wpp_base_url', '').rstrip('/')
    session = config.get('wpp_session')
    secret_key = config.get('wpp_secret_key')

    if not all([base_url, session, secret_key]):
        log("Missing configuration in config.json", "ERROR")
        return

    # First, get token
    log(f"Fetching token for session '{session}'...", "ACTION")
    token_url = f"{base_url}/api/{session}/{secret_key}/generate-token"
    try:
        res = requests.post(token_url, timeout=15)
        if res.status_code not in [200, 201]:
            log(f"Auth failed: {res.text}", "ERROR")
            return
        token = res.json().get('token')
    except Exception as e:
        log(f"Network error: {e}", "ERROR")
        return

    # Get All Groups
    log("Fetching all groups...", "ACTION")
    groups_url = f"{base_url}/api/{session}/all-groups"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        res = requests.get(groups_url, headers=headers, timeout=30)
        if res.status_code != 200:
            log(f"Failed to fetch groups: {res.text}", "ERROR")
            return
        
        groups = res.json().get('response', [])
        if not groups:
            log("No groups found for this account.", "INFO")
            return

        print("\n" + "="*60)
        print(f"{'GROUP NAME':<30} | {'GROUP ID'}")
        print("-" * 60)
        for g in groups:
            name = g.get('name', 'Unnamed Group')
            gid = g.get('id', {}).get('_serialized', g.get('id', 'N/A'))
            print(f"{name[:28]:<30} | {gid}")
        print("="*60 + "\n")
        
        log("Copy the Group ID you want to use and paste it into 'phone_number' in config.json", "SUCCESS")

    except Exception as e:
        log(f"Error fetching groups: {e}", "ERROR")

if __name__ == "__main__":
    get_groups()
