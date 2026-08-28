"""
个人资料(昵称 / 头像)测试
==========================
锁住两件事，都不碰 MySQL/网络：

  1. normalize_display_name —— 长度、空白、零宽/双向覆盖字符、冒充官方的保留词
  2. avatar —— 类型只认文件头(SVG/HTML 必拒)、重编码把附加字节丢干净、
     文件名与路径解析拒绝一切目录穿越

第 2 组是这次改动的安全核心：用户上传的字节是全站唯一一条"外部输入直接
落盘"的路径，这些用例就是那条路径的回归网。
"""
import io

import pytest

from app.auth import avatar
from app.auth.service import AuthError, normalize_display_name

PIL = pytest.importorskip("PIL", reason="头像重编码依赖 Pillow")
from PIL import Image  # noqa: E402


# ── 昵称 ──────────────────────────────────────────────────────────────────────

def test_name_trims_and_keeps_text():
    assert normalize_display_name("  老李  ") == "老李"


def test_name_empty_means_clear():
    assert normalize_display_name("") is None
    assert normalize_display_name(None) is None
    assert normalize_display_name("   ") is None


def test_name_strips_zero_width_and_bidi():
    # 零宽空格和 RLO 都要被剔掉，剩下的是肉眼看到的那几个字符
    assert normalize_display_name("A​B‮C") == "ABC"
    assert normalize_display_name("﻿张三") == "张三"


def test_name_keeps_zwj_so_emoji_survive():
    # ZWJ 是 emoji 组合序列的连接符，剔了"一家三口"会碎成三个人
    name = "\U0001f468‍\U0001f469‍\U0001f467"
    assert normalize_display_name(name) == name


def test_name_collapses_inner_whitespace():
    assert normalize_display_name("老  李   头") == "老 李 头"


def test_name_only_invisible_chars_rejected():
    with pytest.raises(AuthError):
        normalize_display_name("​​​")


def test_name_too_long_rejected():
    normalize_display_name("一" * 16)          # 刚好到上限，放行
    with pytest.raises(AuthError):
        normalize_display_name("一" * 17)


@pytest.mark.parametrize("bad", [
    "管理员", "官方客服", "站长", "系统通知",
    "Admin", "ROOT", "official发布",
])
def test_name_reserved_words_rejected(bad):
    with pytest.raises(AuthError):
        normalize_display_name(bad)


# ── 头像：类型判定 ────────────────────────────────────────────────────────────

def _png_bytes(size=(400, 300), mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, size, (10, 120, 200) if mode == "RGB" else (10, 120, 200, 128)).save(
        buf, format="PNG"
    )
    return buf.getvalue()


def _jpeg_bytes(size=(400, 300)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 40)).save(buf, format="JPEG")
    return buf.getvalue()


def test_sniff_recognizes_supported_types():
    assert avatar.sniff(_png_bytes()) == "png"
    assert avatar.sniff(_jpeg_bytes()) == "jpeg"
    assert avatar.sniff(b"GIF89a" + b"\x00" * 10) == "gif"
    assert avatar.sniff(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00") == "webp"


@pytest.mark.parametrize("payload", [
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b"<!DOCTYPE html><html><body>hi",
    b"\x00\x01\x02\x03",
    b"",
    b"%PDF-1.7",
])
def test_sniff_rejects_everything_else(payload):
    """SVG 是重点：它是 XML，同源打开等于一个存储型 XSS 落点。"""
    assert avatar.sniff(payload) is None


@pytest.mark.parametrize("payload", [
    b'<svg xmlns="http://www.w3.org/2000/svg"/>',
    b"not an image at all",
])
def test_to_png_rejects_non_images(payload):
    with pytest.raises(avatar.AvatarError):
        avatar.to_png(payload)


def test_to_png_rejects_truncated_image():
    """文件头对但内容是坏的 —— 解码那一步必须炸成 AvatarError，不能漏出去。"""
    with pytest.raises(avatar.AvatarError):
        avatar.to_png(_png_bytes()[:40])


# ── 头像：重编码 ──────────────────────────────────────────────────────────────

def test_to_png_outputs_square_png_within_limit():
    out = avatar.to_png(_png_bytes((900, 600)))
    im = Image.open(io.BytesIO(out))
    assert im.format == "PNG"
    assert im.size == (avatar.OUT_SIDE, avatar.OUT_SIDE)


def test_to_png_does_not_upscale_small_images():
    out = avatar.to_png(_png_bytes((64, 90)))
    assert Image.open(io.BytesIO(out)).size == (64, 64)


def test_to_png_drops_appended_payload():
    """polyglot 防线:合法图片后面接一段 HTML,重编码后那段字节不该还在。"""
    payload = b"<script>alert(document.cookie)</script>"
    out = avatar.to_png(_png_bytes() + payload)
    assert payload not in out
    assert b"script" not in out


def test_to_png_drops_exif():
    """EXIF 里可能带地理位置等隐私,也可能塞攻击载荷 —— 一律不带出去。"""
    src = Image.new("RGB", (300, 300), (1, 2, 3))
    buf = io.BytesIO()
    exif = src.getexif()
    exif[0x010E] = "SECRET-CAMERA-NOTE"      # ImageDescription
    src.save(buf, format="JPEG", exif=exif)
    assert b"SECRET-CAMERA-NOTE" in buf.getvalue()

    out = avatar.to_png(buf.getvalue())
    assert b"SECRET-CAMERA-NOTE" not in out
    assert not Image.open(io.BytesIO(out)).getexif()


def test_to_png_keeps_alpha():
    out = avatar.to_png(_png_bytes((200, 200), mode="RGBA"))
    assert Image.open(io.BytesIO(out)).mode == "RGBA"


# ── 头像：文件名与路径 ────────────────────────────────────────────────────────

def test_new_filename_shape_is_resolvable():
    name = avatar.new_filename(7)
    assert name.startswith("7_") and name.endswith(".png")
    assert avatar.public_url(name) == "/media/avatar/" + name


@pytest.mark.parametrize("bad", [
    "../../app/main.py",
    "../.env",
    "..\\..\\app\\main.py",
    "/etc/passwd",
    "1_abc.png",                       # 随机段长度不对
    "1_" + "z" * 32 + ".png",          # 非 16 进制
    "1_" + "a" * 32 + ".php",          # 扩展名不对
    "1_" + "a" * 32 + ".png/../x",
    "",
    None,
])
def test_resolve_rejects_anything_not_generated_by_us(bad):
    assert avatar.resolve(bad) is None
    assert avatar.public_url(bad) is None


def test_resolve_returns_none_for_wellformed_but_missing_file():
    assert avatar.resolve("999_" + "ab" * 16 + ".png") is None


def test_store_resolve_remove_roundtrip():
    name = avatar.store(123, _jpeg_bytes())
    try:
        path = avatar.resolve(name)
        assert path is not None and path.is_file()
        assert path.parent == avatar.AVATAR_DIR.resolve()
        assert Image.open(path).format == "PNG"
    finally:
        avatar.remove(name)
    assert avatar.resolve(name) is None
    # 临时文件不该留下(写入走 .part → rename)
    assert not list(avatar.AVATAR_DIR.glob("*.part"))


def test_remove_ignores_bad_names(tmp_path):
    """remove 只删得动本目录里形状合法的文件，传什么怪名字都不该动到别处。"""
    victim = tmp_path / "important.txt"
    victim.write_text("keep me", encoding="utf-8")
    avatar.remove(str(victim))
    avatar.remove("../../requirements.txt")
    assert victim.exists()
