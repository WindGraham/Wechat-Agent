# -*- coding: utf-8 -*-
"""test_mm_fallback.py — 多模态 provider 能力标记与回退的离线单测。

2026-09-01：DeepSeek 文本模型吃 image_url 必 400（官方：仅
deepseek-v4-flash-vision-exp 收图，其他模型 "This model does not
support image"）。decider 在窗口含图且主 provider 不支持时切
mm_provider（默认 deepseek/deepseek-v4-flash-vision-exp）。
"""

import unittest

from src.decision.provider.base import DeepSeekProvider, KimiProvider
from src.decision.proxy.decider import Decider


class SupportsImagesTest(unittest.TestCase):
    def test_deepseek_text_model_no_images(self):
        p = DeepSeekProvider("dummy-key", model="deepseek-v4-pro")
        self.assertFalse(p.supports_images)
        self.assertFalse(Decider._supports_image_chat(p))

    def test_deepseek_vision_model_supports_images(self):
        p = DeepSeekProvider("dummy-key",
                             model="deepseek-v4-flash-vision-exp")
        self.assertTrue(p.supports_images)
        self.assertTrue(Decider._supports_image_chat(p))

    def test_kimi_supports_images_by_default(self):
        p = KimiProvider("dummy-key", model="k3")
        self.assertTrue(Decider._supports_image_chat(p))


class MmProviderRegistryTest(unittest.TestCase):
    def test_mm_provider_lazy_create_and_cache(self):
        from src.decision.proxy.providers import ProviderRegistry

        calls = []

        class _FakeProvider:
            supports_images = True
            model = "deepseek-v4-flash-vision-exp"

            def set_token_limits(self, a, b):
                pass

        reg = ProviderRegistry(_FakeProvider(), lambda k, d=None: d)
        import src.decision.provider.factory as factory
        orig = factory.create_provider
        factory.create_provider = lambda prefer, model: (
            calls.append((prefer, model)) or _FakeProvider())
        try:
            p1 = reg.mm_provider()
            p2 = reg.mm_provider()
        finally:
            factory.create_provider = orig
        self.assertEqual(calls, [("deepseek", "deepseek-v4-flash-vision-exp")])
        self.assertIs(p1, p2)                 # 缓存：只创建一次


if __name__ == "__main__":
    unittest.main()
