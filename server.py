"""
Local Python Server — DOM Data Capture
Receives extracted data from Chrome extension and sends reports via WhatsApp (WPPConnect)
"""
import os
import json
import time
import ctypes
import ctypes.wintypes
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from WPP_Whatsapp import Create
import pyautogui
import threading
import pystray
from PIL import Image
import tkinter as tk
from tkinter import scrolledtext
from queue import Queue
import psutil
import pyperclip

# ─── Win32 helpers ───
user32 = ctypes.windll.user32

# Explicitly define argument and return types for 64-bit compatibility
user32.SetWindowPos.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.wintypes.UINT]
user32.SetWindowPos.restype = ctypes.wintypes.BOOL

user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = ctypes.wintypes.BOOL

user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL

user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.wintypes.BOOL

user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
user32.IsWindow.restype = ctypes.wintypes.BOOL

user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.BringWindowToTop.argtypes = [ctypes.wintypes.HWND]
user32.BringWindowToTop.restype = ctypes.wintypes.BOOL

user32.SetFocus.argtypes = [ctypes.wintypes.HWND]
user32.SetFocus.restype = ctypes.wintypes.HWND

# ─── Global State for Targeted Window ───
target_hwnd = None
target_window_title = "Chưa chọn"
tray_icon = None

def _enum_windows_callback(hwnd, results):
    """Collect all visible top-level windows."""
    if user32.IsWindowVisible(hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            results.append((hwnd, buf.value))
    return True

def get_all_chrome_windows():
    """Returns a list of (hwnd, title) for all visible Chrome windows."""
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.py_object)
    windows = []
    user32.EnumWindows(EnumWindowsProc(_enum_windows_callback), ctypes.py_object(windows))
    return [(hwnd, title) for hwnd, title in windows
            if 'google chrome' in title.lower() or 'chromium' in title.lower()]

def find_chrome_window():
    """
    Find the Chrome window. Uses auto-selection for windows with 'SCADA' in title,
    then falls back to manual selection. Returns hwnd or None.
    """
    global target_hwnd, target_window_title, tray_icon
    
    # 1. Auto-selection logic: find Chrome window with "SCADA" in title
    chrome_windows = get_all_chrome_windows()
    scada_windows = [(hwnd, title) for hwnd, title in chrome_windows if 'scada' in title.lower()]
    
    if len(scada_windows) == 1:
        hwnd, title = scada_windows[0]
        if target_hwnd != hwnd:
            target_hwnd = hwnd
            target_window_title = title
            log(f"Tự động chọn cửa sổ SCADA: {title} (hwnd={hwnd})", "SUCCESS")
            # Try to update tray menu visual 
            if tray_icon:
                try:
                    tray_icon.update_menu()
                except Exception:
                    pass
        else:
            log(f"Đang dùng cửa sổ SCADA tự động: hwnd={hwnd}", "SUCCESS")
        return hwnd
    
    # 2. Ambiguity or missing cases
    if len(scada_windows) > 1:
        log(f"Tìm thấy {len(scada_windows)} cửa sổ SCADA. Hủy tự động chọn.", "WARNING")
    elif len(chrome_windows) > 0:
        log("Không tìm thấy cửa sổ SCADA nào. Thử sử dụng cửa sổ chọn thủ công.", "WARNING")

    # 3. Fallback to manually selected window
    if target_hwnd and user32.IsWindow(target_hwnd) and user32.IsWindowVisible(target_hwnd):
        log(f"Sử dụng cửa sổ chọn thủ công: hwnd={target_hwnd}", "SUCCESS")
        return target_hwnd

    log("Chưa chọn cửa sổ hợp lệ.", "WARNING")
    return None

# ─── Native WinAPI Window Lock ───

user32.LockSetForegroundWindow.argtypes = [ctypes.wintypes.UINT]
user32.LockSetForegroundWindow.restype = ctypes.wintypes.BOOL
LSFW_LOCK = 1
LSFW_UNLOCK = 2

user32.GetWindowRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.RECT)]
user32.GetWindowRect.restype = ctypes.wintypes.BOOL

user32.ClipCursor.argtypes = [ctypes.POINTER(ctypes.wintypes.RECT)]
user32.ClipCursor.restype = ctypes.wintypes.BOOL

