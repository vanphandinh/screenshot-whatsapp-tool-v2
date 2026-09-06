# Plan triển khai — Ẩn tốc độ gió & công suất khi cả 12 TB dừng do gió thấp

> **Yêu cầu gốc:** Thêm điều kiện, trong trường hợp cả 12 turbin đều dừng do gió thấp, mà không phải lỗi hay bảo trì, thì không cần gửi thêm: tốc độ gió và công suất phát trong tin nhắn báo cáo.
> **Trạng thái plan:** ✅ ĐÃ CHỐT (2026-09-06) + ĐÃ CODE — chờ audit vòng lặp
> **Quyết định chốt (user 2026-09-06):**
> - ✅ Tách `build_caption()` ra `caption_math.py` để unit-test độc lập (PA2)
> - ✅ Strict 12: `active==0 && low_wind==12 && m==0 && f==0`
> - ✅ Giữ DEG cho báo cáo 22h/23h như cũ
> - ✅ Áp dụng cho cả `is_test` (live & test đồng nhất)
> - ✅ Thêm vòng lặp debug/audit vào plan và fix đến khi không còn lỗi nghiêm trọng
> **Branch:** `arena/01a076ec-screenshot-whatsapp-tool-v2`
> **Tác giả:** Agent Arena (khảo sát từ `server.py` + `caption_math.py` + `tests/test_caption_math.py` + `chrome-extension/background.js` + `popup.js`)

---

## 1. Tổng quan & mục tiêu

### 1.1. Vấn đề hiện tại
Caption báo cáo WhatsApp hiện luôn gắn đuôi cố định:

```
... tốc độ gió {AWS} m/s, công suất phát {TAP} MW.
[+ Sản lượng đầu cực ... MWh. nếu force_22h]
```

Bất kể farm ở trạng thái nào. Khi cả farm đứng yên vì gió thấp (đặc biệt ban đêm, TAP ≈ 0 MW, AWS ≈ 1–2 m/s), việc gửi thêm hai thông số này là dư thừa, gây nhiễu.

### 1.2. Mục tiêu thay đổi
Khi **đủ điều kiện “cả 12 TB đều dừng do gió thấp, không lẫn lỗi/bảo trì”** thì caption **rút gọn**, chỉ giữ:

```
BC BLĐ: Hiện tại 0 TB đang hoạt động, 12 TB dừng do tốc độ gió thấp.
[+ Sản lượng đầu cực ... nếu 22h/23h fallback]
```

Không gửi đoạn `tốc độ gió ... m/s, công suất phát ... MW.`

### 1.3. Ngoài phạm vi (không làm)
- Không thay đổi cách tính `m_eff / f_eff / low_wind / active` trong `caption_math.py` (giữ nguyên công thức TBS + MI 2-phase).
- Không thay đổi logic `AWS >= 6 fold low_wind vào active`.
- Không thay đổi hợp đồng `capture_id`, `force_22h`, `manual_intervention`, hay API `/api/capture`.
- Không thêm config toggle (hard-code theo quyết định).

---

## 2. Phân tích hiện trạng

### 2.1. Luồng liên quan

```
Extension (background.js#captureData)
  → POST /api/capture { DC, AWS, TAP, DEG, TB1..12, TBS1..12, manual_intervention, force_22h, capture_id, is_test }
    → server.py#capture()
      → parse_number() + validate DC/AWS/TAP + compute_caption_counts() [caption_math.py]
      → build caption (server.py ~ dòng 1048-1068 hiện tại)
      → screenshot + WPP send
```

### 2.2. Điểm sửa: khối build caption trong `server.py`

Trích trước khi sửa (rút gọn):

```python
# server.py ~1060 (cũ)
show_low_wind = (low_wind > 0) if mi_enabled else (low_wind > 0 and aws_num < 6)
caption = (
    f"BC BLĐ: Hiện tại {active} TB đang hoạt động, " +
    (f"{low_wind} TB dừng do tốc độ gió thấp, " if show_low_wind else "") +
    (f"{m_eff} TB dừng do đang bảo trì, " if m_eff > 0 else "") +
    (f"{f_eff} TB dừng do bị lỗi, " if f_eff > 0 else "") +
    f"tốc độ gió {aws_display} m/s, "
    f"công suất phát {tap_display} MW."
)
if force_22h and deg_display:
    caption += f" Sản lượng đầu cực đến thời điểm hiện tại đạt {deg_display} MWh."
```

