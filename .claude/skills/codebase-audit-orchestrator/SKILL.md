---
name: codebase-audit-orchestrator
description: "Điều phối harness rà soát toàn bộ codebase screenshot-whatsapp-tool-v2 (fan-out 3 auditor → báo cáo). Bắt buộc dùng khi user nói: rà soát codebase, audit lỗi, review toàn bộ, tìm bug, kiểm tra lỗi, chạy audit, lại audit, cập nhật/bổ sung/cải thiện kết quả audit trước, audit lại phần server/extension/boundary. Không dùng cho sửa feature thường hoặc câu hỏi một file đơn giản — khi đó trả lời trực tiếp. Chỉ báo cáo, không tự sửa trừ khi user yêu cầu riêng sau."
---

# Codebase Audit Orchestrator

Điều phối 3 chuyên gia (sub-agent fan-out trên Cursor Task) để rà soát repo và xuất `AUDIT_REPORT.md`. Không sửa code ứng dụng trong lần chạy audit.

## Execution mode: Sub-agent (Cursor)

Claude Code `TeamCreate`/`SendMessage` không dùng được ở đây. Dùng `Task` song song + file trong `_workspace/`.

## Agent roster

| Agent | Def file | Task `subagent_type` | Skill | Output |
|-------|----------|----------------------|-------|--------|
| server-auditor | `.claude/agents/server-auditor.md` | `generalPurpose` | `audit-python-server` | `_workspace/02_server-auditor_findings.md` |
| extension-auditor | `.claude/agents/extension-auditor.md` | `generalPurpose` | `audit-chrome-extension` | `_workspace/02_extension-auditor_findings.md` |
| boundary-qa | `.claude/agents/boundary-qa.md` | `generalPurpose` | `audit-integration-boundaries` | `_workspace/02_boundary-qa_findings.md` |

Khi gọi Task: `model: claude-opus-5-thinking-high`. Prompt phải bảo agent **đọc** def + skill tương ứng trước khi audit.

## Finding schema (canonical)

Mọi finding trong raw + báo cáo cuối:

- `id` — prefix `SRV` / `EXT` / `BND` (sau dedupe có thể thêm `DUP-` note)
- `severity` — `critical` | `high` | `medium` | `low` | `info`
- `area` — `server` | `extension` | `boundary` | `security` | `reliability`
- `title`, `evidence`, `why`, `repro_or_trigger`, `suggested_fix`

## Workflow

### Phase 0: Context

1. Kiểm tra `_workspace/` tồn tại chưa
2. Nhánh:
   - **Không có** → initial run → Phase 1
   - **Có + user partial** (“chỉ audit lại server”, “bổ sung boundary”) → partial: chỉ spawn agent liên quan; giữ findings khác; synthesize lại
   - **Có + full re-run / input mới** → đổi tên `_workspace/` → `_workspace_YYYYMMDD_HHMMSS/`, rồi Phase 1

### Phase 1: Prepare

1. Tạo `_workspace/`
2. Ghi `_workspace/00_input/scope.md`: repo root, chế độ (full/partial), ghi chú user, timestamp
3. Nhắc: không sửa `server.py` / `chrome-extension/**` trong audit

### Phase 2: Fan-out

Trong **một** lượt, spawn tối đa 3 Task (full run) với `run_in_background` nếu hỗ trợ, hoặc parallel Tool calls:

Mỗi prompt gồm:
1. Đọc agent def + skill (boundary: thêm `references/boundary-checklist.md`)
2. Audit theo skill
3. Ghi đúng output path
4. Không sửa application code
5. Nếu partial + có file findings cũ: đọc và cải thiện

| Agent | Input | Output |
|-------|-------|--------|
| server-auditor | repo + skill | `_workspace/02_server-auditor_findings.md` |
| extension-auditor | repo + skill | `_workspace/02_extension-auditor_findings.md` |
| boundary-qa | repo + skill + checklist | `_workspace/02_boundary-qa_findings.md` |

### Phase 3: Fan-in synthesize

1. Read 3 findings files (hoặc subset partial)
2. Dedupe: cùng root cause từ 2 agent → giữ một, note sources trong `evidence`/`why`
3. Sort: critical → high → medium → low → info
4. Viết:
   - `_workspace/03_synthesized_findings.md` (raw merge)
   - `AUDIT_REPORT.md` ở **repo root** (báo cáo user-facing)

#### AUDIT_REPORT.md structure

```markdown
# Codebase audit report

- Date: ...
- Scope: full | partial (...)
- Agents: ok/failed list

## Summary
| Severity | Count |
|----------|-------|
| critical | N |
| ... | |

## Findings
### CRITICAL / HIGH / ...
(for each: id, title, area, evidence, why, repro, suggested_fix)

## Coverage gaps
(agents failed / areas not reviewed)

## Next steps
(gợi ý ưu tiên sửa — không implement trong audit)
```

### Phase 4: Wrap-up

1. Giữ `_workspace/` (không xóa)
2. Tóm tắt cho user: số finding theo severity + path `AUDIT_REPORT.md`
3. Hỏi ngắn: có muốn sửa theo priority không / có feedback harness không (không ép)

## Error handling

| Case | Strategy |
|------|----------|
| 1 agent fail | Retry 1 lần cùng prompt; vẫn fail → báo cáo ghi missing; tiếp tục synthesize phần còn |
| ≥2 agents fail | Báo user; vẫn xuất partial report nếu còn ≥1 file |
| Conflict findings | Giữ cả hai hoặc merge với note nguồn — không xóa thầm |
| Empty findings file | Treat as fail/retry |

## Data flow

```
Orchestrator → Task×3 (parallel)
    → _workspace/02_*_findings.md
    → Orchestrator Read + dedupe
    → AUDIT_REPORT.md + _workspace/03_synthesized_findings.md
```

## Test scenarios

### Happy path

1. User: “rà soát toàn bộ codebase xem có lỗi gì không”
2. Phase 0: không `_workspace/` → initial
3. Phase 1: tạo workspace + scope
4. Phase 2: 3 Task hoàn tất, 3 findings files
5. Phase 3: `AUDIT_REPORT.md` có summary + findings sorted
6. User nhận path báo cáo và counts

### Error path (one agent fails)

1. Phase 2: `extension-auditor` fail sau 1 retry
2. Orchestrator ghi gap trong Coverage gaps
3. Vẫn synthesize server + boundary findings
4. Report `status` phản ánh partial coverage — không pretend full audit

## Trigger notes

- Full keywords → full fan-out
- “chỉ server” / “chỉ extension” / “chỉ boundary” → partial
- “cải thiện kết quả trước” → đọc `_workspace/`, re-run agents liên quan
