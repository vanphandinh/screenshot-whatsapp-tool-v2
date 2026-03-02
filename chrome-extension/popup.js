// =============================================================================
// Popup Script — DOM Data Capture Extension
// Manages UI interactions, config persistence, element picker, schedule display
// =============================================================================

document.addEventListener('DOMContentLoaded', init);

let config = {
    serverUrl: 'http://localhost:5001',
    targetUrl: '',
    selectors: {},
    autoCapture: false
};

const logs = [];

// ─── Init ───
async function init() {
    // Load config from background
    config = await sendMsg({ type: 'GET_CONFIG' }) || config;

    // Populate UI
    renderSelectors();
    document.getElementById('inputTargetUrl').value = config.targetUrl || '';
    document.getElementById('inputServerUrl').value = config.serverUrl || 'http://localhost:5001';
    document.getElementById('toggleAutoCapture').checked = config.autoCapture !== false;
    document.getElementById('autoCaptureLabel').textContent = config.autoCapture !== false ? 'Bật' : 'Tắt';

    // Load schedule state
    await refreshScheduleStatus();

    // Load logs from storage
    chrome.storage.local.get('captureLogs', (data) => {
        if (data.captureLogs) {
            // Load into global array and UI without re-saving
            data.captureLogs.forEach(l => {
                logs.push(l); // Just push to array
                renderLogEntry(l.type, l.msg, l.time, true); // Use append=true to keep newest on top
            });
        }
    });

    checkServerStatus();
    bindEvents();
}

// ─── Bind Events ───
function bindEvents() {
    // Tabs
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');
        });
    });

    // Add field
    document.getElementById('btnAddField').addEventListener('click', () => {
        const name = document.getElementById('newFieldName').value.trim();
        if (!name) return;
        if (config.selectors[name] !== undefined) {
            alert(`Trường "${name}" đã tồn tại!`);
            return;
        }
        config.selectors[name] = '';
        document.getElementById('newFieldName').value = '';
        renderSelectors();
        saveConfigToBackground();
    });

    // Enter key for add field
    document.getElementById('newFieldName').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') document.getElementById('btnAddField').click();
    });

    // Refresh preview
    document.getElementById('btnRefreshPreview').addEventListener('click', refreshPreview);

    // Manual capture (test)
    document.getElementById('btnTestNormal').addEventListener('click', () => captureNow(false));
    document.getElementById('btnTest22h').addEventListener('click', () => captureNow(true));

    // Save settings
    document.getElementById('btnSaveSettings').addEventListener('click', saveSettings);

    // Check server
    document.getElementById('btnCheckServer').addEventListener('click', checkServerStatus);

    // Clear logs
    document.getElementById('btnClearLogs')?.addEventListener('click', clearLogs);

    // Auto-capture toggle
    document.getElementById('toggleAutoCapture').addEventListener('change', (e) => {
        document.getElementById('autoCaptureLabel').textContent = e.target.checked ? 'Bật' : 'Tắt';
    });

    // Listen for picker results from content script
    chrome.runtime.onMessage.addListener((msg) => {
        if (msg.type === 'PICKER_RESULT') {
            config.selectors[msg.fieldName] = msg.selector;
            renderSelectors();
            saveConfigToBackground();
            addLog('success', `Picker: "${msg.fieldName}" → ${msg.selector}`);
        }
        if (msg.type === 'PICKER_CANCELLED') {
            addLog('info', 'Picker đã bị hủy');
        }
    });
}

// ─── Render Selector Rows ───
function renderSelectors() {
    const list = document.getElementById('selectorList');
    list.innerHTML = '';

    for (const [name, selector] of Object.entries(config.selectors)) {
        const row = document.createElement('div');
        row.className = 'selector-row';
        row.innerHTML = `
      <span class="selector-name">${escapeHtml(name)}</span>
      <input type="text" class="selector-input" value="${escapeHtml(selector)}" placeholder="CSS selector..." data-field="${escapeHtml(name)}">
      <button class="btn-picker" title="Pick element trên trang" data-field="${escapeHtml(name)}">🎯</button>
      <button class="btn-remove" title="Xóa" data-field="${escapeHtml(name)}">✕</button>
    `;
        list.appendChild(row);
    }

    // Bind input changes
    list.querySelectorAll('.selector-input').forEach(input => {
        input.addEventListener('change', (e) => {
            config.selectors[e.target.dataset.field] = e.target.value.trim();
            saveConfigToBackground();
        });
    });

    // Bind picker buttons
    list.querySelectorAll('.btn-picker').forEach(btn => {
        btn.addEventListener('click', (e) => {
            startPicker(e.target.dataset.field);
        });
    });

    // Bind remove buttons
    list.querySelectorAll('.btn-remove').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const field = e.target.dataset.field;
            delete config.selectors[field];
            renderSelectors();
            saveConfigToBackground();
        });
    });
}