class NativeWindowLock:
    """A context manager that uses LockSetForegroundWindow and ClipCursor to lock focus."""
    def __init__(self, hwnd):
        self.hwnd = hwnd

    def __enter__(self):
        if self.hwnd and user32.IsWindow(self.hwnd):
            log(f"Kích hoạt Native WinAPI Lock cho hwnd={self.hwnd}...", "DEBUG")
            
            # 1. Force to TopMost
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            
            # 2. Lock Foreground transitions
            user32.LockSetForegroundWindow(LSFW_LOCK)
            
            # 3. Clip Cursor to window bounds
            rect = ctypes.wintypes.RECT()
            if user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
                user32.ClipCursor(ctypes.byref(rect))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 1. Release Cursor
        user32.ClipCursor(None)
        
        # 2. Unlock Foreground transitions
        user32.LockSetForegroundWindow(LSFW_UNLOCK)
        
        # 3. Release TopMost
        if self.hwnd and user32.IsWindow(self.hwnd):
            log(f"Giải phóng Native Lock cho hwnd={self.hwnd}.", "DEBUG")
            HWND_NOTOPMOST = -2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)

def focus_and_restore_window(hwnd):
    """Bring window to foreground, restore, maximize."""
    SW_MAXIMIZE = 3
    # Ensure window is maximized
    log(f"Maximizing and focusing hwnd={hwnd}...", "DEBUG")
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.SetFocus(hwnd)

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
        config = json.load(f)
    # Default to True if not present
    if 'logout_on_quit' not in config:
        config['logout_on_quit'] = True
    return config

def save_config(config):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
        log("Configuration saved.", "DEBUG")
    except Exception as e:
        log(f"Error saving config: {e}", "ERROR")

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
        # Set Playwright browser path to a permanent location (not TEMP)
        # This prevents Windows from cleaning up the browser binaries
        browsers_path = os.path.join(os.path.dirname(__file__), 'playwright-browsers')
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = browsers_path
        log(f"Playwright browsers path: {browsers_path}", "DEBUG")

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
        log(f"Full traceback:\n{traceback.format_exc()}", "ERROR")

def get_groups():
    """Fetch groups from WhatsApp and return a list of (name, id) tuples."""
    global whatsapp_client
    if not whatsapp_client or whatsapp_creator.state != 'CONNECTED':
        log("WhatsApp not connected.", "ERROR")
        return []
    
    try:
        log("Fetching group list...", "ACTION")
        groups = whatsapp_client.getAllGroups()
        results = []
        for g in groups:
            name = g.get('name', 'Unnamed Group')
            gid = g.get('id', {}).get('_serialized', g.get('id', 'N/A'))
            results.append({"name": name, "id": gid})
        return results
    except Exception as e:
        log(f"Error fetching groups: {e}", "ERROR")
        return []

