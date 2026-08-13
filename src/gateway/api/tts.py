# -*- coding: utf-8 -*-
"""gateway/api/tts.py — TTS 模型对比 API 蓝图。

输入一段文本，用 MiMo TTS / MiniMax TTS 合成语音，返回 base64 data URL。
两家各支持多种「音色来源」：

  MiMo     preset(预置) / clone(克隆 NekoAudio 猫娘音) / design(文本设计音色)
  MiniMax  preset(预置) / clone(复刻 NekoAudio 猫娘音 → voice_id)

密钥读取 workspace/.env（MIMO_API_KEY / MINIMAX_API_KEY），绝不回传前端。
MiniMax 复刻 voice_id 持久化在 workspace/runtime/tts_voices.json。

路由：
  /api/tts/voices   GET   音色 + 音色来源模式 + key 就绪状态
  /api/tts/compare  POST  {text, mimo_mode, minimax_mode, mimo_design?}
  /api/tts/clone    POST  MiniMax 音色复刻（重新生成 voice_id）
"""

import base64
import json
import os
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Blueprint, jsonify, request, send_file

from .common import parse_env

ENV_REL = os.path.join("workspace", ".env")
SAMPLE_WAV_REL = os.path.join("workspace", "neko_audio", "samples", "sample_clean.wav")
TTV_REL = os.path.join("workspace", "runtime", "tts_voices.json")
SHOWCASE_REL = os.path.join("workspace", "mimo_showcase")
VOXCPM_OUT_REL = os.path.join("workspace", "voxcpm_out")
NEKO_AUDIO_REL = os.path.join("workspace", "neko_audio", "audio")
NEKO_META_REL = os.path.join("workspace", "neko_audio", "metadata.jsonl")

# showcase 展示目录：来源标签 → 相对路径
SHOWCASE_DIRS = {
    "mimo": SHOWCASE_REL,
    "voxcpm": VOXCPM_OUT_REL,
}

# MiMo 预置音色（活泼少女/知性优先，贴近猫娘）
MIMO_VOICES = {
    "冰糖": "活泼少女",
    "茉莉": "知性女声",
    "苏打": "阳光少年(男)",
    "白桦": "成熟男声(男)",
    "Mia": "英文女声",
}
MIMO_DEFAULT = "冰糖"

# MiniMax 预置音色（中文可爱/少女系优先）
MINIMAX_VOICES = {
    "female-shaonv": "少女",
    "female-tianmei": "甜美女性",
    "qiaopi_mengmei": "俏皮萌妹",
    "tianxin_xiaoling": "甜心小玲",
    "diadia_xuemei": "嗲嗲学妹",
    "lovely_girl": "萌萌女童",
    "female-yujie": "御姐",
}
MINIMAX_DEFAULT = "qiaopi_mengmei"

# 音色来源模式
MIMO_MODES = {
    "preset": "预置音色",
    "clone": "克隆猫娘音(sample_clean)",
    "design": "音色设计(文本描述)",
}
MINIMAX_MODES = {
    "preset": "预置音色",
    "clone": "克隆猫娘音(复刻voice_id)",
}
MIMO_DEFAULT_MODE = "preset"
MINIMAX_DEFAULT_MODE = "preset"

# MiMo voicedesign 默认音色描述
MIMO_DESIGN_DEFAULT = "可爱的猫娘声线，活泼黏人的少女音，尾音轻轻带一点喵"

MIMO_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_MODEL_PRESET = "mimo-v2.5-tts"
MIMO_MODEL_CLONE = "mimo-v2.5-tts-voiceclone"
MIMO_MODEL_DESIGN = "mimo-v2.5-tts-voicedesign"
MINIMAX_URL = "https://api.minimaxi.com/v1/t2a_v2"
MINIMAX_UPLOAD_URL = "https://api.minimaxi.com/v1/files/upload"
MINIMAX_CLONE_URL = "https://api.minimaxi.com/v1/voice_clone"
MINIMAX_MODEL = "speech-2.8-hd"

TIMEOUT_S = 90


# ------------------------------------------------------------------ 基础设施
def _read_env(root):
    path = os.path.join(root, ENV_REL)
    return parse_env(path) if os.path.isfile(path) else {}