// ─── Start Element Picker ───
async function startPicker(fieldName) {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) {
        addLog('error', 'Không tìm thấy tab đang mở');
        return;
    }

    try {
        try {
            await chrome.tabs.sendMessage(tab.id, { type: 'PING' });
        } catch {
            await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content.js'] });
            await chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ['content.css'] });
        }

        await chrome.tabs.sendMessage(tab.id, { type: 'START_PICKER', fieldName });
        addLog('info', `Picker bắt đầu cho "${fieldName}". Chuyển sang tab để chọn element.`);
        window.close();
    } catch (err) {
        addLog('error', `Picker lỗi: ${err.message}`);
    }
}

// ─── Refresh Preview ───
async function refreshPreview() {
    const btn = document.getElementById('btnRefreshPreview');
    btn.classList.add('loading');

    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab) throw new Error('No active tab');

        try {
            await chrome.tabs.sendMessage(tab.id, { type: 'PING' });
        } catch {
            await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content.js'] });
            await chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ['content.css'] });
        }

        const response = await chrome.tabs.sendMessage(tab.id, {
            type: 'EXTRACT_DATA',
            selectors: config.selectors
        });

        if (response && response.ok) {
            renderPreview(response.data);
        } else {
            addLog('error', 'Không thể trích xuất dữ liệu');
        }
    } catch (err) {
        addLog('error', `Preview lỗi: ${err.message}`);
    } finally {
        btn.classList.remove('loading');
    }
}

// ─── Render Preview Grid ───
function renderPreview(data) {
    const grid = document.getElementById('previewGrid');
    grid.innerHTML = '';

    for (const [name, info] of Object.entries(data)) {
        const card = document.createElement('div');
        card.className = 'preview-card';
        card.innerHTML = `
      <div class="label">${escapeHtml(name)}</div>
      <div class="value ${info.found === false ? 'error' : ''}">${info.found === false
                ? '⚠ Not found'
                : escapeHtml(info.value || '(trống)')
            }</div>
    `;
        grid.appendChild(card);
    }
}

// ─── Capture Now (manual test) ───
async function captureNow(force22h = false) {
    const btnId = force22h ? 'btnTest22h' : 'btnTestNormal';
    const btn = document.getElementById(btnId);

    if (btn) btn.classList.add('loading');

    try {
        // --- Fix: Check server status BEFORE sending message and closing popup ---
        const isOnline = await checkServerStatus();
        if (!isOnline) {
            alert('Không thể kết nối đến server. Vui lòng bật server và thử lại.');
            if (btn) btn.classList.remove('loading');
            return;
        }

        // Server is online, proceed with capture
        sendMsg({ type: 'CAPTURE_NOW', force22h });

        // Wait 200ms just to ensure the message is sent before closing
        setTimeout(() => window.close(), 200);
    } catch (err) {
        alert('Lỗi: ' + err.message);
        if (btn) btn.classList.remove('loading');
    }
}

// ─── Refresh Schedule Status ───
async function refreshScheduleStatus() {
    const state = await sendMsg({ type: 'GET_SCHEDULE' }) || {};

    const statusEl = document.getElementById('scheduleStatus');
    const nextRunEl = document.getElementById('scheduleNextRun');
    const lastResultEl = document.getElementById('scheduleLastResult');

    // Status
    const statusMap = {
        'scheduled': '🟢 Đang chạy',
        'success': '🟢 Đang chạy',
        'retrying': '🟡 Đang retry',
        'disabled': '🔴 Tắt',
        'idle': '⚪ Chưa khởi động'
    };
    statusEl.textContent = statusMap[state.status] || '⚪ Unknown';

    // Next run
    if (state.nextRun) {
        const nextDate = new Date(state.nextRun);
        nextRunEl.textContent = nextDate.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } else {
        nextRunEl.textContent = '—';
    }

    // Last result
    if (state.lastResult) {
        lastResultEl.textContent = state.lastResult;
        lastResultEl.style.color = state.status === 'retrying' ? '#FFA726' : '#66BB6A';
    } else {
        lastResultEl.textContent = '—';
        lastResultEl.style.color = '';
    }

    // Update header badge
    updateStatusBadge(state.status === 'scheduled' || state.status === 'success');
}

