"""Regression: server coi công suất dash-only ('--') là không đọc được.

Trước đây '--' lọt qua bước kiểm tra missing (truthy) rồi bị parse_number()
từ chối -> 400 "Invalid TB12 value: '--'". is_unreadable_power() chặn trước,
để nhánh lost_signal (TBS 'Front-end interruption') xử lý như mong đợi.

Lưu ý: server.py import WPP_Whatsapp/Flask nên không import trực tiếp trong test;
helper được trích từ source (cùng kỹ thuật với tests/test_extension_normalizer.py).
"""
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(REPO_ROOT, 'server.py')


def _load_helper():
    src = open(SERVER_PY, encoding='utf-8').read()
    a = src.index('_PLACEHOLDER_POWER_RE = re.compile(')
    b = src.index('def parse_number(v):')
    ns = {'re': re}
    exec(src[a:b], ns)
    return ns['is_unreadable_power']


is_unreadable_power = _load_helper()

PLACEHOLDERS = [
    '-', '--', '---', '- -', ' -- ',
    chr(0x2010), chr(0x2015), chr(0x2014), chr(0x2212),
]
READABLE = ['', '0', '1.5', '-0.2', 'n/a', None, '5,3']


@pytest.mark.parametrize('raw', PLACEHOLDERS, ids=[ascii(v) for v in PLACEHOLDERS])
def test_dash_only_power_is_unreadable(raw):
    assert is_unreadable_power(raw) is True


@pytest.mark.parametrize('raw', READABLE, ids=[ascii(v) for v in READABLE])
def test_readable_power_is_not_flagged(raw):
    assert is_unreadable_power(raw) is False
