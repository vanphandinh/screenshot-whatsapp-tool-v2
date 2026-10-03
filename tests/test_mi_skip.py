"""MI scrape-skip: giá trị scrape của TB can thiệp (dummy) không ảnh hưởng caption.

Khóa hành vi server.py mới: TB can thiệp dùng tb=0.0 / tbs="" dummy,
compute_caption_counts phải cho cùng kết quả như khi scrape đầy đủ.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from caption_math import compute_caption_counts  # noqa: E402


def _tb_all_active(power=1.5):
    return [power] * 12


def _tbs_prod(n=12):
    return ["Power Production"] * n


def test_mi_dummy_scrape_ignored():
    """TB12 can thiệp error: scrape thật (Fault stop/0.0) hay dummy (\"\"/0.0) đều như nhau."""
    tb_real = _tb_all_active()
    tb_real[11] = 0.0
    tbs_real = _tbs_prod()
    tbs_real[11] = "Fault stop"
    r_real = compute_caption_counts(
        tb_real, tbs_real, dc_num=12, aws_num=5.0,
        mi_enabled=True, turbines={12: "error"},
    )
    # server thay scrape TB12 bằng dummy 0.0 / ""
    tb_dummy = _tb_all_active()
    tb_dummy[11] = 0.0
    tbs_dummy = _tbs_prod()
    tbs_dummy[11] = ""
    r_dummy = compute_caption_counts(
        tb_dummy, tbs_dummy, dc_num=12, aws_num=5.0,
        mi_enabled=True, turbines={12: "error"},
    )
    assert r_dummy["active"] == r_real["active"] == 11
    assert r_dummy["f_eff"] == r_real["f_eff"] == 1
    assert r_dummy["m_eff"] == r_real["m_eff"] == 0
    assert r_dummy["low_wind"] == r_real["low_wind"] == 0


def test_mi_dummy_invalid_number_ignored():
    """TB3 can thiệp normal: scrape lỗi (0.0 + Fault stop) cũng bị bỏ qua."""
    tb = _tb_all_active()
    tb[2] = 0.0  # nếu không ignore, phase1 sẽ đếm thêm 1 error
    tbs = _tbs_prod()
    tbs[2] = "Fault stop"
    r = compute_caption_counts(
        tb, tbs, dc_num=12, aws_num=5.0,
        mi_enabled=True, turbines={3: "normal"},
    )
    assert r["f_eff"] == 0  # không lọt error từ scrape TB3
    assert r["active"] == 12
    assert r["phase1"]["non_count"] == 11
    assert r["phase2"]["normal"] == 1