// ─── Save Settings ───
async function saveSettings() {
    const targetUrl = document.getElementById('inputTargetUrl').value.trim();
    const serverUrl = document.getElementById('inputServerUrl').value.trim();
    const autoCapture = document.getElementById('toggleAutoCapture').checked;

    if (autoCapture) {
        const isOnline = await checkServerStatus();
        if (!isOnline) {
            alert('Không thể bật chế độ tự động vì server chưa chạy.');
            document.getElementById('toggleAutoCapture').checked = false;
            document.getElementById('autoCaptureLabel').textContent = 'Tắt';
            return;
        }
    }

    config.targetUrl = targetUrl;
    config.serverUrl = serverUrl;
    config.autoCapture = document.getElementById('toggleAutoCapture').checked;

    await saveConfigToBackground();
    addLog('success', 'Cài đặt đã được lưu');

    // If autoCapture was just turned ON, update badge immediately
    if (autoCapture) {
        updateStatusBadge(true);
    } else {
        updateStatusBadge(false);
    }

    // Refresh schedule display after save (which will also call updateStatusBadge)
    setTimeout(() => refreshScheduleStatus(), 800);
}

// ─── Save config to background ───
async function saveConfigToBackground() {
    await sendMsg({ type: 'SAVE_CONFIG', config });
}

// ─── Check Server Status ───
async function checkServerStatus() {
    const statusEl = document.getElementById('serverStatus');
    const serverUrlInput = document.getElementById('inputServerUrl');
    const serverUrl = (serverUrlInput ? serverUrlInput.value : config.serverUrl).trim();

    if (statusEl) statusEl.innerHTML = '<span class="status-dot"></span> Đang kiểm tra...';

    try {
        const res = await fetch(`${serverUrl}/api/status`, { method: 'GET', signal: AbortSignal.timeout(3000) });
        if (res.ok) {
            if (statusEl) statusEl.innerHTML = '<span class="status-dot online"></span> Server đang chạy';
            return true;
        } else {
            if (statusEl) statusEl.innerHTML = '<span class="status-dot offline"></span> Server lỗi: ' + res.status;
            return false;
        }
    } catch {
        if (statusEl) statusEl.innerHTML = '<span class="status-dot offline"></span> Không thể kết nối';
        return false;
    }
}

// ─── Update Status Badge ───
function updateStatusBadge(online) {
    const badge = document.getElementById('statusBadge');
    badge.innerHTML = `
    <span class="status-dot ${online ? 'online' : 'offline'}"></span>
    <span class="status-text">${online ? 'Active' : 'Offline'}</span>
  `;
}

// ─── Add Log Entry ───
function addLog(type, msg, time) {
    const entryTime = time || new Date().toISOString();
    renderLogEntry(type, msg, entryTime);

    logs.unshift({ type, msg, time: entryTime });
    if (logs.length > 50) logs.length = 50;
    chrome.storage.local.set({ captureLogs: logs });
}

// ─── Render Log Entry UI Only ───
function renderLogEntry(type, msg, time, append = false) {
    const logList = document.getElementById('logList');
    const empty = logList.querySelector('.log-empty');
    if (empty) empty.remove();

    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    const t = new Date(time).toLocaleTimeString('vi-VN');
    const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️';
    entry.innerHTML = `
    <div class="log-time">${t}</div>
    <div class="log-msg">${icon} ${escapeHtml(msg)}</div>
  `;

    if (append) {
        logList.appendChild(entry);
    } else {
        logList.insertBefore(entry, logList.firstChild);
    }
}

// ─── Clear Logs ───
function clearLogs() {
    if (!confirm('Bạn có chắc chắn muốn xóa toàn bộ log?')) return;

    // Reset global array
    logs.length = 0;

    chrome.storage.local.set({ captureLogs: [] }, () => {
        document.getElementById('logList').innerHTML = '<div class="log-empty">Chưa có log nào.</div>';
    });
}

// ─── Helper: Send message to background ───
function sendMsg(msg) {
    return new Promise((resolve) => {
        try {
            chrome.runtime.sendMessage(msg, (response) => {
                if (chrome.runtime.lastError) {
                    console.warn('[Popup] Message error:', chrome.runtime.lastError.message);
                    resolve({ success: false, error: chrome.runtime.lastError.message });
                } else {
                    resolve(response);
                }
            });
        } catch (err) {
            resolve({ success: false, error: err.message });
        }
    });
}

// ─── Helper: Escape HTML ───
function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
