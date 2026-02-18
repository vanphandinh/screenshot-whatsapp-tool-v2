"""
Local Python Server — DOM Data Capture
Receives extracted data from Chrome extension and sends reports via WhatsApp (WPPConnect)
"""
import os
import sys
import json
import base64
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as http_requests
import pyautogui
import threading
import pystray
from PIL import Image
import ctypes
import webbrowser
import tkinter as tk
from tkinter import scrolledtext
from queue import Queue

app = Flask(__name__)
CORS(app)  # Allow Chrome extension to call this server

# --- Configuration ---
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), 'screenshots')

if not os.path.exists(SCREENSHOT_DIR):
    os.makedirs(SCREENSHOT_DIR)

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

# --- Logging ---
log_queue = Queue()

def log(message, type="INFO"):
    icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "ACTION": "🚀", "DEBUG": "🔍"}
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {icons.get(type, '🔹')} {message}"
    print(formatted_msg)
    log_queue.put(formatted_msg + "\n")

# --- WPPConnect Client ---
class WPPConnectClient:
    def __init__(self, base_url, session, secret_key):
        self.base_url = base_url.rstrip('/')
        self.session = session
        self.secret_key = secret_key
        self.token = None
        self.headers = {"Content-Type": "application/json"}

    def _generate_token(self):
        log(f"Generating access token for session: {self.session}...", "DEBUG")
        url = f"{self.base_url}/api/{self.session}/{self.secret_key}/generate-token"
        try:
            response = http_requests.post(url, timeout=20)
            if response.status_code in [200, 201]:
                self.token = response.json().get('token')
                self.headers["Authorization"] = f"Bearer {self.token}"
                log("Token generated successfully.", "SUCCESS")
                return True
            log(f"Failed to generate token: {response.text}", "ERROR")
        except Exception as e:
            log(f"Token generation error: {e}", "ERROR")
        return False

    def send_image(self, phone_number, file_path, caption=""):
        if not self.token:
            if not self._generate_token():
                return False

        is_group = "@g.us" in phone_number
        chat_id = phone_number if is_group else f"{phone_number.replace('+', '')}"
        
        try:
            with open(file_path, "rb") as img:
                b64 = base64.b64encode(img.read()).decode('utf-8')
                base64_data = f"data:image/png;base64,{b64}"
        except Exception as e:
            log(f"Image encoding error: {e}", "ERROR")
            return False

        url = f"{self.base_url}/api/{self.session}/send-image"
        payload = {
            "phone": chat_id,
            "base64": base64_data,
            "caption": caption,
            "isGroup": is_group
        }
        
        log(f"Sending image to {phone_number}...", "ACTION")
        try:
            res = http_requests.post(url, headers=self.headers, json=payload, timeout=45)
            if res.status_code == 401:
                if self._generate_token():
                    res = http_requests.post(url, headers=self.headers, json=payload, timeout=45)
            
            if res.status_code in [200, 201]:
                log("Message sent successfully!", "SUCCESS")
                return True
            log(f"Send failed: {res.status_code} - {res.text}", "ERROR")
        except Exception as e:
            log(f"WPPConnect exception: {e}", "ERROR")
        return False

    def send_text(self, phone_number, message):
        if not self.token:
            if not self._generate_token():
                return False

        is_group = "@g.us" in phone_number
        chat_id = phone_number if is_group else f"{phone_number.replace('+', '')}"

        url = f"{self.base_url}/api/{self.session}/send-message"
        payload = {
            "phone": chat_id,
            "message": message,
            "isGroup": is_group
        }
        
        log(f"Sending text to {phone_number}...", "ACTION")
        try:
            res = http_requests.post(url, headers=self.headers, json=payload, timeout=45)
            if res.status_code == 401:
                if self._generate_token():
                    res = http_requests.post(url, headers=self.headers, json=payload, timeout=45)
            
            if res.status_code in [200, 201]:
                log("Text sent successfully!", "SUCCESS")
                return True
            log(f"Send failed: {res.status_code} - {res.text}", "ERROR")
        except Exception as e:
            log(f"WPPConnect exception: {e}", "ERROR")
        return False