def show_group_selector():
    """Fetch groups and show the selection window."""
    groups = get_groups()
    if not groups:
        log("No groups found or not connected.", "WARNING")
        return
    
    # Run UI in the main thread using after()
    if log_window.root:
        log_window.root.after(0, lambda: group_window.show(groups))


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
    """
    Capture full screen using pyautogui.
    Before capturing:
      1. Find the Chrome window (must be manually selected).
      2. Bring it to the foreground and set to TopMost.
      3. Capture screenshot and release TopMost.
    """
    try:
        config = load_config()
        cleanup_old_screenshots(config.get('max_retention_days', 3))
    except Exception:
        pass

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SCREENSHOT_DIR, f"capture_{ts}.png")

    try:
        # 1. Find & focus/maximize the Chrome window
        hwnd = find_chrome_window()
        if hwnd:
            focus_and_restore_window(hwnd)
            # 2. Use NativeWindowLock to trap focus and cursor during capture
            with NativeWindowLock(hwnd):
                time.sleep(0.5) 
                log(f"Đang chụp màn hình (Native Lock active)...", "INFO")
                img = pyautogui.screenshot()
        else:
            log("No Chrome window selected. Capture cancelled.", "ERROR")
            return None

        img.save(path)
        log(f"Full-screen screenshot saved: {path}", "SUCCESS")
        return path
    except Exception as e:
        if 'hwnd' in locals() and hwnd:
            focus_and_restore_window(hwnd)
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

        tb1 = get_val("TB1")
        tb2 = get_val("TB2")
        tb3 = get_val("TB3")
        tb4 = get_val("TB4")
        tb5 = get_val("TB5")
        tb6 = get_val("TB6")
        tb7 = get_val("TB7")
        tb8 = get_val("TB8")
        tb9 = get_val("TB9")
        tb10 = get_val("TB10")
        tb11 = get_val("TB11")
        tb12 = get_val("TB12")

        # Validate required fields
        missing = []
        if not dc: missing.append("DC")
        if not aws: missing.append("AWS")
        if not tap: missing.append("TAP")
        if not tb1: missing.append("TB1")
        if not tb2: missing.append("TB2")
        if not tb3: missing.append("TB3")
        if not tb4: missing.append("TB4")
        if not tb5: missing.append("TB5")
        if not tb6: missing.append("TB6")
        if not tb7: missing.append("TB7")
        if not tb8: missing.append("TB8")
        if not tb9: missing.append("TB9")
        if not tb10: missing.append("TB10")
        if not tb11: missing.append("TB11")
        if not tb12: missing.append("TB12")
        if not deg: missing.append("DEG")
        if not f_val: missing.append("F")
        if not m_val: missing.append("M")

        if missing:
            msg = f"Missing required fields: {', '.join(missing)}"
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg}), 400

        # Count number of tb1-tb12 that are less than or equal to 0
        try:
            tb_values = [tb1, tb2, tb3, tb4, tb5, tb6, tb7, tb8, tb9, tb10, tb11, tb12]
            inactive_tb = [tb for tb in tb_values if float(tb) <= 0]
            inactive_tb_count = len(inactive_tb)
        except ValueError:
            log(f"Invalid TB value: {tb1}, {tb2}, {tb3}, {tb4}, {tb5}, {tb6}, {tb7}, {tb8}, {tb9}, {tb10}, {tb11}, {tb12}", "ERROR")
            return jsonify({"success": False, "error": f"Invalid TB value: {tb1}, {tb2}, {tb3}, {tb4}, {tb5}, {tb6}, {tb7}, {tb8}, {tb9}, {tb10}, {tb11}, {tb12}"}), 400

        # Calculate active devices
        try:
            dc_num = int(dc)
            f_num = int(f_val)
            m_num = int(m_val)
            active = dc_num - inactive_tb_count
            low_wind = max(0, inactive_tb_count - f_num - m_num)
        except ValueError:
            log(f"Invalid DC, F, or M value: {dc}, {f_val}, {m_val}", "ERROR")
            return jsonify({"success": False, "error": f"Invalid DC, F, or M value: {dc}, {f_val}, {m_val}"}), 400

        # Build caption
        caption = (
            f"BC BLĐ: Hiện tại {active} TB đang hoạt động, " +
            (f"{low_wind} TB dừng do tốc độ gió thấp, " if low_wind > 0 else "") +
            (f"{m_num} TB dừng do đang bảo trì, " if m_num > 0 else "") +
            (f"{f_num} TB dừng do bị lỗi, " if f_num > 0 else "") +
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
        is_test = payload.get('is_test', False)
        target_number = config.get('phone_number')
        if is_test:
            test_phone = config.get('test_phone_number', '').strip()
            if test_phone:
                target_number = test_phone

        send_success = False
        if whatsapp_client and whatsapp_creator.state == 'CONNECTED':
            if not target_number:
                log("Target WhatsApp number not configured.", "ERROR")
            else:
                try:
                    msg_type_log = "TEST" if is_test else "LIVE"
                    if screenshot_path:
                        log(f"Sending image to {target_number} ({msg_type_log})...", "ACTION")
                        whatsapp_client.sendImage(target_number, screenshot_path, caption=caption)
                    else:
                        log(f"Sending text to {target_number} ({msg_type_log})...", "ACTION")
                        whatsapp_client.sendText(target_number, caption)
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
        
        self.text_area = scrolledtext.ScrolledText(self.root, wrap=tk.NONE, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 10))
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

