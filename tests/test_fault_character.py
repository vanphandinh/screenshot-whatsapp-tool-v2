"""Regression tests for Fault Character turbine status."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from caption_math import compute_caption_counts  # noqa: E402


def test_fault_character_counted_as_error():
    tb = [1.5] * 12
    tb[11] = 0.0
    tbs = ["Power Production"] * 12
    tbs[11] = "Fault Character"

    result = compute_caption_counts(tb, tbs, dc_num=12, aws_num=5.3)

    assert result["f_eff"] == 1
    assert result["low_wind"] == 0
    assert result["active"] == 11