`caption` là chuỗi duy nhất gửi kèm ảnh qua ` _send_whatsapp_image()`. Không có nơi nào khác render caption (extension chỉ log lại `serverResponse.caption`).

**Sau khi sửa (đã code):** logic build caption được tách ra `caption_math.build_caption()` + helper `is_all_low_wind()`.

### 2.3. Nguồn dữ liệu cho điều kiện

Sau `compute_caption_counts()` ta có:

| biến | ý nghĩa |
|------|---------|
| `active` | TB đang hoạt động (đã trừ inactive, đã fold low_wind nếu AWS >=6) |
| `low_wind` | TB dừng do gió thấp (phần còn lại của inactive sau khi trừ `m_eff`,`f_eff`) |
| `m_eff` | TB công suất <=0 + TBS ∈ {`service mode`, `hmi stop`} |
| `f_eff` | TB công suất <=0 + TBS ∈ {`fault stop`} |
| `aws_num`, `tap_num` | đã parse, dùng để hiển thị |

Tất cả đã chuẩn hóa qua `caption_math.py` (bao gồm cả MI 2-phase). Vì vậy **điều kiện mới đặt ngay sau khi có `counts`**, trước khi build caption.

---

## 3. Định nghĩa điều kiện kích hoạt — ĐÃ CHỐT

### 3.1. Diễn giải yêu cầu bằng chữ
> “Cả 12 turbin đều dừng do gió thấp, mà không phải lỗi hay bảo trì”

### 3.2. Quyết định chốt — Strict 12 (user xác nhận)

```python
# caption_math.is_all_low_wind()
def is_all_low_wind(*, active, low_wind, m_eff, f_eff) -> bool:
    return active == 0 and low_wind == 12 and m_eff == 0 and f_eff == 0
```

- Đúng chữ “cả 12”.
- Không phụ thuộc DC (DC phải =12 mới có thể ra `active==0 && low_wind==12`; nếu DC khác 12, điều kiện tự fail — an toàn).
- Bao phủ cả hai nhánh: legacy và MI.
- Áp dụng cho **cả live và test** (không phân biệt `is_test`).

### 3.3. Biến thể đã loại (ghi chú để sau này)

| # | Điều kiện | Lý do loại |
|---|-----------|------------|
| **B** | `active==0 && m==0 && f==0 && low_wind>0` | Linh hoạt hơn nhưng lệch chữ “12”, không theo yêu cầu chốt |
| **C** | `low_wind==12 && m==0 && f==0` (bỏ active) | Rủi ro caption “0 TB” nhưng active không 0 do bug |

Helper `is_all_low_wind()` bọc logic để sau này chỉ cần đổi 1 dòng nếu muốn B/C.

### 3.4. Tương tác với Manual Intervention (MI) và AWS fold

- **Legacy (mi_enabled=False):** `low_wind = inactive - m - f`; nếu `AWS >=6`, `low_wind` được fold vào `active`. Khi `low_wind==12`, `AWS` chắc chắn `<6` (gió thấp), nên không bị fold — `active` vẫn 0.
- **MI (mi_enabled=True):** `low_wind = low_wind_1 (phase1 còn lại sau fold) + n_low (phase2 override)`. Chỉ `low_wind_1` mới bị fold khi `AWS>=6`; `n_low` (override) **không bao giờ** bị fold. Trường hợp 12 TB đều override `low_wind` → `active==0, low_wind==12` → vẫn thỏa mãn điều kiện.
- **Kết luận:** Dùng giá trị **cuối cùng** từ `compute_caption_counts()` (đã tính MI + fold) để đánh giá điều kiện.

### 3.5. DEG — ĐÃ CHỐT: giữ nguyên

Yêu cầu chỉ nói ẩn “tốc độ gió và công suất phát”. **DEG** giữ nguyên:

```python
if force_22h and deg_display:
    caption += f" Sản lượng đầu cực đến thời điểm hiện tại đạt {deg_str} MWh."
```

Tức khi `is_all_low_wind == True` **và** `force_22h == True`, caption sẽ là 2 câu: câu đầu rút gọn + câu DEG. Đã có test `test_build_caption_all_low_wind_keeps_deg_when_force22h`.

