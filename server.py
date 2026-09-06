"""
Local Python Server — DOM Data Capture
Receives extracted data from Chrome extension and sends reports via WhatsApp (WPPConnect)
"""
import os
import sys
import urllib.request
import urllib.error
import re
import json
import time
import uuid
import secrets
import ctypes
import ctypes.wintypes
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from flask import Flask, request, jsonify
from WPP_Whatsapp import Create
import threading
import pystray
from PIL import Image, ImageGrab
import tkinter as tk
from tkinter import scrolledtext
from queue import Queue
import psutil
import pyperclip
from caption_math import (
    CaptionMathError,
    build_caption,
    compute_caption_counts,
    is_all_low_wind,
    parse_manual_intervention,
)

# WhatsApp send must finish before the extension's fetch abort (200s)
WHATSAPP_SEND_TIMEOUT_SEC = 150
WHATSAPP_SEND_HARD_CEILING_SEC = 600  # abandon wedged send after 10 min
# After queueing (waitForAck:false), poll message.ack — avoid awaiting sendMsgResult
WA_ACK_POLL_TIMEOUT_SEC = 25
WA_ACK_POLL_INTERVAL_SEC = 0.5
WA_ACK_SENT = 1  # AckType.SENT — left client toward WhatsApp servers
_send_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wa-send")
_wa_send_lock = threading.Lock()
_wa_send_started_at = None
_config_cache = None
_config_cache_mtime = None
_config_lock = threading.RLock()  # RLock: load_config may call save_config via _ensure_api_token
_wa_init_lock = threading.Lock()
_wa_boot_grace_until = 0.0
# Last WhatsApp send outcome — surfaced via /api/status so the extension can tell a
# confirmed failure apart from an unknown/duplicate-risk timeout (BND-001).
_last_send_outcome = None
_last_send_outcome_lock = threading.Lock()
# Idempotency: remember recent successful capture_ids (avoid duplicate WA sends)
_recent_capture_ids = {}
_recent_capture_lock = threading.Lock()
_RECENT_CAPTURE_TTL_SEC = 4 * 3600
_RECENT_CAPTURE_MAX = 64


def _reset_send_executor():
    """Replace a wedged single-worker send executor."""
    global _send_executor
    old = _send_executor
    _send_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wa-send")
    try:
        old.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        old.shutdown(wait=False)
    except Exception as e:
        log(f"Send executor shutdown error: {e}", "WARNING")

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

user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

# ─── Global State for Targeted Window ───
target_hwnd = None
target_window_title = "Chưa chọn"
tray_icon = None

def _get_window_class(hwnd):
    """Get the Win32 class name of a window."""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value

def _enum_windows_callback(hwnd, results):
    """Collect all visible top-level windows."""
    if user32.IsWindowVisible(hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            results.append((hwnd, buf.value))
    return True

def _is_chrome_window(hwnd, title):
    """Check if a window is a Chrome/Chromium window by title or class name."""
    title_lower = title.lower()
    if 'google chrome' in title_lower or 'chromium' in title_lower:
        return True
    # Fallback: detect by Win32 window class name (works for app mode, fullscreen, etc.)
    class_name = _get_window_class(hwnd)
    return class_name == 'Chrome_WidgetWin_1'

def get_all_chrome_windows():
    """Returns a list of (hwnd, title) for all visible Chrome windows."""
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.py_object)
    windows = []
    user32.EnumWindows(EnumWindowsProc(_enum_windows_callback), ctypes.py_object(windows))
    return [(hwnd, title) for hwnd, title in windows if _is_chrome_window(hwnd, title)]

def find_chrome_window():
    """
    Find the Chrome window. Uses manual selection only. Returns hwnd or None.
    Re-validates HWND is still a Chrome window (not recycled to another app).
    """
    global target_hwnd, target_window_title, tray_icon

    if target_hwnd:
        if (user32.IsWindow(target_hwnd) and user32.IsWindowVisible(target_hwnd)
                and _get_window_class(target_hwnd) == 'Chrome_WidgetWin_1'):
            log(f"Sử dụng cửa sổ chọn thủ công: hwnd={target_hwnd}", "SUCCESS")
            return target_hwnd
        else:
            log("Cửa sổ đã chọn không còn hợp lệ (đóng/ẩn/HWND recycle). Trở về chưa chọn.", "WARNING")
            target_hwnd = None
            target_window_title = "Chưa chọn"
            if tray_icon:
                try:
                    tray_icon.update_menu()
                except Exception:
                    pass
            return None

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
# No open CORS — Chrome extension uses host_permissions and does not need CORS.
# Browser pages on the same machine therefore cannot complete JSON POSTs to this API.

# --- Configuration ---
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), 'screenshots')
capture_lock = threading.Lock()

if not os.path.exists(SCREENSHOT_DIR):
    os.makedirs(SCREENSHOT_DIR)

def _ensure_api_token(config):
    """Ensure a shared API token exists; persist if newly generated."""
    placeholders = {
        '',
        'paste-token-from-server-config-or-log',
        'changeme',
        'your-token-here',
    }
    token = (config.get('api_token') or '').strip()
    if token and token not in placeholders and token.lower() not in placeholders:
        return config
    token = secrets.token_hex(16)
    config['api_token'] = token
    try:
        save_config(config)
        log(f"Generated api_token (prefix {token[:8]}… — copy full value from config.json into extension)", "ACTION")
    except Exception:
        pass
    return config

