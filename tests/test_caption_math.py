"""Unit tests for caption count math + manual intervention (no Flask/WA needed)."""
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


def test_baseline_all_active():
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), f_num=0, m_num=0, dc_num=12, aws_num=5.3,
    )
    assert r["active"] == 12
    assert r["f_eff"] == 0
    assert r["m_eff"] == 0
    assert r["low_wind"] == 0


def test_baseline_low_wind_and_aws_fold():
    tb = _tb_all_active()
    tb[9] = tb[10] = tb[11] = 0.0
    tbs = _tbs_prod()
    for i in (9, 10, 11):
        tbs[i] = "Warning Character Code"
    r_low = compute_caption_counts(tb, tbs, 0, 0, 12, 2.1)
    assert r_low["active"] == 9
    assert r_low["low_wind"] == 3

    r_hi = compute_caption_counts(tb, tbs, 0, 0, 12, 7.5)
    assert r_hi["low_wind"] == 3
    assert r_hi["active"] == 12


def test_baseline_inconsistent_rejects():
    tb = _tb_all_active()
    tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[11] = "Warning Character Code"
    with pytest.raises(CaptionMathError) as ei:
        compute_caption_counts(tb, tbs, f_num=2, m_num=0, dc_num=12, aws_num=5.0)
    assert ei.value.error_code == "INCONSISTENT_COUNTS"


def test_mi_phase2_adds_error_on_top_of_phase1():
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), f_num=0, m_num=0, dc_num=12, aws_num=5.3,
        mi_enabled=True, turbines={12: "error"},
    )
    assert r["phase1"]["active"] == 11
    assert r["phase1"]["f"] == 0
    assert r["f_eff"] == 1
    assert r["active"] == 11


def test_mi_no_double_count_error():
    """1 scraped error + 1 low_wind; override the first inactive as error — F redistributes to remaining."""
    tb = _tb_all_active()
    tb[10] = tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[10] = tbs[11] = "Warning Character Code"
    # Override TB11; F=1 applies to remaining inactive TB12 only → f_1=1, phase2 +error → wait
    # Override TB11 as error: non has TB12; F→TB12; phase2 +1 error → f_eff=2
    # Better case: override TB11 as maintenance so F redistributes to TB12
    r = compute_caption_counts(
        tb, tbs, f_num=1, m_num=0, dc_num=12, aws_num=2.0,
        mi_enabled=True, turbines={11: "maintenance"},
    )
    assert r["m_eff"] == 1
    assert r["f_eff"] == 1  # F preserved on TB12
    assert r["phase1"]["f"] == 1
    assert r["phase1"]["low_wind"] == 0


def test_mi_respects_dc():
    """DC=10 with all TB>0; override TB12 normal → active 10 not 12."""
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), 0, 0, dc_num=10, aws_num=5.0,
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
        tb, tbs, f_num=0, m_num=0, dc_num=12, aws_num=5.0,
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
    tbs[11] = "Warning Character Code"
    r = compute_caption_counts(
        tb, tbs, f_num=1, m_num=0, dc_num=12, aws_num=5.0,
        mi_enabled=True, turbines={12: "normal"},
    )
    assert r["f_eff"] == 0
    assert r["active"] == 12


def test_mi_low_wind_override_not_folded_when_aws_high():
    """Override low_wind must stay in caption counts; AWS>=6 must not add it to active."""
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), 0, 0, 12, 7.0,
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
        tb, tbs, 0, 0, 12, 7.0,
        mi_enabled=True, turbines={12: "low_wind"},
    )
    assert r["phase1"]["low_wind"] == 1
    assert r["phase2"]["low_wind"] == 1
    assert r["low_wind"] == 1  # only override remains for caption after fold
    assert r["active"] == 11  # dc_1=11, inactive_1=1 → active_1=10, +fold 1 → 11, +0 normal


def test_mi_all_overridden():
    turbines = {i: "error" for i in range(1, 13)}
    r = compute_caption_counts(
        _tb_all_active(), _tbs_prod(), 0, 0, 12, 5.0,
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
    base = compute_caption_counts(tb, tbs, 0, 0, 12, 2.0, mi_enabled=False)
    mi = compute_caption_counts(tb, tbs, 0, 0, 12, 2.0, mi_enabled=True, turbines={})
    assert mi == base


def test_mi_still_rejects_farm_inconsistent_fm():
    """MI must not clamp away farm-wide F+M > inactive (legacy reject still applies)."""
    tb = _tb_all_active()
    tb[10] = tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[10] = tbs[11] = "Warning Character Code"
    with pytest.raises(CaptionMathError) as ei:
        compute_caption_counts(
            tb, tbs, f_num=5, m_num=0, dc_num=12, aws_num=5.0,
            mi_enabled=True, turbines={3: "normal"},
        )
    assert ei.value.error_code == "INCONSISTENT_COUNTS"


def test_mi_empty_enabled_still_rejects_inconsistent():
    tb = _tb_all_active()
    tb[11] = 0.0
    tbs = _tbs_prod()
    tbs[11] = "Warning Character Code"
    with pytest.raises(CaptionMathError):
        compute_caption_counts(tb, tbs, 2, 0, 12, 5.0, mi_enabled=True, turbines={})
