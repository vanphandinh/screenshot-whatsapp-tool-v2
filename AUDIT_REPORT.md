# Codebase audit report

- **Date:** 2026-09-04
- **Scope:** full (extension + Flask server + biên giới tích hợp)
- **Branch:** `arena/01a06b57-screenshot-whatsapp-tool-v2` (base `25043b1`)
- **Agents:** server-auditor ✅ · extension-auditor ✅ · boundary-qa ✅ (thực thi in-process; không có `Task` tool trong sandbox nên fan-out chạy tuần tự, đọc đúng agent def + skill)
- **Verification:** `py_compile` OK · `node --check` 4 file OK · `pytest tests/` → **18 passed** (caption_math + manual intervention). Runtime Flask/Win32/WPP **không chạy được** trong sandbox (Windows-only + cần playwright/WPP_Whatsapp) → static review.

## Summary

| Severity | Count |
|----------|-------|
| critical | 0 |
| high | 0 |
| medium | 3 |
| low | 8 |
| info | 7 |
| **Tổng** | **18** |

Kết luận tổng quan: codebase **khá chín** — đã có sẵn nhiều cơ chế phòng thủ (idempotency server-side, SEND_TIMEOUT vs SEND_FAILED để tránh trùng, optimistic pre-schedule, watchdog, pendingReload, fail-fast trước khi chiếm desktop, token auth + compare_digest, atomic config write). Không phát hiện lỗi **critical/high** (không sai SĐT, không mất dữ liệu trực tiếp). Ba lỗi medium đều nằm ở **reliability trong cửa sổ hẹp** (race, dedupe chưa wired, silent loss).

## Fix log (2026-09-04)

Theo yêu cầu của user, chỉ sửa **3 findings medium** — không đụng đến các nhóm low/info. Verify: `py_compile` OK · `node --check` OK · `pytest tests/` 18 passed.

| Finding | Fix |
|---------|-----|
| **SRV-001** ✅ FIXED | `server.py` — `init_whatsapp()` giờ kiểm tra `_wa_send_lock.locked()` trước khi `sync_close()`/đổi session → tray "Reconnect" và path phục hồi không còn xé session giữa lúc gửi; health loop vẫn retry sau khi gửi xong. |
| **EXT-001** ✅ FIXED | `chrome-extension/background.js` — `captureData(force22h, isTest, opts)` giờ hỗ trợ `opts.reuse`; **chỉ** các job tự động phục hồi (`runScheduledJob`, `runDegFallbackJob`) truyền `reuse:true` để tái dùng `capture_id` trong cửa sổ 12 phút cho server dedupe; capture thủ công (`CAPTURE_NOW`) và test (`TEST_WITH_DATA`) luôn mint id mới (tránh dedupe nhầm giữa live/test). |
| **BND-001** ✅ FIXED | `server.py` — thêm `_set_send_outcome()`/`_get_send_outcome()` + field `last_send_outcome` trong `/api/status` (có `status`, `detail`, `capture_id`, `at`), ghi outcome ở mọi nhánh kết quả gửi (success / late-send / wedged / fail). `chrome-extension/background.js` — thêm `getLastSendOutcome(captureId)` (poll ngắn, match theo `capture_id` để không dính outcome cũ); `sendToServer` gắn outcome vào kết quả timeout (cả nhánh 504 lẫn fetch-abort); `runScheduledJob`/`runDegFallbackJob`/handler `CAPTURE_NOW` giờ phân biệt: **failed** → retry, **success** → không retry (tránh trùng), **unknown/wedged** → giữ nguyên hành vi an toàn cũ. |

---

## Findings

### MEDIUM

#### SRV-001 — Re-init WhatsApp chạy song song với send đang bay (race) — ✅ FIXED
- **area:** reliability
- **evidence:** `server.py:1549-1551` (tray "Reconnect" → `init_whatsapp`), `server.py:1143-1145` (path phục hồi sau wedge → `init_whatsapp`), `server.py:429-441` (`sync_close()` + `whatsapp_client = None`), guard đúng chỉ có ở health loop `server.py:629`
- **why:** `init_whatsapp` chỉ giữ `_wa_init_lock`, không kiểm tra `_wa_send_lock`. Bấm "Reconnect WhatsApp" trên tray đúng lúc executor đang gửi → `sync_close()` đóng browser cũ + null client ngay dưới chân thread gửi → send fail "Execution context was destroyed" → 502 SEND_FAILED → extension coi là transient → retry (re-focus/re-maximize desktop thêm lần nữa) → report trễ.
- **repro:** Đang auto-capture (gửi ~2-3 phút) → bấm chuột phải tray → "Reconnect WhatsApp". Hoặc send wedge >600s → path phục hồi tự gọi re-init khi send cũ vẫn chạy (`cancel_futures` không giết thread đang chạy).
- **suggested_fix:** Trong `init_whatsapp`, trước `sync_close()` hãy đợi/kiểm tra `_wa_send_lock` (như health loop); path phục hồi nên join executor trước khi re-init.

