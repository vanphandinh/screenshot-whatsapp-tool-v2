# Boundary checklist — screenshot-whatsapp-tool-v2

Dùng khi chạy `audit-integration-boundaries`. Đánh giá từng mục bằng cách đọc **cả hai phía**. Fail → finding `BND-*`.

## 1. Capture payload ↔ `/api/capture`

- [ ] Keys POST từ `chrome-extension/background.js` (hoặc nơi build body) khớp keys `server.py` đọc/validate
- [ ] Tên field giám sát: DC, AWS, TAP, F, M, DEG, TB1–TB12 — spelling/case đồng nhất hai phía
- [ ] Field phụ (phone override, test mode, flags) — server có bỏ qua hay reject?
- [ ] Missing/null/empty string: extension gửi gì; server trả status nào; extension xử lý ra sao
- [ ] Content-Type và JSON parse errors được handle hai phía

## 2. Immediate HTTP success ↔ async WhatsApp

- [ ] `/api/capture` trả 200 **trước** khi `send_whatsapp_async` hoàn tất?
- [ ] Extension có coi 200 = “đã gửi WA” không?
- [ ] Exception trong thread gửi: có log/tray/notify lại extension không?
- [ ] Client có poll `/api/status` hoặc cơ chế khác cho kết quả gửi không? (nếu không → finding reliability)

## 3. Focus & status APIs

- [ ] Extension gọi `/api/focus` với params server expect (nếu có)
- [ ] Response shape `/api/status` khớp chỗ extension đọc (health, ready, WA connected…)
- [ ] Timeout / server down: extension auto-disable và recover path

## 4. Scrape fields ↔ caption builder

- [ ] Output `EXTRACT_DATA` / content script keys = input caption math trong `server.py`
- [ ] Số vs string: parse hai phía có lệch không (dấu phẩy, đơn vị)
- [ ] Freeze MAIN world có làm extract đọc giá trị stale không (timing)

## 5. Config schema drift

- [ ] `config.json.example` keys ⊆ keys server thực sự đọc
- [ ] Popup / `chrome.storage` keys đồng bộ với server config (phone_number, test_phone_number, retention, session…)
- [ ] Default khi thiếu key: an toàn hay silent wrong recipient?

## 6. Security surface

- [ ] Flask bind host/port — chỉ loopback hay expose LAN?
- [ ] CORS: origins nào được phép; có cần thiết không?
- [ ] Auth/token trên `/api/capture` và `/api/focus` — thiếu thì severity theo bind address
- [ ] Path screenshots / tokens có lộ qua API không?

## 7. Scheduling handoff

- [ ] Job schedule (alarm) → capture → POST: partial failure có để lại state bẩn (`pendingReload`, flags) không
- [ ] 22h DEG / 23h fallback: extension và server có giả định khác nhau về “báo cáo đặc biệt” không

## Ghi chú severity nhanh

| Tình huống | Severity gợi ý |
|------------|----------------|
| Sai SĐT / gửi nhầm do config drift | critical |
| 200 OK nhưng WA không gửi, user không biết | high |
| Field TB thiếu → caption sai | medium–high |
| CORS mở + bind 127.0.0.1 only | medium / info |
| CORS mở + bind 0.0.0.0 | high–critical |
| Thiếu test tự động cho contract | info |
