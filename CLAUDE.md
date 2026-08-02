## Harness: Codebase Audit

**Mục tiêu:** Rà soát toàn bộ codebase (extension + Flask server + biên giới tích hợp) và xuất báo cáo findings — không tự sửa trừ khi user yêu cầu riêng.

**Trigger:** Khi user yêu cầu rà soát / audit / review toàn bộ / tìm bug / kiểm tra lỗi codebase (kể cả chạy lại, cập nhật, bổ sung, cải thiện kết quả audit trước), dùng skill `codebase-audit-orchestrator`. Câu hỏi đơn giản về một chỗ trong code có thể trả lời trực tiếp.

**Thay đổi lịch sử:**

| Ngày | Thay đổi | Đối tượng | Lý do |
|------|----------|-----------|-------|
| 2026-08-02 | Khởi tạo harness | toàn bộ | Fan-out 3 auditor + báo cáo |