def load_config():
    global _config_cache, _config_cache_mtime
    with _config_lock:
        try:
            if os.path.exists(CONFIG_PATH):
                mtime = os.path.getmtime(CONFIG_PATH)
                if _config_cache is not None and _config_cache_mtime == mtime:
                    return dict(_config_cache)
        except Exception:
            pass

        if not os.path.exists(CONFIG_PATH):
            default_config = {
                "wpp_session": "default_session",
                "phone_number": "",
                "test_phone_number": "",
                "max_retention_days": 3,
                "logout_on_quit": True,
                "api_token": secrets.token_hex(16)
            }
            try:
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, indent=4)
                log(f"Created config.json — set phone_number; api_token prefix {default_config['api_token'][:8]}…", "ACTION")
            except Exception:
                pass
            _config_cache = dict(default_config)
            _config_cache_mtime = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else None
            return default_config

        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log(f"Corrupt config.json ({e}) — using safe defaults until fixed", "ERROR")
            return {
                "wpp_session": "default_session",
                "phone_number": "",
                "test_phone_number": "",
                "max_retention_days": 3,
                "logout_on_quit": True,
                "api_token": ""
            }
        if 'logout_on_quit' not in config:
            config['logout_on_quit'] = True
        config = _ensure_api_token(config)
        try:
            _config_cache = dict(config)
            _config_cache_mtime = os.path.getmtime(CONFIG_PATH)
        except Exception:
            pass
        return config

def require_api_token():
    """Validate X-API-Token header against config. Returns (ok, error_response_tuple_or_None)."""
    config = load_config()
    expected = (config.get('api_token') or '').strip()
    provided = (request.headers.get('X-API-Token') or '').strip()
    try:
        ok = bool(expected) and bool(provided) and secrets.compare_digest(provided, expected)
    except (TypeError, ValueError):
        ok = False
    if not ok:
        return False, (jsonify({"success": False, "error": "Unauthorized: invalid or missing X-API-Token"}), 401)
    return True, None

def parse_number(v):
    """Parse scraped numeric strings (units, thousand separators, decimal comma/dot)."""
    raw = str(v).strip().replace('\u2212', '-').replace('\u2013', '-').replace('\u2014', '-')
    if not raw:
        raise ValueError("empty")
    # Reject unknown unit suffixes instead of silently stripping junk
    unit_m = re.search(r'(?i)\s*([a-z/%]+)\s*$', raw)
    if unit_m:
        unit = unit_m.group(1).lower()
        if unit not in ('mw', 'mwh', 'm/s', 'tb', 'kwh', 'kw'):
            raise ValueError(f"unknown unit: {unit}")
    # Strip known units but keep the number
    s = re.sub(r'(?i)\s*(mw|mwh|m/s|tb|kwh|kw)\s*$', '', raw).strip()
    s = re.sub(r'[^\d,.\-]', '', s)
    if not s or s in ('-', '.', ',', '-.', '.-'):
        raise ValueError(f"unparseable: {v!r}")
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        parts = s.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2 and parts[1].isdigit():
            s = parts[0].replace('.', '') + '.' + parts[1]
        else:
            s = s.replace(',', '')
    elif '.' in s:
        parts = s.split('.')
        # Only treat as vi-VN thousands when there are MULTIPLE dots
        # (e.g. "1.234.567"). A single trailing group of 3 ("3.750") is a decimal.
        if len(parts) > 2 and all(p.isdigit() for p in parts) and all(len(p) == 3 for p in parts[1:]):
            s = ''.join(parts)
        # else leave as decimal: "5.3", "3.750"
    return float(s)


def parse_deg_force22h(raw):
    """Parse DEG for force_22h reports; treat single-group thousands (e.g. 1.234) as 1234 MWh."""
    s = str(raw).strip().replace('\u2212', '-').replace('\u2013', '-').replace('\u2014', '-')
    s = re.sub(r'(?i)\s*(mw|mwh|m/s|tb|kwh|kw)\s*$', '', s).strip()
    s = re.sub(r'[^\d,.\-]', '', s)
    if re.match(r'^\d{1,3}\.\d{3}$', s):
        return float(s.replace('.', ''))
    return parse_number(raw)


def validate_recipient(number):
    """Accept WhatsApp group/user ids or E.164-ish digits."""
    n = (number or '').strip()
    if not n:
        return False
    if re.match(r'^[\d\-]+@(g|c)\.us$', n):
        return True
    if re.match(r'^\+?\d{8,15}$', n):
        return True
    return False

def save_config(config):
    global _config_cache, _config_cache_mtime
    try:
        tmp_path = CONFIG_PATH + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)
        with _config_lock:
            _config_cache = dict(config)
            try:
                _config_cache_mtime = os.path.getmtime(CONFIG_PATH)
            except Exception:
                _config_cache_mtime = None
        log("Configuration saved.", "DEBUG")
    except Exception as e:
        log(f"Error saving config: {e}", "ERROR")
        try:
            if os.path.exists(CONFIG_PATH + '.tmp'):
                os.remove(CONFIG_PATH + '.tmp')
        except Exception:
            pass

# --- Logging ---
log_queue = Queue()

def log(message, type="INFO"):
    icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "ACTION": "🚀", "DEBUG": "🔍", "WARNING": "⚠️"}
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {icons.get(type, '🔹')} {message}"
    print(formatted_msg)
    log_queue.put(formatted_msg + "\n")

# --- WhatsApp Manager (WPP_Whatsapp) ---
whatsapp_client = None
whatsapp_creator = None

