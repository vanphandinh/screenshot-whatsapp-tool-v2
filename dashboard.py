import json
import os
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

CONFIG_FILE = 'config.json'
# In-memory token cache: {session_name: token}
TOKEN_CACHE = {}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def get_access_token(base_url, session, secret_key):
    """Generates an access token using the secret key."""
    if not secret_key:
        return None
    
    # Check cache first
    if session in TOKEN_CACHE:
        return TOKEN_CACHE[session]

    url = f"{base_url.rstrip('/')}/api/{session}/{secret_key}/generate-token"
    print(f"DEBUG: Generating token for '{session}' at {url}")
    try:
        response = requests.post(url, timeout=10)
        if response.status_code == 201 or response.status_code == 200:
            data = response.json()
            token = data.get('token')
            if token:
                TOKEN_CACHE[session] = token
                return token
        print(f"DEBUG: Token generation failed: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"DEBUG: Token exception: {e}")
    return None

@app.route('/')
def index():
    config = load_config()
    return render_template('index.html', config=config)

@app.route('/api/session/start', methods=['POST'])
def start_session():
    data = request.json
    session = data.get('session')
    base_url = data.get('base_url', '').rstrip('/')
    secret_key = data.get('secret_key', '')

    if not session or not base_url:
        return jsonify({"success": False, "message": "Missing session or base_url"}), 400

    # Force token regeneration on Manual Start
    if session in TOKEN_CACHE:
        print(f"DEBUG: Clearing token cache for '{session}' to force restart")
        del TOKEN_CACHE[session]

    token = get_access_token(base_url, session, secret_key)
    
    url = f"{base_url}/api/{session}/start-session"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    print(f"DEBUG: Starting session '{session}' at {url}")
    try:
        response = requests.post(url, headers=headers, json={"waitQrCode": True}, timeout=15)
        print(f"DEBUG: Start response: {response.status_code} - {response.text}")
        return jsonify(response.json()), response.status_code
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/session/status', methods=['GET'])
def get_status():
    session = request.args.get('session')
    base_url = request.args.get('base_url', '').rstrip('/')
    secret_key = request.args.get('secret_key', '')

    if not session or not base_url:
        return jsonify({"success": False, "message": "Missing parameters"}), 400

    token = get_access_token(base_url, session, secret_key)
    
    url = f"{base_url}/api/{session}/status-session"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(url, headers=headers, timeout=10)
        return jsonify(response.json()), response.status_code
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/session/qr', methods=['GET'])
def get_qr():
    session = request.args.get('session')
    base_url = request.args.get('base_url', '').rstrip('/')
    secret_key = request.args.get('secret_key', '')

    if not session or not base_url:
        return jsonify({"success": False, "message": "Missing parameters"}), 400

    token = get_access_token(base_url, session, secret_key)
    
    url = f"{base_url}/api/{session}/qrcode-session"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    print(f"DEBUG: Fetching QR for '{session}' at {url}")
    try:
        response = requests.get(url, headers=headers, timeout=15)
        print(f"DEBUG: QR response status: {response.status_code}")
        print(f"DEBUG: QR content type: {response.headers.get('Content-Type')}")
        
        # If the response is an image, we should probably encode it to base64 for the frontend
        if 'image' in response.headers.get('Content-Type', ''):
            import base64
            img_base64 = base64.b64encode(response.content).decode('utf-8')
            return jsonify({"base64": f"data:image/png;base64,{img_base64}"})

        # Try to parse as JSON
        try:
            return jsonify(response.json()), response.status_code
        except Exception:
            # If not JSON and not image, return raw text or error
            print(f"DEBUG: QR Raw text: {response.text[:100]}...")
            return jsonify({"success": False, "message": "Unexpected response format", "raw": response.text[:200]}), 500
            
    except Exception as e:
        print(f"DEBUG: QR exception: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/session/logout', methods=['POST'])
def logout_session():
    data = request.json
    session = data.get('session')
    base_url = data.get('base_url', '').rstrip('/')
    secret_key = data.get('secret_key', '')

    if not session or not base_url:
        return jsonify({"success": False, "message": "Missing parameters"}), 400

    token = get_access_token(base_url, session, secret_key)
    
    url = f"{base_url}/api/{session}/logout-session"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    print(f"DEBUG: Logging out session '{session}' at {url}")
    try:
        response = requests.post(url, headers=headers, timeout=10)
        # Clear token from cache on logout
        if session in TOKEN_CACHE:
            del TOKEN_CACHE[session]
        
        # Even if 404/401, we want to return success to the dashboard so it can proceed with restart
        if response.status_code in [200, 201, 404, 401]:
            return jsonify({"success": True, "message": "Session closed or already gone"}), 200
        return jsonify(response.json()), response.status_code
    except Exception as e:
        print(f"DEBUG: Logout error (ignoring for refresh): {e}")
        # Return success anyway to allow refresh-cycle to continue
        return jsonify({"success": True, "message": "Proceeding despite error"}), 200

@app.route('/api/save-config', methods=['POST'])
def save_config_api():
    data = request.json
    config = load_config()
    config.update(data)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
