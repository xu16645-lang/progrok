from __future__ import annotations

import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from moemail import extract_verification_codes


def test_chatgpt_html_ignores_six_digit_css_color() -> None:
    raw = """Subject: Your temporary ChatGPT verification code
Content-Type: text/html; charset=utf-8
Content-Transfer-Encoding: quoted-printable

<html><head><style>.main { color:#353740; width: 560000px; }</style></head>
<body><p>Enter this temporary verification code to continue:</p>
<p style=3D"color:#5D5D5D">447820</p></body></html>
"""

    assert extract_verification_codes(raw) == ["447820"]


def test_plain_text_verification_code() -> None:
    assert extract_verification_codes("Your verification code is 938841") == ["938841"]
