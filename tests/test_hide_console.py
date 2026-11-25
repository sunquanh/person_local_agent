# -*- coding: utf-8 -*-
"""C 批次：启动后隐藏 cmd 窗口。

真机效果（Windows 上窗口真的消失）只能在用户 Windows 上验；
这里用假 windll 对象测 _hide_console_window 的调用与容错，
以及 hide_console 配置的校验与默认值。
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module


def _fake_windll(hwnd=12345, show_raises=False, get_raises=False):
    calls = []

    def _get():
        if get_raises:
            raise OSError('no console')
        return hwnd

    def _show(h, cmd):
        if show_raises:
            raise OSError('show failed')
        calls.append((h, cmd))
        return 1

    return SimpleNamespace(
        kernel32=SimpleNamespace(GetConsoleWindow=_get),
        user32=SimpleNamespace(ShowWindow=_show)), calls


class TestHideConsoleWindow(unittest.TestCase):
    def test_hides_when_console_exists(self):
        windll, calls = _fake_windll(hwnd=12345)
        self.assertTrue(app_module._hide_console_window(windll))
        self.assertEqual(calls, [(12345, 0)])  # SW_HIDE = 0

    def test_no_console_returns_false(self):
        windll, calls = _fake_windll(hwnd=0)
        self.assertFalse(app_module._hide_console_window(windll))
        self.assertEqual(calls, [])

    def test_get_console_failure_returns_false(self):
        windll, calls = _fake_windll(get_raises=True)
        self.assertFalse(app_module._hide_console_window(windll))
        self.assertEqual(calls, [])

    def test_show_window_failure_returns_false(self):
        windll, calls = _fake_windll(show_raises=True)
        self.assertFalse(app_module._hide_console_window(windll))

    def test_no_arg_on_non_windows_returns_false(self):
        # 本机（Linux）无参数调用必须直接返回 False，不碰 ctypes；
        # Windows 上无参数调用会真藏控制台，跳过。
        if os.name == 'nt':
            self.skipTest('Windows 上无参数调用会藏测试控制台')
        self.assertFalse(app_module._hide_console_window())


class TestHideConsoleConfig(unittest.TestCase):
    def test_validate_rejects_non_bool(self):
        with self.assertRaises(ValueError):
            app_module.validate_config({'server': {'hide_console': 'yes'}})

    def test_validate_accepts_bool(self):
        cfg = app_module.validate_config({'server': {'hide_console': False}})
        self.assertFalse(cfg['server']['hide_console'])

    def test_default_is_true(self):
        from server.storepath import _defaults
        self.assertIs(_defaults()['server']['hide_console'], True)


if __name__ == '__main__':
    unittest.main()
