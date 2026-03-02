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
from WPP_Whatsapp import Create
import pyautogui
import threading
import pystray
from PIL import Image
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

# --- WhatsApp Manager (WPP_Whatsapp) ---
whatsapp_client = None
whatsapp_creator = None

def init_whatsapp():
    global whatsapp_client, whatsapp_creator
    try:
        config = load_config()
        session_name = config.get('wpp_session', 'default_session')
        log(f"Initializing WhatsApp session: {session_name}...", "INFO")
        log("A browser window will open for QR code scanning.", "ACTION")
        
        whatsapp_creator = Create(session=session_name)
        whatsapp_client = whatsapp_creator.start()
        
        if whatsapp_creator.state == 'CONNECTED':
            log("WhatsApp connected successfully!", "SUCCESS")
        else:
            log(f"WhatsApp state: {whatsapp_creator.state}", "WARNING")
            
    except Exception as e:
        log(f"WhatsApp init error: {e}", "ERROR")

def get_group_ids():
    global whatsapp_client
    if not whatsapp_client or whatsapp_creator.state != 'CONNECTED':
        log("WhatsApp not connected.", "ERROR")
        return []
    
    try:
        log("Fetching group IDs...", "ACTION")
        groups = whatsapp_client.getAllGroups()
        results = []
        for g in groups:
            name = g.get('name', 'Unnamed Group')
            gid = g.get('id', {}).get('_serialized', g.get('id', 'N/A'))
            results.append(f"{name}: {gid}")
        return results
    except Exception as e:
        log(f"Error fetching groups: {e}", "ERROR")
        return []

def copy_groups_to_clipboard():
    try:
        import pyperclip
        groups = get_group_ids()
        if groups:
            text = "\n".join(groups)
            pyperclip.copy(text)
            log("Group IDs copied to clipboard!", "SUCCESS")
        else:
            log("No groups found or not connected.", "WARNING")
    except ImportError:
        log("pyperclip not installed. Cannot copy to clipboard.", "ERROR")
    except Exception as e:
        log(f"Clipboard error: {e}", "ERROR")


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
    except Exception:
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
            caption += f" Sản lượng đầu cực đến thời điểm hiện tại đạt {deg} MWh."

        log(f"Caption: {caption}", "SUCCESS")

        # Send via WhatsApp
        send_success = False
        if whatsapp_client and whatsapp_creator.state == 'CONNECTED':
            try:
                if screenshot_path:
                    log(f"Sending image to {config['phone_number']}...", "ACTION")
                    whatsapp_client.sendImage(config['phone_number'], screenshot_path, caption=caption)
                else:
                    log(f"Sending text to {config['phone_number']}...", "ACTION")
                    whatsapp_client.sendText(config['phone_number'], caption)
                send_success = True
            except Exception as e:
                log(f"WhatsApp sending error: {e}", "ERROR")
        else:
            log("WhatsApp client not connected or initialized.", "ERROR")

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
    log("Shutting down...", "INFO")
    try:
        if whatsapp_client:
            log("Logging out of WhatsApp...", "ACTION")
            whatsapp_client.logout()
            # The library might need time to close the browser
            time.sleep(1)
    except Exception as e:
        log(f"Logout error: {e}", "ERROR")
        
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
            pystray.MenuItem("Copy Group IDs", lambda icon, item: copy_groups_to_clipboard()),
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
    
    # Initialize WhatsApp (runs in the main thread or separate thread as needed)
    # WPP_Whatsapp.Create needs to run where it can open a window if needed
    threading.Thread(target=init_whatsapp, daemon=True).start()
    
    # Start Tkinter mainloop in the main thread (required for GUI)
    log_window.create()
    log_window.hide() # Start hidden
    log_window.root.mainloop()
