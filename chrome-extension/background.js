// =============================================================================
// Background Service Worker — DOM Data Capture Extension
// Scheduling logic matching main.py:
//   - Run every hour at random minute within configured window
//   - Retry in 5 minutes on failure
//   - Auto-start on extension load
// =============================================================================

const DEFAULT_CONFIG = {
    serverUrl: 'http://localhost:5001',
    targetUrl: '',
    intervalMinutes: 60,
    selectors: {},
    autoCapture: false,  // Default OFF as requested
    retryMinutes: 5,
    scheduleMode: '15min', // '15min' = 0-15 phút, '30min' = 0-30 phút
    intervalHours: 1       // 1 = mỗi giờ, 2 = mỗi 2 giờ
};
// ─── Freeze page by injecting into MAIN world ───
// This function runs in the page's REAL JavaScript context (not isolated world)
// so it CAN override XMLHttpRequest, fetch, WebSocket etc.
function freezePageInMainWorld() {
    if (window.__domCaptureFrozen) return;
    window.__domCaptureFrozen = true;

    // 1. Override XMLHttpRequest.prototype.send — silently fake successful empty responses
    const originalSend = XMLHttpRequest.prototype.send;
    const originalOpen = XMLHttpRequest.prototype.open;

    XMLHttpRequest.prototype.open = function (method, url, async, user, password) {
        this.__dcMethod = method;
        this.__dcUrl = url;
        // Still call original open so the object is in proper state
        return originalOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function (body) {
        // Fake a successful empty response immediately
        Object.defineProperty(this, 'readyState', { writable: true, value: 4 });
        Object.defineProperty(this, 'status', { writable: true, value: 200 });
        Object.defineProperty(this, 'statusText', { writable: true, value: 'OK' });
        Object.defineProperty(this, 'responseText', { writable: true, value: '' });
        Object.defineProperty(this, 'response', { writable: true, value: '' });

        // Dispatch readystatechange and load events so the page thinks everything is fine
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
    const originalFetch = window.fetch;
    window.fetch = function () {
        return Promise.resolve(new Response('', { status: 200, statusText: 'OK' }));
    };

    // 3. Close existing WebSocket connections and prevent new ones
    const existingWS = [];
    const originalWebSocket = window.WebSocket;
    window.WebSocket = function (url, protocols) {
        // Create a fake WebSocket-like object that does nothing
        const fakeWS = {
            url, readyState: 3, /* CLOSED */
            send() { },
            close() { },
            addEventListener() { },
            removeEventListener() { },
            CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3,
            binaryType: 'blob', bufferedAmount: 0, extensions: '', protocol: ''
        };
        return fakeWS;
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

// Helper: inject freeze into page's main world
async function injectFreeze(tabId) {
    try {
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

// ─── Get config from storage ───
async function getConfig() {
    return new Promise((resolve) => {
        chrome.storage.local.get('config', (result) => {
            resolve(result.config || { ...DEFAULT_CONFIG });
        });
    });
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
 * Check if the regular schedule will hit hour 22.
 * For interval=1, every hour is hit so 22 is always covered.
 * For interval=2, check if any scheduled hour equals 22.
 * We determine the schedule's starting hour from the first run pattern:
 *   hours are: startHour, startHour+2, startHour+4, ... mod 24
 * Since the schedule anchors to the current hour at start, we check
 * if 22 % intervalHours === currentAnchorHour % intervalHours.
 */
function willScheduleHit22(intervalHours) {
    if (intervalHours <= 1) return true;
    // For 2h interval: even hours (0,2,4,...,20,22) or odd hours (1,3,...,21,23)
    // 22 is even, so if the anchor is even, 22 will be hit
    // General: 22 mod interval === anchor mod interval
    // Since we schedule at "current hour + interval", the anchor is effectively
    // determined by the current hour pattern. We check all possible hours:
    for (let h = 0; h < 24; h += intervalHours) {
        if (h === 22) return true;
    }
    // Also check odd-start pattern
    for (let h = 1; h < 24; h += intervalHours) {
        if (h === 22) return true;
    }
    return false;
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

    const intervalHours = config.intervalHours || 1;
    if (intervalHours <= 1) {
        // Every hour always hits 22h, no fallback needed
        await chrome.alarms.clear('dom-capture-deg-fallback');
        return;
    }

    const alreadySent = await isDegReportSentToday();
    if (alreadySent) {
        await chrome.alarms.clear('dom-capture-deg-fallback');
        return;
    }

    const now = new Date();
    const currentHour = now.getHours();

    // If it's already past 23h, no point scheduling
    if (currentHour >= 23) {
        return;
    }

    // Check if regular schedule will hit 22h
    const hitsAt22 = await willCurrentScheduleHit22();
    if (hitsAt22) {
        // Regular schedule covers 22h, no fallback needed
        await chrome.alarms.clear('dom-capture-deg-fallback');
        return;
    }

    // Schedule fallback at 23:00 today
    const fallbackTime = new Date(now);
    fallbackTime.setHours(23, 0, 0, 0);

    // Only schedule if it's in the future
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

    // Too late for this hour (or < 30s left), schedule next hour within window
    const nextRun = new Date(now);
    nextRun.setHours(nextRun.getHours() + 1);
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
async function getTargetTab(targetUrl) {
    if (!targetUrl) return null;

    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
        if (tab.url && tab.url.startsWith(targetUrl)) {
            return tab;
        }
    }

    // Open a new tab if not found
    const newTab = await chrome.tabs.create({ url: targetUrl, active: false });
    await new Promise((resolve) => {
        const listener = (tabId, info) => {
            if (tabId === newTab.id && info.status === 'complete') {
                chrome.tabs.onUpdated.removeListener(listener);
                resolve();
            }
        };
        chrome.tabs.onUpdated.addListener(listener);
        // Timeout after 30s
        setTimeout(() => {
            chrome.tabs.onUpdated.removeListener(listener);
            resolve();
        }, 30000);
    });

    return newTab;
}

// ─── Inject content script if needed ───
async function ensureContentScript(tabId) {
    try {
        await chrome.tabs.sendMessage(tabId, { type: 'PING' });
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

// ─── Capture data from target tab ───
async function captureData(force22h = false, isTest = false) {
    const config = await getConfig();

    if (!config.targetUrl) {
        return { success: false, error: 'No target URL configured' };
    }

    if (!config.selectors || Object.keys(config.selectors).length === 0) {
        return { success: false, error: 'No selectors configured' };
    }

    try {
        const tab = await getTargetTab(config.targetUrl);
        if (!tab) {
            return { success: false, error: 'Could not open target tab' };
        }

        await ensureContentScript(tab.id);

        // Check if we need to reload the page from a previous failed attempt
        const storage = await new Promise(r => chrome.storage.local.get('pendingReload', r));
        if (storage.pendingReload) {
            console.log('[DOMCapture] Pending reload detected. Reloading tab before extraction...');
            await chrome.storage.local.remove('pendingReload');

            // Reload and wait for completion
            await chrome.tabs.reload(tab.id);
            await new Promise((resolve) => {
                const listener = (tabId, info) => {
                    if (tabId === tab.id && info.status === 'complete') {
                        chrome.tabs.onUpdated.removeListener(listener);
                        resolve();
                    }
                };
                chrome.tabs.onUpdated.addListener(listener);
                setTimeout(() => {
                    chrome.tabs.onUpdated.removeListener(listener);
                    resolve();
                }, 30000); // 30s timeout
            });
            console.log('[DOMCapture] Tab reloaded. Waiting for DOM to settle.');
            await new Promise(r => setTimeout(r, 2000)); // Give it extra 2 seconds

            // Re-inject content script if needed after reload
            await ensureContentScript(tab.id);
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

        // 4. Freeze page — inject into MAIN world to silently intercept XHR/fetch/WS
        await injectFreeze(tab.id);

        // 5. Wait 1 second for freeze to take effect and last XHR responses to finish
        await new Promise(r => setTimeout(r, 1000));

        // Extract data from selectors
        const response = await chrome.tabs.sendMessage(tab.id, {
            type: 'EXTRACT_DATA',
            selectors: config.selectors
        });

        if (!response || !response.ok) {
            console.log('[DOMCapture] Extraction failed. Flagging for reload on next attempt:', tab.id);
            await chrome.storage.local.set({ pendingReload: true });
            return { success: false, error: 'Failed to extract data from page' };
        }

        // Check if any of the extracted data is undefined or empty
        let isDataValid = true;
        for (const [key, info] of Object.entries(response.data)) {
            if (info === undefined || info.value === undefined || info.value === '' || info.value === null) {
                isDataValid = false;
                break;
            }
        }

        if (!isDataValid) {
            console.log('[DOMCapture] Extracted data is missing or empty. Flagging for reload on next attempt:', tab.id);
            await chrome.storage.local.set({ pendingReload: true });
            return { success: false, error: 'Missing or empty data extracted' };
        }


        // Prepare payload for server
        const payload = {
            timestamp: new Date().toISOString(),
            url: config.targetUrl,
            data: response.data,
            force_22h: force22h,
            is_test: isTest
        };

        // Send extracted data to server for processing and screenshot
        const result = await sendToServer(config.serverUrl, payload);

        // Reload page to restore frozen APIs
        console.log('[DOMCapture] Reloading tab:', tab.id);
        try {
            await chrome.tabs.reload(tab.id);
            console.log('[DOMCapture] Tab reload initiated');
        } catch (err) {
            console.log('[DOMCapture] Could not reload tab:', err.message);
        }

        return result;

    } catch (err) {
        console.error('[DOMCapture] Capture error:', err);
        // Flag for reload on next attempt instead of immediate reload
        console.log('[DOMCapture] Error occurred during capture. Flagging for reload on next attempt.');
        await chrome.storage.local.set({ pendingReload: true });
        return { success: false, error: err.message };
    }

}

// ─── Send data to local Python server ───
async function sendToServer(serverUrl, payload) {
    try {
        const res = await fetch(`${serverUrl}/api/capture`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            signal: AbortSignal.timeout(60000) // 60s timeout to prevent hanging the Service Worker
        });

        if (res.ok) {
            const data = await res.json();
            console.log('[DOMCapture] Server response:', data);
            return { success: true, serverResponse: data };
        } else {
            const text = await res.text();
            return { success: false, error: `Server error ${res.status}: ${text}` };
        }
    } catch (err) {
        return { success: false, error: `Cannot connect to server: ${err.message}` };
    }
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

    const result = await captureData(force22h, isTestMode);

    // Store last capture result
    chrome.storage.local.set({
        lastCapture: {
            time: new Date().toISOString(),
            result
        }
    });

    if (result.success) {
        // If we sent a DEG report, mark it as sent for today
        if (force22h) {
            await markDegReportSent();
            console.log('[DOMCapture] ✅ DEG report marked as sent for today');
            // Clear fallback alarm since report was sent
            await chrome.alarms.clear('dom-capture-deg-fallback');
        }

        console.log('[DOMCapture] ✅ Job succeeded! Scheduling next run.');
        await addCaptureLog('success', `Capture thành công. ${result.serverResponse?.caption || ''}`);
        await scheduleNext(true, 'Success');
    } else {
        console.log(`[DOMCapture] ❌ Job failed: ${result.error}. Retrying in 5 min.`);
        await addCaptureLog('error', `Capture thất bại: ${result.error}. Retry trong 5 phút.`);
        await scheduleNext(false, result.error);
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

    // Run capture with force22h = true
    const result = await captureData(true);

    chrome.storage.local.set({
        lastCapture: {
            time: new Date().toISOString(),
            result
        }
    });

    if (result.success) {
        await markDegReportSent();
        console.log('[DOMCapture] ✅ DEG fallback at 23h succeeded!');
        await addCaptureLog('success', `Báo cáo sản lượng đầu cực 23h thành công. ${result.serverResponse?.caption || ''}`);
    } else {
        console.log(`[DOMCapture] ❌ DEG fallback at 23h failed: ${result.error}`);
        await addCaptureLog('error', `Báo cáo sản lượng đầu cực 23h thất bại: ${result.error}`);
    }
}

// ─── Alarm handler ───
chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === 'dom-capture-scheduled') {
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
    }

    if (alarm.name === 'dom-capture-deg-fallback') {
        try {
            await runDegFallbackJob();
        } catch (err) {
            console.error('[DOMCapture] ❌ Unhandled error in DEG fallback job:', err);
            await addCaptureLog('error', `Lỗi báo cáo sản lượng 23h: ${err.message}`);
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
    await saveScheduleState({ status: 'disabled', nextRun: null, lastResult: null });
    await addCaptureLog('info', 'Scheduler stopped.');
    console.log('[DOMCapture] Scheduler stopped.');
}

// ─── Message listener from popup ───
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'CAPTURE_NOW') {
        // Manual capture — add 500ms delay to allow popup to close
        setTimeout(async () => {
            let force22h = msg.force22h || false;
            
            // Auto-detect 22h/23h in manual mode if not already sent today
            const currentHour = new Date().getHours();
            if (currentHour === 22 || currentHour === 23) {
                const alreadySent = await isDegReportSentToday();
                if (!alreadySent) {
                    force22h = true;
                }
            }

            const result = await captureData(force22h, true); // true because it's a manual test
            chrome.storage.local.set({
                lastCapture: { time: new Date().toISOString(), result }
            });
            if (result.success) {
                // Mark DEG report as sent if force22h was used (ensures once-per-day across all modes)
                if (force22h) {
                    await markDegReportSent();
                    await chrome.alarms.clear('dom-capture-deg-fallback');
                }
                addCaptureLog('success', 'Capture thủ công thành công.');
            } else {
                addCaptureLog('error', `Capture thủ công thất bại: ${result.error}`);
            }
            sendResponse(result);
        }, 500);
        return true;
    }

    if (msg.type === 'TEST_WITH_DATA') {
        // Test with mock data — take screenshot then send mock data directly to server
        const force22h = msg.force22h || false;
        const mockData = msg.mockData || {};
        setTimeout(async () => {
            try {
                const config = await getConfig();

                // Take screenshot for visual verification
                const ts = new Date().toISOString();

                // Focus and screenshot the target tab if possible
                if (config.targetUrl) {
                    const tab = await getTargetTab(config.targetUrl);
                    if (tab) {
                        await chrome.windows.update(tab.windowId, { focused: true, state: 'maximized' });
                        await chrome.tabs.update(tab.id, { active: true });
                        await new Promise(r => setTimeout(r, 1000));
                    }
                }

                // Take screenshot using the existing captureVisibleTab or pyautogui on server side
                // We'll send the data and let server handle screenshot
                const payload = {
                    timestamp: ts,
                    url: config.targetUrl || 'test-mode',
                    data: mockData,
                    force_22h: force22h,
                    is_test: true
                };

                const result = await sendToServer(config.serverUrl, payload);

                chrome.storage.local.set({
                    lastCapture: { time: new Date().toISOString(), result }
                });

                if (result.success) {
                    // Mark DEG report as sent if force22h was used (ensures once-per-day across all modes)
                    if (force22h) {
                        await markDegReportSent();
                        await chrome.alarms.clear('dom-capture-deg-fallback');
                    }
                    addCaptureLog('success', `Test thành công. Caption: ${result.serverResponse?.caption || ''}`);
                } else {
                    addCaptureLog('error', `Test thất bại: ${result.error}`);
                }
                sendResponse(result);
            } catch (err) {
                addCaptureLog('error', `Test lỗi: ${err.message}`);
                sendResponse({ success: false, error: err.message });
            }
        }, 500);
        return true;
    }

    if (msg.type === 'GET_CONFIG') {
        getConfig().then(config => sendResponse(config));
        return true;
    }

    if (msg.type === 'SAVE_CONFIG') {
        saveConfig(msg.config).then(async () => {
            // Update scheduler based on autoCapture toggle
            if (msg.config.autoCapture) {
                await startScheduler();
            } else {
                await stopScheduler();
            }
            sendResponse({ ok: true });
        });
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
    if (!config.selectors) {
        await saveConfig(DEFAULT_CONFIG);
    }
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
