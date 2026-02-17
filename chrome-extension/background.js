// =============================================================================
// Background Service Worker — DOM Data Capture Extension
// Scheduling logic matching main.py:
//   - Run every hour at random minute 0-10
//   - Retry in 5 minutes on failure
//   - Auto-start on extension load
// =============================================================================

const DEFAULT_CONFIG = {
    serverUrl: 'http://localhost:5001',
    targetUrl: '',
    intervalMinutes: 60,
    selectors: {},
    autoCapture: true,  // Default ON to match main.py behavior
    retryMinutes: 5
};

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

// ─── Scheduling logic (mirrors main.py) ───

/**
 * Succces -> next hour at random second between 0 and 600 (0-10 min)
 * Failure -> retry in 5 minutes
 * Returns absolute Date object.
 */
function computeNextRunTime(success) {
    const now = new Date();

    if (!success) {
        // Failed → retry in 5 minutes (precise)
        return new Date(now.getTime() + 5 * 60000);
    }

    // Success → calculate start of next hour
    const nextRun = new Date(now);
    nextRun.setHours(nextRun.getHours() + 1);
    nextRun.setMinutes(0);
    nextRun.setSeconds(0);
    nextRun.setMilliseconds(0);

    // Add random 0-600 seconds (0 to 10 minutes)
    const randomSeconds = Math.floor(Math.random() * 601);
    return new Date(nextRun.getTime() + randomSeconds * 1000);
}

/**
 * Compute the first run time.
 * If current minute < 10, pick a random second between (now + 30s) and the 10-minute mark.
 * Otherwise, pick next hour random 0-10 minutes (0-600s).
 * Returns absolute Date object.
 */
function computeFirstRunTime() {
    const now = new Date();
    const currentMinute = now.getMinutes();

    if (currentMinute < 10) {
        // Still within the 10-minute window of the current hour
        const startOfTenMinMark = new Date(now);
        startOfTenMinMark.setMinutes(10);
        startOfTenMinMark.setSeconds(0);
        startOfTenMinMark.setMilliseconds(0);

        const minTime = now.getTime() + 30000; // current time + 30 seconds
        const maxTime = startOfTenMinMark.getTime();

        if (minTime < maxTime) {
            const randomTime = minTime + Math.random() * (maxTime - minTime);
            return new Date(randomTime);
        }
    }

    // Too late for this hour (or < 30s left), schedule next hour 0-10 min window
    const nextRun = new Date(now);
    nextRun.setHours(nextRun.getHours() + 1);
    nextRun.setMinutes(0);
    nextRun.setSeconds(0);
    nextRun.setMilliseconds(0);

    const randomSeconds = Math.floor(Math.random() * 601);
    return new Date(nextRun.getTime() + randomSeconds * 1000);
}

// ─── Schedule next capture alarm ───
async function scheduleNext(success, reason) {
    const nextRun = success !== null
        ? computeNextRunTime(success)
        : computeFirstRunTime();

    const nextRunISO = nextRun.toISOString();

    await chrome.alarms.clear('dom-capture-scheduled');
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
async function captureData(force22h = false) {
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

        // --- NEW: Focus window and Tab before screenshot ---
        // 1. Focus the window and maximize it
        await chrome.windows.update(tab.windowId, {
            focused: true,
            state: 'maximized'
        });

        // 2. Make the target tab active
        await chrome.tabs.update(tab.id, { active: true });

        // 3. Wait 1 second for the OS/Window to settle and come to front
        await new Promise(r => setTimeout(r, 1000));

        // Extract data from selectors
        const response = await chrome.tabs.sendMessage(tab.id, {
            type: 'EXTRACT_DATA',
            selectors: config.selectors
        });

        if (!response || !response.ok) {
            return { success: false, error: 'Failed to extract data from page' };
        }

        // Send data to local server (screenshot is taken server-side)
        const payload = {
            timestamp: new Date().toISOString(),
            url: config.targetUrl,
            data: response.data,
            force_22h: force22h
        };

        const result = await sendToServer(config.serverUrl, payload);
        return result;

    } catch (err) {
        console.error('[DOMCapture] Capture error:', err);
        return { success: false, error: err.message };
    }
}

// ─── Send data to local Python server ───
async function sendToServer(serverUrl, payload) {
    try {
        const res = await fetch(`${serverUrl}/api/capture`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
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

    const result = await captureData();

    // Store last capture result
    chrome.storage.local.set({
        lastCapture: {
            time: new Date().toISOString(),
            result
        }
    });

    if (result.success) {
        console.log('[DOMCapture] ✅ Job succeeded! Scheduling next hour.');
        await addCaptureLog('success', `Capture thành công. ${result.serverResponse?.caption || ''}`);
        await scheduleNext(true, 'Success');
    } else {
        console.log(`[DOMCapture] ❌ Job failed: ${result.error}. Retrying in 5 min.`);
        await addCaptureLog('error', `Capture thất bại: ${result.error}. Retry trong 5 phút.`);
        await scheduleNext(false, result.error);
    }
}

// ─── Alarm handler ───
chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === 'dom-capture-scheduled') {
        await runScheduledJob();
    }
});

// ─── Start scheduling (called on install and when auto-capture is toggled on) ───
async function startScheduler() {
    const config = await getConfig();
    if (!config.autoCapture) {
        console.log('[DOMCapture] Auto-capture disabled. Not scheduling.');
        await chrome.alarms.clear('dom-capture-scheduled');
        await saveScheduleState({ status: 'disabled', nextRun: null, lastResult: null });
        return;
    }

    const nextRun = computeFirstRunTime();
    console.log(`[DOMCapture] Scheduler started. First run at ${nextRun.toISOString()}`);
    await scheduleNext(null, 'Scheduler started');
    await addCaptureLog('info', `Scheduler started. First run at ${nextRun.toLocaleTimeString('vi-VN')}`);
}

// ─── Stop scheduling ───
async function stopScheduler() {
    await chrome.alarms.clear('dom-capture-scheduled');
    await saveScheduleState({ status: 'disabled', nextRun: null, lastResult: null });
    await addCaptureLog('info', 'Scheduler stopped.');
    console.log('[DOMCapture] Scheduler stopped.');
}

// ─── Message listener from popup ───
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'CAPTURE_NOW') {
        // Manual capture — add 500ms delay to allow popup to close
        const force22h = msg.force22h || false;
        setTimeout(async () => {
            const result = await captureData(force22h);
            chrome.storage.local.set({
                lastCapture: { time: new Date().toISOString(), result }
            });
            if (result.success) {
                addCaptureLog('success', 'Capture thủ công thành công.');
            } else {
                addCaptureLog('error', `Capture thủ công thất bại: ${result.error}`);
            }
            sendResponse(result);
        }, 500);
        return true;
    }

    if (msg.type === 'GET_CONFIG') {
        getConfig().then(config => sendResponse(config));
        return true;
    }

    if (msg.type === 'SAVE_CONFIG') {
        saveConfig(msg.config).then(() => {
            // Update scheduler based on autoCapture toggle
            if (msg.config.autoCapture) {
                startScheduler();
            } else {
                stopScheduler();
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
    if (config.autoCapture !== false) {
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