def init_whatsapp():
    global whatsapp_client, whatsapp_creator
    if not _wa_init_lock.acquire(blocking=False):
        log("WhatsApp init already in progress — skip", "WARNING")
        return
    try:
        if _wa_send_lock.locked():
            # A WhatsApp send is using the browser page right now — tearing the session
            # down here would kill that send mid-flight ("Execution context was destroyed").
            # Skip this reconnect; the health loop will retry once the send finishes.
            log(
                "WhatsApp send in progress — skipping re-init (will retry after send completes)",
                "WARNING",
            )
            return
        old_creator = whatsapp_creator
        if old_creator is not None:
            try:
                log("Closing previous WhatsApp session before re-init...", "INFO")
                if hasattr(old_creator, 'sync_close'):
                    old_creator.sync_close()
            except Exception as close_err:
                log(f"Previous WA session close error: {close_err}", "WARNING")
            whatsapp_client = None
            whatsapp_creator = None

        browsers_path = os.path.join(os.path.dirname(__file__), 'playwright-browsers')
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = browsers_path
        log(f"Playwright browsers path: {browsers_path}", "DEBUG")

        config = load_config()
        session_name = config.get('wpp_session', 'default_session')
        log(f"Initializing WhatsApp session: {session_name}...", "INFO")
        log("A browser window will open for QR code scanning.", "ACTION")
        try:
            import importlib.metadata as _imd
            log(f"WPP_Whatsapp version: {_imd.version('WPP_Whatsapp')}", "INFO")
        except Exception:
            pass

        whatsapp_creator = Create(session=session_name)
        whatsapp_client = whatsapp_creator.start()

        if whatsapp_creator.state == 'CONNECTED':
            log("WhatsApp connected successfully!", "SUCCESS")
        else:
            log(f"WhatsApp state: {whatsapp_creator.state}", "WARNING")

    except Exception as e:
        log(f"WhatsApp init error: {e}", "ERROR")
        log(f"Full traceback:\n{traceback.format_exc()}", "ERROR")
    finally:
        _wa_init_lock.release()

# Pairing / boot states — do not treat as a dropped session
_WA_PAIRING_STATES = frozenset({
    None, 'OPENING', 'UNPAIRED', 'PAIRING', 'QRCODE', 'QR', 'STARTING',
    'INITIALIZING', 'SYNCING', 'LOADING'
})

# WPP's stock sendImage awaits result.sendMsgResult (waitForAck:true) which often
# hangs >150s even after the message is already visible in WhatsApp Web, then fails
# with "Execution context was destroyed". Queue with waitForAck:false, then poll ack.
_SEND_IMAGE_NO_ACK_JS = """
async ({ to, base64, filename, caption }) => {
  const result = await WPP.chat.sendFileMessage(to, base64, {
    type: 'image',
    filename,
    caption: caption || '',
    waitForAck: false,
  });
  const id = result && (result.id != null
    ? (typeof result.id === 'string' ? result.id : (result.id._serialized || String(result.id)))
    : null);
  return {
    ack: result && result.ack,
    id,
    error: result && result.message,
  };
}
"""

_GET_MESSAGE_ACK_JS = """
async (messageId) => {
  try {
    let msg = null;
    if (window.WPP && WPP.chat && typeof WPP.chat.getMessageById === 'function') {
      msg = await WPP.chat.getMessageById(messageId);
    }
    if (!msg && window.WAPI && typeof WAPI.getMessageById === 'function') {
      msg = await WAPI.getMessageById(messageId);
    }
    if (!msg) return { found: false, ack: null };
    const ack = typeof msg.ack === 'number' ? msg.ack : null;
    return { found: true, ack };
  } catch (e) {
    return { found: false, ack: null, error: String((e && e.message) || e) };
  }
}
"""


def _normalize_wa_msg_id(raw_id):
    if raw_id is None:
        return None
    if isinstance(raw_id, str):
        return raw_id
    if isinstance(raw_id, dict):
        return raw_id.get("_serialized") or raw_id.get("id") or str(raw_id)
    return str(raw_id)


def _page_eval_sync(client, js, arg, timeout_=15):
    """Run page_evaluate on the Playwright loop (same pattern as send)."""
    async def _evaluate():
        return await client.ThreadsafeBrowser.page_evaluate(js, arg, page=client.page)

    return client.ThreadsafeBrowser.run_threadsafe(_evaluate(), timeout_=timeout_)


def _poll_whatsapp_ack(client, message_id, timeout_sec=WA_ACK_POLL_TIMEOUT_SEC):
    """Poll message.ack until SENT (>=1), FAILED (<0), or timeout. Returns (ack, status)."""
    deadline = time.time() + max(1.0, float(timeout_sec))
    last_ack = None
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        # Don't start an eval that cannot finish before deadline
        eval_timeout = min(10.0, max(1.0, remaining))
        try:
            info = _page_eval_sync(client, _GET_MESSAGE_ACK_JS, message_id, timeout_=eval_timeout)
        except Exception as poll_err:
            log(f"WA ack poll error: {poll_err}", "DEBUG")
            info = None

        if isinstance(info, dict) and info.get("found") and isinstance(info.get("ack"), int):
            last_ack = info["ack"]
            if last_ack >= WA_ACK_SENT:
                return last_ack, "sent"
            if last_ack < 0:
                return last_ack, "failed"
        if time.time() + WA_ACK_POLL_INTERVAL_SEC >= deadline:
            break
        time.sleep(WA_ACK_POLL_INTERVAL_SEC)
    return last_ack, "timeout"


