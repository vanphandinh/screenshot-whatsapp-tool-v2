---
name: audit-chrome-extension
description: "Rà soát Chrome MV3 extension của screenshot-whatsapp-tool: service worker lifecycle, alarms/scheduler, content scrape selectors, freeze inject, popup/storage. Bắt buộc dùng khi đóng vai extension-auditor hoặc khi user hỏi lỗi extension/background/content/popup. Chỉ findings, không sửa code. Không thay orchestrator toàn repo hay deep server review."
---

# Audit Chrome Extension

Hướng dẫn extension-auditor rà soát MV3 một cách có hệ thống. Service worker có thể bị kill giữa job — ưu tiên failure modes lifecycle và selector drift.

## Why this skill

Extension điều khiển lịch gửi và scrape DOM. Bug hay nằm ở alarm/SW restart và selector gãy khi trang đổi, không phải lỗi cú pháp JS.

## Workflow

1. Đọc `chrome-extension/manifest.json` (permissions, SW, content_scripts, icons)
2. Đọc theo flow capture:
   - Trigger (popup / alarm) → `background.js`
   - Inject / freeze → content / MAIN world
   - `EXTRACT_DATA` → POST server
   - Error paths & auto-disable
3. Đọc `popup.js` / HTML/CSS cho config & test scenarios
4. Ghi `_workspace/02_extension-auditor_findings.md`

## Focus areas

### Manifest & assets

Permissions tối thiểu? Icons path tồn tại? Host permissions khớp WhatsApp monitoring page?

### Service worker lifecycle

State in-memory mất khi SW sleep. Pre-schedule / watchdog có đủ không. Job dở khi kill giữa chừng.

### Scheduler

Hourly / 2h / 22h DEG / 23h fallback / random window. `chrome.alarms` vs `setTimeout`. Disable khi server down — có recover không.

### Scrape & freeze

CSS selectors brittle; picker lưu selector đúng chưa. Freeze XHR/fetch/WS — restore khi fail? Side effect SPA.

### Messaging

Runtime message không listener / tab sai / frame sai. Popup đóng giữa config save.

## Finding schema

| Field | Rule |
|-------|------|
| id | `EXT-NNN` |
| severity | critical / high / medium / low / info |
| area | extension / reliability |
| title | một dòng |
| evidence | `chrome-extension/...:line` |
| why | impact |
| repro_or_trigger | điều kiện |
| suggested_fix | gợi ý only |

Missed schedule / silent fail capture → high; selector có thể gãy → medium+; UX copy → low.

## Output

Frontmatter `agent: extension-auditor`, `status`, `finding_count`. Zero bugs: ghi rõ + residual risks (selector drift theo site).

## Do not

- Sửa files trong `chrome-extension/`
- Deep-audit `server.py` validation (boundary-qa)
- Bịa line numbers