def _load_tts_voices(root):
    path = os.path.join(root, TTV_REL)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_tts_voices(root, d):
    path = os.path.join(root, TTV_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _data_url(audio_bytes, mime):
    return "data:%s;base64,%s" % (mime,
                                  base64.b64encode(audio_bytes).decode())


def _neko_talks(root, max_lines=200):
    """file_name → talk 台词映射（读 metadata.jsonl 前 max_lines 行）。

    已下载的素材集中在 00/0~44.wav，都在 metadata 前几十行内；限定行数避免
    每次请求读全量 157MB。
    """
    path = os.path.join(root, NEKO_META_REL)
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                fn = d.get("file_name", "")
                if fn:
                    out[fn] = (d.get("talk") or "").replace("\n", " ")[:120]
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ MiMo 合成
def _mimo_call(model, messages, audio, api_key):
    r = requests.post(
        MIMO_URL,
        headers={"Authorization": "Bearer %s" % api_key,
                 "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "audio": audio},
        timeout=TIMEOUT_S)
    if r.status_code != 200:
        raise RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:200]))
    j = r.json()
    ad = (j.get("choices") or [{}])[0].get("message", {}).get("audio", {}).get("data")
    if not ad:
        raise RuntimeError("响应无音频: %s" % str(j)[:200])
    return base64.b64decode(ad), "audio/wav"


def _mimo_preset(text, voice, api_key):
    return _mimo_call(
        MIMO_MODEL_PRESET,
        [{"role": "assistant", "content": text}],
        {"format": "wav", "voice": voice}, api_key)


def _mimo_clone(text, api_key, sample_path):
    if not os.path.isfile(sample_path):
        raise RuntimeError("缺参考音频 workspace/neko_audio/samples/sample_0.wav")
    with open(sample_path, "rb") as f:
        wav = f.read()
    voice = "data:audio/wav;base64,%s" % base64.b64encode(wav).decode()
    return _mimo_call(
        MIMO_MODEL_CLONE,
        [{"role": "assistant", "content": text}],
        {"format": "wav", "voice": voice}, api_key)


def _mimo_design(text, desc, api_key):
    return _mimo_call(
        MIMO_MODEL_DESIGN,
        [{"role": "user", "content": desc},
         {"role": "assistant", "content": text}],
        {"format": "wav"}, api_key)


def _mimo_dispatch(mode, text, voice, design_desc, api_key, sample_path):
    if mode == "clone":
        return _mimo_clone(text, api_key, sample_path)
    if mode == "design":
        return _mimo_design(text, design_desc, api_key)
    return _mimo_preset(text, voice, api_key)


# ------------------------------------------------------------------ MiniMax 合成
def _minimax_call(text, voice_id, api_key):
    r = requests.post(
        MINIMAX_URL,
        headers={"Authorization": "Bearer %s" % api_key,
                 "Content-Type": "application/json"},
        json={"model": MINIMAX_MODEL, "text": text, "stream": False,
              "voice_setting": {"voice_id": voice_id, "speed": 1,
                                "vol": 1, "pitch": 0},
              "audio_setting": {"sample_rate": 32000, "bitrate": 128000,
                                "format": "mp3", "channel": 1}},
        timeout=TIMEOUT_S)
    if r.status_code != 200:
        raise RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:200]))
    j = r.json()
    hexa = (j.get("data") or {}).get("audio")
    if not hexa:
        raise RuntimeError("响应无音频: %s" % str(j)[:200])
    return bytes.fromhex(hexa), "audio/mpeg"


def _minimax_dispatch(mode, text, voice, clone_voice_id, api_key):
    if mode == "clone":
        if not clone_voice_id:
            raise RuntimeError("MiniMax 尚未复刻，先 POST /api/tts/clone")
        return _minimax_call(text, clone_voice_id, api_key)
    return _minimax_call(text, voice, api_key)


def _minimax_clone_voice(api_key, sample_path):
    """上传参考音频 → 复刻 → 返回新 voice_id。"""
    with open(sample_path, "rb") as f:
        wav = f.read()
    r = requests.post(
        MINIMAX_UPLOAD_URL,
        headers={"Authorization": "Bearer %s" % api_key},
        data={"purpose": "voice_clone"},
        files={"file": (os.path.basename(sample_path), wav, "audio/wav")},
        timeout=TIMEOUT_S)
    if r.status_code != 200:
        raise RuntimeError("上传失败 HTTP %s: %s" % (r.status_code, r.text[:200]))
    file_id = (r.json().get("file") or {}).get("file_id")
    if not file_id:
        raise RuntimeError("上传无 file_id: %s" % str(r.json())[:200])
    voice_id = "NekoAudio%s" % int(time.time())
    r2 = requests.post(
        MINIMAX_CLONE_URL,
        headers={"Authorization": "Bearer %s" % api_key,
                 "Content-Type": "application/json"},
        json={"file_id": file_id, "voice_id": voice_id, "model": MINIMAX_MODEL},
        timeout=TIMEOUT_S)
    if r2.status_code != 200:
        raise RuntimeError("复刻失败 HTTP %s: %s" % (r2.status_code, r2.text[:200]))
    br = (r2.json().get("base_resp") or {})
    if br.get("status_code") != 0:
        raise RuntimeError("复刻失败: %s" % br.get("status_msg"))
    return voice_id