class GroupWindow:
    def __init__(self):
        self.root = None
        self.frame = None

    def create(self):
        if self.root:
            return
        self.root = tk.Toplevel(log_window.root)
        self.root.title("Select WhatsApp Group to Copy ID")
        self.root.geometry("500x600")
        self.root.attributes('-toolwindow', True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        
        # Header
        header = tk.Frame(self.root, bg="#333", padx=10, pady=10)
        header.pack(fill='x')
        tk.Label(header, text="Click on a group to copy its ID", fg="white", bg="#333", font=("Arial", 10, "bold")).pack()

        # Container for the list with scrollbar
        container = tk.Frame(self.root)
        container.pack(expand=True, fill='both')
        
        canvas = tk.Canvas(container, bg="#f5f5f5")
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.scrollable_frame = tk.Frame(canvas, bg="#f5f5f5")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Mouse wheel support
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

    def show(self, groups):
        if not self.root:
            self.create()
        
        # Clear existing rows
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        
        def copy_id(gid, name):
            pyperclip.copy(gid)
            log(f"Copied ID for group '{name}': {gid}", "SUCCESS")
            # Visual feedback optional, but log is fine
            
        for group in groups:
            row = tk.Frame(self.scrollable_frame, bg="white", highlightbackground="#ddd", highlightthickness=1, cursor="hand2")
            row.pack(fill='x', padx=5, pady=2)
            
            name_label = tk.Label(row, text=group['name'], font=("Arial", 10, "bold"), bg="white", anchor="w")
            name_label.pack(fill='x', padx=10, pady=(5, 0))
            
            id_label = tk.Label(row, text=group['id'], font=("Consolas", 9), fg="#666", bg="white", anchor="w")
            id_label.pack(fill='x', padx=10, pady=(0, 5))

            # Bind click events to the whole row
            for widget in (row, name_label, id_label):
                widget.bind("<Button-1>", lambda e, g=group['id'], n=group['name']: copy_id(g, n))

        self.root.deiconify()
        self.root.lift()

    def hide(self):
        if self.root:
            self.root.withdraw()

class ChromeWindowWindow:
    def __init__(self):
        self.root = None
        self.scrollable_frame = None

    def create(self):
        if self.root:
            return
        self.root = tk.Toplevel(log_window.root)
        self.root.title("Chọn cửa sổ Chrome mục tiêu")
        self.root.geometry("600x500")
        self.root.attributes('-toolwindow', True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        
        # Header
        header = tk.Frame(self.root, bg="#0277BD", padx=10, pady=10)
        header.pack(fill='x')
        tk.Label(header, text="Chọn cửa sổ trình duyệt bạn muốn chụp màn hình", fg="white", bg="#0277BD", font=("Arial", 11, "bold")).pack()

        # Container
        container = tk.Frame(self.root)
        container.pack(expand=True, fill='both')
        
        canvas = tk.Canvas(container, bg="#fff")
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.scrollable_frame = tk.Frame(canvas, bg="#fff")

        self.scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def show(self):
        """Thread-safe show using after() to run on the main Tkinter thread."""
        if log_window.root:
            log_window.root.after(0, self._show_sync)

    def _show_sync(self):
        """Actual UI logic to list and show Chrome windows."""
        chrome_windows = get_all_chrome_windows()
        if not chrome_windows:
            log("Không tìm thấy cửa sổ Chrome nào đang mở.", "WARNING")
            return

        if not self.root:
            self.create()
        
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        
        def select_window(hwnd, title):
            global target_hwnd, target_window_title, tray_icon
            target_hwnd = hwnd
            target_window_title = title
            log(f"Đã chọn cửa sổ tiêu: {title} (hwnd={hwnd})", "SUCCESS")
            if tray_icon:
                try:
                    tray_icon.update_menu()
                except Exception:
                    pass
            self.hide()
            
        for hwnd, title in chrome_windows:
            is_selected = (hwnd == target_hwnd)
            bg_color = "#E1F5FE" if is_selected else "white"
            
            row = tk.Frame(self.scrollable_frame, bg=bg_color, highlightbackground="#ddd", highlightthickness=1, cursor="hand2")
            row.pack(fill='x', padx=10, pady=5)
            
            lbl = tk.Label(row, text=title, font=("Arial", 10), bg=bg_color, anchor="w", wraplength=540, justify="left")
            lbl.pack(fill='x', padx=15, pady=10)

            # Bind click
            for widget in (row, lbl):
                widget.bind("<Button-1>", lambda e, h=hwnd, t=title: select_window(h, t))

        self.root.deiconify()
        self.root.lift()

    def hide(self):
        if self.root:
            self.root.withdraw()

log_window = LogWindow()
chrome_window_selector = ChromeWindowWindow()
group_window = GroupWindow()

def on_quit(icon, item):
    """Exit the application when tray icon Quit is clicked."""
    log("Shutting down...", "INFO")
    
    # Failsafe: Force exit after 10 seconds if shutdown hangs
    def force_exit_failsafe():
        time.sleep(10)
        log("Failsafe: Forcing exit.", "WARNING")
        os._exit(0)
    
    threading.Thread(target=force_exit_failsafe, daemon=True).start()

    def library_cleanup():
        # 1. Try to logout if connected and preference is enabled
        config = load_config()
        if config.get('logout_on_quit', True):
            if whatsapp_client and whatsapp_creator and whatsapp_creator.state == 'CONNECTED':
                log("Logging out of WhatsApp...", "ACTION")
                try:
                    # Use a shorter timeout to prevent permanent hang
                    whatsapp_client.logout(timeout=10)
                except Exception as e:
                    log(f"Logout error (expected on shut down): {e}", "DEBUG")
        else:
            log("Skipping WhatsApp logout as per user preference.", "INFO")
        
        # 2. Try to close via library
        if whatsapp_creator:
            log("Closing WhatsApp browser...", "ACTION")
            try:
                # Create.sync_close doesn't take arguments, it uses its own internal timeouts
                whatsapp_creator.sync_close()
            except Exception as e:
                log(f"Library sync_close error: {e}", "DEBUG")

    # Run library cleanup in a separate thread to avoid blocking the main quit thread
    cleanup_thread = threading.Thread(target=library_cleanup)
    cleanup_thread.start()
    
    # Give library a very short time to start closing
    cleanup_thread.join(timeout=10)

    # 3. Force kill any remaining browser processes for this session
    log("Scanning for orphaned browser processes...", "DEBUG")
    try:
        config = load_config()
        session_name = config.get('wpp_session', 'default_session')
        # Look for processes with the session token directory in their command line
        # Use both slash types for robustness
        token_dir_win = f"tokens\\{session_name}"
        token_dir_unix = f"tokens/{session_name}"
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pinfo = proc.info
                cmdline = " ".join(pinfo['cmdline'] or [])
                if ("chrome" in pinfo['name'].lower() or "chromium" in pinfo['name'].lower()) and \
                   (token_dir_win in cmdline or token_dir_unix in cmdline):
                    log(f"Force killing browser process (PID {pinfo['pid']})...", "ACTION")
                    proc.kill()
                    log(f"Process {pinfo['pid']} killed.", "DEBUG")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        log(f"Process cleanup error: {e}", "DEBUG")
        
    log("Finalizing shutdown...", "INFO")
    try:
        icon.stop()
        log("Tray icon stopped.", "DEBUG")
    except Exception as e:
        log(f"Icon stop error: {e}", "DEBUG")

    if log_window.root:
        log("Destroying log window...", "DEBUG")
        try:
            log_window.root.destroy()
            log("Log window destroyed.", "DEBUG")
        except Exception as e:
            log(f"Log window destroy error: {e}", "DEBUG")
            
    log("Exiting application now.", "SUCCESS")
    # Final force exit to ensure all threads (including hangs) are terminated
    os._exit(0)

def setup_tray():
    """Create and run the system tray icon."""
    try:
        icon_path = os.path.join(os.path.dirname(__file__), 'chrome-extension', 'icons', 'icon48.png')
        if os.path.exists(icon_path):
            image = Image.open(icon_path)
        else:
            image = Image.new('RGB', (64, 64), color=(73, 109, 137))
            
        def toggle_logout(icon, item):
            config = load_config()
            new_val = not config.get('logout_on_quit', True)
            config['logout_on_quit'] = new_val
            save_config(config)
            log(f"Logout on quit set to: {new_val}", "INFO")

        config = load_config()
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: f"Target: {target_window_title[:50]}...", lambda: None, enabled=False),
            pystray.MenuItem("Select Target Chrome Window", lambda icon, item: chrome_window_selector.show()),
            pystray.MenuItem("Show Group IDs", lambda icon, item: show_group_selector()),
            pystray.MenuItem("Show Logs", lambda icon, item: log_window.toggle()),
            pystray.MenuItem("Logout On Quit", toggle_logout, checked=lambda item: load_config().get('logout_on_quit', True)),
            pystray.MenuItem("Quit", on_quit)
        )
        
        icon = pystray.Icon("whatsapp_tool_server", image, "WhatsApp Tool Server", menu)
        global tray_icon
        tray_icon = icon
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
    
    # Initialize UI and start loop
    log_window.create()
    log_window.hide() # Start hidden
    log_window.root.mainloop()