---

## 4. Phương án triển khai — ĐÃ CHỌN PA2 (tách build_caption)

### 4.1. PA2 — Tách hàm `build_caption()` ra `caption_math.py` (ĐÃ IMPLEMENT)

```python
# caption_math.py (đã code)

CAPTION_PREFIX = "BC BLĐ: Hiện tại"
DEG_SUFFIX_TEMPLATE = " Sản lượng đầu cực đến thời điểm hiện tại đạt {deg} MWh."

def _format_one_decimal(value: float) -> str:
    return f"{float(value):.1f}".rstrip("0").rstrip(".")

def is_all_low_wind(*, active: int, low_wind: int, m_eff: int, f_eff: int) -> bool:
    return active == 0 and low_wind == 12 and m_eff == 0 and f_eff == 0

def build_caption(
    *,
    active: int, low_wind: int, m_eff: int, f_eff: int,
    aws_num: float, tap_num: float,
    deg_display: str | None = None,
    force_22h: bool = False,
    mi_enabled: bool = False,
) -> str:
    aws_display = _format_one_decimal(aws_num)
    tap_display = _format_one_decimal(tap_num)
    show_low_wind = (low_wind > 0) if mi_enabled else (low_wind > 0 and float(aws_num) < 6)
    if is_all_low_wind(active=active, low_wind=low_wind, m_eff=m_eff, f_eff=f_eff):
        caption = f"{CAPTION_PREFIX} {active} TB đang hoạt động, {low_wind} TB dừng do tốc độ gió thấp."
    else:
        caption = (
            f"{CAPTION_PREFIX} {active} TB đang hoạt động, "
            + (f"{low_wind} TB dừng do tốc độ gió thấp, " if show_low_wind else "")
            + (f"{m_eff} TB dừng do đang bảo trì, " if m_eff > 0 else "")
            + (f"{f_eff} TB dừng do bị lỗi, " if f_eff > 0 else "")
            + f"tốc độ gió {aws_display} m/s, "
            + f"công suất phát {tap_display} MW."
        )
    deg_str = (deg_display or "").strip()
    if force_22h and deg_str:
        caption += f" Sản lượng đầu cực đến thời điểm hiện tại đạt {deg_str} MWh."
    return caption
```

`server.py` chỉ gọi:

```python
from caption_math import build_caption, is_all_low_wind

caption = build_caption(
    active=active, low_wind=low_wind, m_eff=m_eff, f_eff=f_eff,
    aws_num=aws_num, tap_num=tap_num,
    deg_display=deg_display, force_22h=force_22h,
    mi_enabled=mi_enabled,
)
aws_display = f"{aws_num:.1f}".rstrip('0').rstrip('.')
tap_display = f"{tap_num:.1f}".rstrip('0').rstrip('.')
log(f"Caption: {caption}", "SUCCESS")
if is_all_low_wind(active=active, low_wind=low_wind, m_eff=m_eff, f_eff=f_eff):
    log("Caption rút gọn do 12 TB gió thấp (ẩn AWS/TAP) — áp dụng cho cả live/test", "INFO")
```

**Ưu điểm:** Pure function, unit-test không cần Flask/WPP; tái sử dụng.
**Đã implement:** `caption_math.py` 11051 byte, `server.py` đã import và replace khối cũ.

### 4.2. PA1 đã loại

Sửa inline trong `server.py` không tách hàm — khó test, đã không chọn.

---

## 5. Chi tiết thay đổi (file-level) — ĐÃ CODE

| File | Thay đổi | Trạng thái |
|------|----------|------------|
| **`caption_math.py`** | Thêm `CAPTION_PREFIX`, `DEG_SUFFIX_TEMPLATE`, `_format_one_decimal()`, `is_all_low_wind()`, `build_caption()` | ✅ Done, `py_compile` OK |
| **`server.py`** | `from caption_math import build_caption, is_all_low_wind`; replace khối caption cũ (~18 dòng) bằng call `build_caption()` + log INFO khi rút gọn | ✅ Done, `py_compile` OK |
| **`tests/test_build_caption.py`** | **Mới** — 19 tests cho `is_all_low_wind` + `build_caption` (strict 12, DEG, MI, AWS fold, formatting, is_test) | ✅ Done, 19 passed |
| **`tests/test_caption_math.py`** | Giữ nguyên 26 tests cũ (không đụng) | ✅ 26 passed, tổng 45 passed |
| **`chrome-extension/popup.js`** | Không đổi (không cần scenario mới cho logic server; có thể thêm sau) | — |
| **`chrome-extension/background.js`** | Không đổi | `node --check` OK |
| **Docs** | Cập nhật plan này + sẽ thêm CHANGELOG | ✅ |