# ------------------------------------------------------------------ 统一结果包装
def _run_one(fn, *args):
    """单路合成结果包装（不抛穿边界，失败转结构化 dict）。"""
    t0 = time.time()
    try:
        audio, mime = fn(*args)
        return {"ok": True, "mime": mime,
                "data_url": _data_url(audio, mime),
                "bytes": len(audio),
                "latency_s": round(time.time() - t0, 2)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "latency_s": round(time.time() - t0, 2)}


# ------------------------------------------------------------------ 蓝图
def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    bp = Blueprint("tts_api", __name__)

    @bp.route("/api/tts/voices", methods=["GET"])
    def api_tts_voices():
        env = _read_env(root)
        tv = _load_tts_voices(root)
        return jsonify({
            "ok": True,
            "mimo": {
                "ready": bool(env.get("MIMO_API_KEY")),
                "voices": [{"id": k, "label": v} for k, v in MIMO_VOICES.items()],
                "default": MIMO_DEFAULT,
                "modes": MIMO_MODES,
                "default_mode": MIMO_DEFAULT_MODE,
                "design_default": MIMO_DESIGN_DEFAULT,
            },
            "minimax": {
                "ready": bool(env.get("MINIMAX_API_KEY")),
                "voices": [{"id": k, "label": v} for k, v in MINIMAX_VOICES.items()],
                "default": MINIMAX_DEFAULT,
                "modes": MINIMAX_MODES,
                "default_mode": MINIMAX_DEFAULT_MODE,
                "clone_voice_id": tv.get("minimax_clone_voice_id", ""),
            },
        })

    @bp.route("/api/tts/compare", methods=["POST"])
    def api_tts_compare():
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "缺 text"}), 400
        if len(text) > 5000:
            return jsonify({"ok": False, "error": "文本过长(>5000 字符)"}), 400

        env = _read_env(root)
        mimo_key = env.get("MIMO_API_KEY", "")
        mm_key = env.get("MINIMAX_API_KEY", "")
        if not mimo_key and not mm_key:
            return jsonify({"ok": False,
                            "error": "workspace/.env 缺 MIMO_API_KEY / "
                                     "MINIMAX_API_KEY"}), 400

        mimo_mode = body.get("mimo_mode") or MIMO_DEFAULT_MODE
        mm_mode = body.get("minimax_mode") or MINIMAX_DEFAULT_MODE
        if mimo_mode not in MIMO_MODES:
            mimo_mode = MIMO_DEFAULT_MODE
        if mm_mode not in MINIMAX_MODES:
            mm_mode = MINIMAX_DEFAULT_MODE

        mimo_voice = (body.get("mimo_voice") or "").strip() or MIMO_DEFAULT
        mm_voice = (body.get("minimax_voice") or "").strip() or MINIMAX_DEFAULT
        if mimo_voice not in MIMO_VOICES:
            mimo_voice = MIMO_DEFAULT
        if mm_voice not in MINIMAX_VOICES:
            mm_voice = MINIMAX_DEFAULT

        mimo_design = (body.get("mimo_design") or "").strip() or MIMO_DESIGN_DEFAULT
        sample_path = os.path.join(root, SAMPLE_WAV_REL)
        tv = _load_tts_voices(root)
        clone_voice_id = tv.get("minimax_clone_voice_id", "")

        # 两路并发，各自按 mode 分派
        results = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {}
            if mimo_key:
                futures["mimo"] = ex.submit(
                    _run_one, _mimo_dispatch, mimo_mode, text, mimo_voice,
                    mimo_design, mimo_key, sample_path)
            if mm_key:
                futures["minimax"] = ex.submit(
                    _run_one, _minimax_dispatch, mm_mode, text, mm_voice,
                    clone_voice_id, mm_key)
            for name, fut in futures.items():
                results[name] = fut.result()

        def _voice_label(mode, preset_voice, clone_id=""):
            if mode == "clone":
                return clone_id or "sample_clean"
            if mode == "design":
                return "设计音色"
            return preset_voice

        out = {"ok": True, "text": text, "mimo": None, "minimax": None}
        if not mimo_key:
            out["mimo"] = {"ok": False, "error": "缺 MIMO_API_KEY"}
        else:
            out["mimo"] = {**results["mimo"],
                           "voice": _voice_label(mimo_mode, mimo_voice),
                           "mode": mimo_mode}
        if not mm_key:
            out["minimax"] = {"ok": False, "error": "缺 MINIMAX_API_KEY"}
        else:
            out["minimax"] = {**results["minimax"],
                              "voice": _voice_label(mm_mode, mm_voice,
                                                    clone_voice_id),
                              "mode": mm_mode}
        return jsonify(out)

    @bp.route("/api/tts/clone", methods=["POST"])
    def api_tts_clone():
        env = _read_env(root)
        mm_key = env.get("MINIMAX_API_KEY", "")
        if not mm_key:
            return jsonify({"ok": False, "error": "缺 MINIMAX_API_KEY"}), 400
        sample_path = os.path.join(root, SAMPLE_WAV_REL)
        if not os.path.isfile(sample_path):
            return jsonify({"ok": False,
                            "error": "缺参考音频 workspace/neko_audio/"
                                     "samples/sample_0.wav"}), 400
        try:
            voice_id = _minimax_clone_voice(mm_key, sample_path)
            tv = _load_tts_voices(root)
            tv["minimax_clone_voice_id"] = voice_id
            _save_tts_voices(root, tv)
            return jsonify({"ok": True, "voice_id": voice_id})
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False,
                            "error": "%s: %s" % (type(e).__name__, e)}), 500

    @bp.route("/api/tts/showcase", methods=["GET"])
    def api_tts_showcase():
        """列出 showcase 目录（mimo_showcase + voxcpm_out）下的音频作品。"""
        files = []
        for source, rel in SHOWCASE_DIRS.items():
            d = os.path.join(root, rel)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".wav"):
                    continue
                p = os.path.join(d, fn)
                dur = 0.0
                try:
                    w = wave.open(p)
                    dur = round(w.getnframes() / w.getframerate(), 2)
                    w.close()
                except (OSError, wave.Error):
                    pass
                files.append({
                    "name": fn,
                    "source": source,
                    "size": os.path.getsize(p),
                    "duration": dur,
                    "mtime": os.path.getmtime(p),
                })
        files.sort(key=lambda f: f["source"] + f["name"])
        return jsonify({"ok": True, "files": files})

    @bp.route("/api/tts/showcase/audio", methods=["GET"])
    def api_tts_showcase_audio():
        """返回单个作品音频（音频流，支持 <audio> 直接播放/seek）。"""
        name = (request.args.get("name") or "").strip()
        source = (request.args.get("source") or "mimo").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            return jsonify({"ok": False, "error": "bad name"}), 400
        rel = SHOWCASE_DIRS.get(source)
        if not rel:
            return jsonify({"ok": False, "error": "bad source"}), 400
        p = os.path.join(root, rel, name)
        if not os.path.isfile(p):
            return jsonify({"ok": False, "error": "not found"}), 404
        return send_file(p, mimetype="audio/wav")

    @bp.route("/api/tts/neko", methods=["GET"])
    def api_tts_neko():
        """列出 NekoAudio 已下载素材（文件名 + 时长 + 配对台词）。"""
        d = os.path.join(root, NEKO_AUDIO_REL)
        talks = _neko_talks(root)
        files = []
        if os.path.isdir(d):
            for sub in sorted(os.listdir(d)):
                subdir = os.path.join(d, sub)
                if not os.path.isdir(subdir):
                    continue
                for fn in sorted(os.listdir(subdir)):
                    if not fn.endswith(".wav"):
                        continue
                    rel = "%s/%s" % (sub, fn)
                    p = os.path.join(subdir, fn)
                    dur = 0.0
                    try:
                        w = wave.open(p)
                        dur = round(w.getnframes() / w.getframerate(), 2)
                        w.close()
                    except (OSError, wave.Error):
                        pass
                    files.append({
                        "name": rel,
                        "duration": dur,
                        "talk": talks.get(rel, ""),
                    })
        files.sort(key=lambda f: f["name"])
        return jsonify({"ok": True, "files": files,
                        "total": len(files)})

    @bp.route("/api/tts/neko/audio", methods=["GET"])
    def api_tts_neko_audio():
        """返回单个 NekoAudio 素材音频流。"""
        name = (request.args.get("name") or "").strip()
        if not name or ".." in name or name.startswith("/"):
            return jsonify({"ok": False, "error": "bad name"}), 400
        p = os.path.join(root, NEKO_AUDIO_REL, name)
        if not os.path.isfile(p):
            return jsonify({"ok": False, "error": "not found"}), 404
        return send_file(p, mimetype="audio/wav")

    return bp
