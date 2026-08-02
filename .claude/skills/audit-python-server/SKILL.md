---
name: audit-python-server
description: "Rà soát server.py Flask/Win32/WhatsApp của screenshot-whatsapp-tool: API validation, screenshot lock, caption math, async send, tray/config, secrets. Bắt buộc dùng khi đóng vai server-auditor hoặc khi user hỏi lỗi phía Python server /api/capture. Không dùng để sửa code — chỉ findings. Không thay orchestrator toàn repo."
---

# Audit Python Server

Hướng dẫn cách server-auditor tìm lỗi phía Python một cách có hệ thống. Static review vì repo không có pytest/CI — evidence từ mã nguồn phải đủ mạnh để tái hiện.

## Why this skill

Server ghép nhiều surface (HTTP + Win32 UI automation + Playwright WhatsApp). Lỗi thường nằm ở race và “success sớm” chứ không ở syntax. Checklist buộc đi qua từng lớp thay vì chỉ skim routes.

## Workflow

1. Đọc `requirements.txt`, `config.json.example`, `setup_env.bat` / `run_server.bat` để nắm runtime assumptions
2. Đọc `server.py` theo lớp (không skim một lượt rồi kết luận):
   - Config load/save & defaults
   - Window focus / NativeWindowLock
   - `/api/*` handlers
   - Caption / field math
   - WhatsApp init + async send
   - Tray / shutdown / cleanup
3. Với mỗi nghi ngờ: ghi finding đủ schema; bỏ qua style-only trừ khi gây bug
4. Ghi `_workspace/02_server-auditor_findings.md`

## Focus areas

### API & validation

Kiểm tra thiếu field, type coercion, empty strings, số âm. Response shape có ổn định khi lỗi một phần không.

### Win32 & screenshot

Focus HWND sai, lock không release khi exception, maximize/TopMost ảnh hưởng desktop user, path screenshot fail giữa chừng.

### Caption math

Logic active / low-wind / F / M / DEG / TB1–TB12. Edge: thiếu key, parse fail, AWS ngưỡng, báo cáo 22h.

### WhatsApp reliability

Session/token path, QR, gửi sau khi HTTP đã 200, exception trong thread không báo client, quit/kill browser.

### Security / ops

Bind `127.0.0.1` vs `0.0.0.0`, CORS mở, phone trong config, retention screenshots, logging PII.

## Finding schema

Mỗi finding:

| Field | Rule |
|-------|------|
| id | `SRV-NNN` tuần tự |
| severity | critical / high / medium / low / info |
| area | server / security / reliability |
| title | một dòng |
| evidence | `server.py:line` (hoặc file liên quan) |
| why | impact nếu xảy ra |
| repro_or_trigger | điều kiện kích hoạt |
| suggested_fix | gợi ý ngắn — **không implement** |

Severity gợi ý: data loss / wrong WhatsApp recipient / unlock fail → high+; edge caption → medium; missing tests → info.

## Output

Viết đúng path `_workspace/02_server-auditor_findings.md` với frontmatter `agent`, `status`, `finding_count`. Nếu zero bugs: `finding_count: 0` và mục “No issues found” + residual risks ngắn.

## Do not

- Sửa `server.py` hoặc dependencies
- Audit sâu extension (trừ cite tối thiểu)
- Bịa line numbers — đọc lại file trước khi ghi evidence
