"""Caption count math + manual intervention parsing (no Flask deps)."""

MAINT_TBS = frozenset({"service mode", "hmi stop"})
MI_STATUSES = frozenset({"normal", "maintenance", "error", "low_wind"})


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


def _legacy_counts(tb_values, tbs_raw, f_num, m_num, dc_num, aws_num):
    """Exact pre-MI caption math (reject if F+M > inactive)."""
    inactive_tb_count = sum(1 for tb in tb_values if tb <= 0)
    m_from_tbs = sum(
        1 for tb, tbs in zip(tb_values, tbs_raw)
        if tb <= 0 and _norm_tbs(tbs) in MAINT_TBS
    )
    if m_from_tbs > 0:
        m_eff = m_from_tbs
        m_eff_source = "tbs"
    else:
        m_eff = m_num
        m_eff_source = "scraped"
    if f_num + m_eff > inactive_tb_count:
        raise CaptionMathError(
            f"F+M ({f_num}+{m_eff}) exceeds inactive TB count ({inactive_tb_count})"
            + (f" [m from {m_eff_source}, scraped M={m_num}]" if m_eff_source == "tbs" else ""),
            error_code="INCONSISTENT_COUNTS",
            fields={
                "F": f_num, "M": m_eff, "M_scraped": m_num,
                "M_source": m_eff_source, "inactive": inactive_tb_count,
            },
        )
    f_eff = f_num
    active = dc_num - inactive_tb_count
    low_wind = max(0, inactive_tb_count - f_eff - m_eff)
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
        "m_eff_source": m_eff_source,
        "clamped": False,
        "mi_enabled": False,
    }


def _assign_scrape_statuses(tb_values, tbs_raw, f_num, m_num, only_indices):
    """
    Attribute scrape-derived status using legacy F/M rules on only_indices
    (0-based). F/M farm totals apply to that pool only.
    Returns (statuses[12], m_eff_source, clamped). Entries outside only_indices are None.
    """
    only_indices = set(only_indices)
    statuses = [None] * 12
    for i in only_indices:
        if tb_values[i] > 0:
            statuses[i] = "normal"
        elif _norm_tbs(tbs_raw[i]) in MAINT_TBS:
            statuses[i] = "maintenance"
        else:
            statuses[i] = "unclassified"

    inactive_count = sum(1 for i in only_indices if tb_values[i] <= 0)
    m_from_tbs = sum(1 for i in only_indices if statuses[i] == "maintenance")
    clamped = False

    if m_from_tbs > 0:
        m_eff_source = "tbs"
        m_assign = m_from_tbs
        f_assign = f_num
        if f_assign + m_assign > inactive_count:
            f_assign = max(0, inactive_count - m_assign)
            clamped = True
        f_left = f_assign
        for i in range(12):
            if i in only_indices and statuses[i] == "unclassified" and f_left > 0:
                statuses[i] = "error"
                f_left -= 1
    else:
        m_eff_source = "scraped"
        m_assign = m_num
        f_assign = f_num
        if f_assign + m_assign > inactive_count:
            m_assign = min(m_assign, inactive_count)
            f_assign = min(f_assign, max(0, inactive_count - m_assign))
            clamped = True
        f_left = f_assign
        for i in range(12):
            if i in only_indices and statuses[i] == "unclassified" and f_left > 0:
                statuses[i] = "error"
                f_left -= 1
        m_left = m_assign
        for i in range(12):
            if i in only_indices and statuses[i] == "unclassified" and m_left > 0:
                statuses[i] = "maintenance"
                m_left -= 1

    for i in only_indices:
        if statuses[i] == "unclassified":
            statuses[i] = "low_wind"

    return statuses, m_eff_source, clamped


def compute_caption_counts(tb_values, tbs_raw, f_num, m_num, dc_num, aws_num, mi_enabled=False, turbines=None):
    """
    Compute active / m_eff / f_eff / low_wind for WhatsApp caption.

    When mi_enabled is False or turbines empty: legacy full-farm math.
    When mi_enabled with overrides:
      phase1 — legacy F/M attribution on non-intervened TBs only (DC-aware)
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
        return _legacy_counts(tb_values, tbs_raw, f_num, m_num, dc_num, aws_num)

    # Full-farm consistency first (same gate as legacy) — do not let MI clamp
    # hide F+M > inactive farm-wide errors.
    inactive_all = sum(1 for tb in tb_values if tb <= 0)
    m_from_tbs_all = sum(
        1 for tb, tbs in zip(tb_values, tbs_raw)
        if tb <= 0 and _norm_tbs(tbs) in MAINT_TBS
    )
    m_check = m_from_tbs_all if m_from_tbs_all > 0 else m_num
    m_check_source = "tbs" if m_from_tbs_all > 0 else "scraped"
    if f_num + m_check > inactive_all:
        raise CaptionMathError(
            f"F+M ({f_num}+{m_check}) exceeds inactive TB count ({inactive_all})"
            + (f" [m from {m_check_source}, scraped M={m_num}]" if m_check_source == "tbs" else ""),
            error_code="INCONSISTENT_COUNTS",
            fields={
                "F": f_num, "M": m_check, "M_scraped": m_num,
                "M_source": m_check_source, "inactive": inactive_all,
            },
        )

    overridden = set(turbines.keys())
    non = [i for i in range(1, 13) if i not in overridden]

    if not non:
        active_1 = m_1 = f_1 = low_wind_1 = 0
        m_eff_source = "none"
        clamped = False
    else:
        non_idx = [i - 1 for i in non]
        scrape_statuses, m_eff_source, clamped = _assign_scrape_statuses(
            tb_values, tbs_raw, f_num, m_num, only_indices=non_idx
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
        "m_eff_source": m_eff_source,
        "clamped": clamped,
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
