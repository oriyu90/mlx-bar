"""v3.1.0 の欠落テスト（WP-911）: GUI 側の不変条件をソースで固定する.

v3.1.0 は Tests を 1 行も追加せずにリリースされ、DESIGN が参照する
`test_v3_1_0_gui_stability.py` は存在しなかった。Swift の挙動そのものは
`Tests/MLXBarTests/`（XCTest）が受け持つが、Python のスイートからも
「壊れたら気づく」最低限の不変条件を確認できるようにしておく。

対象は ISS-1（赤文字の寿命）と ISS-3/ISS-4 の周辺:
* 20 秒はハードコードではなく定数で、テストから差し替えられる;
* ステータスポーリングの catch は `errorMessage` を書かない（再アームの根）;
* 持続的な接続断は `connectionLost` という別チャネル;
* preload 状態は additive な `PreloadStatus` としてデコードされる。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEW_MODEL = ROOT / "Sources" / "MLXBar" / "MenuBar" / "MenuBarViewModel.swift"
MENU_VIEW = ROOT / "Sources" / "MLXBar" / "MenuBar" / "MenuBarView.swift"
EN_STRINGS = (ROOT / "Sources" / "MLXBar" / "Resources" / "en.lproj"
              / "Localizable.strings")
SWIFT_TESTS = ROOT / "Tests" / "MLXBarTests"


def function_body(source: str, name: str) -> str:
    """Return the brace-balanced body of `func name` in `source`."""
    match = re.search(rf"\bfunc {re.escape(name)}\s*\(", source)
    if match is None:
        raise AssertionError(f"func {name} not found")
    start = source.index("{", match.end())
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unbalanced braces in {name}")


class ErrorLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = VIEW_MODEL.read_text(encoding="utf-8")

    def test_the_lifetime_is_a_named_constant(self):
        self.assertIn("errorAutoDismissDuration", self.source)
        # It must be overridable by the tests rather than hard-coded in the
        # Task body (the v3.1.0 code slept `.seconds(20)` inline).
        self.assertRegex(self.source, r"var errorDismissDuration")

    def test_a_cancelled_timer_does_not_clear_the_newer_error(self):
        # The timer lives in `errorMessage.didSet`, not in a method.
        start = self.source.index("var errorMessage: String?")
        end = self.source.index("private var errorClearTask")
        body = self.source[start:end]
        self.assertIn("catch { return }", body)
        # The generation/value guard: a timer that lost the race must not nil
        # out the message that replaced it.
        self.assertIn("errorClearGeneration", body)

    def test_status_polling_never_writes_the_transient_banner(self):
        body = function_body(self.source, "refreshStatus")
        self.assertNotRegex(body, r"(?<![\w.])errorMessage\s*=(?!=)")
        self.assertIn("connectionLost", body)


class ConnectionStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = VIEW_MODEL.read_text(encoding="utf-8")

    def test_connection_loss_is_its_own_channel(self):
        self.assertRegex(self.source, r"var connectionLost")

    def test_the_icon_uses_it_instead_of_the_red_banner(self):
        source = self.source
        match = re.search(r"var icon: String \{(.*?)\n    \}", source, re.S)
        self.assertIsNotNone(match)
        self.assertIn("connectionLost", match.group(1))

    def test_the_menu_view_shows_a_temporary_badge_for_session_pins(self):
        menu = MENU_VIEW.read_text(encoding="utf-8")
        self.assertIn('LS("一時")', menu)
        self.assertIn("profilePinned", menu)


class PreloadSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = VIEW_MODEL.read_text(encoding="utf-8")

    def test_the_status_field_is_decoded(self):
        self.assertIn("struct PreloadStatus", self.source)
        self.assertIn('json["preload"]', self.source)

    def test_the_settings_mirror_is_read_at_startup(self):
        self.assertIn("UserDefaults", self.source)
        self.assertIn("guiLanguage", self.source)


class SwiftTestTargetTests(unittest.TestCase):
    def test_the_swift_test_target_exists(self):
        package = (ROOT / "Package.swift").read_text(encoding="utf-8")
        self.assertIn(".testTarget", package)
        self.assertIn("MLXBarTests", package)
        self.assertTrue(SWIFT_TESTS.is_dir())
        self.assertTrue(list(SWIFT_TESTS.glob("*.swift")))

    def test_new_strings_have_english_translations(self):
        english = EN_STRINGS.read_text(encoding="utf-8")
        for literal in ("常駐モデルを読み込み中…", "一時", "設定を読み込み中"):
            self.assertIn(f'"{literal}"', english)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