**Không đổi:** `config.json.example`, `requirements.txt`, `manifest.json`, `content.js`, `ws-tracker.js`.

---

## 6. Ví dụ caption trước & sau (đã verify bằng test)

### 6.1. Case kích hoạt (12 TB gió thấp, không lỗi/bảo trì) — ĐÃ CHỐT

Input:
```
DC=12, AWS=1.9, TAP=0, TB1..12=0, TBS1..12="Warning Character Code"
→ counts: active=0, low_wind=12, m=0, f=0
```

| | Caption |
|---|---------|
| **Trước** | `BC BLĐ: Hiện tại 0 TB đang hoạt động, 12 TB dừng do tốc độ gió thấp, tốc độ gió 1.9 m/s, công suất phát 0 MW.` |
| **Sau (đã code)** | `BC BLĐ: Hiện tại 0 TB đang hoạt động, 12 TB dừng do tốc độ gió thấp.` |
| **Sau + force_22h (DEG=72.3)** | `BC BLĐ: Hiện tại 0 TB đang hoạt động, 12 TB dừng do tốc độ gió thấp. Sản lượng đầu cực đến thời điểm hiện tại đạt 72.3 MWh.` |

Test: `test_build_caption_all_low_wind_12_rut_gon`, `test_build_caption_all_low_wind_keeps_deg_when_force22h` — **pass**.

### 6.2. Case không kích hoạt (11 gió thấp + 1 bảo trì)

```
TB11=0 TBS11="Service mode" → m=1, low_wind=11, active=0
```
Caption giữ nguyên đầy đủ (có AWS/TAP) vì `m_eff !=0`:
`BC BLĐ: Hiện tại 0 TB đang hoạt động, 11 TB dừng do tốc độ gió thấp, 1 TB dừng do đang bảo trì, tốc độ gió 2.1 m/s, công suất phát 0 MW.`
Test: `test_build_caption_not_trigger_when_one_maint` — **pass**.

### 6.3. Case MI: 12 TB override low_wind

```
mi_enabled=True, turbines={1..12: low_wind}, AWS=5.0
→ active=0, low_wind=12, m=0, f=0 → kích hoạt rút gọn
```
Test: `test_build_caption_all_low_wind_with_mi` — **pass**.

---

## 7. Ma trận kiểm thử — ĐÃ PASS 45/45

### 7.1. Unit test (pytest) — `tests/test_build_caption.py` (19) + `tests/test_caption_math.py` (26)

