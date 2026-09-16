"""Embedding provider: omlx /v1/embeddings (e.g. bge-m3) if available, else local hashing.

本地兜底算法：字符 bigram（CJK）+ 英文词元的哈希袋向量（512 维，L2 归一化）。
omlx 装载 embedding 模型后自动切换真语义向量（维度随模型）。
"""

import hashlib
import math
import re

import httpx

from .config import Config

EMBED_DIM = 512  # 本地兜底维度；remote 模型（如 bge-m3）可能为 1024

_remote_ok: bool | None = None  # 探测缓存（进程级）
_remote_dim: int | None = None
_default_cfg: Config | None = None


def _probe_cfg() -> Config:
    global _default_cfg
    if _default_cfg is None:
        _default_cfg = Config.load()
    return _default_cfg


def _remote_embed(texts: list[str]) -> list[list[float]] | None:
    global _remote_ok, _remote_dim
    if _remote_ok is False:
        return None
    try:
        resp = httpx.post(
            f"{_probe_cfg().omlx_base_url}/v1/embeddings",
            json={"input": texts, "model": _probe_cfg().omlx_embed_model},
            headers={"Authorization": f"Bearer {_probe_cfg().omlx_api_key}"},
            timeout=60.0,
        )
        if resp.status_code != 200:
            _remote_ok = False
            return None
        data = resp.json()["data"]
        _remote_ok = True
        _remote_dim = len(data[0]["embedding"])
        return [d["embedding"] for d in data]
    except Exception:  # noqa: BLE001
        _remote_ok = False
        return None


def detect_dim() -> int:
    """当前生效的嵌入维度（remote 成功则用其维度，否则本地 512）。"""
    if _remote_ok is None:
        _remote_embed(["probe"])
    if _remote_ok and _remote_dim:
        return _remote_dim
    return EMBED_DIM


def _local_embed(text: str) -> list[float]:
    text = (text or "").lower().strip()
    vec = [0.0] * EMBED_DIM
    if not text:
        return vec
    tokens: list[str] = []
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(a + b for a, b in zip(cjk, cjk[1:]))  # CJK bigrams
    tokens.extend(re.findall(r"[a-z0-9]+", text))       # 英文/数字词
    for tok in tokens:
        h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "little")
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def to_blob(vec: list[float]) -> bytes:
    from array import array
    return array("f", vec).tobytes()


def embed_one(text: str) -> bytes:
    """Single text → float32 blob (remote provider if available, else local)."""
    text = (text or "").strip()
    remote = _remote_embed([text])
    if remote and remote[0] is not None:
        return to_blob([float(x) for x in remote[0]])
    return to_blob(_local_embed(text))
