"""Caption count math + manual intervention parsing (no Flask deps).

M (bảo trì) và F (lỗi) được suy ra trực tiếp từ dữ liệu scrape:
  M = số TB có công suất <= 0 và TBS ∈ {service mode, hmi stop}
  F = số TB có công suất <= 0 và TBS ∈ {fault stop}
Không còn dùng giá trị F/M scrape từ dashboard (đã bỏ 2026-09).
"""

MAINT_TBS = frozenset({"service mode", "hmi stop"})
FAULT_TBS = frozenset({"fault stop"})
MI_STATUSES = frozenset({"normal", "maintenance", "error", "low_wind"})

# Caption formatting helpers (pure, no Flask deps)
CAPTION_PREFIX = "BC BLĐ: Hiện tại"
DEG_SUFFIX_TEMPLATE = " Sản lượng đầu cực đến thời điểm hiện tại đạt {deg} MWh."


class CaptionMathError(Exception):
    """Caption / manual-intervention validation failure (maps to HTTP 400)."""

    def __init__(self, message, error_code="INCONSISTENT_COUNTS", fields=None):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.fields = fields


def _norm_tbs(s):
    return " ".join(str(s).replace("\u00a0", " ").split()).casefold()


def parse_manual_intervention(raw):
    """
    Parse payload manual_intervention.
    Returns (enabled: bool, turbines: dict[int, str]).
    Raises CaptionMathError on invalid shape.
    """
    if raw is None:
        return False, {}
    if not isinstance(raw, dict):
        raise CaptionMathError(
            "manual_intervention must be an object",
            error_code="INVALID_MANUAL_INTERVENTION",
        )
    # Strict: only real JSON boolean true enables MI (avoids truthy strings)
    if raw.get("enabled") is not True:
        return False, {}
    turbines_raw = raw.get("turbines", {})
    if turbines_raw is None:
        turbines_raw = {}
    if not isinstance(turbines_raw, dict):
        raise CaptionMathError(
            "manual_intervention.turbines must be an object",
            error_code="INVALID_MANUAL_INTERVENTION",
        )
    turbines = {}
    for key, status in turbines_raw.items():
        key_s = str(key).strip()
        if not key_s.isdigit():
            raise CaptionMathError(
                f"Invalid turbine key: {key!r}",
                error_code="INVALID_MANUAL_INTERVENTION",
            )
        idx = int(key_s)
        if idx < 1 or idx > 12:
            raise CaptionMathError(
                f"Turbine index out of range (1..12): {idx}",
                error_code="INVALID_MANUAL_INTERVENTION",
            )
        st = str(status).strip().casefold()
        if st not in MI_STATUSES:
            raise CaptionMathError(
                f"Invalid status for TB{idx}: {status!r}",
                error_code="INVALID_MANUAL_INTERVENTION",
            )
        turbines[idx] = st
    # enabled with zero turbines → treat as off (keep legacy math / reject path)
    if not turbines:
        return False, {}
    return True, turbines


def _count_maintenance(tb_values, tbs_raw):
    """M = các TB có công suất <= 0 và TBS ở trạng thái bảo trì (service mode/hmi stop)."""
    return sum(
        1 for tb, tbs in zip(tb_values, tbs_raw)
        if tb <= 0 and _norm_tbs(tbs) in MAINT_TBS
    )


def _count_fault(tb_values, tbs_raw):
    """F = các TB có công suất <= 0 và TBS ở trạng thái lỗi (fault stop)."""
    return sum(
        1 for tb, tbs in zip(tb_values, tbs_raw)
        if tb <= 0 and _norm_tbs(tbs) in FAULT_TBS
    )


def _legacy_counts(tb_values, tbs_raw, dc_num, aws_num):
    """Caption math: M/F suy ra từ TBS + công suất âm (không còn scraped F/M)."""
    inactive_tb_count = sum(1 for tb in tb_values if tb <= 0)
    m_eff = _count_maintenance(tb_values, tbs_raw)
    f_eff = _count_fault(tb_values, tbs_raw)
    # m_eff + f_eff luôn <= inactive vì đếm trên các tập TB rời nhau
    low_wind = inactive_tb_count - m_eff - f_eff
    active = dc_num - inactive_tb_count
    if low_wind > 0 and aws_num >= 6:
        active += low_wind
    if active < 0:
        raise CaptionMathError(
            f"Inconsistent counts: active={active} (DC={dc_num}, inactive={inactive_tb_count})",
            error_code="INCONSISTENT_COUNTS",
        )
    return {
        "active": active,
        "m_eff": m_eff,
        "f_eff": f_eff,
        "low_wind": low_wind,
        "mi_enabled": False,
    }


def _assign_scrape_statuses(tb_values, tbs_raw, only_indices):
    """
    Classify each TB in only_indices (0-based) from scraped data:
      TB > 0                          → normal
      TB <= 0 & TBS ∈ MAINT_TBS       → maintenance
      TB <= 0 & TBS ∈ FAULT_TBS       → error
      else (TB <= 0, other TBS)       → low_wind
    Returns statuses[12]. Entries outside only_indices are None.
    """
    only_indices = set(only_indices)
    statuses = [None] * 12
    for i in only_indices:
        if tb_values[i] > 0:
            statuses[i] = "normal"
        elif _norm_tbs(tbs_raw[i]) in MAINT_TBS:
            statuses[i] = "maintenance"
        elif _norm_tbs(tbs_raw[i]) in FAULT_TBS:
            statuses[i] = "error"
        else:
            statuses[i] = "low_wind"
    return statuses