# ─── Full-screen screenshot ───
def cleanup_old_screenshots(retention_days):
    """Delete screenshots older than retention_days."""
    if not retention_days or retention_days <= 0:
        return
    
    now = time.time()
    cutoff = now - (retention_days * 86400)
    
    try:
        count = 0
        for filename in os.listdir(SCREENSHOT_DIR):
            if not filename.endswith('.png'):
                continue
            
            filepath = os.path.join(SCREENSHOT_DIR, filename)
            if os.path.isfile(filepath):
                file_time = os.path.getmtime(filepath)
                if file_time < cutoff:
                    os.remove(filepath)
                    count += 1
        if count > 0:
            log(f"Cleaned up {count} old screenshots (retention: {retention_days} days).", "INFO")
    except Exception as e:
        log(f"Cleanup error: {e}", "ERROR")

def take_fullscreen_screenshot():
    """Capture full screen using pyautogui and save to screenshots dir."""
    try:
        # Run cleanup before taking new screenshot
        config = load_config()
        cleanup_old_screenshots(config.get('max_retention_days', 3))
    except:
        pass

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SCREENSHOT_DIR, f"capture_{ts}.png")
    try:
        img = pyautogui.screenshot()
        img.save(path)
        log(f"Full-screen screenshot saved: {path}", "SUCCESS")
        return path
    except Exception as e:
        log(f"Screenshot error: {e}", "ERROR")
        return None


# ─── API Endpoints ─── 

@app.route('/api/status', methods=['GET'])
def status():
    """Health check endpoint"""
    return jsonify({
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    })


@app.route('/api/capture', methods=['POST'])
def capture():
    """
    Receives captured data from Chrome extension.
    Takes screenshot, processes data, and sends via WhatsApp.
    Expected payload:
    {
        "timestamp": "ISO string",
        "url": "source URL",
        "force_22h": bool,
        "data": {
            "DC": {"value": "12", "found": true},
            "AWS": {"value": "5.3", "found": true},
            "TAP": {"value": "-2.1", "found": true},
            ...
        }
    }
    """
    try:
        payload = request.json
        if not payload:
            return jsonify({"success": False, "error": "No JSON payload"}), 400

        config = load_config()
        data = payload.get('data', {})

        log("=" * 40, "INFO")
        log(f"Received capture at {payload.get('timestamp', 'unknown')}", "ACTION")
        log(f"Source URL: {payload.get('url', 'unknown')}", "DEBUG")

        # Log extracted values
        for name, info in data.items():
            val = info.get('value', '') if isinstance(info, dict) else info
            found = info.get('found', True) if isinstance(info, dict) else True
            status_icon = "✅" if found else "❌"
            log(f"  {status_icon} {name}: {val}", "DEBUG")

        # Take full-screen screenshot
        log("Capturing full-screen screenshot...", "INFO")
        screenshot_path = take_fullscreen_screenshot()

        # Extract values
        def get_val(name):
            info = data.get(name, {})
            if isinstance(info, dict):
                return info.get('value', '').strip()
            return str(info).strip()

        dc = get_val("DC")
        aws = get_val("AWS")
        tap = get_val("TAP")
        f_val = get_val("F")
        m_val = get_val("M")
        deg = get_val("DEG")

        # Validate required fields
        missing = []
        if not dc: missing.append("DC")
        if not aws: missing.append("AWS")
        if not tap: missing.append("TAP")

        if missing:
            msg = f"Missing required fields: {', '.join(missing)}"
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg}), 400

        # Calculate active devices
        try:
            dc_num = int(dc)
            f_num = int(f_val) if f_val else 0
            m_num = int(m_val) if m_val else 0
            active = dc_num - f_num - m_num
        except ValueError:
            active = dc

        # Parse TAP
        try:
            tap_num = float(tap)
        except ValueError:
            log(f"Invalid TAP value: {tap}", "ERROR")
            return jsonify({"success": False, "error": f"Invalid TAP value: {tap}"}), 400

        # Build caption
        if tap_num < 0:
            caption = "BC BLĐ: Hiện tại 12 TB đang dừng, tốc độ gió thấp."
        else:
            caption = (
                f"BC BLĐ: Hiện tại {active} TB đang hoạt động, "
                f"tốc độ gió {aws} m/s, "
                f"công suất phát {tap} MW."
            )

        # 22h report logic
        force_22h = payload.get('force_22h', False)
        current_hour = datetime.now().hour
        if (current_hour == 22 or force_22h) and deg:
            caption += f" Sản lượng đầu cực đến 22h đạt {deg} MWh."

        log(f"Caption: {caption}", "SUCCESS")

        # Send via WhatsApp
        client = WPPConnectClient(
            config['wpp_base_url'],
            config['wpp_session'],
            config['wpp_secret_key']
        )

        send_success = False
        if screenshot_path:
            send_success = client.send_image(config['phone_number'], screenshot_path, caption)
        else:
            log("No screenshot available, sending text only", "INFO")
            send_success = client.send_text(config['phone_number'], caption)

        if send_success:
            log("Report sent successfully!", "SUCCESS")
            return jsonify({
                "success": True,
                "caption": caption,
                "values": {
                    "DC": dc, "AWS": aws, "TAP": tap,
                    "F": f_val, "M": m_val, "DEG": deg,
                    "active": str(active)
                }
            })
        else:
            return jsonify({"success": False, "error": "WhatsApp sending failed"}), 500

    except Exception as e:
        log(f"Capture processing error: {e}", "ERROR")
        return jsonify({"success": False, "error": str(e)}), 500


