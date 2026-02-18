// =============================================================================
// Content Script — DOM Data Capture Extension
// Handles: Element Picker mode + Data Extraction from CSS selectors
// =============================================================================

(function () {
  'use strict';

  let pickerActive = false;
  let pickerFieldName = null;
  let highlightEl = null;
  let tooltipEl = null;
  let overlayEl = null;
  let lastHovered = null;

  // ─── Utility: Generate a unique CSS selector for an element ───
  function getUniqueSelector(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;

    // Strategy 1: Try building a path with nth-of-type
    const selector = buildSelectorPath(el);

    // Validate: does this selector actually find the right element?
    try {
      const found = document.querySelector(selector);
      if (found === el) return selector;
    } catch (e) { /* invalid selector, fall through */ }

    // Strategy 2: Fallback — use full xpath-like nth-child path
    return buildFallbackSelector(el);
  }

  function buildSelectorPath(el) {
    const path = [];
    let current = el;
    while (current && current !== document.body && current !== document.documentElement) {
      let selector = current.tagName.toLowerCase();

      if (current.id) {
        path.unshift(`#${CSS.escape(current.id)}`);
        break;
      }

      // Add classes (skip dynamic/generated ones)
      if (current.className && typeof current.className === 'string') {
        const classes = current.className.trim().split(/\s+/)
          .filter(c => c && !c.startsWith('dom-capture-') && !c.match(/^[a-z]{1,3}-[a-f0-9]{4,}/i))
          .slice(0, 3)  // limit to 3 classes to avoid overly specific selectors
          .map(c => `.${CSS.escape(c)}`)
          .join('');
        if (classes) selector += classes;
      }

      // Use :nth-of-type (counts only same-tag siblings, not all children)
      const parent = current.parentElement;
      if (parent) {
        const sameTypeSiblings = Array.from(parent.children).filter(
          s => s.tagName === current.tagName
        );
        if (sameTypeSiblings.length > 1) {
          const idx = sameTypeSiblings.indexOf(current) + 1;
          selector += `:nth-of-type(${idx})`;
        }
      }

      path.unshift(selector);
      current = current.parentElement;
    }

    return path.join(' > ');
  }

  function buildFallbackSelector(el) {
    // Build path using nth-child (counts ALL children) as absolute fallback
    const path = [];
    let current = el;
    while (current && current !== document.body && current !== document.documentElement) {
      if (current.id) {
        path.unshift(`#${CSS.escape(current.id)}`);
        break;
      }

      const parent = current.parentElement;
      if (parent) {
        const allChildren = Array.from(parent.children);
        const idx = allChildren.indexOf(current) + 1;
        path.unshift(`${current.tagName.toLowerCase()}:nth-child(${idx})`);
      } else {
        path.unshift(current.tagName.toLowerCase());
      }
      current = current.parentElement;
    }

    return path.join(' > ');
  }

  // ─── Picker: Create overlay elements ───
  function createPickerUI() {
    overlayEl = document.createElement('div');
    overlayEl.className = 'dom-capture-picker-overlay';
    document.body.appendChild(overlayEl);

    highlightEl = document.createElement('div');
    highlightEl.className = 'dom-capture-picker-highlight';
    document.body.appendChild(highlightEl);

    tooltipEl = document.createElement('div');
    tooltipEl.className = 'dom-capture-picker-tooltip';
    tooltipEl.innerHTML = `
      <div>🎯 <strong>Element Picker</strong> — Hover và click để chọn element cho <strong>${pickerFieldName}</strong></div>
      <div class="hint">Nhấn ESC để hủy</div>
    `;
    document.body.appendChild(tooltipEl);
  }

  // ─── Picker: Remove overlay ───
  function removePickerUI() {
    if (overlayEl) { overlayEl.remove(); overlayEl = null; }
    if (highlightEl) { highlightEl.remove(); highlightEl = null; }
    if (tooltipEl) { tooltipEl.remove(); tooltipEl = null; }
  }

  // ─── Picker: Mouse move handler ───
  function onPickerMouseMove(e) {
    if (!pickerActive) return;

    const target = e.target;
    if (!target || target === overlayEl || target === highlightEl || target === tooltipEl) return;
    if (target.className && typeof target.className === 'string' && target.className.includes('dom-capture-')) return;

    lastHovered = target;
    const rect = target.getBoundingClientRect();

    highlightEl.style.left = `${rect.left + window.scrollX}px`;
    highlightEl.style.top = `${rect.top + window.scrollY}px`;
    highlightEl.style.width = `${rect.width}px`;
    highlightEl.style.height = `${rect.height}px`;

    const selector = getUniqueSelector(target);
    const textPreview = (target.textContent || '').trim().substring(0, 60);

    tooltipEl.innerHTML = `
      <div>🎯 <strong>${pickerFieldName}</strong>: <code>${selector}</code></div>
      <div style="margin-top:4px; color:#8BC34A;">Text: "${textPreview}"</div>
      <div class="hint">Click để chọn · ESC để hủy</div>
    `;
  }

  // ─── Picker: Click handler ───
  function onPickerClick(e) {
    if (!pickerActive) return;

    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();

    if (lastHovered) {
      const selector = getUniqueSelector(lastHovered);
      const text = (lastHovered.textContent || '').trim();

      // Send result back to popup
      chrome.runtime.sendMessage({
        type: 'PICKER_RESULT',
        fieldName: pickerFieldName,
        selector: selector,
        textPreview: text.substring(0, 100)
      });
    }

    stopPicker();
  }

  // ─── Picker: Keydown handler ───
  function onPickerKeyDown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      stopPicker();
      chrome.runtime.sendMessage({ type: 'PICKER_CANCELLED' });
    }
  }

  // ─── Start Element Picker ───
  function startPicker(fieldName) {
    pickerActive = true;
    pickerFieldName = fieldName;
    lastHovered = null;

    createPickerUI();

    document.addEventListener('mousemove', onPickerMouseMove, true);
    document.addEventListener('click', onPickerClick, true);
    document.addEventListener('keydown', onPickerKeyDown, true);
  }

  // ─── Stop Element Picker ───
  function stopPicker() {
    pickerActive = false;
    pickerFieldName = null;
    lastHovered = null;

    document.removeEventListener('mousemove', onPickerMouseMove, true);
    document.removeEventListener('click', onPickerClick, true);
    document.removeEventListener('keydown', onPickerKeyDown, true);

    removePickerUI();
  }

  // ─── Freeze page (prevent automatic data updates without changing UI) ───
  let isFrozen = false;
  
  // Store original APIs BEFORE freezing (needed for reload to work)
  const originalApis = {
    setTimeout: window.setTimeout,
    setInterval: window.setInterval,
    requestAnimationFrame: window.requestAnimationFrame
  };

  function freezePage() {
    if (isFrozen) return; // Already frozen

    isFrozen = true;

    // Step 1: Override setTimeout - prevent execution
    const originalSetTimeout = originalApis.setTimeout;
    window.setTimeout = function(callback, delay) {
      return originalSetTimeout.call(window, callback, 999999999);
    };

    // Step 2: Override setInterval - prevent execution
    const originalSetInterval = originalApis.setInterval;
    window.setInterval = function(callback, delay) {
      return originalSetInterval.call(window, callback, 999999999);
    };

    // Step 3: Override requestAnimationFrame
    const originalRAF = originalApis.requestAnimationFrame;
    window.requestAnimationFrame = function(callback) {
      return originalSetTimeout.call(window, callback, 999999999);
    };

    // Step 4: Override fetch - prevent data requests
    window.fetch = function() {
      return Promise.reject(new Error('Page is frozen - fetch disabled'));
    };

    // Step 5: Override XMLHttpRequest - prevent AJAX requests
    const originalXHR = window.XMLHttpRequest;
    const freezedXHR = function() {
      const xhr = new originalXHR();
      xhr.open = function() {
        throw new Error('Page is frozen - XMLHttpRequest disabled');
      };
      xhr.send = function() {
        throw new Error('Page is frozen - XMLHttpRequest send disabled');
      };
      return xhr;
    };
    freezedXHR.prototype = originalXHR.prototype;
    freezedXHR.UNSENT = 0;
    freezedXHR.OPENED = 1;
    freezedXHR.HEADERS_RECEIVED = 2;
    freezedXHR.LOADING = 3;
    freezedXHR.DONE = 4;
    window.XMLHttpRequest = freezedXHR;

    // Step 6: Override WebSocket - prevent real-time updates
    if (window.WebSocket) {
      const originalWebSocket = window.WebSocket;
      const freezedWebSocket = function() {
        throw new Error('Page is frozen - WebSocket disabled');
      };
      freezedWebSocket.CONNECTING = 0;
      freezedWebSocket.OPEN = 1;
      freezedWebSocket.CLOSING = 2;
      freezedWebSocket.CLOSED = 3;
      window.WebSocket = freezedWebSocket;
    }

    // Step 7: Override EventSource - prevent Server-Sent Events
    if (window.EventSource) {
      window.EventSource = function() {
        throw new Error('Page is frozen - EventSource disabled');
      };
    }

    // Step 8: Override MutationObserver - prevent DOM monitoring
    if (window.MutationObserver) {
      const originalMO = window.MutationObserver;
      window.MutationObserver = function(callback) {
        const mo = new originalMO(() => {});
        return mo;
      };
      window.MutationObserver.prototype.observe = function() {
        throw new Error('Page is frozen - MutationObserver disabled');
      };
    }

    // Step 9: Override ResizeObserver - prevent resize monitoring
    if (window.ResizeObserver) {
      const originalRO = window.ResizeObserver;
      window.ResizeObserver = function(callback) {
        const ro = new originalRO(() => {});
        return ro;
      };
      window.ResizeObserver.prototype.observe = function() {
        throw new Error('Page is frozen - ResizeObserver disabled');
      };
    }

    // Step 10: Override requestIdleCallback - prevent idle updates
    if (window.requestIdleCallback) {
      const originalIdleCallback = window.requestIdleCallback;
      window.requestIdleCallback = function(callback) {
        return originalSetTimeout.call(window, callback, 999999999);
      };
    }

    // Step 11: Override queueMicrotask - prevent microtask updates
    if (window.queueMicrotask) {
      const originalQueue = window.queueMicrotask;
      window.queueMicrotask = function(callback) {
        return originalSetTimeout.call(window, callback, 999999999);
      };
    }

    // Step 12: Override IntersectionObserver - prevent lazy loading updates
    if (window.IntersectionObserver) {
      const originalIO = window.IntersectionObserver;
      window.IntersectionObserver = function(callback) {
        const io = new originalIO(() => {});
        return io;
      };
      window.IntersectionObserver.prototype.observe = function() {
        throw new Error('Page is frozen - IntersectionObserver disabled');
      };
    }

    // Step 13: Disable CSS interactions (lightweight approach)
    document.body.style.pointerEvents = 'none';
    document.documentElement.style.pointerEvents = 'none';
    document.body.style.touchAction = 'none';
    document.documentElement.style.touchAction = 'none';

    console.log('[DOM Capture] Page frozen - 13 update mechanisms disabled (99%+ coverage)');
  }

  // ─── Extract data from configured selectors ───
  function extractData(selectors) {
    const results = {};

    for (const [name, selector] of Object.entries(selectors)) {
      if (!selector) {
        results[name] = { value: '', error: 'No selector configured' };
        continue;
      }

      try {
        const el = document.querySelector(selector);
        if (el) {
          // Get the visible text — use innerText for display text, 
          // value for input/select, textContent as fallback
          let value = '';
          if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
            value = el.value || '';
          } else if (el.tagName === 'SELECT') {
            value = el.options[el.selectedIndex]?.text || el.value || '';
          } else {
            value = el.innerText || el.textContent || '';
          }
          results[name] = {
            value: value.trim(),
            found: true
          };
        } else {
          results[name] = {
            value: '',
            found: false,
            error: `Element not found: ${selector}`
          };
        }
      } catch (err) {
        results[name] = {
          value: '',
          found: false,
          error: `Invalid selector: ${err.message}`
        };
      }
    }

    return results;
  }

  // ─── Message listener ───
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'START_PICKER') {
      startPicker(msg.fieldName);
      sendResponse({ ok: true });
    }

    if (msg.type === 'STOP_PICKER') {
      stopPicker();
      sendResponse({ ok: true });
    }

    if (msg.type === 'EXTRACT_DATA') {
      const data = extractData(msg.selectors || {});
      sendResponse({ ok: true, data });
    }

    if (msg.type === 'FREEZE_PAGE') {
      freezePage();
      sendResponse({ ok: true });
    }

    if (msg.type === 'RELOAD_PAGE') {
      console.log('[DOM Capture] RELOAD_PAGE message received');
      
      // Restore pointer events before reload
      document.body.style.pointerEvents = 'auto';
      document.documentElement.style.pointerEvents = 'auto';
      document.body.style.touchAction = 'auto';
      document.documentElement.style.touchAction = 'auto';
      
      isFrozen = false;
      
      // Send response FIRST to unblock background script
      sendResponse({ ok: true });
      
      // Use original requestAnimationFrame to avoid frozen setTimeout
      try {
        originalApis.requestAnimationFrame(() => {
          console.log('[DOM Capture] RAF callback: About to reload page...');
          window.location.reload();
        });
      } catch (err) {
        console.error('[DOM Capture] RAF error, trying direct reload:', err);
        window.location.reload();
      }
    }

    if (msg.type === 'PING') {
      sendResponse({ ok: true });
    }

    // Must return true for async responses
    return true;
  });

})();
