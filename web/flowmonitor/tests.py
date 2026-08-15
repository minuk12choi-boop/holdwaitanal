# -*- coding: utf-8 -*-
"""템플릿 JS 정합성 검사.

템플릿을 문자열 치환으로 고치다 보면 블록이 중복되거나 함수 중간이 잘릴 수 있다.
실제로 그런 사고가 있었으므로 최소한의 방어선을 둔다.

    cd web && python manage.py test flowmonitor
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

TPL_DIR = Path(__file__).resolve().parent / "templates" / "flowmonitor"

PAGES = {
    "fab_status.html": [
        "const nf =", "function loadSummary(", "function renderWip()",
        "function renderStatus()", "function renderWt()", "function renderCause()",
        "function openDrill", "function pickLine", "function smoothTo",
    ],
    "fab_metrics.html": [
        "const nf =", "function load()", "const gapLine", "function drawMove",
    ],
}


class TemplateJsTest(SimpleTestCase):
    def _js(self, name):
        html = (TPL_DIR / name).read_text(encoding="utf-8")
        return html, max(re.findall(r"<script>(.*?)</script>", html, re.S), key=len)

    def test_no_duplicate_definitions(self):
        for name, keys in PAGES.items():
            html, _ = self._js(name)
            for key in keys:
                self.assertEqual(html.count(key), 1,
                                 f"{name}: {key} 가 {html.count(key)}회")

    @staticmethod
    def _strip_literals(js):
        """문자열/템플릿/주석을 걷어낸다.

        괄호 세기는 코드 구조만 봐야 한다. 안내 문구에 '(1회)' 같은 괄호가
        들어가면 짝이 안 맞는 것처럼 보여 오탐이 난다.
        """
        js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)        # /* */
        js = re.sub(r"(?m)//.*$", " ", js)                      # //
        js = re.sub(r"`(?:\\.|[^`\\])*`", "``", js)             # 템플릿
        js = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", js)           # '...'
        js = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', js)           # "..."
        return js

    def test_braces_balanced(self):
        for name in PAGES:
            _, js = self._js(name)
            code = self._strip_literals(js)
            for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
                self.assertEqual(code.count(a), code.count(b),
                                 f"{name}: {a}{b} 개수 불일치")

    def test_no_unclosed_top_level_block(self):
        for name in PAGES:
            _, js = self._js(name)
            code = self._strip_literals(js)
            depth = 0
            for line in code.splitlines():
                depth += line.count("{") - line.count("}")
            self.assertEqual(depth, 0, f"{name}: 닫히지 않은 블록")