def _send_whatsapp_image(client, to, file_path, filename, caption, timeout=60):
    """Send image+caption; queue without sendMsgResult, then verify via ack poll."""
    to_id = client.valid_chatId(to)
    if not file_path or not os.path.exists(file_path):
        raise FileNotFoundError(f"Screenshot not found: {file_path}")
    b64 = client.fileToBase64(file_path)
    if not b64:
        raise RuntimeError(f"Could not encode image: {file_path}")
    mime = client.base64MimeType(b64)
    if not mime or "image" not in mime:
        raise RuntimeError(f"Not an image (mime={mime!r})")

    # Reserve time inside the outer Future timeout for ack polling
    queue_timeout = max(15, int(timeout) - int(WA_ACK_POLL_TIMEOUT_SEC) - 5)

    result = _page_eval_sync(
        client,
        _SEND_IMAGE_NO_ACK_JS,
        {
            "to": to_id,
            "base64": b64,
            "filename": filename or os.path.basename(file_path),
            "caption": caption or "",
        },
        timeout_=queue_timeout,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected sendImage result: {result!r}")

    msg_id = _normalize_wa_msg_id(result.get("id"))
    if not msg_id:
        err = result.get("error") or "no message id"
        raise RuntimeError(f"WhatsApp send did not return message id: {err}")

    initial_ack = result.get("ack")
    if isinstance(initial_ack, int) and initial_ack < 0:
        raise RuntimeError(f"WhatsApp send failed immediately (ack={initial_ack})")
    if isinstance(initial_ack, int) and initial_ack >= WA_ACK_SENT:
        log(f"WA message queued+acked id={msg_id} ack={initial_ack}", "SUCCESS")
        return {"id": msg_id, "ack": initial_ack, "verified": True}

    ack, status = _poll_whatsapp_ack(client, msg_id, timeout_sec=WA_ACK_POLL_TIMEOUT_SEC)
    if status == "sent":
        log(f"WA message verified id={msg_id} ack={ack}", "SUCCESS")
        return {"id": msg_id, "ack": ack, "verified": True}
    if status == "failed":
        raise RuntimeError(f"WhatsApp send failed (ack={ack}, id={msg_id})")

    # Queued (have msg_id) but ack not yet SENT — usually still delivers; do NOT raise
    # (raising SEND_FAILED caused extension transient retry → duplicate reports)
    log(
        f"WA message queued, ack unverified id={msg_id} last_ack={ack} "
        f"(waited {WA_ACK_POLL_TIMEOUT_SEC}s)",
        "WARNING"
    )
    return {"id": msg_id, "ack": ack, "verified": False}


def whatsapp_health_loop():
    """Poll WA state; re-init only after a real disconnect (not during QR pairing)."""
    backoff_sec = 60
    while True:
        time.sleep(backoff_sec)
        try:
            if time.time() < _wa_boot_grace_until:
                continue
            if _wa_init_lock.locked():
                continue
            if _wa_send_lock.locked():
                # Do not sync_close / re-init while a send is using the browser page
                continue
            state = getattr(whatsapp_creator, 'state', None) if whatsapp_creator else None
            if state == 'CONNECTED':
                backoff_sec = 60
                continue
            if whatsapp_creator is None or state in _WA_PAIRING_STATES:
                continue
            log(f"WhatsApp disconnected (state={state!r}) — attempting reconnect...", "WARNING")
            init_whatsapp()
            if whatsapp_creator and getattr(whatsapp_creator, 'state', None) == 'CONNECTED':
                log("WhatsApp reconnected.", "SUCCESS")
                backoff_sec = 60
            else:
                backoff_sec = min(600, max(60, backoff_sec * 2))
                log(f"Reconnect incomplete; next try in {backoff_sec}s", "WARNING")
        except Exception as e:
            backoff_sec = min(600, max(60, backoff_sec * 2))
            log(f"WhatsApp health loop error: {e}. Next try in {backoff_sec}s", "ERROR")

def get_groups():
    """Fetch groups from WhatsApp and return a list of (name, id) tuples."""
    global whatsapp_client
    if not whatsapp_client or not whatsapp_creator or whatsapp_creator.state != 'CONNECTED':
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
    try:
        retention_days = int(retention_days)
    except (TypeError, ValueError):
        log(f"Invalid max_retention_days={retention_days!r}, using 3", "WARNING")
        retention_days = 3
    if retention_days <= 0:
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
    Capture the full desktop (all monitors, including taskbar).
    Still focuses/maximizes the selected Chrome window first so the target UI is visible.
    Returns (path, None) on success, (None, error_code) on failure.
    error_code: NO_TARGET_WINDOW | SCREENSHOT_FAILED
    """
    try:
        config = load_config()
        cleanup_old_screenshots(config.get('max_retention_days', 3))
    except Exception as e:
        log(f"Screenshot cleanup skipped: {e}", "WARNING")

    hwnd = None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SCREENSHOT_DIR, f"capture_{ts}_{uuid.uuid4().hex[:8]}.png")

    try:
        hwnd = find_chrome_window()
        if not hwnd:
            log("No Chrome window selected. Capture cancelled.", "ERROR")
            return None, "NO_TARGET_WINDOW"

        focus_and_restore_window(hwnd)
        with NativeWindowLock(hwnd):
            time.sleep(0.5)
            log("Đang chụp full desktop (Native Lock active)...", "INFO")
            try:
                img = ImageGrab.grab(all_screens=True)
            except TypeError:
                img = ImageGrab.grab()

        img.save(path)
        log(f"Screenshot saved: {path}", "SUCCESS")
        return path, None
    except Exception as e:
        if hwnd:
            focus_and_restore_window(hwnd)
        log(f"Screenshot error: {e}", "ERROR")
        return None, "SCREENSHOT_FAILED"


# ─── API Endpoints ─── 

def _remember_capture(capture_id, response_body):
    if not capture_id:
        return
    now = time.time()
    with _recent_capture_lock:
        # Drop expired / overflow
        expired = [k for k, (ts, _) in _recent_capture_ids.items() if now - ts > _RECENT_CAPTURE_TTL_SEC]
        for k in expired:
            _recent_capture_ids.pop(k, None)
        while len(_recent_capture_ids) >= _RECENT_CAPTURE_MAX:
            oldest = min(_recent_capture_ids.items(), key=lambda kv: kv[1][0])[0]
            _recent_capture_ids.pop(oldest, None)
        _recent_capture_ids[capture_id] = (now, response_body)


def _lookup_capture(capture_id):
    if not capture_id:
        return None
    now = time.time()
    with _recent_capture_lock:
        entry = _recent_capture_ids.get(capture_id)
        if not entry:
            return None
        ts, body = entry
        if now - ts > _RECENT_CAPTURE_TTL_SEC:
            _recent_capture_ids.pop(capture_id, None)
            return None
        return body


def _set_send_outcome(status, detail, capture_id=None):
    """Record the latest WhatsApp send outcome (surfaced via /api/status)."""
    global _last_send_outcome
    with _last_send_outcome_lock:
        _last_send_outcome = {
            "status": status,
            "detail": detail or "",
            "capture_id": capture_id or None,
            "at": datetime.now().isoformat(),
        }


def _get_send_outcome():
    with _last_send_outcome_lock:
        return dict(_last_send_outcome) if _last_send_outcome else None


@app.route('/api/status', methods=['GET'])
def status():
    """Health check endpoint (no auth — does not expose secrets)."""
    wa_connected = bool(
        whatsapp_client and whatsapp_creator and getattr(whatsapp_creator, 'state', None) == 'CONNECTED'
    )
    try:
        cfg = load_config()
        phone = (cfg.get('phone_number') or '').strip()
        test_phone = (cfg.get('test_phone_number') or '').strip()
        recipient_ok = validate_recipient(phone)
        test_recipient_ok = validate_recipient(test_phone)
        expected = (cfg.get('api_token') or '').strip()
        provided = (request.headers.get('X-API-Token') or '').strip()
        if not expected:
            token_valid = False
        elif not provided:
            token_valid = False
        else:
            token_valid = secrets.compare_digest(provided, expected)
    except Exception:
        recipient_ok = False
        test_recipient_ok = False
        token_valid = False
    window_ok = bool(
        target_hwnd
        and user32.IsWindow(target_hwnd)
        and user32.IsWindowVisible(target_hwnd)
        and _get_window_class(target_hwnd) == 'Chrome_WidgetWin_1'
    )
    return jsonify({
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0",
        "whatsapp_connected": wa_connected,
        "whatsapp_send_busy": _wa_send_lock.locked(),
        "last_send_outcome": _get_send_outcome(),
        "recipient_configured": recipient_ok,
        "test_recipient_configured": test_recipient_ok,
        "token_valid": token_valid,
        "target_window_selected": window_ok,
        "auth_required": True
    })

@app.route('/api/focus', methods=['POST'])
def api_focus_target():
    """Brings the user-selected target Chrome window to the foreground."""
    ok, err = require_api_token()
    if not ok:
        return err
    if capture_lock.locked():
        return jsonify({"success": False, "error": "Capture in progress", "error_code": "CAPTURE_BUSY"}), 409
    try:
        hwnd = find_chrome_window()
        if hwnd:
            focus_and_restore_window(hwnd)
            log(f"API yêu cầu đưa cửa sổ target lên trước thành công: {target_window_title}", "SUCCESS")
            return jsonify({"success": True})
        else:
            log("API yêu cầu focus nhưng chưa chọn cửa sổ", "WARNING")
            return jsonify({"success": False, "error": "Chưa chọn cửa sổ Chrome trên server", "error_code": "NO_TARGET_WINDOW"}), 400
    except Exception as e:
        log(f"Lỗi khi focus cửa sổ từ API: {e}", "ERROR")
        return jsonify({"success": False, "error": "Focus failed"}), 500


@app.route('/api/capture', methods=['POST'])
def capture():
    """
    Receives captured data from Chrome extension.
    Validates payload, takes screenshot, sends via WhatsApp synchronously,
    then returns the real send outcome.
    """
    ok, err = require_api_token()
    if not ok:
        return err

    if not capture_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "Capture already in progress", "error_code": "CAPTURE_BUSY"}), 409

    try:
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({"success": False, "error": "No JSON payload", "error_code": "BAD_PAYLOAD"}), 400

        capture_id = (payload.get('capture_id') or '').strip()
        cached = _lookup_capture(capture_id)
        if cached is not None:
            log(f"Duplicate capture_id={capture_id!r} — returning cached success (no re-send)", "WARNING")
            body = dict(cached)
            body['duplicate'] = True
            return jsonify(body)

        config = load_config()
        data = payload.get('data', {})
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Invalid data payload", "error_code": "BAD_PAYLOAD"}), 400
        is_test = bool(payload.get('is_test', False))

        log("=" * 40, "INFO")
        log(f"Received capture at {payload.get('timestamp', 'unknown')}", "ACTION")

        for name, info in data.items():
            val = info.get('value', '') if isinstance(info, dict) else info
            found = info.get('found', True) if isinstance(info, dict) else True
            status_icon = "✅" if found else "❌"
            log(f"  {status_icon} {name}: {val}", "DEBUG")

        # Resolve recipient BEFORE screenshot (fail fast)
        target_number = (config.get('phone_number') or '').strip()
        if is_test:
            test_phone = (config.get('test_phone_number') or '').strip()
            if not test_phone:
                msg = "Không thể gửi test: test_phone_number chưa được cấu hình trong config.json."
                log(msg, "ERROR")
                return jsonify({"success": False, "error": msg, "error_code": "INVALID_RECIPIENT"}), 400
            target_number = test_phone
        if not target_number:
            msg = "phone_number chưa được cấu hình trong config.json."
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg, "error_code": "INVALID_RECIPIENT"}), 400
        if not validate_recipient(target_number):
            msg = f"Invalid recipient format: {target_number!r}"
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg, "error_code": "INVALID_RECIPIENT"}), 400

        def get_val(name):
            info = data.get(name, {})
            if isinstance(info, dict):
                return str(info.get('value') if info.get('value') is not None else '').strip()
            return str(info if info is not None else '').strip()

        dc = get_val("DC")
        aws = get_val("AWS")
        tap = get_val("TAP")
        deg = get_val("DEG")
        tb_names = [f"TB{i}" for i in range(1, 13)]
        tb_raw = [get_val(n) for n in tb_names]
        tbs_names = [f"TBS{i}" for i in range(1, 13)]
        tbs_raw = [get_val(n) for n in tbs_names]
        force_22h = bool(payload.get('force_22h', False))

        try:
            mi_enabled, mi_turbines = parse_manual_intervention(payload.get('manual_intervention'))
        except CaptionMathError as e:
            log(e.message, "ERROR")
            return jsonify({
                "success": False,
                "error": e.message,
                "error_code": e.error_code,
                "fields": e.fields,
            }), 400

        # DEG only required for 22h/DEG report; hourly runs must not fail on empty DEG
        # F/M no longer accepted from the extension — derived server-side from TBS + power
        missing = []
        for name, val in [("DC", dc), ("AWS", aws), ("TAP", tap)]:
            if not val:
                missing.append(name)
        if force_22h and not deg:
            missing.append("DEG")
        for name, val in zip(tb_names, tb_raw):
            if not val:
                missing.append(name)
        for name, val in zip(tbs_names, tbs_raw):
            if not val:
                missing.append(name)
        if missing:
            msg = f"Missing required fields: {', '.join(missing)}"
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg, "error_code": "MISSING_FIELDS", "fields": missing}), 400

        try:
            tb_values = [parse_number(tb) for tb in tb_raw]
        except ValueError:
            msg = f"Invalid TB value: {', '.join(tb_raw)}"
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg, "error_code": "INVALID_FIELD"}), 400

        try:
            dc_num = int(parse_number(dc))
            aws_num = parse_number(aws)
            tap_num = parse_number(tap)
        except ValueError:
            msg = f"Invalid numeric value in DC/AWS/TAP: DC={dc}, AWS={aws}, TAP={tap}"
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg, "error_code": "INVALID_FIELD"}), 400

        # Sign / range checks — reject bad data instead of sending a distorted caption.
        # TAP chỉ cần là số (đã được parse_number kiểm tra ở trên): giá trị âm là hợp lệ
        # khi nhiều turbine có công suất âm, không giới hạn 0..100 nữa.
        if dc_num < 0 or dc_num > 12:
            msg = f"DC out of range (0..12): {dc_num}"
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg, "error_code": "INVALID_FIELD", "field": "DC"}), 400
        if aws_num < 0 or aws_num > 50:
            msg = f"AWS out of range (0..50 m/s): {aws_num}"
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg, "error_code": "INVALID_FIELD", "field": "AWS"}), 400

        deg_display = deg
        if force_22h and deg:
            try:
                deg_num = parse_deg_force22h(deg)
                if deg_num < 0:
                    raise ValueError("negative DEG")
                deg_display = f"{deg_num:.1f}".rstrip('0').rstrip('.')
            except ValueError:
                msg = f"Invalid DEG value: {deg!r}"
                log(msg, "ERROR")
                return jsonify({"success": False, "error": msg, "error_code": "INVALID_FIELD", "field": "DEG"}), 400

        try:
            counts = compute_caption_counts(
                tb_values, tbs_raw, dc_num, aws_num,
                mi_enabled=mi_enabled, turbines=mi_turbines,
            )
        except CaptionMathError as e:
            log(e.message, "ERROR")
            body = {"success": False, "error": e.message, "error_code": e.error_code}
            if e.fields is not None:
                body["fields"] = e.fields
            return jsonify(body), 400

        m_eff = counts["m_eff"]
        f_eff = counts["f_eff"]
        active = counts["active"]
        low_wind = counts["low_wind"]

        if mi_enabled:
            log(
                f"Manual intervention ON turbines={mi_turbines} "
                f"phase1={counts.get('phase1')} phase2={counts.get('phase2')}",
                "INFO",
            )

        # Fail fast BEFORE screenshot — avoids desktop hijack when WA is down or a prior send is wedged
        if not whatsapp_client or not whatsapp_creator or whatsapp_creator.state != 'CONNECTED':
            msg = "WhatsApp client not connected or initialized."
            log(msg, "ERROR")
            return jsonify({"success": False, "error": msg, "error_code": "WA_DISCONNECTED"}), 503

        wa_lock_held = False
        if not _wa_send_lock.acquire(blocking=False):
            msg = "WhatsApp send already in progress (previous send still running)"
            log(msg, "WARNING")
            return jsonify({"success": False, "error": msg, "error_code": "SEND_BUSY"}), 409
        wa_lock_held = True

        try:
            # Screenshot only after validation + WA ready + send lock held
            log("Capturing target window screenshot...", "INFO")
            screenshot_path, shot_err = take_fullscreen_screenshot()
            if not screenshot_path:
                code = shot_err or "SCREENSHOT_FAILED"
                if code == "NO_TARGET_WINDOW":
                    msg = "Không thể chụp ảnh màn hình (chưa chọn cửa sổ). Huỷ gửi báo cáo."
                    log(msg, "ERROR")
                    return jsonify({"success": False, "error": msg, "error_code": "NO_TARGET_WINDOW"}), 400
                msg = "Chụp ảnh màn hình thất bại (lỗi tạm thời). Thử lại sau."
                log(msg, "ERROR")
                return jsonify({"success": False, "error": msg, "error_code": "SCREENSHOT_FAILED"}), 503

            # Build caption via pure function (caption_math) — rút gọn khi 12 TB gió thấp
            caption = build_caption(
                active=active,
                low_wind=low_wind,
                m_eff=m_eff,
                f_eff=f_eff,
                aws_num=aws_num,
                tap_num=tap_num,
                deg_display=deg_display,
                force_22h=force_22h,
                mi_enabled=mi_enabled,
            )
            # Display values for response payload (keep same formatting as caption)
            aws_display = f"{aws_num:.1f}".rstrip('0').rstrip('.')
            tap_display = f"{tap_num:.1f}".rstrip('0').rstrip('.')

            log(f"Caption: {caption}", "SUCCESS")
            if is_all_low_wind(active=active, low_wind=low_wind, m_eff=m_eff, f_eff=f_eff):
                log("Caption rút gọn do 12 TB gió thấp (ẩn AWS/TAP) — áp dụng cho cả live/test", "INFO")

            msg_type_log = "TEST" if is_test else "LIVE"
            log(f"Sending image to {target_number} ({msg_type_log})...", "ACTION")
            global _wa_send_started_at
            _wa_send_started_at = time.time()
            # Prefer _send_whatsapp_image (no sendMsgResult wait) — stock sendImage often hangs >150s
            img_filename = os.path.basename(screenshot_path) if screenshot_path else "capture.png"
            future = _send_executor.submit(
                _send_whatsapp_image,
                whatsapp_client,
                target_number,
                screenshot_path,
                img_filename,
                caption,
                WHATSAPP_SEND_TIMEOUT_SEC,
            )
            try:
                send_result = future.result(timeout=WHATSAPP_SEND_TIMEOUT_SEC)
            except FuturesTimeoutError:
                msg = f"WhatsApp send timed out after {WHATSAPP_SEND_TIMEOUT_SEC}s"
                log(msg, "ERROR")

                # Hold send lock until send finishes OR hard ceiling, then recover
                late_body = {
                    "success": True,
                    "caption": caption,
                    "sent": True,
                    "is_test": is_test,
                    "values": {
                        "DC": dc, "AWS": aws_display, "TAP": tap_display,
                        "DEG": deg_display,
                        "active": str(active)
                    }
                }

                def _release_after_send(f, cid, body):
                    global _wa_send_started_at
                    try:
                        f.result(timeout=WHATSAPP_SEND_HARD_CEILING_SEC)
                        log("Late WA send completed after HTTP timeout", "SUCCESS")
                        _remember_capture(cid, body)
                        _set_send_outcome("success", "Late send completed after HTTP timeout", cid)
                        try:
                            if screenshot_path and os.path.exists(screenshot_path):
                                os.remove(screenshot_path)
                        except Exception:
                            pass
                    except FuturesTimeoutError:
                        log(
                            f"WA send wedged >{WHATSAPP_SEND_HARD_CEILING_SEC}s — "
                            "resetting send executor and releasing lock",
                            "ERROR"
                        )
                        _set_send_outcome(
                            "wedged",
                            "WhatsApp send wedged >600s and was abandoned",
                            cid,
                        )
                        _reset_send_executor()
                        try:
                            # cancel_futures can't kill the running thread; the lock is
                            # released below, after which init_whatsapp's send-lock guard
                            # (SRV-001) will let the re-init proceed safely.
                            threading.Thread(target=init_whatsapp, daemon=True).start()
                        except Exception:
                            pass
                    except Exception as wait_err:
                        log(f"Background WA send after timeout ended with: {wait_err}", "WARNING")
                        _set_send_outcome("failed", f"Late send failed: {wait_err}", cid)
                    finally:
                        _wa_send_started_at = None
                        _wa_send_lock.release()

                threading.Thread(
                    target=_release_after_send,
                    args=(future, capture_id, late_body),
                    daemon=True
                ).start()
                wa_lock_held = False  # ownership transferred
                return jsonify({
                    "success": False,
                    "error": msg,
                    "error_code": "SEND_TIMEOUT",
                    "caption": caption
                }), 504

            _set_send_outcome("success", "WhatsApp report sent successfully", capture_id)
            log("Report sent successfully!", "SUCCESS")
            _wa_send_started_at = None
            try:
                os.remove(screenshot_path)
                log(f"Deleted screenshot after send: {screenshot_path}", "DEBUG")
            except Exception as del_err:
                log(f"Could not delete screenshot: {del_err}", "WARNING")
            wa_meta = send_result if isinstance(send_result, dict) else {}
            success_body = {
                "success": True,
                "caption": caption,
                "sent": True,
                "is_test": is_test,
                "wa_message_id": wa_meta.get("id"),
                "wa_ack": wa_meta.get("ack"),
                "wa_verified": wa_meta.get("verified"),
                "values": {
                    "DC": dc, "AWS": aws_display, "TAP": tap_display,
                    "DEG": deg_display,
                    "active": str(active)
                }
            }
            _remember_capture(capture_id, success_body)
            return jsonify(success_body)
        except Exception as e:
            log(f"WhatsApp sending error: {e}\n{traceback.format_exc()}", "ERROR")
            _set_send_outcome("failed", f"WhatsApp send failed: {str(e)[:300]}", capture_id)
            return jsonify({
                "success": False,
                "error": "WhatsApp send failed",
                "error_code": "SEND_FAILED",
                "detail": str(e)[:300],
                "caption": locals().get('caption', '')
            }), 502
        finally:
            if wa_lock_held:
                _wa_send_lock.release()

    except Exception as e:
        log(f"Capture processing error: {e}\n{traceback.format_exc()}", "ERROR")
        return jsonify({"success": False, "error": "Capture processing failed"}), 500
    finally:
        capture_lock.release()


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
            
            # Prevent infinite memory consumption over months of 24/7 uptime
            try:
                num_lines = int(float(self.text_area.index('end-1c')))
                if num_lines > 2000:
                    self.text_area.delete('1.0', f'{num_lines - 2000 + 1}.0')
            except Exception:
                pass
                
            self.text_area.see(tk.END)
        self.root.after(100, self.update_logs)

    def show(self):
        if not self.root:
            # Should not happen as it's created at startup
            pass
        else:
            self.root.after(0, lambda: (self.root.deiconify(), self.root.lift()))
            self.visible = True

    def hide(self):
        if self.root:
            self.root.after(0, self.root.withdraw)
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

    # Event to signal when cleanup is done (success or fail)
    cleanup_done = threading.Event()

    # Failsafe: Force exit after 10 seconds if shutdown hangs
    def force_exit_failsafe():
        if not cleanup_done.wait(timeout=10):
            log("Failsafe: Forcing exit after 10s timeout.", "WARNING")
            os._exit(0)

    threading.Thread(target=force_exit_failsafe, daemon=True).start()

    def library_cleanup():
        try:
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
        finally:
            cleanup_done.set()  # Signal completion even if cleanup failed

    # Run library cleanup in a separate thread to avoid blocking the main quit thread
    cleanup_thread = threading.Thread(target=library_cleanup)
    cleanup_thread.start()

    # Give library time to finish cleanup
    cleanup_thread.join(timeout=10)
    cleanup_done.set()  # Ensure failsafe doesn't fire if cleanup finished in time

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
                cmdline = " ".join(str(arg) for arg in (pinfo.get('cmdline') or []))
                proc_name = pinfo.get('name') or ''
                if ("chrome" in proc_name.lower() or "chromium" in proc_name.lower()) and \
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
            log_window.root.after(0, log_window.root.destroy)
            log("Log window destroy command queued.", "DEBUG")
        except Exception as e:
            log(f"Log window destroy command queue error: {e}", "DEBUG")
            
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
        def reconnect_whatsapp(icon, item):
            log("Manual WhatsApp reconnect requested...", "ACTION")
            threading.Thread(target=init_whatsapp, daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: f"Target: {target_window_title[:50]}{'...' if len(target_window_title) > 50 else ''}", lambda: None, enabled=False),
            pystray.MenuItem("Select Target Chrome Window", lambda icon, item: chrome_window_selector.show()),
            pystray.MenuItem("Reconnect WhatsApp", reconnect_whatsapp),
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
    # Prevent two server instances (Flask binds 127.0.0.1:5001)
    try:
        probe = urllib.request.urlopen('http://127.0.0.1:5001/api/status', timeout=2)
        probe.read()
        log("ERROR: Another server instance is already running on 127.0.0.1:5001", "ERROR")
        try:
            import tkinter.messagebox as _mb
            _mb.showerror("WhatsApp Tool Server", "Server đã chạy trên cổng 5001.\nChỉ được mở một instance.")
        except Exception:
            pass
        sys.exit(1)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass

    log("=" * 50, "INFO")
    log("DOM Data Capture Server starting...", "ACTION")
    log(f"Config: {CONFIG_PATH}", "DEBUG")
    log(f"Screenshots: {SCREENSHOT_DIR}", "DEBUG")
    log("Endpoints:", "INFO")
    log("  GET  /api/status   — Health check", "INFO")
    log("  POST /api/focus    — Focus target window (auth)", "INFO")
    log("  POST /api/capture  — Receive data + send WhatsApp (auth)", "INFO")
    try:
        cfg = load_config()
        tok = (cfg.get('api_token') or '')[:8]
        log(f"API token prefix: {tok}... (full value in config.json → paste into extension)", "ACTION")
        if not (cfg.get('phone_number') or '').strip():
            log("WARNING: phone_number is empty — live captures will be rejected until configured", "WARNING")
    except Exception as e:
        log(f"Could not load config at startup: {e}", "ERROR")
    log("=" * 50, "INFO")
    
    # Run Flask in a background thread
    flask_thread = threading.Thread(target=lambda: app.run(host='127.0.0.1', port=5001, debug=False, use_reloader=False))
    flask_thread.daemon = True
    flask_thread.start()
    
    # Setup System Tray (runs in its own thread internally now)
    setup_tray()
    
    # Initialize WhatsApp (runs in the main thread or separate thread as needed)
    # WPP_Whatsapp.Create needs to run where it can open a window if needed
    _wa_boot_grace_until = time.time() + 180  # 3 min grace for QR pairing
    threading.Thread(target=init_whatsapp, daemon=True).start()
    threading.Thread(target=whatsapp_health_loop, daemon=True).start()
    
    # Initialize UI and start loop
    log_window.create()
    log_window.hide() # Start hidden
    log_window.root.mainloop()
