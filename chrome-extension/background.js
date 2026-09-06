// =============================================================================
// Background Service Worker — DOM Data Capture Extension
// Scheduling logic matching main.py:
//   - Run every hour at random minute within configured window
//   - Retry in 5 minutes on failure
//   - Auto-start on extension load
// =============================================================================

// Các trường dữ liệu server yêu cầu (phải khớp tên với server.py)
// Lưu ý: không còn F/M — server tự tính từ TBS (service mode/hmi stop/fault stop) + công suất
const REQUIRED_FIELDS = [
    'DC', 'AWS', 'TAP', 'DEG',
    'TB1', 'TB2', 'TB3', 'TB4', 'TB5', 'TB6',
    'TB7', 'TB8', 'TB9', 'TB10', 'TB11', 'TB12',
    'TBS1', 'TBS2', 'TBS3', 'TBS4', 'TBS5', 'TBS6',
    'TBS7', 'TBS8', 'TBS9', 'TBS10', 'TBS11', 'TBS12'
];

const DEFAULT_CONFIG = {
    serverUrl: 'http://127.0.0.1:5001',
    targetUrl: '',
    selectors: Object.fromEntries(REQUIRED_FIELDS.map(f => [f, ''])),
    autoCapture: false,  // Default OFF as requested
    scheduleMode: '15min', // '15min' = 0-15 phút, '30min' = 0-30 phút
    intervalHours: 1,      // 1 = mỗi giờ, 2 = mỗi 2 giờ
    apiToken: '',          // Must match server config.json api_token
    manualIntervention: {
        enabled: false,
        turbines: {} // { "3": "maintenance", ... } only selected TBs
    }
};

// Prevent overlapping capture jobs in this service-worker lifetime
let captureInProgress = false;


