# -*- coding: utf-8 -*-
"""provider/gemini.py — Gemini Provider（中转站 /v1beta generateContent）。

与 Kimi/DeepSeek 的差异：
  - 走 Gemini 原生端点 /v1beta/models/{model}:generateContent（中转站
    的 OpenAI 兼容 /v1 不支持 gemini 模型名，2026-08-12 实测 503）
  - 输入 messages（[system, user]）映射为 systemInstruction + contents
  - 原生多模态：vision() 用 inline_data 直传图片（不转 base64 URL）
  - 缓存命中统计：usageMetadata 里若有 cachedContentTokenCount 则记录
    （中转站当前不返回；统计归一化在 base.cache_stats() 统一处理）
"""

import base64
import logging
import time

import requests

from .base import LLMProvider, ProviderError

log = logging.getLogger("decision.provider.gemini")

DEFAULT_MODEL = "gemini-3.1-pro-preview"
DEFAULT_BASE_URL = "https://api.aixhan.com"


class GeminiProvider(LLMProvider):
    """Gemini（中转站）。thinking 由中转站按模型自动开（thoughtSignature），
    chat_full 返回 (content, "")——不暴露思考文本。"""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout: int = 120, **kw):
        super().__init__(api_key, model, base_url, timeout=timeout, **kw)
        self._gen_url = (base_url.rstrip("/")
                         + "/v1beta/models/{model}:generateContent")
        # Gemini 不走基类 _url（chat/completions 路径），置空避免误用
        self._url = ""
        self._token_floor = 0          # gemini 不强制思考预留
        self._token_ceiling = 0

    # ---------------------------------------------------------------- 转换
    @staticmethod
    def _split_messages(messages):
        """messages → (system, contents)。system 全并进 systemInstruction；
        其余按 role 映射 user/assistant 进 contents。"""
        system_parts = []
        contents = []
        for m in messages or []:
            role = m.get("role", "user")
            content = m.get("content", "")
            if not content:
                continue
            if role == "system":
                system_parts.append(content)
            else:
                g_role = "model" if role == "assistant" else "user"
                contents.append({"role": g_role,
                                 "parts": [{"text": content}]})
        system = "\n\n".join(system_parts)
        return system, contents

    def _payload(self, messages, max_tokens, temperature):
        system, contents = self._split_messages(messages)
        payload = {"contents": contents or [{"role": "user",
                                             "parts": [{"text": ""}]}],
                   "generationConfig": {"maxOutputTokens":
                                        self._clamp_tokens(max_tokens)}}
        if not self._omit_temperature:
            payload["generationConfig"]["temperature"] = temperature
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    @staticmethod
    def _parse_response(data):
        """generateContent 响应 → (text, usage)。"""
        cands = data.get("candidates") or []
        parts = (cands[0].get("content", {}).get("parts", [])
                 if cands else [])
        text = "".join(p.get("text", "") for p in parts).strip()
        usage = data.get("usageMetadata") or {}
        return text, usage

    def _post(self, payload, timeout):
        url = self._gen_url.format(model=self.model)
        try:
            r = self._sess.post(url, json=payload, timeout=timeout)
        except requests.RequestException as e:
            raise ProviderError(f"gemini request failed: {type(e).__name__}") \
                from None
        if r.status_code != 200:
            raise ProviderError(f"gemini API error: HTTP {r.status_code}: "
                                f"{r.text[:160]}")
        return r.json()

    # ---------------------------------------------------------------- 文本
    def chat(self, messages, max_tokens=2048, temperature=0.8) -> str:
        """POST /v1beta generateContent。5xx/429 退避重试 1 次。"""
        payload = self._payload(messages, max_tokens, temperature)
        last_err = None
        for attempt in range(2):
            try:
                data = self._post(payload, self._timeout)
            except ProviderError as e:
                last_err = str(e)
                if attempt == 0 and ("429" in last_err or "5" in last_err[0:4]
                                     or "request failed" in last_err):
                    time.sleep(4)
                    continue
                raise
            text, usage = self._parse_response(data)
            self._last_usage = usage
            return text
        raise ProviderError(f"gemini retried: {last_err}")

    def chat_full(self, messages, max_tokens=2048, temperature=0.8):
        """chat + thinking。中转站不返回思考文本 → ("", "")。"""
        out = self.chat(messages, max_tokens=max_tokens,
                        temperature=temperature)
        return out, ""

    # ---------------------------------------------------------------- 视觉
    def vision(self, image_bytes: bytes, question: str,
               max_tokens: int = 500) -> str:
        """原生多模态：inline_data 直传图片（Gemini 端点，不经 chat/completions）。"""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "contents": [{"role": "user", "parts": [
                {"text": question},
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": b64}},
            ]}],
            "generationConfig": {"maxOutputTokens":
                                 self._clamp_tokens(max_tokens)},
        }
        data = self._post(payload, self._timeout)
        text, usage = self._parse_response(data)
        self._last_usage = usage
        return text
