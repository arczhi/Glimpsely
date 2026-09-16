"""Local ASR: sherpa-onnx + SenseVoice-Small int8 (CPU, dialect-capable).

链路：微信语音(.silk) → pilk 解码为 wav → SenseVoice 转写 → 文本交给理解管道。
不占用 GPU/统一内存（纯 CPU onnxruntime），与 MLX 互不争抢。
"""

import logging
import threading
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

from .config import Config

_reco = None
_lock = threading.Lock()


def get_recognizer(cfg: Config):
    global _reco
    if _reco is None:
        with _lock:
            if _reco is None:
                _reco = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(cfg.asr_model),
                    tokens=str(cfg.asr_tokens),
                    use_itn=True,
                )
    return _reco


def available(cfg: Config) -> bool:
    return Path(cfg.asr_model).exists() and Path(cfg.asr_tokens).exists()


def _read_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()),
                                dtype=np.int16).astype(np.float32) / 32768.0
    return rate, samples


def transcribe(cfg: Config, audio_path) -> str | None:
    """支持 .silk（微信语音）与 .wav 直通；返回转写文本（无语音内容返回 None）。"""
    path = Path(audio_path)
    try:
        wav_path = path
        if path.suffix.lower() == ".silk":
            import pilk
            wav_path = Path("/tmp") / f"glimpsely_{path.stem}.wav"
            pilk.silk_to_wav(str(path), str(wav_path))
        rate, samples = _read_wav(wav_path)
        reco = get_recognizer(cfg)
        stream = reco.create_stream()
        stream.accept_waveform(rate, samples)
        reco.decode_stream(stream)
        text = (stream.result.text or "").strip()
        return text or None
    except Exception:  # noqa: BLE001 — ASR 失败交由调用方兜底
        logging.getLogger(__name__).exception("asr transcribe failed")
        return None


def warmup(cfg: Config) -> None:
    if not available(cfg):
        logging.getLogger(__name__).warning("asr model missing, asr disabled")
        return
    get_recognizer(cfg)
    logging.getLogger(__name__).info("asr warmed up")
