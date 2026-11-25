# -*- coding: utf-8 -*-
"""记忆召回兜底：问记忆本身且关键词召回为空时，回退到最新重要事实。

回归：欢迎页显示 15 条记忆，但问 agent“你有什么记忆”时它说没有——
build_context 只注入关键词召回的事实，泛问的词（记忆/记得）几乎不会
出现在事实文本里，召回为空就什么都不注入。兜底只在“问记忆本身”时触发。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agent.memory import Memory


class TestMemoryRecallFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mem = Memory(str(self.tmp / 'ws' / '.agent' / 'memory.db'))
        # 两条与“记忆/记得”毫无关键词交集的事实
        self.mem.save_fact('用户在深圳做跨境电商，主卖户外灯具', important=True)
        self.mem.save_fact('用户偏好用中文回复，结论先行')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generic_memory_question_falls_back(self):
        ctx = self.mem.build_context('你有什么记忆')
        self.assertIn('户外灯具', ctx)
        self.assertIn('最近的重要事实', ctx)
        # 回退按重要优先排序（复审 S2）：重要事实在前
        self.assertLess(ctx.index('户外灯具'), ctx.index('中文回复'))

    def test_generic_memory_question_variant_falls_back(self):
        ctx = self.mem.build_context('你还记得我吗')
        self.assertIn('户外灯具', ctx)
        self.assertIn('最近的重要事实', ctx)

    def test_instruction_with_remember_does_not_trigger(self):
        # “记得帮我发”是指令，不是问记忆：不得回退注入无关事实
        ctx = self.mem.build_context('记得帮我把报表发给张三')
        self.assertNotIn('户外灯具', ctx)
        self.assertNotIn('长期记忆', ctx)

    def test_instruction_with_you_remember_does_not_trigger(self):
        # 带“你”的指令形式同样不得触发（复审 S1：第一版会误判）
        ctx = self.mem.build_context('你记得帮我发一下报表')
        self.assertNotIn('户外灯具', ctx)
        self.assertNotIn('长期记忆', ctx)

    def test_instruction_remember_my_email_does_not_trigger(self):
        # “请记住我的邮箱是…”是让模型记下新事实，不是问记忆
        ctx = self.mem.build_context('请记住我的邮箱是a@b.com')
        self.assertNotIn('户外灯具', ctx)
        self.assertNotIn('长期记忆', ctx)

    def test_how_much_do_you_know_about_me_falls_back(self):
        ctx = self.mem.build_context('你对我知道多少')
        self.assertIn('户外灯具', ctx)
        self.assertIn('最近的重要事实', ctx)

    def test_info_about_me_falls_back(self):
        ctx = self.mem.build_context('你有什么关于我的信息')
        self.assertIn('户外灯具', ctx)
        self.assertIn('最近的重要事实', ctx)

    def test_do_you_remember_me_falls_back(self):
        ctx = self.mem.build_context('你记不记得我')
        self.assertIn('户外灯具', ctx)
        self.assertIn('最近的重要事实', ctx)

    def test_plain_question_without_recall_injects_nothing(self):
        ctx = self.mem.build_context('今天天气怎么样')
        self.assertNotIn('户外灯具', ctx)
        self.assertNotIn('长期记忆', ctx)

    def test_keyword_hit_still_uses_recall(self):
        ctx = self.mem.build_context('我的户外灯具生意怎么样')
        self.assertIn('户外灯具', ctx)
        self.assertIn('相关事实', ctx)
        self.assertNotIn('最近的重要事实', ctx)


if __name__ == '__main__':
    unittest.main()
