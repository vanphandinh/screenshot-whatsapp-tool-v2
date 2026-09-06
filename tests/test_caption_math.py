"""Unit tests for caption count math + manual intervention (no Flask/WA needed).

Luật mới (không còn scraped F/M):
  m_eff = số TB có công suất <= 0 và TBS ∈ {service mode, hmi stop}
  f_eff = số TB có công suất <= 0 và TBS ∈ {fault stop}
  còn lại (TB <= 0) → low_wind; AWS >= 6 → fold low_wind vào active
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from caption_math import (  # noqa: E402
    CaptionMathError,
    compute_caption_counts,
    parse_manual_intervention,
)


def _tbs_prod(n=12):
    return ["Power Production"] * n


def _tb_all_active(power=1.5):
    return [power] * 12


# ─── parse_manual_intervention (không đổi) ───

def test_parse_mi_disabled_and_absent():
    assert parse_manual_intervention(None) == (False, {})
    assert parse_manual_intervention({"enabled": False, "turbines": {"1": "error"}}) == (False, {})
    assert parse_manual_intervention({"enabled": True, "turbines": {}}) == (False, {})
    assert parse_manual_intervention({"enabled": "true", "turbines": {"1": "error"}}) == (False, {})


def test_parse_mi_valid():
    enabled, turbines = parse_manual_intervention({
        "enabled": True,
        "turbines": {"3": "maintenance", "7": "Error", "12": "low_wind"},
    })
    assert enabled is True
    assert turbines == {3: "maintenance", 7: "error", 12: "low_wind"}


def test_parse_mi_invalid_status():
    with pytest.raises(CaptionMathError) as ei:
        parse_manual_intervention({"enabled": True, "turbines": {"1": "broken"}})
    assert ei.value.error_code == "INVALID_MANUAL_INTERVENTION"


def test_parse_mi_rejects_non_digit_keys():
    with pytest.raises(CaptionMathError):
        parse_manual_intervention({"enabled": True, "turbines": {"1_0": "error"}})


# ─── Luật M từ Service mode / HMI stop ───

def test_baseline_all_active():
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), dc_num=12, aws_num=5.3,
    )
    assert r["active"] == 12
    assert r["f_eff"] == 0
    assert r["m_eff"] == 0
    assert r["low_wind"] == 0


def test_maint_counted_from_service_mode_and_hmi_stop():
    """TB <= 0 với TBS 'Service mode' / 'HMI stop' → đếm vào M (bảo trì)."""
    tb = _tb_all_active()
    tb[10] = tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[10] = "Service mode"
    tbs[11] = "HMI stop"
    r = compute_caption_counts(tb, tbs, dc_num=12, aws_num=5.3)
    assert r["m_eff"] == 2
    assert r["f_eff"] == 0
    assert r["low_wind"] == 0
    assert r["active"] == 10


def test_tbs_matching_ignores_case_whitespace_and_nbsp():
    """Match trạng thái bỏ qua hoa/thường, khoảng trắng kép và NBSP."""
    tb = _tb_all_active()
    tb[9] = tb[10] = tb[11] = 0.0
    tbs = ["Power Production"] * 9 + ["Service\u00a0Mode", "HMI  stop", "FAULT\u00a0STOP"]
    r = compute_caption_counts(tb, tbs, dc_num=12, aws_num=5.0)
    assert r["m_eff"] == 2
    assert r["f_eff"] == 1
    assert r["low_wind"] == 0


# ─── Luật F từ Fault stop ───

def test_fault_stop_counted_as_error():
    """TB <= 0 với TBS 'Fault stop' → đếm vào F (lỗi)."""
    tb = _tb_all_active()
    tb[9] = tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[9] = "Fault stop"
    tbs[11] = "Fault Stop"
    r = compute_caption_counts(tb, tbs, dc_num=12, aws_num=5.3)
    assert r["f_eff"] == 2
    assert r["m_eff"] == 0
    assert r["low_wind"] == 0
    assert r["active"] == 10


def test_maint_fault_and_low_wind_combined():
    """Trộn đủ 3 nhóm: bảo trì + lỗi + gió thấp trong cùng 1 farm."""
    tb = [1.5] * 7 + [0.0] * 5
    tbs = ["Power Production"] * 7 + [
        "Service mode", "HMI stop", "Fault stop",
        "Warning Character Code", "Warning Character Code",
    ]
    r = compute_caption_counts(tb, tbs, dc_num=12, aws_num=2.1)
    assert r["m_eff"] == 2
    assert r["f_eff"] == 1
    assert r["low_wind"] == 2
    assert r["active"] == 7  # AWS < 6 → không fold


def test_positive_power_not_counted_even_with_status_match():
    """Công suất > 0 thì không đếm vào M/F dù TBS khớp; công suất âm mới đếm."""
    tb = _tb_all_active()
    tb[9] = 0.4    # > 0 + TBS Service mode → vẫn active, không vào M
    tb[10] = -0.2  # công suất âm + Fault stop → F
    tbs = ["Power Production"] * 9 + ["Service mode", "Fault stop", "Power Production"]
    r = compute_caption_counts(tb, tbs, dc_num=12, aws_num=5.0)
    assert r["m_eff"] == 0
    assert r["f_eff"] == 1
    assert r["low_wind"] == 0
    assert r["active"] == 11


def test_zero_power_counts_as_inactive():
    """Công suất đúng 0 là 'âm như cũ' (<= 0) → vẫn đếm."""
    tb = _tb_all_active()
    tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[11] = "Fault stop"
    r = compute_caption_counts(tb, tbs, dc_num=12, aws_num=5.0)
    assert r["f_eff"] == 1
    assert r["active"] == 11


# ─── Low wind + AWS fold (giữ nguyên) ───

def test_baseline_low_wind_and_aws_fold():
    tb = _tb_all_active()
    tb[9] = tb[10] = tb[11] = 0.0
    tbs = _tbs_prod()
    for i in (9, 10, 11):
        tbs[i] = "Warning Character Code"
    r_low = compute_caption_counts(tb, tbs, 12, 2.1)
    assert r_low["active"] == 9
    assert r_low["low_wind"] == 3

    r_hi = compute_caption_counts(tb, tbs, 12, 7.5)
    assert r_hi["low_wind"] == 3
    assert r_hi["active"] == 12


def test_dc_below_inactive_rejects():
    """DC nhỏ hơn số TB dừng → active âm → từ chối (INCONSISTENT_COUNTS)."""
    tb = [0.0] * 10 + [1.5, 1.5]
    tbs = ["Warning Character Code"] * 12
    with pytest.raises(CaptionMathError) as ei:
        compute_caption_counts(tb, tbs, dc_num=9, aws_num=5.0)
    assert ei.value.error_code == "INCONSISTENT_COUNTS"


def test_invalid_lengths_reject():
    with pytest.raises(CaptionMathError) as ei:
        compute_caption_counts([1.5] * 11, _tbs_prod(11), dc_num=12, aws_num=5.0)
    assert ei.value.error_code == "INVALID_FIELD"


# ─── Manual Intervention ───

def test_mi_phase2_adds_error_on_top_of_phase1():
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), dc_num=12, aws_num=5.3,
        mi_enabled=True, turbines={12: "error"},
    )
    assert r["phase1"]["active"] == 11
    assert r["phase1"]["f"] == 0
    assert r["f_eff"] == 1
    assert r["active"] == 11


def test_mi_phase1_classifies_fault_stop_as_error():
    """Phase1 tự phân loại TB Fault stop thành error, không cần F scrape."""
    tb = _tb_all_active()
    tb[10] = tb[11] = 0.0
    tbs = ["Power Production"] * 10 + ["Fault stop", "Service mode"]
    # Override TB12 (đang Service mode) thành normal → phase1 chỉ còn TB11
    r = compute_caption_counts(
        tb, tbs, dc_num=12, aws_num=5.0,
        mi_enabled=True, turbines={12: "normal"},
    )
    assert r["phase1"]["f"] == 1
    assert r["phase1"]["m"] == 0
    assert r["f_eff"] == 1
    assert r["m_eff"] == 0
    assert r["active"] == 11  # dc_1=11, inactive_1=1 → 10, +1 override normal


def test_mi_override_fault_tb_to_maintenance():
    """Override TB Fault stop thành maintenance → turbine đó thành M thay vì F."""
    tb = _tb_all_active()
    tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[11] = "Fault stop"
    r = compute_caption_counts(
        tb, tbs, dc_num=12, aws_num=5.0,
        mi_enabled=True, turbines={12: "maintenance"},
    )
    assert r["phase1"]["f"] == 0  # TB12 bị override → không còn trong phase1
    assert r["phase2"]["maintenance"] == 1
    assert r["f_eff"] == 0
    assert r["m_eff"] == 1
    assert r["active"] == 11


def test_mi_no_double_count_error():
    """Phase1 đếm Fault stop đúng 1 lần; phase2 chỉ cộng TB override."""
    tb = _tb_all_active()
    tb[10] = tb[11] = 0.0
    tbs = ["Power Production"] * 10 + ["Fault stop", "Fault stop"]
    r = compute_caption_counts(
        tb, tbs, dc_num=12, aws_num=2.0,
        mi_enabled=True, turbines={12: "error"},
    )
    # Phase1: TB11 fault → f_1=1; phase2: TB12 override error +1 (không đếm lại TB11)
    assert r["phase1"]["f"] == 1
    assert r["f_eff"] == 2
    assert r["phase1"]["low_wind"] == 0
    assert r["active"] == 10


def test_mi_respects_dc():
    """DC=10 with all TB>0; override TB12 normal → active 10 not 12."""
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), dc_num=10, aws_num=5.0,
        mi_enabled=True, turbines={12: "normal"},
    )
    assert r["phase1"]["dc_1"] == 9
    assert r["phase1"]["active"] == 9
    assert r["active"] == 10


def test_mi_maintenance_and_normal_override():
    tb = _tb_all_active()
    tb[8] = 0.0
    tbs = _tbs_prod()
    tbs[8] = "Warning Character Code"
    r = compute_caption_counts(
        tb, tbs, dc_num=12, aws_num=5.0,
        mi_enabled=True, turbines={9: "normal", 10: "maintenance"},
    )
    assert r["phase1"]["non_count"] == 10
    assert r["active"] == 11
    assert r["m_eff"] == 1
    assert r["f_eff"] == 0


def test_mi_override_error_tb_to_normal():
    tb = _tb_all_active()
    tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[11] = "Fault stop"
    r = compute_caption_counts(
        tb, tbs, dc_num=12, aws_num=5.0,
        mi_enabled=True, turbines={12: "normal"},
    )
    assert r["f_eff"] == 0
    assert r["active"] == 12


def test_mi_low_wind_override_not_folded_when_aws_high():
    """Override low_wind must stay in caption counts; AWS>=6 must not add it to active."""
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), 12, 7.0,
        mi_enabled=True, turbines={12: "low_wind"},
    )
    assert r["low_wind"] == 1
    assert r["active"] == 11  # 11 phase1 active; do not fold override


def test_mi_scrape_low_wind_still_folded_when_aws_high():
    """Phase1 scrape low_wind still folds into active when AWS>=6; override low_wind does not."""
    tb = _tb_all_active()
    tb[10] = 0.0  # TB11 scrape inactive → low_wind in phase1
    tbs = _tbs_prod()
    tbs[10] = "Warning Character Code"
    r = compute_caption_counts(
        tb, tbs, 12, 7.0,
        mi_enabled=True, turbines={12: "low_wind"},
    )
    assert r["phase1"]["low_wind"] == 1
    assert r["phase2"]["low_wind"] == 1
    assert r["low_wind"] == 1  # only override remains for caption after fold
    assert r["active"] == 11  # dc_1=11, inactive_1=1 → active_1=10, +fold 1 → 11, +0 normal


def test_mi_all_overridden():
    turbines = {i: "error" for i in range(1, 13)}
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), 12, 5.0,
        mi_enabled=True, turbines=turbines,
    )
    assert r["phase1"]["active"] == 0
    assert r["f_eff"] == 12
    assert r["active"] == 0


def test_mi_enabled_empty_turbines_uses_legacy():
    tb = _tb_all_active()
    tb[10] = tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[10] = tbs[11] = "Warning Character Code"
    base = compute_caption_counts(tb, tbs, 12, 2.0, mi_enabled=False)
    mi = compute_caption_counts(tb, tbs, 12, 2.0, mi_enabled=True, turbines={})
    assert mi == base


def test_mi_rejects_when_phase1_active_negative():
    """DC nhỏ hơn số TB dừng trong phase1 → active âm → từ chối."""
    tb = [0.0] * 4 + [1.5] * 8
    tbs = ["Warning Character Code"] * 12
    with pytest.raises(CaptionMathError) as ei:
        compute_caption_counts(
            tb, tbs, dc_num=3, aws_num=5.0,
            mi_enabled=True, turbines={12: "normal"},
        )
    assert ei.value.error_code == "INCONSISTENT_COUNTS"
