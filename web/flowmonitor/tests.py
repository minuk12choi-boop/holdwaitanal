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
        "function openDrill", "function pickLine",
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

    BUILTIN = {
        "if", "for", "while", "switch", "catch", "return", "function", "typeof",
        "fetch", "parseInt", "parseFloat", "isNaN", "isFinite", "setTimeout",
        "setInterval", "clearInterval", "clearTimeout", "requestAnimationFrame",
        "encodeURIComponent", "console", "map", "filter", "forEach", "join",
        "split", "push", "indexOf", "slice", "replace", "toFixed", "toUpperCase",
        "toLowerCase", "trim", "find", "findIndex", "sort", "localeCompare",
        "has", "add", "get", "set", "querySelectorAll", "getElementById",
        "addEventListener", "then", "json", "update", "draw", "fill", "stroke",
        "save", "restore", "arc", "beginPath", "closePath", "fillRect",
        "strokeRect", "fillText", "getProps", "getDatasetMeta", "concat",
        "substring", "substr", "startsWith", "matchAll", "values", "entries",
        "keys", "reduce", "some", "every", "includes", "padStart", "contains",
        "toLocaleTimeString", "toLocaleString", "remove", "toggle", "register",
        "stopPropagation", "translate", "cos", "sin", "max", "min", "abs",
        "round", "floor", "ceil", "apply", "of", "from", "click", "destroy",
        "closest", "insertAdjacentHTML", "querySelector", "splice", "pop",
        "getElementsAtEventForMode", "createElement", "appendChild",
        "removeChild", "select", "execCommand", "writeText", "setData",
        "preventDefault", "getBoundingClientRect", "setAttribute",
        "getAttribute", "toISOString", "setMinutes", "getMinutes", "getHours",
        "var", "not", "at", "done", "name", "rgba", "num", "nf", "ck", "key",
        "afterDatasetsDraw", "beforeDatasetsDraw", "afterDraw", "beforeDraw",
        "afterDataLimits", "afterFit", "callback", "onPick", "label", "title",
    }

    def test_no_duplicate_function_defs(self):
        """같은 함수가 두 번 정의되면 나중 것이 이겨 조용히 오작동한다."""
        for name in PAGES:
            _, js = self._js(name)
            code = self._strip_literals(js)
            names = re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", code)
            dup = sorted({n for n in names if names.count(n) > 1})
            self.assertEqual(dup, [], f"{name}: 중복 정의 {dup}")

    def test_no_missing_functions(self):
        """호출하는데 정의가 없는 함수를 잡는다.

        블록을 통째로 치환하다 정의만 날아가는 사고가 반복됐다.
        그런 경우 화면이 조용히 멈추므로 여기서 걸러 낸다.
        """
        for name in PAGES:
            _, js = self._js(name)
            code = self._strip_literals(js)
            defs = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", code))
            called = set(re.findall(r"(?:^|[^\w.$])([a-z][\w$]*)\s*\(", code))
            missing = sorted(called - defs - self.BUILTIN)
            self.assertEqual(missing, [], f"{name}: 정의 없는 호출 {missing}")

    # JS 가 만들어 내는 id(템플릿 문자열 안에서 조립)는 정적으로 못 찾는다.
    DYNAMIC_IDS = {
        "df_", "dfb_", "dfm_", "dfq_", "balc", "balq", "czh", "czp", "czc",
        "cz", "p", "d",
    }

    # 대문자로만 쓰는 상수는 선언이 사라지면 첫 사용에서 바로 죽는다.
    JS_GLOBALS = {
        "Math", "JSON", "Object", "Array", "Set", "Map", "Date", "String",
        "Number", "Boolean", "Promise", "RegExp", "Error", "NaN", "Infinity",
        "Chart", "DATA",
        "GY", "DAY", "SW",          # SHIFT 이름(문자열 리터럴)
    }

    def test_no_missing_identifiers(self):
        """UPPER_SNAKE 상수가 선언 없이 쓰이는지 본다.

        블록 치환 중 선언만 날아가면 ReferenceError 로 화면이 멈춘다.
        """
        for name in PAGES:
            _, js = self._js(name)
            code = self._strip_literals(js)
            # let A = 1, B = 2; 처럼 한 줄에 여러 개를 선언하기도 한다.
            declared = set()
            for stmt in re.findall(r"(?:^|[\s;{(])(?:const|let|var)\s+([^;\n]+)",
                                   code):
                declared |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=", stmt))
                declared |= set(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*$", stmt))
            declared |= set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", code))
            used = set(re.findall(r"(?<![\w.$])([A-Z][A-Z0-9_]{2,})\b", code))
            missing = sorted(used - declared - self.JS_GLOBALS)
            self.assertEqual(missing, [], f"{name}: 선언 없는 상수 {missing}")

    # JS 가 만들어 붙이는 요소는 마크업에 없다.
    JS_MADE_IDS = {"dntip"}

    def test_element_ids_exist(self):
        """getElementById 로 찾는 id 가 마크업에 있는지 본다.

        요소를 빼먹으면 innerHTML 대입에서 조용히 죽는다.
        """
        base = (TPL_DIR / "base.html").read_text(encoding="utf-8")
        for name in PAGES:
            html, js = self._js(name)
            # 공통 요소는 base.html 에 있다. 함께 본다.
            have = set(re.findall(r'id="([\w-]+)"', html + base))
            want = set(re.findall(r'getElementById\(\s*"([\w-]+)"\s*\)', js))
            missing = sorted(w for w in want
                             if w not in have and w not in self.JS_MADE_IDS)
            self.assertEqual(missing, [], f"{name}: 없는 id 참조 {missing}")

    # 이 규칙이 빠지면 flex 열이 내용만큼 부풀어 페이지에 가로 스크롤이 생긴다.
    REQUIRED_CSS = [
        ".rowcards > .farcol",
        "flex:1 1 0; width:0; min-width:0",
        ".rowcards > .leftcol",
        "align-items:flex-start",
        ".leftcol > #czcard",
        ".maincol",
        "canvas { max-width:100% !important",
        ".farcol .gridwrap",
    ]

    def test_layout_rules_present(self):
        """레이아웃을 잡아 주는 CSS 가 남아 있는지 본다.

        블록 치환 중 규칙이 사라져 가로 스크롤이 생긴 적이 여러 번 있었다.
        """
        css = (TPL_DIR / "base.html").read_text(encoding="utf-8")
        missing = [r for r in self.REQUIRED_CSS if r not in css]
        self.assertEqual(missing, [], f"base.html: 사라진 CSS {missing}")
