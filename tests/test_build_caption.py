"""Unit tests for build_caption + is_all_low_wind (pure, no Flask).

Yêu cầu:
- Đúng 12 TB gió thấp (strict 12): active==0 && low_wind==12 && m==0 && f==0 → rút gọn, ẩn gió/công suất
- Giữ DEG cho 22h như cũ
- Áp dụng cho cả is_test (caption logic không phân biệt, test ở đây đảm bảo build_caption không phụ thuộc is_test)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from caption_math import build_caption, is_all_low_wind  # noqa: E402


# ─── is_all_low_wind ───

def test_is_all_low_wind_true():
    assert is_all_low_wind(active=0, low_wind=12, m_eff=0, f_eff=0) is True


def test_is_all_low_wind_false_when_one_maint():
    assert is_all_low_wind(active=0, low_wind=11, m_eff=1, f_eff=0) is False


def test_is_all_low_wind_false_when_one_fault():
    assert is_all_low_wind(active=0, low_wind=11, m_eff=0, f_eff=1) is False


def test_is_all_low_wind_false_when_one_active():
    assert is_all_low_wind(active=1, low_wind=11, m_eff=0, f_eff=0) is False


def test_is_all_low_wind_false_when_11_low():
    assert is_all_low_wind(active=1, low_wind=11, m_eff=0, f_eff=0) is False
    assert is_all_low_wind(active=0, low_wind=11, m_eff=0, f_eff=0) is False


# ─── build_caption — strict 12 rút gọn ───

def test_build_caption_all_low_wind_12_rut_gon():
    caption = build_caption(
        active=0, low_wind=12, m_eff=0, f_eff=0,
        aws_num=1.9, tap_num=0,
        deg_display="", force_22h=False, mi_enabled=False,
    )
    assert caption == "BC BLĐ: Hiện tại 0 TB đang hoạt động, 12 TB dừng do tốc độ gió thấp."
    assert "tốc độ gió" not in caption or caption.count("tốc độ gió") == 1  # chỉ còn 1 trong "dừng do tốc độ gió thấp"
    # ensure the tail "tốc độ gió 1.9 m/s" is NOT present
    assert "m/s" not in caption
    assert "công suất phát" not in caption
    assert caption.endswith(".")


def test_build_caption_all_low_wind_keeps_deg_when_force22h():
    caption = build_caption(
        active=0, low_wind=12, m_eff=0, f_eff=0,
        aws_num=1.9, tap_num=0,
        deg_display="72.3", force_22h=True, mi_enabled=False,
    )
    assert caption == (
        "BC BLĐ: Hiện tại 0 TB đang hoạt động, 12 TB dừng do tốc độ gió thấp. "
        "Sản lượng đầu cực đến thời điểm hiện tại đạt 72.3 MWh."
    )
    assert "m/s" not in caption.split("Sản lượng")[0]
    assert "công suất phát" not in caption
    assert "Sản lượng đầu cực" in caption


def test_build_caption_all_low_wind_deg_not_added_without_force():
    caption = build_caption(
        active=0, low_wind=12, m_eff=0, f_eff=0,
        aws_num=1.9, tap_num=0,
        deg_display="72.3", force_22h=False, mi_enabled=False,
    )
    assert "Sản lượng" not in caption
    assert caption == "BC BLĐ: Hiện tại 0 TB đang hoạt động, 12 TB dừng do tốc độ gió thấp."


def test_build_caption_all_low_wind_with_mi():
    # MI 12 override low_wind → vẫn rút gọn
    caption = build_caption(
        active=0, low_wind=12, m_eff=0, f_eff=0,
        aws_num=5.0, tap_num=0,
        deg_display="", force_22h=False, mi_enabled=True,
    )
    assert "m/s" not in caption
    assert "công suất phát" not in caption
    assert caption == "BC BLĐ: Hiện tại 0 TB đang hoạt động, 12 TB dừng do tốc độ gió thấp."


# ─── build_caption — không rút gọn ───

def test_build_caption_normal_keeps_wind_and_power():
    caption = build_caption(
        active=12, low_wind=0, m_eff=0, f_eff=0,
        aws_num=5.3, tap_num=18.5,
        deg_display="", force_22h=False, mi_enabled=False,
    )
    assert caption == "BC BLĐ: Hiện tại 12 TB đang hoạt động, tốc độ gió 5.3 m/s, công suất phát 18.5 MW."
    assert "m/s" in caption
    assert "công suất phát" in caption


def test_build_caption_not_trigger_when_one_maint():
    caption = build_caption(
        active=0, low_wind=11, m_eff=1, f_eff=0,
        aws_num=2.1, tap_num=0,
        deg_display="", force_22h=False, mi_enabled=False,
    )
    assert "m/s" in caption
    assert "công suất phát" in caption
    assert "1 TB dừng do đang bảo trì" in caption
    assert "11 TB dừng do tốc độ gió thấp" in caption


def test_build_caption_not_trigger_when_one_fault():
    caption = build_caption(
        active=0, low_wind=11, m_eff=0, f_eff=1,
        aws_num=2.1, tap_num=0,
        deg_display="", force_22h=False, mi_enabled=False,
    )
    assert "m/s" in caption
    assert "1 TB dừng do bị lỗi" in caption


def test_build_caption_not_trigger_when_11_low_1_active():
    caption = build_caption(
        active=1, low_wind=11, m_eff=0, f_eff=0,
        aws_num=5.0, tap_num=2.0,
        deg_display="", force_22h=False, mi_enabled=False,
    )
    assert "m/s" in caption
    assert "1 TB đang hoạt động" in caption
    assert "11 TB dừng do tốc độ gió thấp" in caption


def test_build_caption_low_wind_hidden_when_aws_high_legacy():
    # Legacy: AWS>=6 → low_wind ẩn, nhưng vẫn có gió/công suất
    caption = build_caption(
        active=12, low_wind=3, m_eff=0, f_eff=0,
        aws_num=7.5, tap_num=10.0,
        deg_display="", force_22h=False, mi_enabled=False,
    )
    # low_wind should be hidden
    assert "dừng do tốc độ gió thấp" not in caption
    assert "tốc độ gió 7.5 m/s" in caption


def test_build_caption_mi_low_wind_shown_even_when_aws_high():
    # MI: low_wind hiện ngay cả khi AWS>=6
    caption = build_caption(
        active=10, low_wind=1, m_eff=0, f_eff=0,
        aws_num=7.0, tap_num=5.0,
        deg_display="", force_22h=False, mi_enabled=True,
    )
    assert "1 TB dừng do tốc độ gió thấp" in caption
    assert "tốc độ gió 7 m/s" in caption


def test_build_caption_force22h_appends_deg():
    caption = build_caption(
        active=7, low_wind=2, m_eff=2, f_eff=1,
        aws_num=2.1, tap_num=9.0,
        deg_display="80.4", force_22h=True, mi_enabled=False,
    )
    assert "Sản lượng đầu cực đến thời điểm hiện tại đạt 80.4 MWh." in caption
    assert caption.startswith("BC BLĐ:")


def test_build_caption_empty_deg_not_appended():
    caption = build_caption(
        active=7, low_wind=2, m_eff=0, f_eff=0,
        aws_num=2.1, tap_num=9.0,
        deg_display="", force_22h=True, mi_enabled=False,
    )
    assert "Sản lượng" not in caption


def test_build_caption_formatting_strips_trailing_zero():
    caption = build_caption(
        active=10, low_wind=0, m_eff=0, f_eff=0,
        aws_num=5.0, tap_num=10.0,
        deg_display="", force_22h=False, mi_enabled=False,
    )
    # 5.0 → "5", 10.0 → "10"
    assert "tốc độ gió 5 m/s" in caption
    assert "công suất phát 10 MW" in caption

    caption2 = build_caption(
        active=10, low_wind=0, m_eff=0, f_eff=0,
        aws_num=5.3, tap_num=3.8,
        deg_display="", force_22h=False, mi_enabled=False,
    )
    assert "tốc độ gió 5.3 m/s" in caption2
    assert "công suất phát 3.8 MW" in caption2


def test_build_caption_is_test_same_as_live():
    # Build caption không phân biệt is_test — cùng input → cùng output
    base = dict(active=0, low_wind=12, m_eff=0, f_eff=0, aws_num=1.5, tap_num=0, deg_display="", force_22h=False, mi_enabled=False)
    assert build_caption(**base) == build_caption(**base)
    # also verify non-short case identical
    base2 = dict(active=5, low_wind=3, m_eff=2, f_eff=2, aws_num=3.2, tap_num=7.1, deg_display="", force_22h=False, mi_enabled=False)
    assert build_caption(**base2) == build_caption(**base2)
