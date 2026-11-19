# -*- coding: utf-8 -*-
"""工作目录是会话和配置的根。解析不随进程 cwd 变化。"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.storepath import StorePathError, prepare_launch, load_ui, save_ui  # noqa: E402


class TestStorePath(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.prog = self.tmp / '程序'
        self.prog.mkdir()
        self.cwd = Path.cwd()

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_workspace_from_different_cwd(self):
        ws = self.tmp / '工作 区' / '我的台'
        ws.mkdir(parents=True)
        (self.prog / '.workspace-root').write_text(str(ws), encoding='utf-8')
        other = self.tmp / '别的启动目录'
        other.mkdir()
        os.chdir(other)
        first = prepare_launch(str(self.prog))
        os.chdir(self.tmp)
        second = prepare_launch(str(self.prog))
        self.assertEqual(first.workspace_root, str(ws.resolve()))
        self.assertEqual(second.workspace_root, first.workspace_root)
        self.assertEqual(Path(first.config_path), ws.resolve() / '.agent' / 'config.json')
        sid = 'sess-cwd'
        sdir = ws / '.agent' / 'sessions'
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / sid).mkdir()
        (ws / '.agent' / 'sessions.db').write_text('same', encoding='utf-8')
        os.chdir('/')
        again = prepare_launch(str(self.prog))
        self.assertEqual((Path(again.workspace_root) / '.agent' / 'sessions.db').read_text(encoding='utf-8'), 'same')

    def test_relative_root_uses_program_dir_not_cwd(self):
        (self.prog / 'config.json').write_text(json.dumps({
            'workspace': {'root': '相对 工作区'},
            'model': {'name': 'from-legacy', 'api_key': 'secret-legacy'},
        }), encoding='utf-8')
        os.chdir(self.tmp)
        got = prepare_launch(str(self.prog))
        self.assertEqual(got.workspace_root, str((self.prog / '相对 工作区').resolve()))
        self.assertTrue((self.prog / 'config.json').is_file())
        live = json.loads(Path(got.config_path).read_text(encoding='utf-8'))
        self.assertEqual(live['model']['name'], 'from-legacy')
        backup = Path(got.workspace_root) / '.agent' / 'import-backup' / 'config.json'
        self.assertTrue(backup.is_file())
        self.assertIn('secret-legacy', backup.read_text(encoding='utf-8'))

    def test_explicit_config_wins_and_is_not_ignored(self):
        chosen = self.tmp / '显式配置.json'
        ws = self.tmp / '显式工作区'
        chosen.write_text(json.dumps({
            'workspace': {'root': str(ws)},
            'model': {'name': 'explicit-model', 'api_key': 'secret-explicit'},
        }), encoding='utf-8')
        (self.prog / '.workspace-root').write_text(str(self.tmp / '别的'), encoding='utf-8')
        got = prepare_launch(str(self.prog), config_arg=str(chosen))
        self.assertEqual(got.workspace_root, str(ws.resolve()))
        self.assertEqual(got.config['model']['name'], 'explicit-model')
        self.assertEqual(got.explicit_config_path, str(chosen.resolve()))

    def test_existing_workspace_config_not_overwritten(self):
        ws = self.tmp / '已有'
        ws.mkdir()
        agent = ws / '.agent'
        agent.mkdir()
        (agent / 'config.json').write_text(json.dumps({
            'workspace': {'root': str(ws)},
            'model': {'name': 'keep-me', 'api_key': 'secret-keep'},
        }), encoding='utf-8')
        (self.prog / 'config.json').write_text(json.dumps({
            'workspace': {'root': str(ws)},
            'model': {'name': 'legacy', 'api_key': 'secret-legacy'},
        }), encoding='utf-8')
        legacy_bytes = (self.prog / 'config.json').read_bytes()
        got = prepare_launch(str(self.prog))
        self.assertEqual(got.config['model']['name'], 'keep-me')
        self.assertEqual((self.prog / 'config.json').read_bytes(), legacy_bytes)
        note = agent / 'import-backup' / 'config.conflict.txt'
        self.assertTrue(note.is_file())
        self.assertNotIn('secret-legacy', note.read_text(encoding='utf-8'))
        self.assertNotIn('secret-keep', note.read_text(encoding='utf-8'))

    def test_ab_workspaces_do_not_share_sessions_or_config(self):
        a = self.tmp / '工作区A'
        b = self.tmp / '工作区B'
        (self.prog / '.workspace-root').write_text(str(a), encoding='utf-8')
        first = prepare_launch(str(self.prog))
        (Path(first.workspace_root) / '.agent' / 'marker.txt').write_text('only-a', encoding='utf-8')
        cfg = json.loads(Path(first.config_path).read_text(encoding='utf-8'))
        cfg['model'] = {'name': 'model-a', 'api_key': ''}
        Path(first.config_path).write_text(json.dumps(cfg), encoding='utf-8')
        (self.prog / '.workspace-root').write_text(str(b), encoding='utf-8')
        second = prepare_launch(str(self.prog))
        self.assertNotEqual(first.workspace_root, second.workspace_root)
        self.assertFalse((Path(second.workspace_root) / '.agent' / 'marker.txt').exists())
        self.assertEqual((Path(first.workspace_root) / '.agent' / 'marker.txt').read_text(encoding='utf-8'), 'only-a')
        self.assertNotEqual(second.config.get('model', {}).get('name'), 'model-a')

    def test_unwritable_does_not_fall_back_to_program_dir(self):
        blocked = self.tmp / '不是目录'
        blocked.write_text('file', encoding='utf-8')
        ws = blocked / '工作区'
        (self.prog / '.workspace-root').write_text(str(ws), encoding='utf-8')
        with self.assertRaises(StorePathError) as ctx:
            prepare_launch(str(self.prog))
        self.assertIn('工作目录', str(ctx.exception))
        self.assertFalse((self.prog / 'config.json').exists())

    def test_ui_state_roundtrip_strips_secrets(self):
        ws = self.tmp / 'ui区'
        (self.prog / '.workspace-root').write_text(str(ws), encoding='utf-8')
        launch = prepare_launch(str(self.prog))
        saved = save_ui(launch.workspace_root, {
            'theme': 'dark',
            'last_session': 'abc',
            'model_overrides': {'s1': 'm'},
            'api_key': 'secret-ui',
            'panel_widths': {'sb': 220},
        })
        self.assertNotIn('api_key', saved)
        text = Path(launch.workspace_root, '.agent', 'ui.json').read_text(encoding='utf-8')
        self.assertNotIn('secret-ui', text)
        again = load_ui(launch.workspace_root)
        self.assertEqual(again['theme'], 'dark')
        self.assertEqual(again['last_session'], 'abc')


class TestTurnDiffLines(unittest.TestCase):
    def test_added_changed_and_unchanged(self):
        from agent.workspace import SessionCtx, Workspace
        root = self.tmp if hasattr(self, 'tmp') else Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        ws_root = root / 'ws'
        ws = Workspace(str(ws_root))
        notes = ws_root / 'notes'
        (notes / 'old.md').write_text('保持\n旧行\n', encoding='utf-8')
        (notes / 'same.md').write_text('无变化\n', encoding='utf-8')
        (notes / 'untouched.md').write_text('别动\n', encoding='utf-8')
        ws.set_session(SessionCtx('s1', '测试', '2026-01-01'))
        turn = ws.begin_turn('s1', '100')
        new_real, _new_rel = ws.resolve_write('notes/new.md')
        new_real.write_text('新增甲\n新增乙\n', encoding='utf-8')
        old_real, _old_rel = ws.resolve_write('notes/old.md')
        old_real.write_text('保持\n新行\n', encoding='utf-8')
        same_real, _same_rel = ws.resolve_write('notes/same.md')
        same_real.write_text('无变化\n', encoding='utf-8')
        ws.end_turn()
        diff = ws.turn_diff('s1', turn)
        by = {f['rel']: f for f in diff['files']}
        self.assertNotIn('notes/untouched.md', by)
        added = by['notes/new.md']
        self.assertEqual(added['status'], 'added')
        self.assertEqual(added['added'], 2)
        self.assertEqual(added['removed'], 0)
        self.assertTrue(any(l['t'] == '+' and '新增甲' in l['s'] for l in added['lines']))
        changed = by['notes/old.md']
        self.assertEqual(changed['status'], 'modified')
        self.assertGreaterEqual(changed['removed'], 1)
        self.assertTrue(any(l['t'] == '+' and '新行' in l['s'] for l in changed['lines']))
        self.assertTrue(any(l['t'] == '-' and '旧行' in l['s'] for l in changed['lines']))
        same = by['notes/same.md']
        self.assertEqual(same['added'], 0)
        self.assertEqual(same['removed'], 0)