# ─── System Tray & GUI Logic ───

import tkinter as tk
from tkinter import scrolledtext
import queue

log_queue = queue.Queue()

class LogWindow:
    def __init__(self):
        self.root = None
        self.text_area = None
        self.visible = False

    def create(self):
        if self.root:
            return
        
        self.root = tk.Tk()
        self.root.title("WhatsApp Tool - Server Logs")
        self.root.geometry("600x400")
        
        # Keep off taskbar: use toolwindow attribute
        self.root.attributes('-toolwindow', True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        
        self.text_area = scrolledtext.ScrolledText(self.root, wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 10))
        self.text_area.pack(expand=True, fill='both')
        
        self.visible = True
        self.update_logs()

    def update_logs(self):
        if not self.root:
            return
        while not log_queue.empty():
            msg = log_queue.get()
            self.text_area.insert(tk.END, msg)
            self.text_area.see(tk.END)
        self.root.after(100, self.update_logs)

    def show(self):
        if not self.root:
            self.create()
        else:
            self.root.deiconify()
            self.visible = True

    def hide(self):
        if self.root:
            self.root.withdraw()
            self.visible = False

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()

log_window = LogWindow()

def on_quit(icon, item):
    """Exit the application when tray icon Quit is clicked."""
    icon.stop()
    if log_window.root:
        log_window.root.destroy()
    os._exit(0)

def setup_tray():
    """Create and run the system tray icon."""
    try:
        icon_path = os.path.join(os.path.dirname(__file__), 'chrome-extension', 'icons', 'icon48.png')
        if os.path.exists(icon_path):
            image = Image.open(icon_path)
        else:
            image = Image.new('RGB', (64, 64), color=(73, 109, 137))
            
        menu = pystray.Menu(
            pystray.MenuItem("Status: Running", lambda: None, enabled=False),
            pystray.MenuItem("Show/Hide Logs", lambda icon, item: log_window.toggle()),
            pystray.MenuItem("Quit", on_quit)
        )
        
        icon = pystray.Icon("whatsapp_tool_server", image, "WhatsApp Tool Server", menu)
        log("System Tray Icon started.", "SUCCESS")
        
        # Run pystray in a separate thread so tkinter can own the main thread
        threading.Thread(target=icon.run, daemon=True).start()
    except Exception as e:
        log(f"Tray icon error: {e}", "ERROR")

if __name__ == '__main__':
    log("=" * 50, "INFO")
    log("DOM Data Capture Server starting...", "ACTION")
    log(f"Config: {CONFIG_PATH}", "DEBUG")
    log(f"Screenshots: {SCREENSHOT_DIR}", "DEBUG")
    log("Endpoints:", "INFO")
    log("  GET  /api/status   — Health check", "INFO")
    log("  POST /api/capture  — Receive data from extension", "INFO")
    log("=" * 50, "INFO")
    
    # Run Flask in a background thread
    flask_thread = threading.Thread(target=lambda: app.run(host='127.0.0.1', port=5001, debug=False, use_reloader=False))
    flask_thread.daemon = True
    flask_thread.start()
    
    # Setup System Tray (runs in its own thread internally now)
    setup_tray()
    
    # Start Tkinter mainloop in the main thread (required for GUI)
    log_window.create()
    log_window.hide() # Start hidden
    log_window.root.mainloop()
