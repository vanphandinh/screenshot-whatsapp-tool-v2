"""Regression: placeholder '--' (mất tín hiệu) phải được coi là missing.

Bug gốc: normalizeScrapedNumber() trong chrome-extension/background.js chỉ nhận
diện placeholder MỘT dấu gạch ('-'), nên '--' lọt qua nguyên văn -> server trả
400 "Invalid TB12 value: '--'" và capture thủ công thất bại.

Test chạy CHÍNH hàm normalizeScrapedNumber trong background.js qua node
(không sao chép logic), nên sẽ đỏ nếu regression quay lại.
"""
import json
import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKGROUND_JS = os.path.join(REPO_ROOT, 'chrome-extension', 'background.js')

pytestmark = pytest.mark.skipif(
    shutil.which('node') is None,
    reason='node không có sẵn để chạy normalizeScrapedNumber',
)

NODE_SCRIPT = r'''
const fs = require("fs");
const file = process.argv[1];
const src = fs.readFileSync(file, "utf8");
const a = src.indexOf("function normalizeScrapedNumber");
const b = src.indexOf("function isFrontEndInterruptionTbs");
if (a < 0 || b < 0) { console.error("EXTRACT_FAIL"); process.exit(2); }
const normalize = eval("(" + src.slice(a, b) + ")");
const cases = JSON.parse(process.argv[2]);
const out = cases.map(function (c) { return [c[0], c[1], normalize(c[0], c[1])]; });
process.stdout.write(JSON.stringify(out));
'''

CASES = [
    # Placeholder công suất không đọc được -> '' (server tính lost_signal)
    ('-', 'TB12', ''),
    ('--', 'TB12', ''),
    ('---', 'TB12', ''),
    (chr(0x2014), 'TB12', ''),   # em dash
    (chr(0x2212), 'TB12', ''),   # unicode minus
    ('- -', 'TB12', ''),
    ('-- MW', 'TB12', ''),
    ('--MW', 'TB12', ''),
    # Giá trị thật giữ nguyên
    ('5.3', 'TB12', '5.3'),
    ('-0.2', 'TB12', '-0.2'),
    # Placeholder cũ & biến thể với field khác
    ('n/a', 'TAP', ''),
    ('--', 'TAP', ''),
    # TBS giữ nguyên text
    ('Front-end interruption', 'TBS12', 'Front-end interruption'),
]


def test_dashboard_placeholders_become_missing():
    """'--' (kể cả kèm đơn vị) phải về '' để server tính lost_signal."""
    payload = json.dumps([[raw, field] for raw, field, _ in CASES])
    res = subprocess.run(
        ['node', '-e', NODE_SCRIPT, BACKGROUND_JS, payload],
        capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert res.returncode == 0, 'node failed: ' + res.stderr
    got = {(raw, field): value for raw, field, value in json.loads(res.stdout)}
    for raw, field, expected in CASES:
        assert got[(raw, field)] == expected, (
            '%s=%s normalized to %s, expected %s'
            % (field, ascii(raw), ascii(got[(raw, field)]), ascii(expected))
        )
