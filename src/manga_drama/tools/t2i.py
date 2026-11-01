"""文生图可插拔适配器。

默认 WanxAdapter 走 DashScope 通义万相（ImageSynthesis）。ABC 拆三方法 + 能力声明：
text_to_image / image_to_image / edit_image + supports_reference() / supports_edit()。
默认 T2I 模型只做文生图，不声称支持参考图/编辑--需锁脸时切支持 ref 的端点或换可灵/即梦。
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import httpx

from ..config import MangaDramaConfig
from ..errors import ContentPolicyError, NonRetryableError, RetryableError
from ..retry import with_retry

DEFAULT_T2I_MODEL = os.environ.get("T2I_MODEL", "wanx2.1-t2i-turbo")
DEFAULT_T2I_SIZE = os.environ.get("T2I_SIZE", "720*1280")  # 接近 9:16 竖屏


def _get(obj, key, default=None):
    """兼容 dict 与对象属性两种访问。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _download(url: str, out_path: str | Path) -> str:
    """下载图片 URL 到本地。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    @with_retry()
    def _do():
        try:
            r = httpx.get(url, timeout=60.0, follow_redirects=True)
            r.raise_for_status()
        except httpx.RequestError as e:
            raise RetryableError(f"下载图片网络错误: {e}") from e
        except httpx.HTTPStatusError as e:
            if 500 <= e.response.status_code < 600 or e.response.status_code == 429:
                raise RetryableError(f"下载图片 HTTP {e.response.status_code}") from e
            raise NonRetryableError(f"下载图片 HTTP {e.response.status_code}") from e
        return r.content

    out.write_bytes(_do())
    return str(out)


class T2IAdapter(ABC):
    """文生图适配器抽象基类。"""

    @abstractmethod
    def text_to_image(
        self,
        prompt: str,
        negative_prompt: Optional[str],
        seed: int,
        size: str,
        out_path: str | Path,
    ) -> str:
        """纯文生图，返回保存后的本地图片路径。"""

    def image_to_image(
        self,
        prompt: str,
        negative_prompt: Optional[str],
        ref_image_path: str,
        ref_strength: float,
        seed: int,
        size: str,
        out_path: str | Path,
    ) -> str:
        raise NotImplementedError(f"{type(self).__name__} 不支持参考图引导")

    def edit_image(
        self,
        prompt: str,
        base_image_path: str,
        mask_image_path: Optional[str],
        seed: int,
        out_path: str | Path,
    ) -> str:
        raise NotImplementedError(f"{type(self).__name__} 不支持图像编辑")

    def supports_reference(self) -> bool:
        return False

    def supports_edit(self) -> bool:
        return False


class WanxAdapter(T2IAdapter):
    """通义万相文生图适配器（DashScope）。默认仅 T2I，不声称支持 ref/edit。"""

    def __init__(self, cfg: MangaDramaConfig, model: str = DEFAULT_T2I_MODEL):
        self.cfg = cfg
        self.model = model

    def _call(self, **kwargs):
        from dashscope import ImageSynthesis  # 延迟导入，便于测试 mock

        api_key = self.cfg.require_dashscope()

        @with_retry()
        def _do():
            try:
                # 异步提交任务（部分 DashScope key 不支持同步调用，
                # 返回 "AccessDenied: current user api does not support synchronous calls"）
                rsp = ImageSynthesis.async_call(api_key=api_key, **kwargs)
                if rsp.status_code != 200:
                    return rsp  # 提交失败，错误交给 _extract_url 处理
                # 等待任务完成（内部轮询，阻塞直到生成完毕或超时）
                result = ImageSynthesis.wait(rsp.output.task_id)
                return result
            except httpx.RequestError as e:
                raise RetryableError(f"文生图网络错误: {e}") from e
            except Exception as e:
                name = type(e).__name__
                msg = str(e)
                if "ApiKey" in name or "401" in msg or "Unauthorized" in msg:
                    raise NonRetryableError(f"文生图鉴权失败: {e}") from e
                # 其余视为瞬时错误，交由 with_retry 重试
                raise RetryableError(f"文生图调用异常 ({name}): {e}") from e

        return _do()

    @staticmethod
    def _extract_url(rsp) -> str:
        if rsp is None:
            raise RetryableError("文生图返回空响应")
        status = getattr(rsp, "status_code", 200)
        msg = _get(rsp, "message", "") or ""
        if status != 200:
            if status in (429, 500, 502, 503, 504):
                raise RetryableError(f"文生图临时错误 {status}: {msg}")
            if status == 400 and any(
                k in msg.lower() for k in ("content", "policy", "risk", "sensitive")
            ):
                raise ContentPolicyError(f"内容策略拒绝: {msg}")
            raise NonRetryableError(f"文生图错误 {status}: {msg}")
        output = _get(rsp, "output", None)
        results = _get(output, "results", None)
        task_status = _get(output, "task_status", None)
        if not results:
            if task_status == "FAILED":
                raise NonRetryableError(f"文生图任务失败: {msg}")
            raise RetryableError(f"文生图无结果，task_status={task_status}")
        first = results[0]
        url = _get(first, "url", None)
        if not url:
            raise NonRetryableError("文生图结果缺少 url")
        return url

    def text_to_image(
        self,
        prompt: str,
        negative_prompt: Optional[str] = None,
        seed: int = 0,
        size: str = DEFAULT_T2I_SIZE,
        out_path: str | Path = None,
    ) -> str:
        rsp = self._call(
            model=self.model,
            prompt=prompt,
            negative_prompt=negative_prompt,
            n=1,
            size=size,
            seed=seed or 0,
        )
        url = self._extract_url(rsp)
        return _download(url, out_path)


class VolcengineAdapter(T2IAdapter):
    """火山引擎即梦AI 文生图适配器。

    即梦AI 是字节跳动的专业图像生成服务，通过火山引擎 Visual Generation API 提供，
    画面质量高于通义万相，适合漫剧分镜画面生成。

    需要 VOLC_ACCESS_KEY + VOLC_SECRET_KEY（火山引擎控制台获取）。
    """

    def __init__(self, cfg: MangaDramaConfig, model: str = "high_aes_general_v21"):
        self.cfg = cfg
        self.model = model

    def text_to_image(
        self,
        prompt: str,
        negative_prompt: Optional[str] = None,
        seed: int = 0,
        size: str = "1024*1820",
        out_path: str | Path = None,
    ) -> str:
        from volcengine.visual.VisualService import VisualService

        ak = self.cfg.volc_access_key
        sk = self.cfg.volc_secret_key
        if not ak or not sk:
            raise NonRetryableError(
                "即梦AI 需要 VOLC_ACCESS_KEY + VOLC_SECRET_KEY（火山引擎控制台获取）"
            )

        visual = VisualService()
        visual.set_ak(ak)
        visual.set_sk(sk)

        w, h = (int(x) for x in size.split("*"))
        req = {
            "req_key": self.model,
            "prompt": prompt,
            "width": w,
            "height": h,
            "seed": seed if seed else -1,
            "return_url": True,
            "scale": 3.5,
            "ddim_steps": 25,
        }
        if negative_prompt:
            req["negative_prompt"] = negative_prompt

        @with_retry()
        def _do():
            try:
                resp = visual.cv_process(req)
                code = resp.get("code", -1)
                if code != 10000:
                    msg = resp.get("message", "unknown error")
                    if code in (429, 500, 502, 503):
                        raise RetryableError(f"即梦AI 临时错误 {code}: {msg}")
                    raise NonRetryableError(f"即梦AI 错误 {code}: {msg}")
                data = resp.get("data") or {}
                urls = data.get("image_urls") or []
                if urls:
                    return _download(urls[0], out_path)
                # 部分版本返回 base64 而非 URL
                b64_list = data.get("binary_data_base64") or []
                if b64_list:
                    import base64
                    out = Path(out_path)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(base64.b64decode(b64_list[0]))
                    return str(out)
                raise NonRetryableError("即梦AI 返回无图片")
            except (NonRetryableError, RetryableError):
                raise
            except Exception as e:
                name = type(e).__name__
                msg = str(e)
                if "ApiKey" in name or "401" in msg or "Unauthorized" in msg:
                    raise NonRetryableError(f"即梦AI 鉴权失败: {e}") from e
                raise RetryableError(f"即梦AI 调用异常 ({name}): {e}") from e

        return _do()


# ---- 注册表与工厂 ----

_REGISTRY: dict[str, type[T2IAdapter]] = {
    "wanx": WanxAdapter,
    "dreamina": VolcengineAdapter,
}


def register_t2i(name: str, cls: type[T2IAdapter]) -> None:
    _REGISTRY[name.lower()] = cls


def get_t2i(cfg: MangaDramaConfig) -> T2IAdapter:
    """按 cfg.t2i_provider 取适配器实例。"""
    name = cfg.t2i_provider.lower()
    if name not in _REGISTRY:
        raise NonRetryableError(
            f"未知文生图 provider: {name}（已注册: {list(_REGISTRY)}）"
        )
    return _REGISTRY[name](cfg)
