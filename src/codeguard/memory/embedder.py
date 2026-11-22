"""文本向量化器（Embedder）：支持 OpenAI/DashScope 兼容接口与本地确定性哈希向量。"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Protocol, runtime_checkable

import numpy as np

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[\u4e00-\u9fff]{1,2}")


@runtime_checkable
class Embedder(Protocol):
    """统一向量编码协议。"""

    dim: int
    model_name: str

    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def _l2_normalize(vec: np.ndarray) -> list[float]:
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-12:
        return [0.0] * int(vec.shape[0])
    normalized = (vec / norm).astype(np.float32)
    return [float(x) for x in normalized]


class LocalHashEmbedder:
    """本地确定性特征哈希向量化器：零外部网络依赖，适合离线检索与回归测试。"""

    def __init__(self, dim: int = 256):
        if dim <= 0:
            raise ValueError("embedding dim 必须为正整数")
        self.dim = int(dim)
        self.model_name = f"local-hash-{self.dim}"

    def _features(self, text: str) -> list[str]:
        lowered = (text or "").lower()
        feats: list[str] = []
        for tok in _TOKEN_RE.findall(lowered):
            feats.append(f"w:{tok}")
            if tok.isascii() and len(tok) >= 4:
                for sub in tok.split("_"):
                    if sub:
                        feats.append(f"s:{sub}")
                for idx in range(len(tok) - 2):
                    feats.append(f"g:{tok[idx : idx + 3]}")
        if not feats and lowered.strip():
            feats.append(f"raw:{lowered.strip()[:64]}")
        return feats

    def embed(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float32)
        for feat in self._features(text):
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            raw_int = int.from_bytes(digest, "little")
            bucket = raw_int % self.dim
            sign = 1.0 if ((raw_int >> 63) & 1) == 0 else -1.0
            vec[bucket] += sign
        return _l2_normalize(vec)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(item) for item in texts]


class OpenAICompatibleEmbedder:
    """OpenAI / DashScope 兼容 Embedding 客户端，异常时可安全回退本地向量。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = "text-embedding-v3",
        dim: int = 256,
        fallback_local: bool = True,
        timeout: float = 20.0,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model_name = model
        self.dim = int(dim)
        self.fallback_local = fallback_local
        self.timeout = timeout
        self._local = LocalHashEmbedder(dim=self.dim)
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url or None,
                timeout=self.timeout,
            )
        return self._client

    def _fit_dim(self, raw: list[float]) -> list[float]:
        arr = np.asarray(raw, dtype=np.float32)
        if arr.shape[0] > self.dim:
            arr = arr[: self.dim]
        elif arr.shape[0] < self.dim:
            padded = np.zeros(self.dim, dtype=np.float32)
            padded[: arr.shape[0]] = arr
            arr = padded
        return _l2_normalize(arr)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        cleaned = [(item or "").strip() or " " for item in texts]
        if not cleaned:
            return []
        try:
            client = self._get_client()
            resp = client.embeddings.create(model=self.model_name, input=cleaned)
            return [self._fit_dim(list(item.embedding)) for item in resp.data]
        except Exception:
            if not self.fallback_local:
                raise
            return self._local.embed_batch(cleaned)

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]


def build_embedder(cfg: Any) -> Embedder:
    """根据 Settings 构造向量化器；默认无显式配置时使用离线本地哈希向量。"""
    dim = int(getattr(cfg, "embedding_dim", 256) or 256)
    provider = str(getattr(cfg, "embedding_provider", "auto") or "auto").strip().lower()
    if provider == "local":
        return LocalHashEmbedder(dim=dim)

    api_key = getattr(cfg, "embedding_api_key", None)
    base_url = str(getattr(cfg, "embedding_base_url", "") or "").strip()
    model = str(getattr(cfg, "embedding_model", "text-embedding-v3") or "text-embedding-v3")

    if provider == "dashscope":
        api_key = api_key or getattr(cfg, "dashscope_api_key", None)
        base_url = base_url or str(getattr(cfg, "dashscope_base_url", "") or "")
    elif provider == "openai":
        base_url = base_url or "https://api.openai.com/v1"
    elif provider == "auto":
        # 仅当显式配置了 embedding_api_key 或 embedding_base_url 时启用远端向量 API
        if not api_key and not base_url:
            return LocalHashEmbedder(dim=dim)
        api_key = api_key or getattr(cfg, "dashscope_api_key", None)

    if not api_key:
        return LocalHashEmbedder(dim=dim)

    return OpenAICompatibleEmbedder(
        api_key=str(api_key),
        base_url=base_url,
        model=model,
        dim=dim,
        fallback_local=True,
    )


__all__ = [
    "Embedder",
    "LocalHashEmbedder",
    "OpenAICompatibleEmbedder",
    "build_embedder",
]
