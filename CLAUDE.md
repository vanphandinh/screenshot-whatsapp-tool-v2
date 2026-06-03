# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A monitoring report automation tool for wind turbine operations. A Chrome extension scrapes real-time data (DC, AWS, TAP, F, M, DEG, TB1-TB12) from a monitoring dashboard, sends it to a local Python server which takes a full-screen screenshot and forwards the data + screenshot to a WhatsApp group via the WPP_Whatsapp library. A system tray icon provides window selection and application control.

## Architecture

```
Chrome Extension (Manifest V3)          Python Server (Flask :5001)
┌─────────────────────────────┐         ┌──────────────────────────────┐
│ popup.js  ──UI───────────── │         │                              │
│ background.js ──scheduler── │──POST──▶│  /api/capture                │
│   (service worker)          │ /capture│    → take_fullscreen_screenshot()
│ content.js ──DOM scraper─── │         │      (pyautogui + Win32 API) │
│   (isolated world)          │         │    → build Vietnamese caption │
│   + freezePageInMainWorld() │         │    → whatsapp_client.sendImage()
│   (MAIN world injection)    │         │                              │
└─────────────────────────────┘         │  System Tray (pystray)       │
                                        │  Log Window (tkinter)        │
                                        │  Chrome Window Selector      │
                                        │  Group ID Selector           │
                                        └──────────────────────────────┘
```

### Key interaction flow:
1. **Extension** finds/opens the target URL tab, injects `content.js`, freezes the page's network activity (XHR/fetch/WebSocket), extracts DOM values from configured CSS selectors
2. **Extension** POSTs extracted data to `POST /api/capture` on the server
3. **Server** uses Win32 API (`user32`) to bring the pre-selected Chrome window to foreground, takes a pyautogui screenshot
4. **Server** builds a Vietnamese caption describing turbine status (active count, stopped-by-wind, maintenance, errors, wind speed, power output)
5. **Server** sends the screenshot + caption to the configured WhatsApp group via `WPP_Whatsapp` (Playwright-based WhatsApp client)
6. WhatsApp sending runs in a background thread so the HTTP response returns immediately (Chrome MV3 kills slow service workers)

## Setup & Running

```bash
# First-time setup (Windows only — uses Win32 API)
setup_env.bat          # Creates venv, installs requirements.txt

# Start the server (no console window, uses pythonw)
run_server.bat

# Or manually:
venv\Scripts\python.exe server.py
```

The server runs Flask on `127.0.0.1:5001` and opens a system tray icon. No browser console window appears — use the tray icon to show/hide logs, select the Chrome target window, or quit.

### ⚠️ Venv Requirement

**ALL Python commands MUST run inside the `venv` virtual environment.** This applies to:
- **Running** the server: `venv\Scripts\python.exe server.py`
- **Debugging**: activate venv first or use `venv\Scripts\python.exe` directly
- **Testing**: always use `venv\Scripts\pip.exe` for installing packages and `venv\Scripts\python.exe` for running tests
- **Any ad-hoc Python scripts or one-liners**: prefix with `venv\Scripts\python.exe`

Never run `python` or `pip` globally — always use the venv-prefixed paths (`venv\Scripts\python.exe`, `venv\Scripts\pip.exe`). This ensures all dependencies (WPP_Whatsapp, pyautogui, pystray, flask, etc.) are available and avoids polluting the system Python.

## Configuration

`config.json` (gitignored, template at `config.json.example`):
- `phone_number` — WhatsApp group ID (e.g., `120363407108755570@g.us`) for live reports
- `test_phone_number` — WhatsApp group ID for test/demo reports
- `wpp_session` — WPP_Whatsapp session name (stores auth in `tokens/<session>/`)
- `max_retention_days` — auto-delete screenshots older than N days
- `logout_on_quit` — whether to log out of WhatsApp on exit (togglable via tray menu)

