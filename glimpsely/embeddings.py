"""Embedding provider: omlx /v1/embeddings if available, else local n-gram hashing.

本地兜底算法：字符 bigram（CJK）+ 英文词元 的哈希袋向量（512 维，L2 归一化）。
确定性、零依赖、中文检索可用；omlx 将来装载 embedding 模型后自动切换真语义向量。
"""

import hashlib
import math
import re

import httpx

from .config import Config

EMBED_DIM = 512

_remote_ok: bool | None = None  # 探测缓存（进程级）


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


def _remote_embed(cfg: Config, texts: list[str]) -> list[str | None] | None:
    global _remote_ok
    if _remote_ok is False:
        return None
    try:
        resp = httpx.post(
            f"{cfg.omlx_base_url}/v1/embeddings",
            json={"input": texts, "model": cfg.omlx_model},
            headers={"Authorization": f"Bearer {cfg.omlx_api_key}"},
            timeout=30.0,
        )
        if resp.status_code != 200:
            _remote_ok = False
            return None
        data = resp.json()["data"]
        _remote_ok = True
        return [d["embedding"] for d in data]
    except Exception:  # noqa: BLE001
        _remote_ok = False
        return None


def to_blob(vec: list[float]) -> bytes:
    from array import array
    return array("f", vec).tobytes()


def embed_one(text: str, cfg: Config | None = None) -> bytes:
    """Single text → float32 blob (remote provider if available, else local)."""
    text = (text or "").strip()
    if cfg is not None and text:
        remote = _remote_embed(cfg, [text])
        if remote and remote[0] is not None:
            return to_blob([float(x) for x in remote[0]])
    return to_blob(_local_embed(text))