def _format_one_decimal(value: float) -> str:
    """Format AWS/TAP/DEG with one decimal, strip trailing .0 (e.g. 5.0→5, 5.3→5.3)."""
    return f"{float(value):.1f}".rstrip("0").rstrip(".")


def is_all_low_wind(*, active: int, low_wind: int, m_eff: int, f_eff: int) -> bool:
    """
    Strict-12 rule: cả 12 TB đều dừng do gió thấp, không lẫn lỗi/bảo trì.
    Áp dụng cho cả live và test (is_test không ảnh hưởng caption).
    """
    return active == 0 and low_wind == 12 and m_eff == 0 and f_eff == 0


def build_caption(
    *,
    active: int,
    low_wind: int,
    m_eff: int,
    f_eff: int,
    aws_num: float,
    tap_num: float,
    deg_display: str | None = None,
    force_22h: bool = False,
    mi_enabled: bool = False,
) -> str:
    """
    Build WhatsApp caption. Pure function — easy to unit-test.

    - Khi is_all_low_wind (active==0 && low_wind==12 && m==0 && f==0):
      rút gọn, KHÔNG gửi `tốc độ gió` và `công suất phát`.
    - DEG vẫn được gắn nếu force_22h và deg_display có giá trị (giữ hành vi 22h/23h cũ).
    - Áp dụng cho cả is_test và live (không phân biệt).

    Returns caption string ending with '.' (và có thể thêm câu DEG).
    """
    # Validate core counts are ints
    active = int(active)
    low_wind = int(low_wind)
    m_eff = int(m_eff)
    f_eff = int(f_eff)

    aws_display = _format_one_decimal(aws_num)
    tap_display = _format_one_decimal(tap_num)

    # MI: low_wind đã loại phần fold, luôn hiện nếu >0; legacy: ẩn khi AWS >=6
    show_low_wind = (low_wind > 0) if mi_enabled else (low_wind > 0 and float(aws_num) < 6)

    if is_all_low_wind(active=active, low_wind=low_wind, m_eff=m_eff, f_eff=f_eff):
        # Rút gọn: chỉ giữ số liệu TB, bỏ gió/công suất
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

    # DEG: giữ nguyên hành vi 22h/23h — không bị ảnh hưởng bởi rút gọn
    deg_str = (deg_display or "").strip()
    if force_22h and deg_str:
        # deg_str đã được format ở server (deg_display), chỉ cần gắn
        caption += DEG_SUFFIX_TEMPLATE.format(deg=deg_str)

    return caption


def compute_caption_counts(tb_values, tbs_raw, dc_num, aws_num, mi_enabled=False, turbines=None):
    """
    Compute active / m_eff / f_eff / low_wind for WhatsApp caption.

    m_eff = TBs with power <= 0 and TBS in MAINT_TBS (service mode / hmi stop)
    f_eff = TBs with power <= 0 and TBS in FAULT_TBS (fault stop)

    When mi_enabled is False or turbines empty: full-farm math.
    When mi_enabled with overrides:
      phase1 — TBS-based classification on non-intervened TBs only (DC-aware)
      phase2 — add override status counts
      AWS>=6 — fold only phase1 (scrape) low_wind into active; never fold override low_wind
    """
    if len(tb_values) != 12 or len(tbs_raw) != 12:
        raise CaptionMathError(
            "Expected 12 TB and 12 TBS values",
            error_code="INVALID_FIELD",
        )
    turbines = turbines or {}

    if not mi_enabled or not turbines:
        return _legacy_counts(tb_values, tbs_raw, dc_num, aws_num)

    overridden = set(turbines.keys())
    non = [i for i in range(1, 13) if i not in overridden]

    if not non:
        active_1 = m_1 = f_1 = low_wind_1 = 0
    else:
        non_idx = [i - 1 for i in non]
        scrape_statuses = _assign_scrape_statuses(
            tb_values, tbs_raw, only_indices=non_idx
        )
        inactive_1 = sum(
            1 for i in non if scrape_statuses[i - 1] not in (None, "normal")
        )
        m_1 = sum(1 for i in non if scrape_statuses[i - 1] == "maintenance")
        f_1 = sum(1 for i in non if scrape_statuses[i - 1] == "error")
        low_wind_1 = sum(1 for i in non if scrape_statuses[i - 1] == "low_wind")
        dc_1 = max(0, dc_num - len(overridden))
        active_1 = dc_1 - inactive_1

    n_normal = n_maint = n_error = n_low = 0
    for st in turbines.values():
        if st == "normal":
            n_normal += 1
        elif st == "maintenance":
            n_maint += 1
        elif st == "error":
            n_error += 1
        elif st == "low_wind":
            n_low += 1

    active = active_1 + n_normal
    m_eff = m_1 + n_maint
    f_eff = f_1 + n_error

    # AWS>=6: fold only scrape (phase1) low_wind into active — never fold override low_wind
    if low_wind_1 > 0 and aws_num >= 6:
        active += low_wind_1
        low_wind = n_low
    else:
        low_wind = low_wind_1 + n_low

    if active < 0:
        raise CaptionMathError(
            f"Inconsistent counts after manual intervention: active={active}",
            error_code="INCONSISTENT_COUNTS",
        )
    return {
        "active": active,
        "m_eff": m_eff,
        "f_eff": f_eff,
        "low_wind": low_wind,
        "mi_enabled": True,
        "phase1": {
            "active": active_1,
            "m": m_1,
            "f": f_1,
            "low_wind": low_wind_1,
            "non_count": len(non),
            "dc_1": max(0, dc_num - len(overridden)),
        },
        "phase2": {
            "normal": n_normal,
            "maintenance": n_maint,
            "error": n_error,
            "low_wind": n_low,
        },
    }