The extension stores its own config in `chrome.storage.local` (target URL, CSS selectors, schedule settings). The extension auto-populates 19 required CSS selector fields (DC, AWS, TAP, F, M, DEG, TB1-TB12) — matching the server's validated fields — with empty values. Users must fill in the actual CSS selectors for their target dashboard.

### Extension Config Structure
```javascript
config = {
    serverUrl: 'http://localhost:5001',
    targetUrl: '',
    selectors: { DC: '', AWS: '', TAP: '', F: '', M: '', DEG: '', TB1: '', ... TB12: '' },
    autoCapture: false,
    scheduleMode: '15min',   // '15min' or '30min' random window
    intervalHours: 1         // 1 = hourly, 2 = bi-hourly, 0 = test mode (1 min)
}
```

### Required Fields System
A `REQUIRED_FIELDS` array is defined in both `background.js` and `popup.js` (must stay in sync):
- `DC`, `AWS`, `TAP`, `F`, `M`, `DEG` — summary metrics
- `TB1`–`TB12` — individual turbine statuses
- 19 fields total, matching the server's validation list
- `DEFAULT_CONFIG.selectors` is generated from this array with empty string values
- `getConfig()` merges any missing fields from `DEFAULT_CONFIG` on every read, ensuring upgrades never lose fields
- On `onInstalled`, the merged config is persisted to storage

## Chrome Extension Setup

Load unpacked extension from `chrome-extension/` in `chrome://extensions`. The extension:
- Uses Manifest V3 with service worker (`background.js`)
- Requires host permissions for `<all_urls>` to inject content scripts
- Uses `chrome.alarms` API for scheduling (hourly or bi-hourly, random minute within 0-15 or 0-30 window)
- Stores selector configs in `chrome.storage.local` (mirrors `server.py` validated fields)

## Key Dependencies

| Package | Purpose |
|---------|---------|
| `WPP_Whatsapp` | Playwright-based WhatsApp Web client (session management, messaging) |
| `pyautogui` | Full-screen screenshot capture |
| `pystray` + `Pillow` | Windows system tray icon |
| `tkinter` | Log window and popup UIs (Chrome window selector, group ID picker) |
| `psutil` | Process cleanup on shutdown |
| `pyperclip` | Copy group IDs to clipboard |
| `flask` + `flask-cors` | Local API server |

## Critical Implementation Details

### Win32 Window Management (`server.py`)
The server uses `ctypes` to call `user32.dll` directly for:
- `EnumWindows` to list all visible Chrome windows (by title "Google Chrome" or class `Chrome_WidgetWin_1`)
- Manual target window selection (must be explicitly chosen via tray menu)
- `NativeWindowLock` context manager: sets target window to TopMost, locks foreground transitions (`LockSetForegroundWindow`), and clips the cursor to the window bounds during screenshot capture — then releases everything in `__exit__`
- `focus_and_restore_window()`: ShowWindow(SW_MAXIMIZE) → SetForegroundWindow → BringWindowToTop → SetFocus

### Page Freezing (`background.js` → `content.js` → MAIN world)
During capture, `freezePageInMainWorld()` is injected into the page's MAIN execution world (not the isolated content script world). This overrides `XMLHttpRequest.prototype.send`, `window.fetch`, and `window.WebSocket` to return fake empty responses, preventing the monitoring dashboard from updating mid-capture. The page is reloaded after capture to restore normal APIs.

### Optimistic Scheduling (`background.js`)
Chrome MV3 kills service workers during long operations. The scheduler pre-registers the next alarm BEFORE running the capture job, so even if the SW is killed mid-job, the next scheduled run still fires. A 4-minute watchdog alarm acts as a safety net for hung jobs.

### DEG Report (22h daily)
At 22:00 or 23:00, if a DEG (sản lượng đầu cực) report hasn't been sent today, the capture includes `force_22h: true` which appends a daily energy output line to the caption. For 2-hour interval schedules that skip hour 22, a fallback alarm fires at 23:00. A `degReportDate` flag in storage enforces once-per-day semantics. The DEG fallback job also participates in the auto-disable system — if selectors are missing or the server is unreachable, it disables the scheduler.