| # | Test name | Input | Kỳ vọng | Status |
|---|-----------|-------|---------|--------|
| 1 | `test_is_all_low_wind_true` | 0/12/0/0 | True | ✅ |
| 2 | `test_is_all_low_wind_false_when_one_maint` | 0/11/1/0 | False | ✅ |
| 3 | `test_is_all_low_wind_false_when_one_fault` | 0/11/0/1 | False | ✅ |
| 4 | `test_is_all_low_wind_false_when_one_active` | 1/11/0/0 | False | ✅ |
| 5 | `test_is_all_low_wind_false_when_11_low` | 0/11/0/0 | False | ✅ |
| 6 | `test_build_caption_all_low_wind_12_rut_gon` | 0/12/0/0, AWS 1.9 | rút gọn, không m/s, không công suất | ✅ |
| 7 | `test_build_caption_all_low_wind_keeps_deg_when_force22h` | như trên + DEG 72.3 force | rút gọn + DEG | ✅ |
| 8 | `test_build_caption_all_low_wind_deg_not_added_without_force` | như trên DEG nhưng force False | không DEG | ✅ |
| 9 | `test_build_caption_all_low_wind_with_mi` | MI 12 low | rút gọn | ✅ |
| 10 | `test_build_caption_normal_keeps_wind_and_power` | 12/0/0/0 | đầy đủ | ✅ |
| 11 | `test_build_caption_not_trigger_when_one_maint` | 0/11/1/0 | đầy đủ + bảo trì | ✅ |
| 12 | `test_build_caption_not_trigger_when_one_fault` | 0/11/0/1 | đầy đủ + lỗi | ✅ |
| 13 | `test_build_caption_not_trigger_when_11_low_1_active` | 1/11/0/0 | đầy đủ + 1 active | ✅ |
| 14 | `test_build_caption_low_wind_hidden_when_aws_high_legacy` | 12/3/0/0 AWS 7.5 | ẩn low_wind, vẫn gió | ✅ |
| 15 | `test_build_caption_mi_low_wind_shown_even_when_aws_high` | MI 1 low AWS 7 | hiện low_wind | ✅ |
| 16 | `test_build_caption_force22h_appends_deg` | 7/2/2/1 + DEG 80.4 force | có DEG | ✅ |
| 17 | `test_build_caption_empty_deg_not_appended` | như trên DEG rỗng | không DEG | ✅ |
| 18 | `test_build_caption_formatting_strips_trailing_zero` | 5.0→5, 10.0→10 | format đúng | ✅ |
| 19 | `test_build_caption_is_test_same_as_live` | so sánh 2 call cùng input | identical | ✅ |
| 20–45 | 26 tests cũ `test_caption_math.py` | — | — | ✅ |

**Lệnh chạy:**
```bash
python -m pytest tests/test_build_caption.py tests/test_caption_math.py -v
# 45 passed in 0.10s
```

### 7.2. Tích hợp (server.py mock, không cần WPP)

- Mock `compute_caption_counts` → `build_caption` tích hợp: đã cover qua các test count + caption.
- Cần thêm 1 test E2E nhỏ: `POST /api/capture` với payload 12 TB gió thấp (live và is_test) → kỳ vọng `response.json.caption` rút gọn. Đã lên kế hoạch trong vòng audit (mục 10.3).

### 7.3. Thủ công (popup + extension)

1. Mở `popup.html` → tab Test → dùng scenario mới (hoặc mock thủ công).
2. Gửi test (nút Test, `is_test=true`) → kiểm tra caption rút gọn.
3. Kiểm tra log server có dòng `Caption rút gọn do 12 TB gió thấp`.
4. Thử 22h: `force_22h=true` + DEG → đảm bảo DEG vẫn hiện sau câu rút gọn.

### 7.4. Hồi quy

`pytest` 45 passed, `py_compile` OK, `node --check` 3 file OK.

---

## 8. Rủi ro & giảm thiểu — Cập nhật sau chốt

| Rủi ro | Mức | Giảm thiểu (đã làm) |
|--------|-----|---------------------|
| **Hiểu nhầm “12” vs “tất cả”** | Đã chốt | Strict 12, helper 1 dòng, test 5 case is_all_low_wind |
| **Câu cú tiếng Việt khi rút gọn** | Thấp | Mẫu cố định `... 12 TB dừng do tốc độ gió thấp.` (đã test `endswith "."`, không thừa `,`) |
| **DEG mất theo** | Thấp | Nhánh `if force_22h` ngoài `is_all_low_wind`, test 2 case DEG |
| **AWS fold làm sai điều kiện** | Thấp | Dùng `counts` cuối cùng (đã fold), test `aws_high_legacy` và `mi_low_wind_shown` |
| **MI override làm low_wind “ảo”** | Thấp | Dùng `counts` cuối, test `with_mi` |
| **Extension preview lệch server** | Thấp | Extension không tự build caption, chỉ hiển thị server trả về |
| **Log/monitoring** | Thấp | Đã thêm log INFO khi rút gọn (cả live/test) |

---

## 9. Câu hỏi PO — ĐÃ TRẢ LỜI (2026-09-06)

1. **Có phải đúng 12 mới ẩn?** → ✅ **Đúng 12** (strict).
2. **Có giữ DEG không?** → ✅ **Giữ DEG** cho 22h/23h.
3. **Có áp dụng cho cả `is_test` không?** → ✅ **Có** (đồng nhất).
4. **Có cần config toggle không?** → ✅ **Không**, hard-code.
5. **Có cần tách `build_caption()` không?** → ✅ **Có**, đã tách và test độc lập.

