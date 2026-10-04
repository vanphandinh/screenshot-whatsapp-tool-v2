"""Regression tests cho trạng thái mất tín hiệu (lost_signal / front-end interruption).

Luật:
  - TB không đọc được giá trị công suất (None) + TBS chứa 'front-end interruption'
    → tính vào lost_signal.
  - active = DC - (m_eff + f_eff + low_wind + lost_signal).
  - Caption thêm "{x} TB mất tín hiệu đường truyền, " theo thứ tự:
    đang hoạt động → lỗi → bảo trì → mất tín hiệu → gió thấp.
  - MI_STATUSES có 'lost_signal' (can thiệp thủ công).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from caption_math import (  # noqa: E402
    CaptionMathError,
    build_caption,
    compute_caption_counts,
    is_all_low_wind,
    is_lost_signal_tbs,
    parse_manual_intervention,
)


def _tb_all_active(power=1.5):
    return [power] * 12


def _tbs_prod(n=12):
    return ["Power Production"] * n


# ─── Đếm lost_signal từ dữ liệu scrape ───

def test_missing_power_with_front_end_interruption_counted_as_lost_signal():
    tb = _tb_all_active()
    tb[11] = None  # không đọc được công suất
    tbs = _tbs_prod()
    tbs[11] = "Front-end interruption"
    r = compute_caption_counts(tb, tbs, dc_num=12)
    assert r["lost_signal"] == 1
    assert r["active"] == 11
    assert r["m_eff"] == 0
    assert r["f_eff"] == 0
    assert r["low_wind"] == 0


def test_lost_signal_tbs_matching_ignores_case_nbsp_hyphen():
    for tbs_value in (
        "FRONT-END INTERRUPTION",
        "Front\u00a0end  interruption",
        "Frontend Interruption",
        "Front-end Interruption (lost)",
    ):
        tb = _tb_all_active()
        tb[9] = None
        tbs = _tbs_prod()
        tbs[9] = tbs_value
        r = compute_caption_counts(tb, tbs, dc_num=12)
        assert r["lost_signal"] == 1, tbs_value
        assert r["active"] == 11, tbs_value


def test_public_is_lost_signal_tbs():
    assert is_lost_signal_tbs("Front-end interruption") is True
    assert is_lost_signal_tbs("  FRONT END INTERRUPTION (x) ") is True
    assert is_lost_signal_tbs("No enough wind") is False
    assert is_lost_signal_tbs("") is False
    assert is_lost_signal_tbs(None) is False


def test_readable_power_with_front_end_tbs_not_lost_signal():
    """Vẫn đọc được công suất (0/âm) + TBS front-end → giữ hành vi cũ, không tính lost."""
    tb = _tb_all_active()
    tb[10] = 0.0
    tb[11] = -0.2
    tbs = _tbs_prod()
    tbs[10] = "Front-end interruption"
    tbs[11] = "Front-end interruption"
    r = compute_caption_counts(tb, tbs, dc_num=12)
    assert r["lost_signal"] == 0
    assert r["active"] == 12  # TBS không thuộc nhóm nào khác → không trừ active (hành vi cũ)

    tb2 = _tb_all_active()
    tbs2 = _tbs_prod()
    tbs2[11] = "Front-end interruption"
    r2 = compute_caption_counts(tb2, tbs2, dc_num=12)
    assert r2["lost_signal"] == 0
    assert r2["active"] == 12


def test_missing_power_with_other_tbs_not_lost_signal():
    """TB None nhưng TBS không phải front-end → không tính lost (server sẽ reject trước)."""
    tb = _tb_all_active()
    tb[5] = None
    tbs = _tbs_prod()
    tbs[5] = "No enough wind"
    r = compute_caption_counts(tb, tbs, dc_num=12)
    assert r["lost_signal"] == 0
    assert r["low_wind"] == 0
    assert r["active"] == 12


def test_lost_signal_combined_with_other_groups():
    tb = [1.5] * 7 + [0.0, None, 0.0, 0.0, 0.0]
    tbs = _tbs_prod(7) + [
        "Fault stop", "Front-end interruption", "Service mode",
        "No enough wind", "No enough wind",
    ]
    r = compute_caption_counts(tb, tbs, dc_num=12)
    assert r["f_eff"] == 1
    assert r["m_eff"] == 1
    assert r["low_wind"] == 2
    assert r["lost_signal"] == 1
    assert r["active"] == 7


def test_lost_signal_reduces_active_and_rejects_when_negative():
    tb = _tb_all_active()
    tb[10] = tb[11] = None
    tbs = _tbs_prod()
    tbs[10] = tbs[11] = "Front-end interruption"
    r = compute_caption_counts(tb, tbs, dc_num=12)
    assert r["lost_signal"] == 2
    assert r["active"] == 10
    with pytest.raises(CaptionMathError) as ei:
        compute_caption_counts(tb, tbs, dc_num=1)
    assert ei.value.error_code == "INCONSISTENT_COUNTS"


# ─── Manual intervention: status lost_signal ───

def test_parse_mi_accepts_lost_signal():
    enabled, turbines = parse_manual_intervention(
        {"enabled": True, "turbines": {"7": "LOST_SIGNAL"}}
    )
    assert enabled is True
    assert turbines == {7: "lost_signal"}


def test_mi_override_to_lost_signal():
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), dc_num=12,
        mi_enabled=True, turbines={12: "lost_signal"},
    )
    assert r["phase2"]["lost_signal"] == 1
    assert r["lost_signal"] == 1
    assert r["active"] == 11


def test_mi_phase1_classifies_missing_power_front_end_as_lost_signal():
    tb = _tb_all_active()
    tb[10] = None
    tbs = _tbs_prod()
    tbs[10] = "Front-end interruption"
    r = compute_caption_counts(
        tb, tbs, dc_num=12,
        mi_enabled=True, turbines={12: "normal"},
    )
    assert r["phase1"]["lost_signal"] == 1
    assert r["lost_signal"] == 1
    assert r["active"] == 11  # dc_1=11, dừng 1 (lost) → 10, +1 override normal


def test_mi_all_overridden_lost_signal():
    turbines = {i: "lost_signal" for i in range(1, 13)}
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), 12,
        mi_enabled=True, turbines=turbines,
    )
    assert r["phase2"]["lost_signal"] == 12
    assert r["lost_signal"] == 12
    assert r["active"] == 0


# ─── Caption: câu chữ + thứ tự mới ───

def test_build_caption_order_and_lost_signal_phrase():
    caption = build_caption(
        active=7, low_wind=1, m_eff=2, f_eff=1, lost_signal=1,
        aws_num=5.3, tap_num=10.0,
        dpg_display="", force_22h=False,
    )
    assert caption == (
        "BC BLĐ: Hiện tại 7 TB đang hoạt động, "
        "1 TB dừng do bị lỗi, "
        "2 TB dừng do đang bảo trì, "
        "1 TB mất tín hiệu đường truyền, "
        "1 TB dừng do tốc độ gió thấp, "
        "tốc độ gió 5.3 m/s, công suất phát 10 MW."
    )


def test_build_caption_lost_signal_only():
    caption = build_caption(
        active=11, low_wind=0, m_eff=0, f_eff=0, lost_signal=1,
        aws_num=5.3, tap_num=16.0,
        dpg_display="", force_22h=False,
    )
    assert caption == (
        "BC BLĐ: Hiện tại 11 TB đang hoạt động, "
        "1 TB mất tín hiệu đường truyền, "
        "tốc độ gió 5.3 m/s, công suất phát 16 MW."
    )


def test_build_caption_lost_signal_hidden_when_zero():
    caption = build_caption(
        active=12, low_wind=0, m_eff=0, f_eff=0, lost_signal=0,
        aws_num=5.3, tap_num=18.5,
        dpg_display="", force_22h=False,
    )
    assert "mất tín hiệu" not in caption
    assert caption == "BC BLĐ: Hiện tại 12 TB đang hoạt động, tốc độ gió 5.3 m/s, công suất phát 18.5 MW."


def test_build_caption_strict12_low_wind_unchanged():
    caption = build_caption(
        active=0, low_wind=12, m_eff=0, f_eff=0, lost_signal=0,
        aws_num=1.9, tap_num=0,
        dpg_display="", force_22h=False,
    )
    assert caption == "BC BLĐ: Hiện tại 0 TB đang hoạt động, 12 TB dừng do tốc độ gió thấp."


def test_is_all_low_wind_false_with_lost_signal():
    assert is_all_low_wind(active=0, low_wind=11, m_eff=0, f_eff=0, lost_signal=1) is False
