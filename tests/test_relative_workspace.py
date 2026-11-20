# -*- coding: utf-8 -*-
"""相对工作目录：程序目录内的工作区存相对路径，整个文件夹拷走仍指向新位置。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.storepath import prepare_launch, read_pointer  # noqa: E402


class RelativeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.tmp = Path(tempfile.mkdtemp(prefix='relws-'))
        self.prog = self.tmp / 'agent'
        self.prog.mkdir()

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)
        home_ws = Path.home() / '我的工作台'
        if home_ws.is_dir() and not any(home_ws.iterdir()):
            home_ws.rmdir()

    def _legacy(self, prog, root_value):
        (prog / 'config.json').write_text(json.dumps({
            'workspace': {'root': root_value},
            'model': {'name': 'm', 'api_key': ''},
        }), encoding='utf-8')

    def test_pointer_and_live_config_store_relative_inside_program_dir(self):
        self._legacy(self.prog, 'work')
        got = prepare_launch(str(self.prog))
        self.assertEqual(got.workspace_root, str((self.prog / 'work').resolve()))
        pointer = (self.prog / '.workspace-root').read_text(encoding='utf-8').strip()
        self.assertEqual(pointer, 'work')
        live = json.loads(Path(got.config_path).read_text(encoding='utf-8'))
        self.assertEqual(live['workspace']['root'], 'work')
        # 内存里的 config 仍是绝对路径，运行期比较不受影响
        self.assertEqual(got.config['workspace']['root'], got.workspace_root)

    def test_copied_program_dir_resolves_to_new_location(self):
        self._legacy(self.prog, 'work')
        prepare_launch(str(self.prog))
        moved = self.tmp / '另一块盘' / 'agent'
        moved.parent.mkdir(parents=True)
        shutil.copytree(self.prog, moved)
        got = prepare_launch(str(moved))
        self.assertEqual(got.workspace_root, str((moved / 'work').resolve()))
        self.assertEqual(read_pointer(str(moved)), str((moved / 'work').resolve()))

    def test_outside_program_dir_stays_absolute(self):
        outside = self.tmp / '外面的台'
        self._legacy(self.prog, str(outside))
        got = prepare_launch(str(self.prog))
        self.assertEqual(got.workspace_root, str(outside.resolve()))
        pointer = (self.prog / '.workspace-root').read_text(encoding='utf-8').strip()
        self.assertEqual(pointer, str(outside.resolve()))
        live = json.loads(Path(got.config_path).read_text(encoding='utf-8'))
        self.assertEqual(live['workspace']['root'], str(outside.resolve()))

    def test_fresh_install_defaults_to_program_work(self):
        got = prepare_launch(str(self.prog))
        self.assertEqual(got.workspace_root, str((self.prog / 'work').resolve()))
        pointer = (self.prog / '.workspace-root').read_text(encoding='utf-8').strip()
        self.assertEqual(pointer, 'work')

    def test_existing_absolute_pointer_still_works(self):
        ws = self.prog / 'work'
        ws.mkdir()
        (self.prog / '.workspace-root').write_text(str(ws.resolve()), encoding='utf-8')
        got = prepare_launch(str(self.prog))
        self.assertEqual(got.workspace_root, str(ws.resolve()))
        # 写回时自动转相对
        pointer = (self.prog / '.workspace-root').read_text(encoding='utf-8').strip()
        self.assertEqual(pointer, 'work')


if __name__ == '__main__':
    unittest.main()
