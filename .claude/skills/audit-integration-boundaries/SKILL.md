---
name: audit-integration-boundaries
description: "QA biên giới extension↔Flask server: đối chiếu POST payload với /api/capture, HTTP 200 vs WhatsApp async, config schema drift, CORS/auth localhost. Bắt buộc dùng khi đóng vai boundary-qa hoặc khi nghi ngờ lỗi 'extension báo OK nhưng WhatsApp không gửi' / field mismatch. Đọc hai phía cùng lúc. Chỉ findings. Không thay full-repo orchestrator."
---

# Audit Integration Boundaries

Boundary bugs qua build/review từng phía riêng. Skill này buộc **đọc đồng thời** producer và consumer.

## Why

Trong repo này, capture flow cắt qua extension → HTTP → screenshot → async WhatsApp. Mỗi lớp có thể “đúng cục bộ” mà vẫn sai ở điểm nối (shape, timing, config keys).

## Workflow

1. Đọc `references/boundary-checklist.md` — làm hết mục, đánh dấu pass/fail trong notes nội bộ rồi chuyển thành findings
2. Với mỗi boundary: mở file hai phía, extract shape/keys, so sánh
3. Đặc biệt truy vết nhánh success/error từ extension đến server response và thread gửi WA
4. Ghi `_workspace/02_boundary-qa_findings.md`

## Core comparisons

1. **Payload ↔ validation** — keys DC, AWS, TAP, F, M, DEG, TB1–TB12 (và field phụ); required vs optional
2. **Immediate vs async** — extension tin HTTP 200; `send_whatsapp_async` fail im lặng?
3. **Focus/status APIs** — query params / response JSON extension expect
4. **Config** — `config.json.example` ↔ server load ↔ popup storage keys
5. **Security surface** — ai gọi được `/api/capture` trên máy local

## Finding schema

| Field | Rule |
|-------|------|
| id | `BND-NNN` |
| severity | critical / high / medium / low / info |
| area | boundary / security / reliability |
| title | một dòng |
| evidence | **hai phía** khi là contract: `ext:line ; server:line` |
| why | mismatch gây gì |
| repro_or_trigger | bước tái hiện |
| suggested_fix | align phía nào — không code |

Wrong field name / silent WA fail sau 200 → high/critical. CORS open trên loopback → medium/high tùy bind address.

## Output

Frontmatter `agent: boundary-qa`, `status`, `finding_count`. Checklist item fail → ít nhất một finding (hoặc gom nếu cùng root cause, ghi rõ trong `why`).

## Do not

- Sửa code hai phía
- Chỉ review một phía rồi kết luận “OK”
- Duplicate deep Win32/caption math (server-auditor) trừ khi contract field sai
