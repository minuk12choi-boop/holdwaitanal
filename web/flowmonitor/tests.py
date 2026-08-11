# -*- coding: utf-8 -*-
"""템플릿 JS 정합성 검사.

템플릿을 문자열 치환으로 고치다 보면 블록이 중복되거나 함수 중간이 잘릴 수 있다.
실제로 그런 사고가 있었으므로 최소한의 방어선을 둔다.

    cd web && python manage.py test flowmonitor
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

TPL = (Path(__file__).resolve().parent / "templates" / "flowmonitor"
       / "flowstack.html")

MUST_BE_ONCE = [
    "const KIND_KO", "const gapLine", "const nf =",
    "function load()", "function cardHtml", "function drawCards",
    "function renderCard", "function setCenter", "function openDrill",
    "function drawWt", "let STATUS_DATA",
]


class TemplateJsTest(SimpleTestCase):
    def setUp(self):
        self.html = TPL.read_text(encoding="utf-8")
        self.js = max(re.findall(r"<script>(.*?)</script>", self.html, re.S),
                      key=len)

    def test_no_duplicate_definitions(self):
        for key in MUST_BE_ONCE:
            self.assertEqual(self.html.count(key), 1,
                             f"{key} 가 {self.html.count(key)}회 정의됨")

    def test_braces_balanced(self):
        for ch_open, ch_close in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(self.js.count(ch_open), self.js.count(ch_close),
                             f"{ch_open}{ch_close} 개수 불일치")

    def test_no_unclosed_top_level_block(self):
        depth = 0
        for line in self.js.splitlines():
            depth += line.count("{") - line.count("}")
        self.assertEqual(depth, 0, "닫히지 않은 블록이 있음")
