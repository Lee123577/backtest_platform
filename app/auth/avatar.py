"""
头像文件的存储与安全校验
========================

用户上传的图片是整个站点上**唯一**一条"外部字节直接落到磁盘"的路径，
按被攻击来设计：

  1. 不信扩展名，不信 Content-Type —— 只按文件头(magic bytes)判类型，
     只认 PNG / JPEG / GIF / WebP 四种。**SVG 永远拒绝**：它是 XML，
     同源下打开等于给攻击者一个存储型 XSS 的落点。
  2. 不原样落盘 —— 一律用 Pillow 解码后重新编码成 PNG。这一步会丢掉
     EXIF/ICC/注释等所有附加段，也就顺手打掉了 "既是合法图片又是合法
     HTML/JS" 的 polyglot 文件：重编码之后那些字节根本不会被写出去。
  3. 解压炸弹有闸门 —— Image.MAX_IMAGE_PIXELS 卡住 42000×42000 这种
     "几十 KB 文件解出来吃几个 G 内存"的构造(生产机只有 3.6G)。
  4. 文件名**完全由服务端生成**(user_id + 128 位随机 + 固定 .png)，
     一个字节都不来自用户输入，从源头上没有目录穿越/覆写别人文件的可能。
  5. 存在 data/avatars/ —— 刻意放在 app/static/ 之外。static 是被
     StaticFiles 整目录挂出去的，任何"意外落到那儿的文件"都会被直接发出来；
     头像走独立路由，响应头由我们写死。

Pillow 不可用时**拒绝上传**而不是退回"原样存"：宁可这个功能暂时不能用，
也不把未经重编码的字节存到线上。
"""
from __future__ import annotations

import io
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# data/avatars —— 与 data/cache 同级的运行时目录，不进 git(见 .gitignore)
AVATAR_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "avatars"

MAX_UPLOAD_BYTES = 2 * 1024 * 1024   # 2MB:头像用不了这么大，超了直接 413
OUT_SIDE = 256                       # 输出边长(正方形，前端按圆形裁切显示)
MAX_PIXELS = 40_000_000              # 解码前的像素数上限(解压炸弹闸门)

# 服务端生成的文件名形状，读取时按这个校验(而不是靠 os.path 拼完再祈祷)
_NAME_RE = re.compile(r"\A[0-9]{1,20}_[0-9a-f]{32}\.png\Z")


class AvatarError(RuntimeError):
    """用户侧错误(格式不认/解码失败)，路由层转 400。"""


class AvatarUnavailable(RuntimeError):
    """服务端能力缺失(没装 Pillow)，路由层转 503。"""


# ── 类型判定：只看文件头 ─────────────────────────────────────────────────────
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def sniff(raw: bytes) -> Optional[str]:
    """按 magic bytes 判图片类型；不认识返回 None(含 SVG/HTML/任意文本)。"""
    for sig, kind in _MAGIC:
        if raw.startswith(sig):
            return kind
    # WebP: 'RIFF' + 4 字节长度 + 'WEBP'
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


# ── 重编码 ───────────────────────────────────────────────────────────────────

def _pillow():
    try:
        from PIL import Image, ImageOps  # noqa: WPS433
    except ImportError as e:  # 线上漏装依赖时给一句人能看懂的话
        raise AvatarUnavailable("头像服务暂不可用，请稍后再试") from e
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    return Image, ImageOps


def to_png(raw: bytes) -> bytes:
    """解码 → 居中裁成正方形 → 缩到 256 → 重新编码成 PNG。

    动图只取第一帧(头像不需要动，且逐帧解码在小内存机上不划算)。
    """
    kind = sniff(raw)
    if kind is None:
        raise AvatarError("只支持 PNG / JPG / GIF / WebP 格式的图片")

    Image, ImageOps = _pillow()
    resample = getattr(Image, "LANCZOS", None) or Image.Resampling.LANCZOS

    try:
        src = Image.open(io.BytesIO(raw))
    except Exception as e:
        raise AvatarError("图片无法解析，请换一张试试") from e

    try:
        try:
            if getattr(src, "n_frames", 1) > 1:
                src.seek(0)
            src.load()                      # 真正解码：坏文件/炸弹在这一步现形
        except Exception as e:
            raise AvatarError("图片无法解析，请换一张试试") from e

        img = ImageOps.exif_transpose(src) or src   # 手机竖拍的方向修正
        # P(调色板)可能带透明通道，一并按 RGBA 走，避免透明区变黑块
        keep_alpha = "A" in img.getbands() or img.mode in ("P", "LA")
        img = img.convert("RGBA" if keep_alpha else "RGB")

        # 小图不放大：目标边长取 min(256, 原图短边)
        side = min(OUT_SIDE, img.width, img.height)
        if side <= 0:
            raise AvatarError("图片尺寸异常")
        img = ImageOps.fit(img, (side, side), resample, centering=(0.5, 0.5))

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
    finally:
        src.close()

    return buf.getvalue()


# ── 落盘 / 读取 / 删除 ───────────────────────────────────────────────────────

def new_filename(user_id: int) -> str:
    """文件名全由服务端生成。带 user_id 只为排查方便，唯一性靠 128 位随机。"""
    return f"{int(user_id)}_{secrets.token_hex(16)}.png"


def store(user_id: int, raw: bytes) -> str:
    """校验 + 重编码 + 落盘，返回文件名(不含目录)。"""
    data = to_png(raw)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    name = new_filename(user_id)
    dest = AVATAR_DIR / name
    # 先写临时文件再 rename：避免半个文件被读到(同目录内 rename 是原子的)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return name


def resolve(filename: str) -> Optional[Path]:
    """按文件名取磁盘路径；形状不对、或解析结果跑出 AVATAR_DIR 一律 None。

    正则已经排除了 '/'、'\' 和 '..'，realpath 那一层是第二道闸
    (软链接指到目录外的情况正则拦不住)。
    """
    if not filename or not _NAME_RE.match(filename):
        return None
    path = (AVATAR_DIR / filename).resolve()
    try:
        base = AVATAR_DIR.resolve()
    except OSError:
        return None
    if base not in path.parents:
        return None
    return path if path.is_file() else None


def remove(filename: str) -> None:
    """删旧头像(换新/恢复默认时)。同样只删得动本目录里形状合法的文件。"""
    path = resolve(filename)
    if path is None:
        return
    try:
        path.unlink()
    except OSError as e:
        logger.info("删除旧头像失败(忽略): %s", e)


def public_url(filename: Optional[str]) -> Optional[str]:
    """给前端的访问地址；文件名变了 URL 就变，所以能安全地长缓存。"""
    if not filename or not _NAME_RE.match(filename):
        return None
    return f"/media/avatar/{filename}"
