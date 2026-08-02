# Codebase audit report (post-fix verify)

- **Date:** 2026-08-02
- **Scope:** fix critical/high/medium → re-verify với model nhẹ (`composer-2.5-fast`)
- **Agents verify:** [server](977a6dc3-4dfd-439d-92b3-89a8d481d9c0) · [extension](402e204d-5f28-4b7e-b114-7bc080264a36) · [boundary](d1471ab1-52f9-4bb8-a63e-0e962fa6b5ad)
- **Raw:** `_workspace/02_*-findings.md`

## Verdict

| Severity | Trước (full opus) | Sau fix + verify |
|----------|-------------------|------------------|
| critical | 1 (TDZ) | **0** (đã sửa; regression `import os` cũng đã vá) |
| high | 6 | **0–2** (boundary còn tranh luận; xem dưới) |
| medium | nhiều | **~3–6** còn lại (screenshot overlay, freeze UX, ws-tracker, picker…) |

## Đã xác nhận FIXED (headline)

- EXT-001 TDZ `isTest`
- SRV-001 / BND-002 ack unverified → `200` + `wa_verified:false` (không retry trùng)
- EXT-002 / BND-003 DEG fallback + `isTestMode`; debug không schedule live DEG
- EXT-003 không arm +1 phút lúc 22h với lịch hourly
- SRV-002 health loop không re-init khi đang send
- SRV-003 / BND-007 `api_token` example rỗng + regenerate placeholder
- EXT-004 token/URL từ DOM khi Save
- EXT-005–009, 012, 014; SRV-004/005/007/009/010; BND-006 placeholders
- Regression **`import os` thiếu** (do refactor verify) → **đã thêm lại**, `import server` OK

## Còn lại (chấp nhận / backlog)

| Id | Severity | Ghi chú |
|----|----------|---------|
| SRV-006 | medium | Screenshot screen-region / overlay — cần PrintWindow (lớn) |
| SRV-008 | medium | Wedged Playwright thread leak |
| EXT-010 | medium | `ws-tracker` trên `<all_urls>` |
| EXT-011 | medium | Picker selector brittle |
| EXT-013 | medium | `willCurrentScheduleHit22` parity |
| BND freeze | medium | Freeze trong lúc chờ WA (cần tách API) |
| BND DEG + wa_verified | note | Mark DEG khi queue unverified là **cố ý** để tránh trùng tin thật |

## Next steps

1. Reload extension + restart server, smoke “Chạy ngay (dữ liệu thật → nhóm test)”.
2. Backlog medium còn lại khi có thời gian (PrintWindow / tách freeze-send / narrow ws-tracker).