### Content Script Extraction
`content.js` `extractData()` iterates the passed `selectors` object. If a selector is empty, it returns `{ value: '', found: false, error: 'No selector configured' }`. Fields with `found: false` are displayed as "⚠ Not found" in the popup preview. If any extracted value is empty/undefined after a real capture, `captureData()` flags the page for reload on the next attempt (`pendingReload` storage flag).

### Validated Fields (turbine data)
Required: DC, AWS, TAP, F, M, DEG, TB1-TB12 (19 fields total). Missing fields halt the report. TB values ≤ 0 count as "inactive turbines." Low-wind turbines (`inactive - F - M`) are reported only if AWS < 6 m/s. Active count = DC - inactive_count, plus low-wind turbines if AWS ≥ 6. **Negative active count guard:** if `active` computes to a negative number (stale/mismatched data), it is clamped to 0 with a warning log.

### Selector Validation & Auto-Disable

**Validation layers** — empty required selectors are blocked at every entry point:

| Layer | Location | Behavior |
|-------|----------|----------|
| Capture choke point | `background.js` `captureData()` | Returns `{success: false, error: "...", missingSelectors: [...]}` listing all unconfigured fields |
| Scheduled runner | `background.js` `runScheduledJob()` | Detects `missingSelectors` → calls `stopScheduler()` + persists `autoCapture: false` (retry won't help) |
| DEG fallback | `background.js` `runDegFallbackJob()` | Same auto-disable logic for missing selectors |
| Manual capture | `popup.js` `captureNow()` | Alerts user with list of unconfigured fields, aborts capture |
| Auto-capture toggle | `popup.js` `saveSettings()` | Blocks enabling auto-capture if any required selector is empty, shows alert + flips toggle back |

**Server unreachable → auto-disable:** When `sendToServer()` returns a connection error (`"Cannot connect to server: ..."`), the scheduled runner and DEG fallback also auto-disable the scheduler (no point retrying if the server isn't running).

**Error classification in scheduled runner:**
- `missingSelectors` → disable auto-capture (configuration issue)
- `"Cannot connect to server:"` → disable auto-capture (server not running)
- All other errors → retry in 5 minutes (transient issue, may recover)

**UI protection** in `popup.js` `renderSelectors()`:
- Required fields are marked with `*` and colored orange (`.required-field` CSS class)
- Remove button is disabled (`disabled`, greyed out, `cursor: not-allowed`) for required fields
- Attempting to delete programmatically shows a warning log entry

### WhatsApp Cleanup on Shutdown
`on_quit()` uses a `threading.Event` (`cleanup_done`) to coordinate the failsafe with actual cleanup progress. The failsafe thread waits up to 10 seconds for `cleanup_done` before calling `os._exit(0)`. Library cleanup (`logout()` + `sync_close()`) runs in a separate thread with a 10-second join timeout and sets `cleanup_done` on completion (via `finally` block, so it fires even on error). Then `psutil` scans for orphaned browser processes matching the session token directory and force-kills them.

### Server-Side Safety Guards
- **`whatsapp_creator` null check:** `get_groups()`, `capture()`, and `send_whatsapp_async()` all check `not whatsapp_client or not whatsapp_creator` before accessing `whatsapp_creator.state` to prevent `AttributeError` on uninitialized client
- **`test_phone_number` required:** Test mode (`is_test: true`) returns a clear 400 error if `test_phone_number` is not configured in `config.json`
- **`hwnd` initialization:** `take_fullscreen_screenshot()` initializes `hwnd = None` before the try block, avoiding a potential `UnboundLocalError` in the except handler
- **Tray menu title truncation:** The target window title menu item uses a lambda to safely truncate long titles without crashing on the `...` suffix