async function beginJobToken(opts = {}) {
    // Only reuse inflight id when explicitly recovering the same timed-out job (watchdog)
    const allowReuse = opts.reuse === true;
    const stored = await chrome.storage.local.get(['inflightCaptureId', 'inflightCaptureExpires']);
    const now = Date.now();
    const reusable = allowReuse
        && stored.inflightCaptureId
        && stored.inflightCaptureExpires
        && now < stored.inflightCaptureExpires;
    const token = reusable
        ? stored.inflightCaptureId
        : `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    await chrome.storage.local.set({
        activeJobToken: token,
        activeJobStatus: 'running',
        inflightCaptureId: token,
        inflightCaptureExpires: reusable ? stored.inflightCaptureExpires : (now + 12 * 60000)
    });
    return token;
}

async function finishJobToken(token, status) {
    const data = await chrome.storage.local.get('activeJobToken');
    if (data.activeJobToken === token) {
        await chrome.storage.local.set({ activeJobStatus: status || 'done' });
    }
}

async function clearInflightCaptureId() {
    await chrome.storage.local.remove(['inflightCaptureId', 'inflightCaptureExpires']);
}

function waitForTabComplete(tabId, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
            chrome.tabs.onUpdated.removeListener(listener);
            reject(new Error('Page load timeout'));
        }, timeoutMs);
        const listener = (id, info) => {
            if (id === tabId && info.status === 'complete') {
                clearTimeout(timer);
                chrome.tabs.onUpdated.removeListener(listener);
                resolve();
            }
        };
        chrome.tabs.onUpdated.addListener(listener);
    });
}

// Track WebSocket/EventSource so freeze can close sockets opened after tracker install
function installRealtimeTracker() {
    if (window.__domCaptureWSTracked) return;
    window.__domCaptureWSTracked = true;
    window.__domCaptureWS = window.__domCaptureWS || [];
    window.__domCaptureES = window.__domCaptureES || [];

    const OrigWS = window.WebSocket;
    window.WebSocket = function (url, protocols) {
        const ws = protocols !== undefined ? new OrigWS(url, protocols) : new OrigWS(url);
        window.__domCaptureWS.push(ws);
        const remove = () => {
            const i = window.__domCaptureWS.indexOf(ws);
            if (i >= 0) window.__domCaptureWS.splice(i, 1);
        };
        ws.addEventListener('close', remove);
        ws.addEventListener('error', remove);
        return ws;
    };
    window.WebSocket.prototype = OrigWS.prototype;
    Object.assign(window.WebSocket, OrigWS);

    if (window.EventSource) {
        const OrigES = window.EventSource;
        window.EventSource = function (url, config) {
            const es = config !== undefined ? new OrigES(url, config) : new OrigES(url);
            window.__domCaptureES.push(es);
            es.addEventListener('error', () => {
                const i = window.__domCaptureES.indexOf(es);
                if (i >= 0) window.__domCaptureES.splice(i, 1);
            });
            return es;
        };
        window.EventSource.prototype = OrigES.prototype;
        Object.assign(window.EventSource, OrigES);
    }
}

// ─── Freeze page by injecting into MAIN world ───
// This function runs in the page's REAL JavaScript context (not isolated world)
// so it CAN override XMLHttpRequest, fetch, WebSocket etc.
function freezePageInMainWorld() {
    if (window.__domCaptureFrozen) return;
    window.__domCaptureFrozen = true;

    // Close tracked realtime connections (opened after tracker install)
    try {
        (window.__domCaptureWS || []).slice().forEach((ws) => {
            try { ws.close(); } catch (e) { /* ignore */ }
        });
        window.__domCaptureWS = [];
        (window.__domCaptureES || []).slice().forEach((es) => {
            try { es.close(); } catch (e) { /* ignore */ }
        });
        window.__domCaptureES = [];
    } catch (e) { /* ignore */ }

    // 1. Override XMLHttpRequest.prototype.send — silently fake successful empty responses
    const originalOpen = XMLHttpRequest.prototype.open;

    XMLHttpRequest.prototype.open = function (method, url, async, user, password) {
        this.__dcMethod = method;
        this.__dcUrl = url;
        return originalOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function (body) {
        Object.defineProperty(this, 'readyState', { writable: true, value: 4 });
        Object.defineProperty(this, 'status', { writable: true, value: 200 });
        Object.defineProperty(this, 'statusText', { writable: true, value: 'OK' });
        Object.defineProperty(this, 'responseText', { writable: true, value: '' });
        Object.defineProperty(this, 'response', { writable: true, value: '' });

        const self = this;
        setTimeout(() => {
            try {
                self.dispatchEvent(new Event('readystatechange'));
                self.dispatchEvent(new Event('load'));
                self.dispatchEvent(new Event('loadend'));
                if (typeof self.onreadystatechange === 'function') {
                    self.onreadystatechange(new Event('readystatechange'));
                }
                if (typeof self.onload === 'function') {
                    self.onload(new Event('load'));
                }
            } catch (e) { /* ignore */ }
        }, 10);
    };

    // 2. Override fetch — return empty successful response
    window.fetch = function () {
        return Promise.resolve(new Response('', { status: 200, statusText: 'OK' }));
    };

    // 3. Prevent new WebSocket connections
    window.WebSocket = function (url, protocols) {
        return {
            url, readyState: 3,
            send() { },
            close() { },
            addEventListener() { },
            removeEventListener() { },
            CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3,
            binaryType: 'blob', bufferedAmount: 0, extensions: '', protocol: ''
        };
    };
    window.WebSocket.CONNECTING = 0;
    window.WebSocket.OPEN = 1;
    window.WebSocket.CLOSING = 2;
    window.WebSocket.CLOSED = 3;

    // 4. Override EventSource
    if (window.EventSource) {
        window.EventSource = function () {
            return { close() { }, addEventListener() { }, removeEventListener() { } };
        };
    }

    console.log('[DOM Capture] Page FROZEN in MAIN world — XHR/fetch/WS silently intercepted');
}

async function injectRealtimeTracker(tabId) {
    try {
        await chrome.scripting.executeScript({
            target: { tabId },
            world: 'MAIN',
            func: installRealtimeTracker
        });
    } catch (err) {
        console.warn('[DOMCapture] WS tracker inject failed:', err.message);
    }
}

// Helper: inject freeze into page's main world
async function injectFreeze(tabId) {
    try {
        await injectRealtimeTracker(tabId);
        await chrome.scripting.executeScript({
            target: { tabId },
            world: 'MAIN',
            func: freezePageInMainWorld
        });
        console.log('[DOMCapture] Freeze injected into MAIN world');
        return true;
    } catch (err) {
        console.error('[DOMCapture] Failed to inject freeze:', err);
        return false;
    }
}

// ─── Send message with timeout (prevents hanging if content script doesn't respond) ───
function sendMessageWithTimeout(tabId, message, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
            reject(new Error(`sendMessage timed out after ${timeoutMs / 1000}s`));
        }, timeoutMs);

        chrome.tabs.sendMessage(tabId, message)
            .then(response => {
                clearTimeout(timer);
                resolve(response);
            })
            .catch(err => {
                clearTimeout(timer);
                reject(err);
            });
    });
}

// ─── Get config from storage ───
// Luôn merge với DEFAULT_CONFIG để đảm bảo mọi trường selector bắt buộc đều có mặt
async function getConfig() {
    return new Promise((resolve) => {
        chrome.storage.local.get('config', (result) => {
            if (result.config) {
                // Merge selectors: giữ giá trị người dùng đã cấu hình, thêm các trường còn thiếu với giá trị rỗng
                const mergedSelectors = { ...DEFAULT_CONFIG.selectors, ...result.config.selectors };
                // Migration: bỏ F/M scrape — server không nhận 2 trường này nữa
                delete mergedSelectors.F;
                delete mergedSelectors.M;
                const miStored = result.config.manualIntervention || {};
                const manualIntervention = {
                    enabled: Boolean(miStored.enabled),
                    turbines: (miStored.turbines && typeof miStored.turbines === 'object')
                        ? { ...miStored.turbines }
                        : {}
                };
                resolve({
                    ...DEFAULT_CONFIG,
                    ...result.config,
                    selectors: mergedSelectors,
                    manualIntervention
                });
            } else {
                resolve({
                    ...DEFAULT_CONFIG,
                    selectors: { ...DEFAULT_CONFIG.selectors },
                    manualIntervention: { enabled: false, turbines: {} }
                });
            }
        });
    });
}

function buildManualInterventionPayload(config) {
    const mi = config.manualIntervention || DEFAULT_CONFIG.manualIntervention;
    const enabled = Boolean(mi.enabled);
    const allowed = new Set(['normal', 'maintenance', 'error', 'low_wind']);
    const turbines = {};
    if (enabled && mi.turbines && typeof mi.turbines === 'object') {
        for (const [k, v] of Object.entries(mi.turbines)) {
            const key = String(k).trim();
            const status = String(v).trim().toLowerCase();
            if (!/^\d+$/.test(key)) continue;
            const idx = parseInt(key, 10);
            if (idx < 1 || idx > 12) continue;
            if (!allowed.has(status)) continue;
            turbines[String(idx)] = status;
        }
    }
    // Match server: enabled with zero turbines → treat as off
    return { enabled: enabled && Object.keys(turbines).length > 0, turbines };
}

// ─── Save config ───
async function saveConfig(config) {
    return new Promise((resolve) => {
        chrome.storage.local.set({ config }, resolve);
    });
}

// ─── Get/save schedule state ───
async function getScheduleState() {
    return new Promise((resolve) => {
        chrome.storage.local.get('scheduleState', (result) => {
            resolve(result.scheduleState || { status: 'idle', nextRun: null, lastResult: null });
        });
    });
}

async function saveScheduleState(state) {
    return new Promise((resolve) => {
        chrome.storage.local.set({ scheduleState: state }, resolve);
    });
}

// ─── DEG Report (sản lượng đầu cực) daily tracking ───

/**
 * Get today's date string in YYYY-MM-DD format (local time).
 */
function getTodayDateString() {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}

/**
 * Check if the DEG report has already been sent today.
 */
async function isDegReportSentToday() {
    return new Promise((resolve) => {
        chrome.storage.local.get('degReportDate', (data) => {
            resolve(data.degReportDate === getTodayDateString());
        });
    });
}

/**
 * Mark the DEG report as sent for today.
 */
async function markDegReportSent() {
    return new Promise((resolve) => {
        chrome.storage.local.set({ degReportDate: getTodayDateString() }, resolve);
    });
}

/**
 * Determine if the current auto-run series will land on hour 22.
 * Uses the actual next run time to figure out the hour pattern.
 */
async function willCurrentScheduleHit22() {
    const config = await getConfig();
    const intervalHours = config.intervalHours || 1;
    if (intervalHours <= 1) return true;

    // Get the schedule state to find the next run hour
    const state = await getScheduleState();
    if (!state.nextRun) return false;

    const nextRunDate = new Date(state.nextRun);
    if (isNaN(nextRunDate.getTime())) return false;

    const nextRunHour = nextRunDate.getHours();
    // Check if 22 is reachable from this pattern
    // Pattern: nextRunHour, nextRunHour+interval, nextRunHour+2*interval, ... mod 24
    for (let i = 0; i < Math.ceil(24 / intervalHours); i++) {
        const h = (nextRunHour + i * intervalHours) % 24;
        if (h === 22) return true;
    }
    return false;
}

/**
 * Schedule the 23h fallback alarm for DEG report if needed.
 * Only schedules if:
 *   1. Auto-capture is enabled
 *   2. Interval is 2h (so 22h might be skipped)
 *   3. The current schedule pattern won't hit 22h
 *   4. DEG report hasn't been sent today
 *   5. It's not already past 23h today
 */
async function scheduleDegFallbackIfNeeded() {
    const config = await getConfig();
    if (!config.autoCapture) return;

    const alreadySent = await isDegReportSentToday();
    if (alreadySent) {
        await chrome.alarms.clear('dom-capture-deg-fallback');
        return;
    }

    const intervalHours = config.intervalHours || 1;
    if (intervalHours === 0) {
        // Debug mode: scheduled test path covers capture; no live DEG fallback
        await chrome.alarms.clear('dom-capture-deg-fallback');
        return;
    }

    const now = new Date();
    const currentHour = now.getHours();

    if (intervalHours <= 1) {
        if (currentHour >= 23) {
            // Late Chrome start: still need DEG tonight
            chrome.alarms.create('dom-capture-deg-fallback', {
                when: Date.now() + 60000
            });
            console.log('[DOMCapture] Past 23h without DEG — scheduling immediate fallback in 1 min');
            await addCaptureLog('info', 'Đã qua 23h chưa gửi DEG → thử báo cáo sản lượng trong 1 phút');
            return;
        }
        // Hourly schedule covers 22h while Chrome stays open
        await chrome.alarms.clear('dom-capture-deg-fallback');
        return;
    }

    // interval > 1: late start past 23h still needs DEG tonight
    if (currentHour >= 23) {
        chrome.alarms.create('dom-capture-deg-fallback', {
            when: Date.now() + 60000
        });
        console.log('[DOMCapture] Past 23h without DEG — scheduling immediate fallback in 1 min');
        await addCaptureLog('info', 'Đã qua 23h chưa gửi DEG → thử báo cáo sản lượng trong 1 phút');
        return;
    }

    // Check if regular schedule will hit 22h
    const hitsAt22 = await willCurrentScheduleHit22();
    if (hitsAt22) {
        await chrome.alarms.clear('dom-capture-deg-fallback');
        return;
    }

    // Schedule fallback at 23:00 today
    const fallbackTime = new Date(now);
    fallbackTime.setHours(23, 0, 0, 0);

    if (fallbackTime.getTime() > now.getTime()) {
        chrome.alarms.create('dom-capture-deg-fallback', {
            when: fallbackTime.getTime()
        });
        console.log(`[DOMCapture] DEG fallback alarm scheduled at 23:00 (${fallbackTime.toISOString()})`);
        await addCaptureLog('info', 'Lịch 22h bị bỏ qua → đã lên lịch báo cáo sản lượng đầu cực lúc 23:00');
    }
}

// ─── Scheduling logic (mirrors main.py) ───

/**
 * Get the max random seconds based on schedule mode.
 * '15min' → 901 seconds (0-900, i.e. 0-15 minutes)
 * '30min' → 1801 seconds (0-1800, i.e. 0-30 minutes)
 */
function getMaxRandomSeconds(mode) {
    return mode === '30min' ? 1801 : 901;
}

/**
 * Get the minute window limit based on schedule mode.
 * '15min' → 15, '30min' → 30
 */
function getMinuteWindow(mode) {
    return mode === '30min' ? 30 : 15;
}

async function computeNextRunTime(success) {
    const now = new Date();

    if (!success) {
        // Failed → retry in 5 minutes (precise)
        return new Date(now.getTime() + 5 * 60000);
    }

    const config = await getConfig();
    const maxSeconds = getMaxRandomSeconds(config.scheduleMode);
    let intervalHours = parseInt(config.intervalHours, 10);
    if (isNaN(intervalHours)) intervalHours = 1;

    if (intervalHours === 0) {
        // Debug mode: schedule exactly 1 minute from now
        return new Date(now.getTime() + 60000);
    }

    // Success → calculate start of next interval window
    const nextRun = new Date(now);
    nextRun.setHours(nextRun.getHours() + intervalHours);
    nextRun.setMinutes(0);
    nextRun.setSeconds(0);
    nextRun.setMilliseconds(0);

    // Add random seconds within the configured window
    const randomSeconds = Math.floor(Math.random() * maxSeconds);
    return new Date(nextRun.getTime() + randomSeconds * 1000);
}

/**
 * Compute the first run time.
 * If current minute < window limit, pick a random second between (now + 30s) and the window mark.
 * Otherwise, pick next hour random within window.
 * Returns absolute Date object.
 */
async function computeFirstRunTime() {
    const now = new Date();
    const currentMinute = now.getMinutes();
    const config = await getConfig();
    const minuteWindow = getMinuteWindow(config.scheduleMode);
    const maxSeconds = getMaxRandomSeconds(config.scheduleMode);
    let intervalHours = parseInt(config.intervalHours, 10);
    if (isNaN(intervalHours)) intervalHours = 1;

    if (intervalHours === 0) {
        // Debug mode: schedule exactly 1 minute from now
        return new Date(now.getTime() + 60000);
    }

    if (currentMinute < minuteWindow) {
        // Still within the window of the current hour
        const windowEnd = new Date(now);
        windowEnd.setMinutes(minuteWindow);
        windowEnd.setSeconds(0);
        windowEnd.setMilliseconds(0);

        const minTime = now.getTime() + 30000; // current time + 30 seconds
        const maxTime = windowEnd.getTime();

        if (minTime < maxTime) {
            const randomTime = minTime + Math.random() * (maxTime - minTime);
            return new Date(randomTime);
        }
    }

    // Too late for this hour's window — schedule next interval (not always +1h)
    const nextRun = new Date(now);
    nextRun.setHours(nextRun.getHours() + Math.max(1, intervalHours));
    nextRun.setMinutes(0);
    nextRun.setSeconds(0);
    nextRun.setMilliseconds(0);

    const randomSeconds = Math.floor(Math.random() * maxSeconds);
    return new Date(nextRun.getTime() + randomSeconds * 1000);
}

// ─── Schedule next capture alarm ───
async function scheduleNext(success, reason) {
    const nextRun = success !== null
        ? await computeNextRunTime(success)
        : await computeFirstRunTime();

    const nextRunISO = nextRun.toISOString();

    // Create the new alarm (automatically replaces any existing alarm with the same name)
    chrome.alarms.create('dom-capture-scheduled', {
        when: nextRun.getTime()
    });

    const state = {
        status: success === null ? 'scheduled' : (success ? 'success' : 'retrying'),
        nextRun: nextRunISO,
        lastResult: reason || null,
        lastRunTime: success !== null ? new Date().toISOString() : null
    };

    await saveScheduleState(state);

    const diffSecs = Math.round((nextRun.getTime() - Date.now()) / 1000);
    console.log(`[DOMCapture] ${success === null ? 'First run' : (success ? 'Next run' : 'Retry')} scheduled in ${diffSecs}s → ${nextRunISO}`);
    return state;
}

// ─── Find or open the target tab ───
async function getTargetTab(targetUrl, serverUrl) {
    if (!targetUrl) return null;

    // 1. Try to sync with Server's target window
    if (serverUrl) {
        try {
            console.log('[DOMCapture] Requesting server to focus its target window...');
            const config = await getConfig();
            const headers = { 'Content-Type': 'application/json' };
            if (config.apiToken) headers['X-API-Token'] = config.apiToken;
            const res = await fetch(`${serverUrl}/api/focus`, {
                method: 'POST',
                headers,
                body: '{}',
                signal: AbortSignal.timeout(5000)
            });
            if (res.ok) {
                const data = await res.json();
                if (data.success) {
                    console.log('[DOMCapture] Server focused target window successfully. Wait 500ms...');
                    await new Promise(r => setTimeout(r, 500));
                    
                    // Get all Chrome windows with their tabs
                    const windows = await chrome.windows.getAll({ populate: true });
                    
                    // Phase A: Match by OS Focus (Find currently focused window)
                    // Since the server JUST focused this window, this is 100% precise and works even if other windows have identical titles.
                    for (const win of windows) {
                        if (win.focused) {
                            for (const tab of win.tabs) {
                                if (tab.url && tab.url.startsWith(targetUrl)) {
                                    console.log('[DOMCapture] Found target tab in the focused window via OS Focus Match!');
                                    return tab;
                                }
                            }
                        }
                    }
                    // Phase B: If the server-focused window has targetUrl but active tab changed
                    const focusedTabs = await chrome.tabs.query({ lastFocusedWindow: true });
                    for (const tab of focusedTabs) {
                        if (tab.url && tab.url.startsWith(targetUrl)) {
                            console.log('[DOMCapture] Found target tab in lastFocusedWindow.');
                            return tab;
                        }
                    }

                    console.log('[DOMCapture] Target URL not found in server-focused window via direct matching. Falling back.');
                } else {
                    console.warn('[DOMCapture] Server failed to focus target window:', data.error);
                }
            }
        } catch (err) {
            console.warn('[DOMCapture] Could not call /api/focus on server. Falling back to default search.', err.message);
        }
    }

    // 2. Fallback: Search all windows
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
        if (tab.url && tab.url.startsWith(targetUrl)) {
            return tab;
        }
    }

    // Open a new tab if not found
    const newTab = await chrome.tabs.create({ url: targetUrl, active: false });
    try {
        await waitForTabComplete(newTab.id, 30000);
    } catch (e) {
        console.warn('[DOMCapture] New tab load timeout:', e.message);
        throw new Error('Page load timeout');
    }

    return newTab;
}

// ─── Inject content script if needed ───
async function ensureContentScript(tabId) {
    try {
        await sendMessageWithTimeout(tabId, { type: 'PING' }, 5000);
    } catch {
        await chrome.scripting.executeScript({
            target: { tabId },
            files: ['content.js']
        });
        await chrome.scripting.insertCSS({
            target: { tabId },
            files: ['content.css']
        });
        // Wait a moment for script to initialize
        await new Promise(r => setTimeout(r, 500));
    }
}

// ─── Validate that all required selectors are configured ───
function getEmptyRequiredSelectors(selectors) {
    return REQUIRED_FIELDS.filter(f => !selectors[f] || !selectors[f].trim());
}

// ─── Preflight server readiness (before focus/freeze/screenshot side effects) ───
async function checkServerReady(serverUrl, opts = {}) {
    try {
        const config = await getConfig();
        const headers = {};
        if (config.apiToken) headers['X-API-Token'] = config.apiToken;
        const res = await fetch(`${serverUrl}/api/status`, {
            method: 'GET',
            headers,
            signal: AbortSignal.timeout(5000)
        });
        if (!res.ok) {
            return { ok: false, errorKind: 'network', error: `Status HTTP ${res.status}` };
        }
        const data = await res.json();
        if (data.token_valid === false) {
            return {
                ok: false,
                errorKind: 'config',
                error: 'API token sai hoặc chưa cấu hình (Settings → API Token)',
                status: data
            };
        }
        let intervalHours = parseInt(config.intervalHours, 10);
        if (isNaN(intervalHours)) intervalHours = 1;
        const needTestRecipient = opts.requireTestRecipient === true || intervalHours === 0;
        // Test-only runs (manual Test / interval=0) need test_phone; live schedule needs phone_number
        if (!needTestRecipient && !data.recipient_configured) {
            return {
                ok: false,
                errorKind: 'config',
                error: 'phone_number chưa cấu hình/không hợp lệ trên server',
                status: data
            };
        }
        if (needTestRecipient && data.test_recipient_configured === false) {
            return {
                ok: false,
                errorKind: 'config',
                error: 'test_phone_number chưa cấu hình/không hợp lệ (cần cho chế độ test)',
                status: data
            };
        }
        if (!data.target_window_selected) {
            return {
                ok: false,
                errorKind: 'config',
                error: 'Chưa chọn cửa sổ Chrome mục tiêu trên server (tray → Select Target)',
                status: data
            };
        }
        if (data.whatsapp_send_busy) {
            return {
                ok: false,
                errorKind: 'busy',
                error: 'WhatsApp send đang bận (tin trước chưa xong)',
                status: data
            };
        }
        if (!data.whatsapp_connected) {
            return {
                ok: false,
                errorKind: 'wa_disconnected',
                error: 'WhatsApp chưa kết nối (cần quét QR / restart server)',
                status: data
            };
        }
        return { ok: true, status: data };
    } catch (err) {
        return {
            ok: false,
            errorKind: 'network',
            error: `Cannot connect to server: ${err.message}`
        };
    }
}

// ─── Capture data from target tab ───
async function captureData(force22h = false, isTest = false, opts = {}) {
    if (captureInProgress) {
        return { success: false, error: 'Capture already in progress', errorKind: 'busy' };
    }

    let jobToken = null;
    try {
    captureInProgress = true;
    // Automatic recovery runs may reuse the inflight capture_id so the server can dedupe
    // a job whose previous POST already reached the server (e.g. SW killed after WA was
    // sent but before the 200 came back). Manual runs always mint a fresh id (EXT-001).
    jobToken = await beginJobToken({ reuse: opts.reuse === true });

    const config = await getConfig();

    if (!config.targetUrl) {
        return { success: false, error: 'No target URL configured', errorKind: 'config' };
    }

    if (!config.selectors || Object.keys(config.selectors).length === 0) {
        return { success: false, error: 'No selectors configured', errorKind: 'config' };
    }

    // Validate that all required selectors are configured (non-empty)
    const emptyRequired = getEmptyRequiredSelectors(config.selectors);
    if (emptyRequired.length > 0) {
        const errMsg = `Thiếu CSS selector cho các trường bắt buộc: ${emptyRequired.join(', ')}`;
        return { success: false, error: errMsg, missingSelectors: emptyRequired, errorKind: 'config' };
    }

    // Repair a page left frozen by a killed SW BEFORE readiness gate (so recover even if WA is down)
    try {
        const storage = await new Promise(r => chrome.storage.local.get('pendingReload', r));
        if (storage.pendingReload && config.targetUrl) {
            console.log('[DOMCapture] Pending reload before readiness check...');
            const tabs = await chrome.tabs.query({});
            const target = tabs.find(t => t.url && t.url.startsWith(config.targetUrl));
            if (target) {
                try {
                    await chrome.tabs.reload(target.id);
                    await waitForTabComplete(target.id, 30000);
                    await chrome.storage.local.remove('pendingReload');
                    await new Promise(r => setTimeout(r, 1500));
                } catch (e) {
                    console.warn('[DOMCapture] Early pendingReload failed:', e.message);
                    return { success: false, error: e.message || 'Page load timeout', errorKind: 'transient' };
                }
            } else {
                await chrome.storage.local.remove('pendingReload');
            }
        }
    } catch (_) { /* ignore */ }

    // Abort BEFORE focus/maximize/freeze if server cannot deliver
    const ready = await checkServerReady(config.serverUrl, { requireTestRecipient: isTest });
    if (!ready.ok) {
        return {
            success: false,
            error: ready.error,
            errorKind: ready.errorKind,
            serverResponse: ready.status
        };
    }

    try {
        const tab = await getTargetTab(config.targetUrl, config.serverUrl);
        if (!tab) {
            return { success: false, error: 'Could not open target tab' };
        }

        await ensureContentScript(tab.id);
        await injectRealtimeTracker(tab.id);

        // Check if we need to reload the page from a previous failed attempt
        const storage = await new Promise(r => chrome.storage.local.get('pendingReload', r));
        if (storage.pendingReload) {
            console.log('[DOMCapture] Pending reload detected. Reloading tab before extraction...');
            await chrome.storage.local.remove('pendingReload');

            await chrome.tabs.reload(tab.id);
            try {
                await waitForTabComplete(tab.id, 30000);
            } catch (e) {
                return { success: false, error: e.message || 'Page load timeout', errorKind: 'transient' };
            }
            console.log('[DOMCapture] Tab reloaded. Waiting for DOM to settle.');
            await new Promise(r => setTimeout(r, 2000));

            // Re-inject content script + WS tracker after reload
            await ensureContentScript(tab.id);
            await injectRealtimeTracker(tab.id);
        }

        // 1. Focus the window and maximize it
        await chrome.windows.update(tab.windowId, {
            focused: true,
            state: 'maximized'
        });

        // 2. Make the target tab active
        await chrome.tabs.update(tab.id, { active: true });

        // 3. Wait 1 second for the OS/Window to settle and come to front
        await new Promise(r => setTimeout(r, 1000));

        // Keep SW alive during long freeze→extract→POST (MV3 idle kill)
        const keepAliveId = setInterval(() => {
            chrome.runtime.getPlatformInfo().catch(() => { });
        }, 20000);

        let pageFrozen = false;
        try {
            // Persist recovery marker BEFORE freeze — if SW dies mid-job, next capture reloads first
            await chrome.storage.local.set({ pendingReload: true });

            // 4. Freeze page — inject into MAIN world to silently intercept XHR/fetch/WS
            const freezeOk = await injectFreeze(tab.id);
            pageFrozen = true; // always reload in finally once we attempted freeze
            if (!freezeOk) {
                console.error('[DOMCapture] Freeze injection failed — aborting capture');
                return { success: false, error: 'Failed to freeze page', errorKind: 'extract' };
            }

            // 5. Wait 1 second for freeze to take effect and last XHR responses to finish
            await new Promise(r => setTimeout(r, 1000));

            // Extract data from selectors (with 30s timeout to prevent hanging)
            const response = await sendMessageWithTimeout(tab.id, {
                type: 'EXTRACT_DATA',
                selectors: config.selectors
            }, 30000);

            if (!response || !response.ok) {
                console.log('[DOMCapture] Extraction failed:', tab.id);
                return { success: false, error: 'Failed to extract data from page', errorKind: 'extract' };
            }

            // DEG may be empty on hourly runs; required only for force22h
            let isDataValid = true;
            const emptyRequired = [];
            for (const key of REQUIRED_FIELDS) {
                if (key === 'DEG') continue; // optional at extract; force_22h handled in payload
                const info = response.data[key];
                if (!info || info.value === undefined || info.value === '' || info.value === null) {
                    isDataValid = false;
                    emptyRequired.push(key);
                }
            }

            if (!isDataValid) {
                console.log('[DOMCapture] Extracted data is missing or empty:', emptyRequired);
                return {
                    success: false,
                    error: `Missing or empty data extracted: ${emptyRequired.join(', ')}`,
                    errorKind: 'extract'
                };
            }

            // Normalize only required numeric fields (keep Unicode minus via helper)
            const normalizedData = {};
            for (const [key, info] of Object.entries(response.data)) {
                if (info && typeof info === 'object' && REQUIRED_FIELDS.includes(key)) {
                    normalizedData[key] = {
                        ...info,
                        value: normalizeScrapedNumber(info.value, key),
                        raw: info.value
                    };
                } else {
                    normalizedData[key] = info;
                }
            }

            let force22hEffective = force22h;
            if (force22h) {
                const degNorm = normalizedData.DEG?.value ?? '';
                if (!degNorm) {
                    force22hEffective = false;
                    console.warn('[DOMCapture] force22h requested but DEG empty — sending hourly report without DEG');
                    await addCaptureLog('warning', 'DEG trống trong khung 22h → gửi báo cáo giờ (không ép sản lượng đầu cực)');
                }
            }

            const payload = {
                timestamp: new Date().toISOString(),
                url: config.targetUrl,
                data: normalizedData,
                force_22h: force22hEffective,
                is_test: isTest,
                capture_id: jobToken,
                manual_intervention: buildManualInterventionPayload(config)
            };

            const sendResult = await sendToServer(config.serverUrl, payload);
            // Mark job done BEFORE tab reload so a killed SW cannot trigger duplicate via watchdog
            await finishJobToken(jobToken, sendResult.success ? 'done' : 'failed');
            if (sendResult.success) {
                await clearInflightCaptureId();
            } else if (sendResult.errorKind === 'timeout') {
                // Keep id only for ~12 min so watchdog can reuse; hourly run must mint fresh
                await chrome.storage.local.set({ inflightCaptureExpires: Date.now() + 12 * 60000 });
            } else if (
                sendResult.errorKind === 'config' ||
                sendResult.errorKind === 'extract' ||
                sendResult.errorKind === 'transient' ||
                sendResult.errorKind === 'wa_disconnected' ||
                sendResult.errorKind === 'busy' ||
                sendResult.errorKind === 'http' ||
                sendResult.errorKind === 'network'
            ) {
                await clearInflightCaptureId();
            }
            return sendResult;
        } catch (err) {
            console.error('[DOMCapture] Capture error after freeze:', err);
            return { success: false, error: err.message, errorKind: 'extract' };
        } finally {
            clearInterval(keepAliveId);
            // Always unfreeze by reloading — leave dashboard usable even on failure
            if (pageFrozen) {
                console.log('[DOMCapture] Reloading tab to clear freeze:', tab.id);
                try {
                    await chrome.tabs.reload(tab.id);
                    await chrome.storage.local.remove('pendingReload');
                } catch (reloadErr) {
                    console.log('[DOMCapture] Could not reload tab:', reloadErr.message);
                    await chrome.storage.local.set({ pendingReload: true });
                }
            }
        }

    } catch (err) {
        console.error('[DOMCapture] Capture error:', err);
        return { success: false, error: err.message };
    }

    } finally {
        // Only finish if still marked running (success path may have finished early)
        if (jobToken) {
            const meta = await chrome.storage.local.get(['activeJobToken', 'activeJobStatus']);
            if (meta.activeJobToken === jobToken && meta.activeJobStatus === 'running') {
                await finishJobToken(jobToken, 'done');
            }
        }
        captureInProgress = false;
    }
}

// ─── Normalize scraped numbers (strip units; keep separators for server parser) ───
function normalizeScrapedNumber(value, fieldName) {
    if (value === undefined || value === null) return '';
    let s = String(value).trim()
        .replace(/\u2212/g, '-')  // Unicode minus
        .replace(/\u2013/g, '-')  // en-dash
        .replace(/\u2014/g, '-'); // em-dash
    // TBS* = turbine status strings — keep text; collapse internal whitespace (incl. NBSP)
    if (fieldName && /^TBS\d+$/.test(fieldName)) {
        s = s.replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
        return s;
    }
    // Dashboard placeholders → missing (validation fails) instead of inventing values
    if (/^(—|–|-|−|n\/?a|null|none|\.|…)$/i.test(s)) {
        return '';
    }
    // Keep digits, separators, minus; drop unit letters/spaces like "MW", "m/s"
    const cleaned = s.replace(/[^\d,.\-]/g, '');
    return cleaned || s;
}

// ─── Send data to local Python server ───
async function sendToServer(serverUrl, payload) {
    try {
        const config = await getConfig();
        const headers = { 'Content-Type': 'application/json' };
        if (config.apiToken) headers['X-API-Token'] = config.apiToken;

        // Client timeout > server WHATSAPP_SEND_TIMEOUT (150s) + screenshot margin
        const res = await fetch(`${serverUrl}/api/capture`, {
            method: 'POST',
            headers,
            body: JSON.stringify(payload),
            signal: AbortSignal.timeout(200000)
        });

        if (res.ok) {
            const data = await res.json();
            console.log('[DOMCapture] Server response:', data);
            if (data.success === false) {
                return {
                    success: false,
                    error: data.error || 'Server reported failure',
                    errorKind: data.error_code === 'SEND_TIMEOUT' ? 'timeout' : 'http',
                    errorCode: data.error_code || null,
                    serverResponse: data
                };
            }
            return { success: true, serverResponse: data };
        } else {
            let parsed = null;
            let text = '';
            try {
                text = await res.text();
                parsed = JSON.parse(text);
            } catch (_) { /* keep raw text */ }

            const errorCode = parsed && parsed.error_code ? parsed.error_code : null;
            const errMsg = (parsed && parsed.error) ? parsed.error : text;

            if (res.status === 401) {
                return {
                    success: false,
                    error: errMsg || 'Unauthorized: invalid API token',
                    errorKind: 'config',
                    errorCode: 'UNAUTHORIZED',
                    status: res.status,
                    serverResponse: parsed
                };
            }
            if (
                errorCode === 'MISSING_FIELDS' ||
                errorCode === 'INVALID_FIELD' ||
                errorCode === 'INVALID_RECIPIENT' ||
                errorCode === 'INCONSISTENT_COUNTS' ||
                (res.status === 400 && errorCode)
            ) {
                return {
                    success: false,
                    error: errMsg || `Bad request (${errorCode || res.status})`,
                    errorKind: 'config',
                    errorCode: errorCode || 'BAD_REQUEST',
                    status: res.status,
                    serverResponse: parsed
                };
            }
            if (res.status === 504 || errorCode === 'SEND_TIMEOUT') {
                // Server timed out waiting for the send — ask it what actually happened
                // (BND-001) before the caller decides whether to retry.
                const outcome = await getLastSendOutcome(payload && payload.capture_id);
                return {
                    success: false,
                    error: errMsg || 'WhatsApp send timed out',
                    errorKind: 'timeout',
                    errorCode: 'SEND_TIMEOUT',
                    status: res.status,
                    serverResponse: parsed,
                    lastSendOutcome: outcome
                };
            }
            if (res.status === 409 || errorCode === 'SEND_BUSY') {
                return {
                    success: false,
                    error: errMsg || 'Send busy',
                    errorKind: 'busy',
                    errorCode: errorCode || 'SEND_BUSY',
                    status: res.status,
                    serverResponse: parsed
                };
            }
            if (errorCode === 'SCREENSHOT_FAILED' || errorCode === 'SEND_FAILED') {
                return {
                    success: false,
                    error: errMsg || errorCode,
                    errorKind: 'transient',
                    errorCode: errorCode,
                    status: res.status,
                    serverResponse: parsed
                };
            }
            if (res.status === 503 || errorCode === 'WA_DISCONNECTED') {
                return {
                    success: false,
                    error: errMsg || 'WhatsApp disconnected',
                    errorKind: 'wa_disconnected',
                    errorCode: 'WA_DISCONNECTED',
                    status: res.status,
                    serverResponse: parsed
                };
            }
            if (errorCode === 'NO_TARGET_WINDOW' || (res.status === 400 && /cửa sổ|window|chưa chọn/i.test(errMsg || ''))) {
                return {
                    success: false,
                    error: errMsg || 'No target window',
                    errorKind: 'config',
                    errorCode: 'NO_TARGET_WINDOW',
                    status: res.status,
                    serverResponse: parsed
                };
            }
            return {
                success: false,
                error: `Server error ${res.status}: ${errMsg || text}`,
                errorKind: 'http',
                errorCode,
                status: res.status
            };
        }
    } catch (err) {
        const name = err && err.name;
        const msg = err && err.message ? err.message : String(err);
        // AbortSignal.timeout → TimeoutError/AbortError — server may still complete send
        if (name === 'TimeoutError' || name === 'AbortError' || /aborted|timeout/i.test(msg)) {
            // Ask the server what actually happened to this capture before deciding
            // whether to retry (BND-001): confirmed "failed" → safe to retry;
            // confirmed "success" → do NOT retry (avoids duplicate); unresolved → unknown.
            const outcome = await getLastSendOutcome(payload && payload.capture_id);
            return {
                success: false,
                error: `Request timed out: ${msg}`,
                errorKind: 'timeout',
                lastSendOutcome: outcome
            };
        }
        // Genuine unreachable (server down, connection refused)
        return {
            success: false,
            error: `Cannot connect to server: ${msg}`,
            errorKind: 'network'
        };
    }
}

// ─── Query the server for the outcome of a (possibly timed-out) WhatsApp send ───
// Returns the last_send_outcome object only when it belongs to captureId (so a stale
// outcome from an earlier job can never be mistaken for this one). When the server is
// still resolving the send (whatsapp_send_busy), polls briefly before giving up.
async function getLastSendOutcome(captureId, attempts = 3, intervalMs = 5000) {
    for (let i = 0; i < attempts; i++) {
        try {
            const config = await getConfig();
            const headers = {};
            if (config.apiToken) headers['X-API-Token'] = config.apiToken;
            const res = await fetch(`${config.serverUrl}/api/status`, {
                method: 'GET',
                headers,
                signal: AbortSignal.timeout(5000)
            });
            if (!res.ok) return null;
            const data = await res.json();
            if (!data || data.last_send_outcome == null) return null;
            if (data.whatsapp_send_busy) {
                // Send still resolving — outcome currently reported belongs to an older
                // job. Wait a moment and retry before concluding "unknown".
            } else {
                const outcome = data.last_send_outcome;
                if (!captureId || outcome.capture_id === captureId) return outcome;
                // Latest resolved outcome is from a different job — cannot use it.
                return null;
            }
        } catch (_) {
            return null;
        }
        if (i < attempts - 1) await new Promise(r => setTimeout(r, intervalMs));
    }
    return null;
}

// ─── Add log entry to storage ───
async function addCaptureLog(type, msg) {
    return new Promise((resolve) => {
        chrome.storage.local.get('captureLogs', (data) => {
            const logs = data.captureLogs || [];
            logs.unshift({ type, msg, time: new Date().toISOString() });
            if (logs.length > 50) logs.length = 50;
            chrome.storage.local.set({ captureLogs: logs }, resolve);
        });
    });
}

// ─── Run the scheduled job ───
async function runScheduledJob() {
    console.log('[DOMCapture] ════════════════════════════════');
    console.log('[DOMCapture] Running scheduled capture job...');

    const config = await getConfig();

    if (!config.autoCapture) {
        console.log('[DOMCapture] Auto-capture is disabled. Skipping.');
        return;
    }

    // ★ OPTIMISTIC SCHEDULING: Schedule the next alarm BEFORE running the job.
    // Chrome MV3 kills the service worker during long-running capture jobs,
    // preventing scheduleNext() from ever being called. By pre-scheduling,
    // the next alarm exists even if the SW dies mid-job.
    const preScheduledState = await scheduleNext(true, 'Pre-scheduled (job running...)');
    console.log('[DOMCapture] ✅ Next run pre-scheduled at', preScheduledState.nextRun);

    // Determine if this run should include DEG report (sản lượng đầu cực)
    const currentHour = new Date().getHours();
    let force22h = false;

    if (currentHour === 22 || currentHour === 23) {
        const alreadySent = await isDegReportSentToday();
        if (!alreadySent) {
            force22h = true;
            console.log(`[DOMCapture] Current hour is ${currentHour} → including DEG report`);
        } else {
            console.log(`[DOMCapture] Current hour is ${currentHour} but DEG report already sent today`);
        }
    }

    let intervalHours = parseInt(config.intervalHours, 10);
    if (isNaN(intervalHours)) intervalHours = 1;
    const isTestMode = (intervalHours === 0);

    const result = await captureData(force22h, isTestMode, { reuse: true });
    console.log('[DOMCapture] captureData returned:', JSON.stringify({ success: result.success, error: result.error }));

    // Store last capture result
    chrome.storage.local.set({
        lastCapture: {
            time: new Date().toISOString(),
            result
        }
    });

    if (result.success) {
        await chrome.storage.local.set({ networkFailCount: 0, waFailCount: 0, extractFailCount: 0, transientFailCount: 0 });
        // Only mark DEG for live (non-debug) successful sends
        if (force22h && !isTestMode) {
            await markDegReportSent();
            console.log('[DOMCapture] ✅ DEG report marked as sent for today');
            await chrome.alarms.clear('dom-capture-deg-fallback');
        }

        console.log('[DOMCapture] ✅ Job succeeded! Next run already pre-scheduled.');
        const dup = result.serverResponse?.duplicate;
        const verified = result.serverResponse?.wa_verified;
        const extra = dup
            ? ' (trùng capture_id — không gửi lại)'
            : (verified === false ? ' (đã queue, ack chưa xác nhận)' : '');
        await addCaptureLog('success', `Capture thành công.${extra} ${result.serverResponse?.caption || ''}`.trim());
        await saveScheduleState({
            ...preScheduledState,
            status: 'success',
            lastResult: 'Success',
            lastRunTime: new Date().toISOString()
        });
    } else {
        // If selectors are missing, disable auto-capture (retry won't help)
        if (result.missingSelectors && result.missingSelectors.length > 0) {
            console.log(`[DOMCapture] ❌ Missing required selectors: ${result.missingSelectors.join(', ')}. Disabling auto-capture.`);
            const tbsHint = result.missingSelectors.some(f => /^TBS\d+$/.test(f))
                ? ' (sau cập nhật cần map thêm TBS1–TBS12 — trạng thái tua bin)'
                : '';
            await addCaptureLog('error', `Thiếu CSS selector cho: ${result.missingSelectors.join(', ')}. Đã TẮT chế độ tự động.${tbsHint}`);
            await stopScheduler();
            // Also persist autoCapture = false
            const currentConfig = await getConfig();
            currentConfig.autoCapture = false;
            await saveConfig(currentConfig);
        } else if (result.errorKind === 'network' || (result.error && result.error.startsWith('Cannot connect to server:'))) {
            // Require several consecutive network failures before disabling auto-capture
            const failCount = ((await chrome.storage.local.get('networkFailCount')).networkFailCount || 0) + 1;
            await chrome.storage.local.set({ networkFailCount: failCount });
            if (failCount >= 3) {
                console.log(`[DOMCapture] ❌ Server unreachable ${failCount}x: ${result.error}. Disabling auto-capture.`);
                await addCaptureLog('error', `Không thể kết nối server (${failCount} lần). Đã TẮT chế độ tự động.`);
                await stopScheduler();
                const currentConfig = await getConfig();
                currentConfig.autoCapture = false;
                await saveConfig(currentConfig);
                await chrome.storage.local.set({ networkFailCount: 0 });
            } else {
                console.log(`[DOMCapture] ⚠️ Network fail ${failCount}/3 — retry, keep auto-capture ON`);
                await addCaptureLog('error', `Không kết nối được server (${failCount}/3). Retry trong 5 phút.`);
                await scheduleNext(false, result.error);
            }
        } else if (result.errorKind === 'timeout') {
            const outcome = result.lastSendOutcome;
            if (outcome && outcome.status === 'failed') {
                // Server confirmed the send FAILED — report did NOT go out → safe to retry.
                console.log(`[DOMCapture] ⚠️ Timeout but server confirmed send FAILED (${outcome.detail}). Retrying.`);
                await addCaptureLog('error', `Timeout nhưng server xác nhận gửi THẤT BẠI. Retry trong 5 phút.`);
                await scheduleNext(false, 'Send failed after timeout (server-confirmed)');
                // Skip scheduleDegFallbackIfNeeded below: the 5-min retry re-attempts with
                // the correct force_22h flag (hour is still 22/23), so a separate 23h
                // fallback alarm would only double-schedule the same report.
                return;
            } else if (outcome && outcome.status === 'success') {
                // Server confirmed the send SUCCEEDED — do NOT retry (avoids duplicates).
                console.log('[DOMCapture] Timeout but server confirmed send SUCCEEDED. No retry.');
                await addCaptureLog('error', 'Timeout nhưng server xác nhận đã gửi thành công. Không retry để tránh trùng.');
                if (force22h && !isTestMode) {
                    await markDegReportSent();
                    await chrome.alarms.clear('dom-capture-deg-fallback');
                }
                await saveScheduleState({
                    ...preScheduledState,
                    status: 'success',
                    lastResult: 'Timeout (sent, server-confirmed)',
                    lastRunTime: new Date().toISOString()
                });
            } else {
                // Unresolved — WA may already be sending. Do NOT 5-min retry (avoids duplicates).
                console.log(`[DOMCapture] ⚠️ Timeout / SEND_TIMEOUT: ${result.error}. Keeping pre-scheduled next run.`);
                await addCaptureLog('error', `Timeout gửi WA (có thể đã/đang gửi). Không retry 5 phút để tránh trùng.`);
                if (force22h && !isTestMode) {
                    await markDegReportSent();
                    await chrome.alarms.clear('dom-capture-deg-fallback');
                }
                await saveScheduleState({
                    ...preScheduledState,
                    status: 'success',
                    lastResult: 'Timeout (may have sent)',
                    lastRunTime: new Date().toISOString()
                });
            }
        } else if (result.errorKind === 'busy') {
            // Prior send still running — back off without hijacking; keep auto-capture
            console.log(`[DOMCapture] ⚠️ SEND_BUSY: ${result.error}. Retry in 15 min.`);
            await addCaptureLog('error', `WhatsApp đang bận gửi tin trước. Retry sau 15 phút.`);
            const retryAt = new Date(Date.now() + 15 * 60000);
            await chrome.alarms.create('dom-capture-scheduled', { when: retryAt.getTime() });
            await saveScheduleState({
                status: 'retrying',
                nextRun: retryAt.toISOString(),
                lastResult: 'SEND_BUSY',
                lastRunTime: new Date().toISOString()
            });
        } else if (result.errorKind === 'extract' || result.errorKind === 'transient' || result.errorKind === 'http') {
            // Selector / screenshot / other HTTP — progressive backoff, do not yank every 5 min forever
            const key = result.errorKind === 'extract' ? 'extractFailCount' : 'transientFailCount';
            const raw = (await chrome.storage.local.get(key))[key] || 0;
            const failCount = raw + 1;
            await chrome.storage.local.set({ [key]: failCount });
            const delayMin = Math.min(60, 5 * Math.pow(2, Math.min(failCount - 1, 4)));
            console.log(`[DOMCapture] ⚠️ ${result.errorKind} fail (${failCount}x): ${result.error}. Backoff ${delayMin} min.`);
            await addCaptureLog('error', `${result.error}. Thử lại sau ${delayMin} phút.`);
            const retryAt = new Date(Date.now() + delayMin * 60000);
            await chrome.alarms.create('dom-capture-scheduled', { when: retryAt.getTime() });
            await saveScheduleState({
                status: 'retrying',
                nextRun: retryAt.toISOString(),
                lastResult: result.errorKind,
                lastRunTime: new Date().toISOString()
            });
        } else if (result.errorKind === 'wa_disconnected' || result.errorKind === 'config') {
            // Progressive backoff — do not yank desktop again every 5 minutes
            const raw = (await chrome.storage.local.get('waFailCount')).waFailCount || 0;
            const failCount = raw + 1;
            await chrome.storage.local.set({ waFailCount: failCount });
            const delayMin = Math.min(60, 5 * Math.pow(2, Math.min(failCount - 1, 4))); // 5,10,20,40,60
            console.log(`[DOMCapture] ⚠️ WA/config not ready (${failCount}x): ${result.error}. Backoff ${delayMin} min.`);
            await addCaptureLog('error', `${result.error}. Thử lại sau ${delayMin} phút (không focus/freeze).`);
            const retryAt = new Date(Date.now() + delayMin * 60000);
            await chrome.alarms.create('dom-capture-scheduled', { when: retryAt.getTime() });
            await saveScheduleState({
                status: 'retrying',
                nextRun: retryAt.toISOString(),
                lastResult: result.errorKind,
                lastRunTime: new Date().toISOString()
            });
        } else {
            await chrome.storage.local.set({ networkFailCount: 0 });
            // Job failed — override the pre-scheduled alarm with a 5-minute retry
            console.log(`[DOMCapture] ❌ Job failed: ${result.error}. Overriding schedule with 5-min retry.`);
            await addCaptureLog('error', `Capture thất bại: ${result.error}. Retry trong 5 phút.`);
            await scheduleNext(false, result.error);
        }
    }

    // After scheduling next run, check if we need a 23h DEG fallback
    await scheduleDegFallbackIfNeeded();
}

// ─── Run the DEG fallback job (23h) ───
async function runDegFallbackJob() {
    console.log('[DOMCapture] ════════════════════════════════');
    console.log('[DOMCapture] Running 23h DEG fallback job...');

    const config = await getConfig();
    if (!config.autoCapture) {
        console.log('[DOMCapture] Auto-capture is disabled. Skipping DEG fallback.');
        return;
    }

    // Check if DEG report was already sent today (by regular 22h run or manual)
    const alreadySent = await isDegReportSentToday();
    if (alreadySent) {
        console.log('[DOMCapture] DEG report already sent today. Skipping 23h fallback.');
        await addCaptureLog('info', 'Báo cáo sản lượng đầu cực đã gửi hôm nay → bỏ qua 23h fallback');
        return;
    }

    let intervalHours = parseInt(config.intervalHours, 10);
    if (isNaN(intervalHours)) intervalHours = 1;
    const isTestMode = (intervalHours === 0);

    // Run capture with force22h = true (test mode when intervalHours === 0)
    const result = await captureData(true, isTestMode, { reuse: true });

    chrome.storage.local.set({
        lastCapture: {
            time: new Date().toISOString(),
            result
        }
    });

    if (result.success) {
        if (!isTestMode) {
            await markDegReportSent();
        }
        console.log('[DOMCapture] ✅ DEG fallback at 23h succeeded!');
        await addCaptureLog('success', `Báo cáo sản lượng đầu cực 23h thành công. ${result.serverResponse?.caption || ''}`);
    } else if (result.errorKind === 'timeout') {
        const outcome = result.lastSendOutcome;
        if (outcome && outcome.status === 'failed') {
            // Server confirmed the DEG send FAILED → retry within the 23h window.
            const now = new Date();
            if (now.getHours() === 23 && now.getMinutes() < 50) {
                chrome.alarms.create('dom-capture-deg-fallback', { when: Date.now() + 10 * 60000 });
                await addCaptureLog('error', `DEG 23h: server xác nhận gửi THẤT BẠI. Retry sau 10 phút.`);
            } else {
                await addCaptureLog('error', `DEG 23h: server xác nhận gửi THẤT BẠI. Hết cửa sổ retry trong ngày.`);
            }
        } else {
            // Unresolved / may have sent — mark DEG to avoid a second report (live only)
            if (!isTestMode) {
                await markDegReportSent();
            }
            await addCaptureLog('error', `DEG 23h timeout (có thể đã gửi). Đánh dấu đã gửi để tránh trùng.`);
        }
    } else if (result.errorKind === 'busy') {
        const now = new Date();
        if (now.getHours() === 23 && now.getMinutes() < 50) {
            chrome.alarms.create('dom-capture-deg-fallback', { when: Date.now() + 10 * 60000 });
            await addCaptureLog('error', `DEG 23h: WhatsApp đang bận. Retry sau 10 phút.`);
        } else {
            await addCaptureLog('error', `DEG 23h: WhatsApp đang bận. Hết cửa sổ retry trong ngày.`);
        }
    } else if (result.errorKind === 'wa_disconnected' || result.errorKind === 'config') {
        const now = new Date();
        if (now.getHours() === 23 && now.getMinutes() < 50) {
            chrome.alarms.create('dom-capture-deg-fallback', { when: Date.now() + 10 * 60000 });
            await addCaptureLog('error', `DEG 23h: ${result.error}. Retry sau 10 phút.`);
        } else {
            await addCaptureLog('error', `DEG 23h: ${result.error}. Cần kết nối WA / cấu hình rồi chạy lại thủ công.`);
        }
    } else {
        console.log(`[DOMCapture] ❌ DEG fallback at 23h failed: ${result.error}`);
        await addCaptureLog('error', `Báo cáo sản lượng đầu cực 23h thất bại: ${result.error}`);
        if (result.missingSelectors && result.missingSelectors.length > 0) {
            await addCaptureLog('error', `Thiếu CSS selector cho: ${result.missingSelectors.join(', ')}. Đã TẮT chế độ tự động.`);
            await stopScheduler();
            const currentConfig = await getConfig();
            currentConfig.autoCapture = false;
            await saveConfig(currentConfig);
        } else if (result.errorKind === 'network' || (result.error && result.error.startsWith('Cannot connect to server:'))) {
            const failCount = ((await chrome.storage.local.get('networkFailCount')).networkFailCount || 0) + 1;
            await chrome.storage.local.set({ networkFailCount: failCount });
            const now = new Date();
            if (failCount >= 3) {
                await addCaptureLog('error', `Không thể kết nối server (${failCount} lần). Đã TẮT chế độ tự động.`);
                await stopScheduler();
                const currentConfig = await getConfig();
                currentConfig.autoCapture = false;
                await saveConfig(currentConfig);
                await chrome.storage.local.set({ networkFailCount: 0 });
            } else if (now.getHours() === 23 && now.getMinutes() < 50) {
                chrome.alarms.create('dom-capture-deg-fallback', { when: Date.now() + 10 * 60000 });
                await addCaptureLog('error', `Không kết nối được server (${failCount}/3). Retry DEG sau 10 phút.`);
            }
        } else {
            const now = new Date();
            if (now.getHours() === 23 && now.getMinutes() < 50) {
                chrome.alarms.create('dom-capture-deg-fallback', { when: Date.now() + 10 * 60000 });
                await addCaptureLog('error', `DEG fail — retry sau 10 phút.`);
            }
        }
    }
}

// ─── Alarm handler ───
chrome.alarms.onAlarm.addListener(async (alarm) => {
    // Safety watchdog: if this fires, the main job hung and SW was killed
    if (alarm.name === 'dom-capture-watchdog') {
        const jobMeta = await chrome.storage.local.get(['activeJobStatus', 'activeJobToken']);
        if (jobMeta.activeJobStatus && jobMeta.activeJobStatus !== 'running') {
            console.log('[DOMCapture] Watchdog ignored — job already finished:', jobMeta.activeJobStatus);
            return;
        }
        console.warn('[DOMCapture] ⚠️ Watchdog fired — previous job likely hung. Rescheduling...');
        await addCaptureLog('error', 'Job trước đó bị treo (watchdog). Đang lên lịch lại...');
        try {
            const config = await getConfig();
            if (config.autoCapture) {
                await scheduleNext(false, 'Watchdog recovery');
            }
        } catch (e) {
            console.error('[DOMCapture] Watchdog recovery failed:', e);
        }
        return;
    }

    if (alarm.name === 'dom-capture-scheduled') {
        // Set a watchdog alarm BEFORE running the job.
        // If the job hangs and the SW dies, this alarm will fire and reschedule.
        chrome.alarms.create('dom-capture-watchdog', { delayInMinutes: 10 });

        try {
            await runScheduledJob();
        } catch (err) {
            console.error('[DOMCapture] ❌ Unhandled error in scheduled job:', err);
            await addCaptureLog('error', `Lỗi không mong muốn: ${err.message}. Retry trong 5 phút.`);
            // Always schedule next run so auto-capture doesn't stop forever
            try {
                await scheduleNext(false, `Unhandled error: ${err.message}`);
            } catch (schedErr) {
                console.error('[DOMCapture] ❌ Failed to schedule retry:', schedErr);
            }
        }

        // Job completed (success or error) — cancel the watchdog
        await chrome.alarms.clear('dom-capture-watchdog');
    }

    if (alarm.name === 'dom-capture-deg-fallback') {
        chrome.alarms.create('dom-capture-watchdog', { delayInMinutes: 10 });
        try {
            await runDegFallbackJob();
        } catch (err) {
            console.error('[DOMCapture] ❌ Unhandled error in DEG fallback job:', err);
            await addCaptureLog('error', `Lỗi báo cáo sản lượng 23h: ${err.message}`);
        } finally {
            await chrome.alarms.clear('dom-capture-watchdog');
        }
    }
});

// ─── Start scheduling (called on install and when auto-capture is toggled on) ───
async function startScheduler() {
    const config = await getConfig();
    if (!config.autoCapture) {
        console.log('[DOMCapture] Auto-capture disabled. Not scheduling.');
        await chrome.alarms.clear('dom-capture-scheduled');
        await chrome.alarms.clear('dom-capture-deg-fallback');
        await saveScheduleState({ status: 'disabled', nextRun: null, lastResult: null });
        return;
    }

    // scheduleNext(null, ...) internally calls computeFirstRunTime() and returns the state
    const state = await scheduleNext(null, 'Scheduler started');
    const actualNextRun = state.nextRun ? new Date(state.nextRun) : null;
    const timeStr = actualNextRun ? actualNextRun.toLocaleTimeString('vi-VN') : '???';
    console.log(`[DOMCapture] Scheduler started. First run at ${state.nextRun}`);
    const modeLabel = config.scheduleMode === '30min' ? '0-30 phút' : '0-15 phút';
    const intervalLabel = (config.intervalHours || 1) === 2 ? 'mỗi 2 giờ' : 'mỗi giờ';
    await addCaptureLog('info', `Scheduler started (${intervalLabel}, ${modeLabel}). First run at ${timeStr}`);

    // Schedule DEG fallback if needed
    await scheduleDegFallbackIfNeeded();
}

// ─── Stop scheduling ───
async function stopScheduler() {
    await chrome.alarms.clear('dom-capture-scheduled');
    await chrome.alarms.clear('dom-capture-deg-fallback');
    await chrome.alarms.clear('dom-capture-watchdog');
    await saveScheduleState({ status: 'disabled', nextRun: null, lastResult: null });
    await addCaptureLog('info', 'Scheduler stopped.');
    console.log('[DOMCapture] Scheduler stopped.');
}

// ─── Message listener from popup ───
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'CAPTURE_NOW') {
        // Run immediately; watchdog covers SW death during long manual jobs
        (async () => {
            if (captureInProgress) {
                sendResponse({ success: false, error: 'Capture already in progress', errorKind: 'busy' });
                return;
            }
            chrome.alarms.create('dom-capture-watchdog', { delayInMinutes: 10 });
            try {
                let force22h = msg.force22h || false;
                const isTest = msg.isTest === true;
                await addCaptureLog('info', isTest
                    ? 'Bắt đầu capture thủ công (TEST → nhóm test)...'
                    : 'Bắt đầu capture thủ công...');

                const currentHour = new Date().getHours();
                if (!isTest && (currentHour === 22 || currentHour === 23)) {
                    const alreadySent = await isDegReportSentToday();
                    if (!alreadySent) {
                        force22h = true;
                    }
                }

                const result = await captureData(force22h, isTest);
                chrome.storage.local.set({
                    lastCapture: { time: new Date().toISOString(), result }
                });
                if (result.success) {
                    if (force22h && !isTest) {
                        await markDegReportSent();
                        await chrome.alarms.clear('dom-capture-deg-fallback');
                    }
                    const dup = result.serverResponse?.duplicate;
                    const ack = result.serverResponse?.wa_ack;
                    const verified = result.serverResponse?.wa_verified;
                    const extra = dup
                        ? ' (trùng capture_id — không gửi lại)'
                        : (verified === false
                            ? ' (đã queue, ack chưa xác nhận)'
                            : (ack != null ? ` (ack=${ack})` : ''));
                    await addCaptureLog('success', `Capture thủ công thành công.${extra}`);
                } else if (result.errorKind === 'timeout') {
                    if (result.lastSendOutcome && result.lastSendOutcome.status === 'failed') {
                        await addCaptureLog('error', `Capture thủ công timeout — server xác nhận gửi THẤT BẠI (${result.lastSendOutcome.detail || 'không rõ'}).`);
                    } else if (force22h && !isTest) {
                        await markDegReportSent();
                        await chrome.alarms.clear('dom-capture-deg-fallback');
                        await addCaptureLog('error', 'Capture thủ công timeout (có thể đã gửi). Đánh dấu DEG để tránh trùng.');
                    } else {
                        await addCaptureLog('error', 'Capture thủ công timeout (có thể đã gửi).');
                    }
                } else {
                    await addCaptureLog('error', `Capture thủ công thất bại: ${result.error}`);
                }
                sendResponse(result);
            } catch (err) {
                await addCaptureLog('error', `Capture thủ công lỗi: ${err.message}`);
                sendResponse({ success: false, error: err.message });
            } finally {
                await chrome.alarms.clear('dom-capture-watchdog');
            }
        })();
        return true;
    }

    if (msg.type === 'TEST_WITH_DATA') {
        const force22h = msg.force22h || false;
        const mockData = msg.mockData || {};
        (async () => {
            if (captureInProgress) {
                sendResponse({ success: false, error: 'Capture already in progress', errorKind: 'busy' });
                return;
            }
            let jobToken = null;
            try {
                captureInProgress = true;
                jobToken = await beginJobToken();
                await addCaptureLog('info', 'Bắt đầu test mock data...');
                const config = await getConfig();
                const ts = new Date().toISOString();

                const ready = await checkServerReady(config.serverUrl, { requireTestRecipient: true });
                if (!ready.ok) {
                    sendResponse({
                        success: false,
                        error: ready.error,
                        errorKind: ready.errorKind,
                        serverResponse: ready.status
                    });
                    return;
                }

                if (config.targetUrl) {
                    const tab = await getTargetTab(config.targetUrl, config.serverUrl);
                    if (tab) {
                        await chrome.windows.update(tab.windowId, { focused: true, state: 'maximized' });
                        await chrome.tabs.update(tab.id, { active: true });
                        await new Promise(r => setTimeout(r, 1000));
                    }
                }

                const payload = {
                    timestamp: ts,
                    url: config.targetUrl || 'test-mode',
                    data: mockData,
                    force_22h: force22h,
                    is_test: true,
                    capture_id: jobToken,
                    manual_intervention: { enabled: false, turbines: {} }
                };

                const result = await sendToServer(config.serverUrl, payload);
                await finishJobToken(jobToken, result.success ? 'done' : 'failed');
                if (result.success) {
                    await clearInflightCaptureId();
                } else if (result.errorKind !== 'timeout') {
                    await clearInflightCaptureId();
                }

                chrome.storage.local.set({
                    lastCapture: { time: new Date().toISOString(), result }
                });

                if (result.success) {
                    // Never mark DEG report sent for mock/test runs
                    await addCaptureLog('success', `Test thành công. Caption: ${result.serverResponse?.caption || ''}`);
                } else {
                    await addCaptureLog('error', `Test thất bại: ${result.error}`);
                }
                sendResponse(result);
            } catch (err) {
                await addCaptureLog('error', `Test lỗi: ${err.message}`);
                sendResponse({ success: false, error: err.message });
            } finally {
                if (jobToken) {
                    const meta = await chrome.storage.local.get(['activeJobToken', 'activeJobStatus']);
                    if (meta.activeJobToken === jobToken && meta.activeJobStatus === 'running') {
                        await finishJobToken(jobToken, 'done');
                    }
                }
                captureInProgress = false;
            }
        })();
        return true;
    }

    if (msg.type === 'GET_CONFIG') {
        getConfig().then(config => sendResponse(config));
        return true;
    }

    if (msg.type === 'SAVE_CONFIG') {
        (async () => {
            const prev = await getConfig();
            await saveConfig(msg.config);
            const scheduleChanged =
                Boolean(prev.autoCapture) !== Boolean(msg.config.autoCapture) ||
                String(prev.intervalHours) !== String(msg.config.intervalHours) ||
                String(prev.scheduleMode) !== String(msg.config.scheduleMode);
            if (scheduleChanged) {
                if (msg.config.autoCapture) {
                    await startScheduler();
                } else {
                    await stopScheduler();
                }
            }
            sendResponse({ ok: true });
        })();
        return true;
    }

    if (msg.type === 'SAVE_MANUAL_INTERVENTION') {
        // Patch-only: never clobber autoCapture / selectors with a stale popup snapshot
        (async () => {
            const cfg = await getConfig();
            const incoming = msg.manualIntervention || {};
            const turbines = {};
            if (incoming.turbines && typeof incoming.turbines === 'object') {
                for (const [k, v] of Object.entries(incoming.turbines)) {
                    turbines[String(k)] = String(v);
                }
            }
            cfg.manualIntervention = {
                enabled: Boolean(incoming.enabled),
                turbines
            };
            await saveConfig(cfg);
            sendResponse({ ok: true });
        })();
        return true;
    }

    if (msg.type === 'GET_SCHEDULE') {
        getScheduleState().then(state => sendResponse(state));
        return true;
    }

    if (msg.type === 'START_SCHEDULER') {
        getConfig().then(async config => {
            config.autoCapture = true;
            await saveConfig(config);
            await startScheduler();
            sendResponse({ ok: true });
        });
        return true;
    }

    if (msg.type === 'STOP_SCHEDULER') {
        getConfig().then(async config => {
            config.autoCapture = false;
            await saveConfig(config);
            await stopScheduler();
            sendResponse({ ok: true });
        });
        return true;
    }

    if (msg.type === 'PICKER_RESULT') {
        // Popup is closed at this point, so save directly to config storage
        getConfig().then(config => {
            config.selectors = config.selectors || {};
            config.selectors[msg.fieldName] = msg.selector;
            saveConfig(config).then(() => {
                console.log(`[DOMCapture] Picker saved: ${msg.fieldName} → ${msg.selector}`);
            });
        });
        sendResponse({ ok: true });
        return true;
    }
});

// ─── On install/update, auto-start scheduler ───
chrome.runtime.onInstalled.addListener(async () => {
    const config = await getConfig();
    // Luôn lưu lại config đã merge để đảm bảo storage có đầy đủ các trường selector bắt buộc
    await saveConfig(config);
    // Auto-start scheduling if autoCapture is enabled
    if (config.autoCapture === true) {
        await startScheduler();
    }
    console.log('[DOMCapture] Extension installed/updated');
});

// ─── On browser startup, resume scheduler ───
chrome.runtime.onStartup.addListener(async () => {
    const config = await getConfig();
    if (config.autoCapture) {
        await startScheduler();
        console.log('[DOMCapture] Scheduler resumed on browser startup');
    }
});