#### EXT-001 — Đường dedupe `beginJobToken({reuse})` chưa được wire → có thể duplicate report — ✅ FIXED
- **area:** reliability
- **evidence:** `chrome-extension/background.js:38-55` (reuse chỉ chạy khi `opts.reuse === true` nhưng **không** caller nào truyền — chỉ `beginJobToken()` tại `:795`, `:1637`); server dedupe chỉ keyed `capture_id` (`server.py:1171-1194`)
- **why:** Cơ chế phòng trùng (SW chết giữa lúc POST → lần sau dùng lại capture_id) chưa từng được kích hoạt → mỗi job mint capture_id mới → server coi là capture mới → gửi lại. Report giờ có thể bị trùng khi browser crash/evict SW trong cửa sổ gửi. (Báo cáo 22h/DEG được bảo vệ bởi `degReportDate`; report giờ thì không.)
- **repro:** Đang gửi lúc server đã gửi WA nhưng extension chưa nhận 200 → tắt browser → alarm kế chạy với capture_id mới → tin trùng.
- **suggested_fix:** Gọi `beginJobToken({reuse:true})` khi `inflightCaptureExpires` còn hiệu lực (đúng comment dòng 39); hoặc server dedupe theo (recipient + thời gian gần).

#### BND-001 — 504 SEND_TIMEOUT + late-send thất bại sau đó = report mất im lặng — ✅ FIXED
- **area:** reliability
- **evidence:** `server.py:1115-1161` (504; thread `_release_after_send` chỉ log, không báo extension) ; `chrome-extension/background.js:1266-1279` (timeout → KHÔNG retry để tránh trùng)
- **why:** `future.result(150s)` timeout → 504; thread nền đợi kết quả late-send. Nếu late-send **thực sự fail** (browser chết, ack<0), chỉ có dòng log server — extension đã nhận 504 và chủ động không retry (đúng), nhưng không có kênh báo "report này KHÔNG gửi được" → user không biết. Cửa sổ hẹp nhưng là silent loss với tool vận hành.
- **repro:** WA giật lúc gửi → server timeout 150s → late-send fail. User chỉ thấy log server.
- **suggested_fix:** Khi late-send fail, đẩy outcome ra endpoint status (`last_send_outcome`) để extension log/notify; chỉ retry khi late-send xác nhận FAILED thật.

### LOW

#### SRV-002 — `int(parse_number(...))` cắt cụt thầm lặng DC/F/M
- **area:** server · **evidence:** `server.py:967-969`
- **why:** Scrape ra "11.5" → `int()` → 11 không báo lỗi → caption sai nhẹ (count phải nguyên).
- **suggested_fix:** Reject nếu `not x.is_integer()` thay vì int().

#### SRV-003 — `/api/status` không auth + là boolean-oracle cho token
- **area:** security · **evidence:** `server.py:787-826`
- **why:** Bind loopback nên rủi ro thấp, nhưng `token_valid` cho phép process local kiểm tra "đoán token đúng không"; lộ nhịp `whatsapp_send_busy`/`target_window_selected`. Không lộ SĐT/token đầy đủ.
- **suggested_fix:** Giữ status cho healthcheck; tách `token_valid`/`target_window_selected`/`whatsapp_send_busy` sang endpoint có auth.

#### SRV-004 — Race single-instance → instance thứ 2 có tray "chạy" nhưng server chết
- **area:** reliability · **evidence:** `server.py:1576-1589`, `server.py:1607-1609`
- **why:** 2 instance khởi động cách nhau <2s → probe chưa thấy instance 1 → instance 2 không exit → `app.run` ném `OSError: address in use` trong daemon thread (chạy `pythonw.exe` không ai thấy traceback) → tray hiện "đang chạy" nhưng API không tồn tại.
- **suggested_fix:** Catch exception trong thread flask → log/tray + exit; hoặc bind socket trước khi khởi động UI; tăng probe timeout + retry.

#### EXT-002 — `intervalMinutes`/`retryMinutes` dead config
- **area:** extension · **evidence:** `chrome-extension/background.js:21,24`
- **why:** Không nơi nào đọc; retry thực tế hardcode 5 phút. Gây hiểu nhầm.
- **suggested_fix:** Xóa hoặc dùng lại.

