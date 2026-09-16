"""Unit tests: ASR pipeline (silk decode + SenseVoice transcribe)."""

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def _make_tone_wav(path: Path, seconds: float = 1.0, rate: int = 24000) -> None:
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(tone.tobytes())


def test_available():
    from glimpsely.asr import available
    from glimpsely.config import Config
    cfg = Config.load(ROOT)
    if not available(cfg):
        pytest.skip("asr model not downloaded (models/sensevoice/)")
    assert available(cfg)


def test_silk_roundtrip_transcribe(tmp_path):
    """wav → silk(模拟微信语音) → silk_to_wav → ASR，全链路跑通。"""
    pytest.importorskip("pilk")
    from glimpsely.asr import transcribe
    from glimpsely.config import Config
    if not (ROOT / "models/sensevoice/model.int8.onnx").exists():
        pytest.skip("asr model not downloaded")

    import pilk
    cfg = Config.load(ROOT)
    wav = tmp_path / "src.wav"
    rate = 24000
    _make_tone_wav(wav, rate=rate)
    silk = tmp_path / "voice.silk"
    pilk.encode(str(wav), str(silk))
    assert silk.exists() and silk.stat().st_size > 0
    text = transcribe(cfg, silk)
    assert text is None or isinstance(text, str)  # 纯音调无语义 → None


def test_real_speech_transcribe():
    zh_wav = ROOT / "models/sensevoice/zh.wav"
    if not zh_wav.exists():
        pytest.skip("zh.wav not downloaded")
    from glimpsely.asr import transcribe
    from glimpsely.config import Config
    text = transcribe(Config.load(ROOT), zh_wav)
    assert text and len(text) >= 4


def test_transcribe_missing_file_returns_none(tmp_path):
    from glimpsely.asr import transcribe
    from glimpsely.config import Config
    assert transcribe(Config.load(ROOT), tmp_path / "nope.wav") is None
