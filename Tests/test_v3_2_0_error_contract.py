"""v3.2.0 (WP-211): the error contract the GUI and the Anthropic shim read.

`attempts[]` is additive: a single-engine failure must keep the exact three-key
`error` payload that existing clients (and the Anthropic compatibility layer)
expect, while a failed auto chain now carries every engine's own reason. Before
this, the auto chain raised the *last* error, which for a vision model was the
mlx-lm guard's "load it with mlx-vlm" text -- an instruction that could not
work and that hid the real mlx-vlm failure (G3).

AC-5-6 (WP-R1): the English table check used to assert a hand-written list of
nine v3.2.0 codes, so the 422 `INVALID_PROFILE` this release introduced -- and
five older codes -- stayed unregistered and leaked the coordinator's Japanese
`message` into an English interface. The check now derives the code set from
`Coordinator/mlxbar/api/*.py` itself, so a code added on the Python side fails
here until it is registered in `ErrorText.swift`. Codes forwarded dynamically
(`event.get("code", ...)`) or emitted by the workers are not statically
extractable from the API package and remain out of this scan's scope.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from mlxbar.errors import MLXBarError

ROOT = Path(__file__).resolve().parents[1]
ERROR_TEXT = ROOT / "Sources" / "MLXBar" / "Services" / "ErrorText.swift"
API_DIR = ROOT / "Coordinator" / "mlxbar" / "api"

#: `{"code": "SOME_CODE"}` / `['code'] = "SOME_CODE"` in the HTTP layer.
API_CODE_RE = re.compile(r"""["']code["']\s*[=:]\s*["']([A-Z][A-Z0-9_]+)["']""")
#: One `"CODE": ("ja", "en"),` entry of `CoordinatorErrorText.table`.
SWIFT_ENTRY_RE = re.compile(
    r'^\s*"([A-Z][A-Z0-9_]+)":\s*\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)', re.M)
KANA_RE = re.compile(r"[\u3040-\u30ff]")
JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def api_codes() -> dict[str, list[str]]:
    """Every literal error code the coordinator's API package can return."""
    found: dict[str, list[str]] = {}
    for path in sorted(API_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in API_CODE_RE.finditer(source):
            found.setdefault(match.group(1), []).append(path.name)
    return found


def swift_table(source: str) -> dict[str, tuple[str, str]]:
    """`code -> (japanese, english)` as written in the Swift table."""
    return {match.group(1): (match.group(2), match.group(3))
            for match in SWIFT_ENTRY_RE.finditer(source)}


class ErrorPayloadShapeTests(unittest.TestCase):
    def test_single_engine_error_keeps_the_three_key_shape(self):
        payload = MLXBarError("MODEL_INCOMPATIBLE", "boom", 409, False).as_dict()
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(set(payload["error"]), {"code", "message", "retryable"})
        self.assertNotIn("attempts", payload["error"])

    def test_attempts_are_attached_without_touching_the_base_keys(self):
        error = MLXBarError(
            "MODEL_INCOMPATIBLE", "both engines failed", 409, False,
            attempts=[{"engine": "mlx-vlm", "code": "MODEL_INCOMPATIBLE",
                       "message": "no vision tower", "retryable": False},
                      {"engine": "mlx-lm", "code": "MODEL_INCOMPATIBLE",
                       "message": "guard", "retryable": False}])
        payload = error.as_dict()["error"]
        self.assertEqual(payload["code"], "MODEL_INCOMPATIBLE")
        self.assertEqual([item["engine"] for item in payload["attempts"]],
                         ["mlx-vlm", "mlx-lm"])
        self.assertEqual(payload["attempts"][0]["message"], "no vision tower")

    def test_attempts_are_copied_not_aliased(self):
        source = [{"engine": "mlx-vlm", "code": "X", "message": "m", "retryable": False}]
        payload = MLXBarError("X", "m", 409, False, attempts=source).as_dict()
        source[0]["message"] = "mutated"
        self.assertEqual(payload["error"]["attempts"][0]["message"], "m")


class SwiftErrorTableTests(unittest.TestCase):
    """AC-5-6: every code the API can return must be registered for the GUI.

    The Swift table maps `code` -> (Japanese, English); an unregistered code
    falls back to the server's Japanese `message`, which leaks Japanese into an
    English interface. The expected set is read from the API sources, not from a
    list maintained by hand.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = ERROR_TEXT.read_text(encoding="utf-8")
        cls.table = swift_table(cls.source)
        cls.codes = api_codes()

    def test_the_scan_actually_sees_the_api_codes(self):
        # Guards against a silently empty scan making the checks below vacuous.
        self.assertGreaterEqual(len(self.codes), 20, sorted(self.codes))
        # F-01 regression: the 422 this release introduced, and a long-standing
        # one, must be visible to the extraction.
        self.assertIn("INVALID_PROFILE", self.codes)
        self.assertIn("MODEL_NOT_ROUTED", self.codes)

    def test_every_api_code_is_in_the_table(self):
        missing = sorted(code for code in self.codes if code not in self.table)
        detail = ", ".join(f"{code} ({', '.join(sorted(set(self.codes[code])))})"
                           for code in missing)
        self.assertEqual(missing, [], f"unregistered error codes: {detail}")

    def test_every_api_code_has_a_japanese_and_an_english_string(self):
        for code in sorted(self.codes):
            with self.subTest(code=code):
                self.assertIn(code, self.table)
                japanese, english = self.table[code]
                self.assertTrue(japanese.strip(), code)
                self.assertTrue(english.strip(), code)
                # Both halves must be in the intended language: the Japanese
                # side must carry kana/kanji, the English side must carry none.
                self.assertIsNotNone(JAPANESE_RE.search(japanese), code)
                self.assertIsNone(JAPANESE_RE.search(english), code)

    def test_english_text_is_ascii_english(self):
        for code in sorted(self.codes):
            _japanese, english = self.table[code]
            with self.subTest(code=code):
                self.assertIsNone(KANA_RE.search(english), code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