---

## 10. Kế hoạch triển khai — Cập nhật sau chốt + Vòng lặp Debug/Audit

### 10.1. Đã hoàn thành (2026-09-06)

- [x] Khảo sát repo, viết plan v1
- [x] PO chốt 4 quyết định (strict 12, giữ DEG, cả is_test, tách build_caption)
- [x] Code `caption_math.py`: thêm `is_all_low_wind()` + `build_caption()` + `_format_one_decimal()`
- [x] Sửa `server.py`: import + replace khối caption + log INFO
- [x] Viết `tests/test_build_caption.py` (19 tests)
- [x] Chạy `pytest 45 passed`, `py_compile` OK, `node --check` OK

### 10.2. Vòng lặp Debug / Audit (YÊU CẦU MỚI — lặp đến khi không còn lỗi nghiêm trọng)

> **Định nghĩa “lỗi nghiêm trọng”:** `critical` hoặc `high` theo thang AUDIT_REPORT (mất tin, sai số, crash, bảo mật). `medium/low/info` được ghi nhận nhưng không chặn merge nếu có mitigation.

**Quy trình 1 vòng lặp (dự kiến 0.5–1h/vòng):**

```mermaid
Audit → Phân loại (critical/high/medium/low) → Fix critical/high → Re-test (pytest + py_compile + node --check) → Audit lại → Lặp
```

**Checklist audit mỗi vòng (áp dụng cho thay đổi này):**

| Nhóm | Kiểm tra | Công cụ | Ngưỡng chặn |
|------|----------|---------|-------------|
| **Logic caption** | Strict 12 có bỏ sót? Có ẩn nhầm khi m/f!=0? DEG có mất? | `pytest test_build_caption` + review `build_caption` | High: sai caption |
| **Tích hợp** | `compute_caption_counts` → `build_caption` có khớp? `is_test` có phân biệt? | Mock payload 12 TB (live/test) | High: thiếu is_test |
| **Hồi quy** | 26 tests cũ có fail? | `pytest test_caption_math` | Critical: fail hồi quy |
| **Syntax** | `py_compile`, `node --check` | CLI | Critical: compile fail |
| **Biên** | AWS fold, MI, DEG rỗng, format 5.0→5 | Test 14,15,18 | Medium: format sai |
| **Vận hành** | Log rút gọn có xuất hiện? Screenshot vẫn gửi? | Manual + log check | Low |

**Tiêu chí thoát vòng lặp:**
- `pytest` 45 passed, `py_compile` OK, `node --check` OK
- Không còn finding `critical`/`high`
- Các `medium` còn lại phải có mitigation ghi trong plan hoặc issue riêng

**Lịch sử vòng lặp (ghi liên tục):**

| Vòng | Ngày | Audit bởi | Findings | Fix | Re-test |
|------|------|-----------|----------|-----|---------|
| 1 | 2026-09-06 | Agent Arena (static + pytest) | 0 critical/high; 3 low +1 info (xem 10.3) | Ghi nhận 3 low, plan mitigation | 45 passed, py_compile OK |
| 1.1 | 2026-09-06 | Agent Arena (fix) | AUDIT-01 low: template chưa dùng | Fix dùng `DEG_SUFFIX_TEMPLATE.format(...)` | 45 passed, py_compile OK, node --check OK |
| 2 | (dự kiến nếu cần E2E) | … | … | … | … |

### 10.3. Audit vòng 1 — Kết quả (2026-09-06, static + pytest)

**Phạm vi:** `caption_math.py` + `server.py` + `tests/test_build_caption.py`

**Findings:**