#### EXT-003 — Race `waitForTabComplete` với trang load quá nhanh
- **area:** reliability · **evidence:** `chrome-extension/background.js:60-75` (gọi tại `:848,861,866,881`)
- **why:** Listener gắn sau create/reload; trang cache load nhanh → miss `complete` → chờ 30s → "Page load timeout" giả.
- **suggested_fix:** Kiểm tra `chrome.tabs.get(tabId).status` trước khi đăng ký listener.

#### EXT-004 — Freeze MAIN world vĩnh viễn → phục hồi bằng reload toàn tab
- **area:** reliability · **evidence:** `chrome-extension/background.js:129-215`, `:1002-1010`, `:897/1009`
- **why:** Override fetch/XHR/WS/ES không có restore; mỗi capture reload dashboard → operator mất state/unsaved work; nếu reload fail thì trang đóng băng (pendingReload mitigate).
- **suggested_fix:** Thêm `unfreezePageInMainWorld()` (restore hàm gốc); chỉ reload khi unfreeze thất bại.

#### EXT-005 — Token plaintext trong popup + storage
- **area:** security · **evidence:** `chrome-extension/popup.html:140`, `popup.js:107`
- **why:** `type="text"` dễ bị soi vai; lưu plaintext trong `chrome.storage.local`. Rủi ro thấp (loopback).
- **suggested_fix:** `type="password"` + toggle; cân nhắc `chrome.storage.session`.

#### BND-002 — Test/mock vẫn yêu cầu chọn cửa sổ Chrome (UX)
- **area:** boundary · **evidence:** `background.js:932-941` ; `server.py:852-1203`
- **why:** Mock data không cần scrape nhưng vẫn bị chặn "Chưa chọn cửa sổ Chrome mục tiêu" vì screenshot luôn được chụp. Đúng kỹ thuật, dễ gây nhầm.
- **suggested_fix:** Message riêng cho chế độ test, hoặc cho test caption-only khi chưa chọn cửa sổ.

### INFO

- **SRV-005** `_wa_send_started_at` chỉ ghi không đọc (`server.py:44,1095,1151,1168`) — dead state.
- **SRV-006** Secrets (token, SĐT, `tokens/`) plaintext cục bộ — đánh đổi chấp nhận được; log chỉ in prefix token (tốt).
- **SRV-007** Screenshot sau send-fail giữ lại đến retention 3 ngày (có thể chứa PII) — cố ý để debug.
- **SRV-008** Không test `server.py` (chỉ `caption_math`); `pytest` nằm trong runtime deps.
- **EXT-006** MV3 keep-alive bằng `getPlatformInfo()` mỗi 20s là heuristic — đã bù bằng watchdog + pre-schedule (residual risk).
- **EXT-007** Không test JS (scheduler, backoff, freeze) — logic 22h/23h dễ sai, chưa có test.
- **BND-003** Contract field/API khớp 100% (DC/AWS/TAP/F/M/DEG/TB1-12/TBS1-12, capture_id, force_22h, is_test, manual_intervention, `/api/status`, `/api/focus`, config keys) — verified, không drift.

---

## Coverage gaps

- Không chạy được runtime Flask/Win32/WPP_Whatsapp trong sandbox (môi trường Linux; repo là tool Windows). Toàn bộ nhận định phía Win32 (`NativeWindowLock`, `ImageGrab`, `ctypes.windll`) và WPP API surface (`ThreadsafeBrowser.run_threadsafe`, `page_evaluate`, `valid_chatId`, `fileToBase64`) là **static review** — khuyến nghị smoke-test trên Windows thật.
- `popup.css`/`content.css` chỉ skim (thuần styling, không phát hiện vấn đề chức năng).

## Next steps (gợi ý ưu tiên)

1. ~~SRV-001 · EXT-001 · BND-001~~ — **đã sửa** (xem Fix log).
2. Còn lại là nhóm **low/info** (chưa đụng theo yêu cầu "chỉ sửa 3 medium"):
   - Security: SRV-003 (tách token oracle khỏi `/api/status`), EXT-005 (`type="password"` cho token).
   - Cleanup dead code: SRV-005 (`_wa_send_started_at`), EXT-002 (`intervalMinutes`/`retryMinutes`).
   - Reliability nhỏ: SRV-004 (single-instance race), EXT-003 (`waitForTabComplete`), EXT-004 (unfreeze thay vì reload).
3. Bổ sung test: `parse_number`/`validate_recipient`/idempotency (Python) + scheduling/backoff (Node).