| ID | Mức | Mô tả | Trạng thái |
|----|-----|-------|------------|
| **AUDIT-01** | low | `DEG_SUFFIX_TEMPLATE` hằng số chưa dùng (chỉ dùng f-string trực tiếp) | ✅ **Đã fix (2026-09-06 vòng 1.1):** đổi `build_caption` sang `DEG_SUFFIX_TEMPLATE.format(deg=deg_str)` — đã re-test 45 passed |
| **AUDIT-02** | low | `build_caption` nhận `deg_display: str\|None` nhưng server truyền `deg_display` đã strip? Đã handle `(deg_display or "").strip()` | ✅ Đã fix trong code, test `empty_deg_not_appended` pass |
| **AUDIT-03** | low | `is_all_low_wind` không check `DC` — nếu DC !=12 nhưng active==0 low==12 có thể xảy ra không? Không, vì active = DC - inactive; nếu DC=10 thì inactive max 12 → active âm → đã reject trước đó (`INCONSISTENT_COUNTS`). Nên an toàn. | ✅ Ghi chú trong plan 3.2, không cần code |
| **AUDIT-04** | info | Chưa có test E2E `POST /api/capture` với Flask test client | **Plan:** thêm `tests/test_server_caption_e2e.py` dùng `app.test_client()` mock screenshot/WA (vòng 2 nếu cần) |

**Kết luận vòng 1:** **Không còn lỗi nghiêm trọng (0 critical/high).** Đủ điều kiện thoát vòng lặp nếu PO chấp nhận 3 low trên. Nếu muốn triệt để, chạy vòng 2 với E2E test.

### 10.4. Bước tiếp theo sau audit

- [ ] **Vòng 2 (optional, nếu team muốn E2E):** Viết `tests/test_server_caption_e2e.py` — mock `take_fullscreen_screenshot` + `whatsapp_client` → POST payload 12 TB (live + test + force_22h) → assert caption.
- [ ] Self-review diff (<80 dòng `caption_math.py` + <15 dòng `server.py` + 1 file test mới).
- [ ] Tạo PR `arena/01a076ec... → main`, gắn plan này + log `45 passed`.
- [ ] Merge sau CI pass.
- [ ] Deploy `server.py` + `caption_math.py` lên máy Windows vận hành, restart service.
- [ ] Theo dõi log 24h: tìm `Caption rút gọn do 12 TB gió thấp`.

### 10.5. Giám sát sau deploy (vận hành)

- [ ] Theo dõi log 24h đầu: `grep "Caption rút gọn"` 
- [ ] Kiểm tra báo cáo WhatsApp thực tế khi gặp đêm gió thấp (nếu chưa gặp, giả lập bằng test mock `is_test=true`).
- [ ] Nếu phát hiện caption sai, rollback bằng cách revert `is_all_low_wind` → `return False` (1 dòng).

---

## 11. Phụ lục — Vị trí code tham chiếu (sau code)

- `caption_math.py:14–15` — hằng `CAPTION_PREFIX`, `DEG_SUFFIX_TEMPLATE`
- `caption_math.py:90–135` — `_format_one_decimal()`, `is_all_low_wind()`, `build_caption()` (mới)
- `caption_math.py:140–305` — `compute_caption_counts()` (không đổi, chỉ đọc)
- `server.py:12–16` — import `build_caption`, `is_all_low_wind`
- `server.py:1045–1068` — call `build_caption()` + log rút gọn (mới, thay thế khối inline cũ)
- `tests/test_build_caption.py` — 19 tests mới (strict 12, DEG, MI, AWS fold, format, is_test)
- `tests/test_caption_math.py` — 26 tests cũ (giữ nguyên)
- `chrome-extension/background.js:1590+` — `TEST_WITH_DATA` (không đổi)
- `chrome-extension/popup.js:50–90` — `TEST_SCENARIOS` (không đổi, có thể thêm `all_low_wind` sau)

---

## 12. Checklist trước khi merge (cập nhật)

- [x] Đã chốt strict 12 vs tổng quát (3.2) — **strict 12**
- [x] Đã chốt có giữ DEG không (3.5) — **giữ DEG**
- [x] Đã chọn PA1 hay PA2 (4) — **PA2 (tách build_caption) — done**
- [x] Đã thống nhất format caption rút gọn (6.1) — **đã test**
- [x] Đã chốt áp dụng cho cả is_test — **có**
- [x] Đã chạy pytest 45 passed + py_compile + node --check
- [x] Đã audit vòng 1 — 0 critical/high
- [ ] (Optional) Vòng 2 E2E test nếu team yêu cầu
- [ ] PO duyệt plan cập nhật này

> Sau khi tick, merge PR và deploy. Vòng lặp audit sẽ tiếp tục sau deploy (giám sát log 24h) đến khi không còn lỗi nghiêm trọng.

