"""把用户的媒体列表渲染成适合分享的图片。

这个模块刻意做成自包含的：自己下载海报、自己做磁盘缓存、自己用 Pillow 排版。
放在服务端做（而不是浏览器 canvas）有两个好处：不受图片跨域限制、能保证输出尺寸一致。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import requests
from django.conf import settings
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

ROOT_DIR = Path(settings.BASE_DIR).parent
SHARE_DIR = ROOT_DIR / ".runtime" / "share"
POSTER_CACHE_DIR = SHARE_DIR / "posters"

POSTER_RATIO = 1.5  # 海报宽高比 2:3
DOWNLOAD_TIMEOUT = 15
DOWNLOAD_WORKERS = 8
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

MEDIA_TYPE_LABELS = {
    "tv": "美剧",
    "season": "剧集",
    "episode": "单集",
    "movie": "电影",
    "anime": "动漫",
    "manga": "漫画",
    "game": "游戏",
    "book": "书籍",
    "comic": "漫画",
    "boardgame": "桌游",
}

STATUS_TITLES = {
    "Completed": "看完的",
    "In progress": "在追的",
    "Planning": "想看的",
    "Paused": "搁置的",
    "Dropped": "弃掉的",
}

# 海报角标：两个字最省地方，颜色沿用「在追=蓝、看完=绿、想看=紫」的直觉
STATUS_BADGES = {
    "In progress": {"label": "在追", "color": (59, 130, 246)},
    "Completed": {"label": "看完", "color": (16, 185, 129)},
    "Planning": {"label": "想看", "color": (139, 92, 246)},
    "Paused": {"label": "搁置", "color": (107, 114, 128)},
    "Dropped": {"label": "弃了", "color": (239, 68, 68)},
}
STATUS_ORDER = ["In progress", "Completed", "Planning", "Paused", "Dropped"]
# 分组时的栏目顺序，跟着用户习惯走：看完 → 在追 → 想看 → 搁置 → 弃掉
SECTION_ORDER = ["Completed", "In progress", "Planning", "Paused", "Dropped"]

TITLE_GAP = 14  # 海报底边到标题的距离
META_GAP = 8  # 标题到底部信息（评分/集数）的距离

THEMES = {
    "dark": {
        "bg_top": (30, 34, 40),
        "bg_bottom": (17, 19, 23),
        "title": (244, 246, 250),
        "subtitle": (146, 154, 167),
        "card_title": (232, 236, 242),
        "meta": (140, 148, 161),
        "accent": (99, 102, 241),
        "star": (250, 204, 21),
        "divider": (48, 54, 62),
        "placeholder": (54, 61, 72),
        "placeholder_text": (120, 129, 143),
        "footer": (108, 115, 127),
    },
    "light": {
        "bg_top": (255, 255, 255),
        "bg_bottom": (238, 241, 246),
        "title": (21, 24, 29),
        "subtitle": (110, 118, 132),
        "card_title": (32, 36, 43),
        "meta": (120, 128, 142),
        "accent": (79, 70, 229),
        "star": (217, 160, 10),
        "divider": (223, 227, 234),
        "placeholder": (228, 232, 239),
        "placeholder_text": (150, 158, 172),
        "footer": (150, 158, 172),
    },
}

# 两种画幅。竖屏高度随内容自适应（保底一屏）；横屏先按 16:9 排，
# 内容实在放不下时才把画布加高，永远只有一张图，不再分页。
LAYOUTS = {
    "portrait": {
        "width": 1080,
        "height": None,
        "min_height": 1920,
        "columns": 4,
        "title_min": 18,
        "meta_min": 15,
        "two_line_min": 170,  # 宽度够就折两行标题，窄了只留一行
        "min_cell_w": 150,
        "readable_cell_w": 150,
        "max_columns": 4,
        "max_items": 160,
        "side": 56,
        "gap_x": 26,
        "gap_y": 36,
        "section": {
            "height": 92,
            "gap_top": 32,
            "bar_w": 10,
            "bar_h": 40,
            "size": 36,
            "count_size": 26,
            "gap": 18,
        },
        "header": {
            "height": 290,
            "side": 64,
            "bar_top": 100,
            "bar_h": 62,
            "title_size": 74,
            "title_y": 88,
            "sub_size": 32,
            "sub_y": 196,
            "inline": False,
            "divider_y": 238,
        },
        "footer": {
            "height": 158,
            "side": 64,
            "line_y": 10,
            "text_y": 62,
            "size": 28,
            "step": 46,
        },
    },
    "landscape": {
        "width": 1920,
        "height": 1080,
        "max_height": 2600,
        "columns": 8,
        "title_min": 13,
        "meta_min": 12,
        "two_line_min": 168,
        "min_cell_w": 96,
        "readable_cell_w": 118,
        "max_columns": 24,
        "max_cell_w": 350,
        "max_items": 240,
        "side": 52,
        "gap_x": 20,
        "gap_y": 24,
        "section": {
            "height": 66,
            "gap_top": 20,
            "bar_w": 8,
            "bar_h": 30,
            "size": 26,
            "count_size": 21,
            "gap": 12,
        },
        "header": {
            "height": 132,
            "side": 52,
            "bar_top": 30,
            "bar_h": 50,
            "title_size": 62,
            "title_y": 24,
            "sub_size": 28,
            "sub_y": 46,
            "inline": True,
            "divider_y": 106,
        },
        "footer": {
            "height": 96,
            "side": 52,
            "line_y": 8,
            "text_y": 36,
            "size": 22,
            "step": 32,
        },
    },
}

# 旧参数名（story / long）统一落到竖屏，避免老链接失效
MODE_ALIASES = {"story": "portrait", "long": "portrait"}

_FONT_FILES = {
    True: [
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/Dengb.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        "/System/Library/Fonts/PingFang.ttc",
    ],
    False: [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/Deng.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/System/Library/Fonts/PingFang.ttc",
    ],
}
_font_cache: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}


# --------------------------------------------------------------------------- #
# 字体与文字
# --------------------------------------------------------------------------- #
def _load_font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    """按需加载中文字体，找不到就退到 Pillow 内置字体。"""
    key = (bold, size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached

    for path in _FONT_FILES[bold] + _FONT_FILES[not bold]:
        candidate = Path(path)
        if not candidate.exists():
            continue
        try:
            font = ImageFont.truetype(str(candidate), size)
        except OSError:  # pragma: no cover - 字体损坏
            continue
        _font_cache[key] = font
        return font

    logger.warning("没有找到可用的中文字体，分享图中的汉字可能显示为方块。")
    font = ImageFont.load_default(size=size)
    _font_cache[key] = font
    return font


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    """文本宽度（含字距）。"""
    return draw.textlength(text, font=font)


def _ellipsize(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
) -> str:
    """超出宽度时截断并补省略号。"""
    if _text_width(draw, text, font) <= max_width:
        return text
    suffix = "…"
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _text_width(draw, text[:mid] + suffix, font) <= max_width:
            low = mid
        else:
            high = mid - 1
    return (text[:low] + suffix) if low else suffix


def _wrap_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    max_lines: int,
) -> list[str]:
    """把标题折成最多 max_lines 行，最后一行不够就省略号收尾。"""
    text = " ".join(text.split())
    if not text:
        return []

    lines: list[str] = []
    remaining = text
    while remaining and len(lines) < max_lines:
        if _text_width(draw, remaining, font) <= max_width:
            lines.append(remaining)
            remaining = ""
            break

        low, high = 1, len(remaining)
        while low < high:
            mid = (low + high + 1) // 2
            if _text_width(draw, remaining[:mid], font) <= max_width:
                low = mid
            else:
                high = mid - 1

        cut = low
        # 英文标题尽量在空格处断行，避免把单词劈开
        head = remaining[:cut]
        space = head.rfind(" ")
        if space > cut * 0.5:
            cut = space

        lines.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining and lines:
        lines[-1] = _ellipsize(draw, lines[-1] + remaining, font, max_width)
    return lines


# --------------------------------------------------------------------------- #
# 海报
# --------------------------------------------------------------------------- #
def _is_usable_image_url(url: str) -> bool:
    """判断这个 image 字段是不是真能拿来当海报用。"""
    if not url:
        return False
    if url == settings.IMG_NONE or url.startswith("data:"):
        return False
    return not url.lower().endswith(".svg")


def _local_poster_path(url: str) -> Path:
    return POSTER_CACHE_DIR / f"{hashlib.md5(url.encode('utf-8')).hexdigest()}.img"


def ensure_poster(url: str) -> str | None:
    """拿到海报的本地路径；能下载就下载，失败返回 None。"""
    if not _is_usable_image_url(url):
        return None

    if url.startswith("/"):  # 本地静态图
        local = Path(settings.BASE_DIR) / url.lstrip("/")
        return str(local) if local.exists() else None

    target = _local_poster_path(url)
    if target.exists() and target.stat().st_size > 0:
        return str(target)

    try:
        resp = requests.get(
            url,
            timeout=DOWNLOAD_TIMEOUT,
            headers={"User-Agent": "Yamtrack-Share/1.0"},
        )
        resp.raise_for_status()
        data = resp.content
        if len(data) < 512:
            logger.warning("海报内容过小，丢弃：%s", url)
            return None
        Image.open(BytesIO(data)).verify()  # 确认真的是图片
    except Exception as exc:  # noqa: BLE001 - 网络问题不该让整个分享失败
        logger.warning("海报下载失败 %s：%s", url, exc)
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return str(target)


def prefetch_posters(items) -> None:
    """并发预热海报缓存，后面排版时就是纯本地读图了。"""
    urls = [getattr(item.item, "image", "") for item in items]
    urls = [u for u in urls if _is_usable_image_url(u) and not u.startswith("/")]
    if not urls:
        return
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        list(pool.map(ensure_poster, urls))


def _open_poster(path: str | None) -> Image.Image | None:
    if not path:
        return None
    try:
        return Image.open(path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning("海报读取失败 %s：%s", path, exc)
        return None


def _round_corners(image: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width - 1, image.height - 1),
        radius=radius,
        fill=255,
    )
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    out.paste(image, (0, 0), mask)
    return out


def _fit_poster(src: Image.Image, size: tuple[int, int], radius: int) -> Image.Image:
    """按 cover 方式裁剪成目标尺寸并切圆角。"""
    target_w, target_h = size
    src_w, src_h = src.size
    scale = max(target_w / src_w, target_h / src_h)
    resized = src.resize(
        (max(target_w, round(src_w * scale)), max(target_h, round(src_h * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - target_w) // 2
    # 海报标题一般在上方，纵向裁切时略微偏上更自然
    top = int((resized.height - target_h) * 0.3)
    cropped = resized.crop((left, top, left + target_w, top + target_h))
    return _round_corners(cropped, radius)


def _placeholder_poster(
    size: tuple[int, int],
    radius: int,
    theme: dict,
    title: str,
) -> Image.Image:
    """没有海报时画一块色卡，中间放剧名首字，保证排版不塌。"""
    target_w, target_h = size
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, target_w - 1, target_h - 1),
        radius=radius,
        fill=theme["placeholder"],
    )

    initial = (title or "?").strip()[:1]
    font = _load_font(True, int(target_w * 0.42))
    draw.text(
        (target_w / 2, target_h / 2),
        initial,
        font=font,
        fill=theme["placeholder_text"],
        anchor="mm",
    )
    return image


# --------------------------------------------------------------------------- #
# 排版
# --------------------------------------------------------------------------- #
def _cell_metrics(layout: dict, cell_w: int) -> dict:
    """给定单元格宽度，算出海报和文字的全部尺寸。"""
    poster_h = int(cell_w * POSTER_RATIO)
    title_size = max(layout["title_min"], int(cell_w * 0.095))
    meta_size = max(layout["meta_min"], int(cell_w * 0.085))
    line_h = int(title_size * 1.34)
    meta_h = int(meta_size * 1.5)
    # 海报窄了就只留一行标题，否则两行字会把格子撑得很难看
    title_lines = 2 if cell_w >= layout["two_line_min"] else 1
    cell_h = poster_h + TITLE_GAP + title_lines * line_h + META_GAP + meta_h

    return {
        "cell_w": cell_w,
        "poster_h": poster_h,
        "title_size": title_size,
        "meta_size": meta_size,
        "line_h": line_h,
        "meta_h": meta_h,
        "title_lines": title_lines,
        "cell_h": cell_h,
        "row_h": cell_h + layout["gap_y"],
    }


def _grid_geometry(layout: dict, columns: int) -> dict:
    width = layout["width"]
    side = layout["side"]
    gap_x = layout["gap_x"]
    cell_w = (width - 2 * side - (columns - 1) * gap_x) // columns
    if layout.get("max_cell_w"):
        # 条目很少时不至于把海报拉得比画幅还夸张
        cell_w = min(cell_w, layout["max_cell_w"])

    grid_w = columns * cell_w + (columns - 1) * gap_x

    return {
        "width": width,
        "columns": columns,
        "side": side,
        "gap_x": gap_x,
        "gap_y": layout["gap_y"],
        "grid_left": max(side, (width - grid_w) // 2),
        "header_h": layout["header"]["height"],
        "footer_h": layout["footer"]["height"],
        "section": layout["section"],
        "header": layout["header"],
        "footer": layout["footer"],
        **_cell_metrics(layout, cell_w),
    }


def _content_height(geom: dict, groups: list, grouped: bool) -> int:
    """整块内容（含栏目标题）占多高。"""
    total = 0
    section = geom["section"]
    for index, (_state, group) in enumerate(groups):
        if grouped:
            if index:
                total += section["gap_top"]
            total += section["height"]
        rows = math.ceil(len(group) / geom["columns"])
        total += rows * geom["row_h"] - geom["gap_y"]
    return total


def _fit_landscape(layout: dict, groups: list, grouped: bool) -> tuple[dict, int]:
    """横屏自适应：挑一个「海报尽量大、又能一次装下」的列数。

    优先塞进 1920×1080；条目多到塞不下时，不翻页、不裁切，
    而是把画布加高（上限 max_height），同时保住海报的可读尺寸。
    """
    target = layout["height"]
    header_h = layout["header"]["height"]
    footer_h = layout["footer"]["height"]
    avail = target - header_h - footer_h
    total_items = sum(len(group) for _state, group in groups)
    max_columns = min(max(len(group) for _state, group in groups), layout["max_columns"])

    best_fit = None  # (排序键, geom, 内容高度)
    readable = None  # 最省高度、但海报仍看得清的方案

    for columns in range(1, max_columns + 1):
        if not grouped and columns < total_items:
            # 避免最后一行只孤零零挂一两部
            fill = (total_items % columns) / columns if total_items % columns else 1.0
            if fill < 0.5:
                continue

        geom = _grid_geometry(layout, columns)
        if geom["cell_w"] < layout["min_cell_w"]:
            continue

        height = _content_height(geom, groups, grouped)
        if height <= avail:
            key = (geom["cell_w"], -height)
            if best_fit is None or key > best_fit[0]:
                best_fit = (key, geom, height)

        if geom["cell_w"] >= layout["readable_cell_w"]:
            if readable is None or height < readable[1]:
                readable = (geom, height)

    if best_fit:
        return best_fit[1], target

    if readable:
        needed = header_h + readable[1] + footer_h + 24
        return readable[0], max(target, min(needed, layout["max_height"]))

    # 极端情况：条目多到怎么排都很小，就取列数最多的方案，画布跟着长
    geom = _grid_geometry(layout, max_columns)
    needed = header_h + _content_height(geom, groups, grouped) + footer_h + 24
    return geom, max(target, min(needed, layout["max_height"]))


def _paint_background(image: Image.Image, theme: dict) -> None:
    """竖直渐变背景，比纯色更有质感。"""
    top = theme["bg_top"]
    bottom = theme["bg_bottom"]
    height = image.height
    gradient = Image.new("RGB", (1, height))
    for y in range(height):
        ratio = y / max(1, height - 1)
        gradient.putpixel(
            (0, y),
            tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)),
        )
    image.paste(gradient.resize((image.width, height)))


def _draw_header(
    draw: ImageDraw.ImageDraw,
    theme: dict,
    geom: dict,
    title: str,
    subtitle: str,
) -> None:
    cfg = geom["header"]
    side = cfg["side"]
    width = geom["width"]

    draw.rounded_rectangle(
        (side, cfg["bar_top"], side + 11, cfg["bar_top"] + cfg["bar_h"]),
        radius=5,
        fill=theme["accent"],
    )

    font_title = _load_font(True, cfg["title_size"])
    font_sub = _load_font(False, cfg["sub_size"])

    if cfg["inline"]:
        # 横屏：副标题右对齐、与标题同一行，省高度
        sub_w = _text_width(draw, subtitle, font_sub)
        title_max = width - side * 2 - 36 - sub_w - 40
        draw.text(
            (side + 36, cfg["title_y"]),
            _ellipsize(draw, title, font_title, title_max),
            font=font_title,
            fill=theme["title"],
        )
        mid_y = cfg["title_y"] + int(cfg["title_size"] * 0.62)
        draw.text(
            (width - side, mid_y),
            subtitle,
            font=font_sub,
            fill=theme["subtitle"],
            anchor="rm",
        )
    else:
        draw.text(
            (side + 36, cfg["title_y"]),
            _ellipsize(draw, title, font_title, width - side * 2 - 36),
            font=font_title,
            fill=theme["title"],
        )
        draw.text(
            (side, cfg["sub_y"]),
            _ellipsize(draw, subtitle, font_sub, width - side * 2),
            font=font_sub,
            fill=theme["subtitle"],
        )

    draw.line(
        [(side, cfg["divider_y"]), (width - side, cfg["divider_y"])],
        fill=theme["divider"],
        width=2,
    )


def _draw_footer(
    draw: ImageDraw.ImageDraw,
    theme: dict,
    geom: dict,
    height: int,
    lines: list[str],
) -> None:
    cfg = geom["footer"]
    width = geom["width"]
    side = cfg["side"]
    top = height - cfg["height"]

    draw.line(
        [(side, top + cfg["line_y"]), (width - side, top + cfg["line_y"])],
        fill=theme["divider"],
        width=2,
    )
    font = _load_font(False, cfg["size"])
    center = width / 2
    y = top + cfg["text_y"]
    for line in lines[:2]:
        draw.text((center, y), line, font=font, fill=theme["footer"], anchor="mm")
        y += cfg["step"]


def _meta_parts(item, media_type: str, show_score: bool) -> tuple[str, str]:
    """返回 (星标部分, 其余部分)。"""
    star = ""
    score = getattr(item, "score", None)
    if show_score and score is not None:
        try:
            star = f"★ {float(score):g}"
        except (TypeError, ValueError):
            star = ""

    rest = ""
    try:
        watched = int(getattr(item, "progress", 0) or 0)
    except (TypeError, ValueError):
        watched = 0
    if watched > 0:
        unit = "集" if media_type in {"tv", "season", "episode"} else ""
        rest = f"{watched}{unit}" if unit else ""

    if not rest:
        try:
            start = getattr(item, "start_date", None)
            if start:
                rest = timezone.localtime(start).strftime("%Y")
        except Exception:  # noqa: BLE001 - 年份只是锦上添花
            rest = ""

    return star, rest


def _draw_status_badge(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    geom: dict,
    status: str,
) -> None:
    """在海报左上角贴一枚状态胶囊。"""
    style = STATUS_BADGES.get(status)
    if not style:
        return

    x, y = origin
    font = _load_font(True, max(15, int(geom["cell_w"] * 0.088)))
    text = style["label"]
    pad_x = max(8, int(font.size * 0.45))
    pad_y = max(5, int(font.size * 0.3))
    box_h = font.size + pad_y * 2
    box_w = int(_text_width(draw, text, font) + pad_x * 2)

    left, top = x + 10, y + 10
    draw.rounded_rectangle(
        (left, top, left + box_w, top + box_h),
        radius=box_h // 2,
        fill=style["color"],
    )
    draw.text(
        (left + box_w / 2, top + box_h / 2 - 1),
        text,
        font=font,
        fill=(255, 255, 255),
        anchor="mm",
    )


def _draw_section_header(
    draw: ImageDraw.ImageDraw,
    theme: dict,
    geom: dict,
    top: int,
    status: str,
    count: int,
) -> None:
    """栏目标题：一根状态色竖条 + 「在追的 2 部」 + 右侧细线。"""
    cfg = geom["section"]
    style = STATUS_BADGES.get(status, {"color": theme["accent"]})
    label = STATUS_TITLES.get(status, status or "其他")
    x = geom["grid_left"]
    mid = top + cfg["height"] / 2

    draw.rounded_rectangle(
        (x, mid - cfg["bar_h"] / 2, x + cfg["bar_w"], mid + cfg["bar_h"] / 2),
        radius=cfg["bar_w"] / 2,
        fill=style["color"],
    )

    font = _load_font(True, cfg["size"])
    text_x = x + cfg["bar_w"] + cfg["gap"]
    draw.text((text_x, mid), label, font=font, fill=theme["title"], anchor="lm")

    font_count = _load_font(False, cfg["count_size"])
    count_text = f"{count} 部"
    count_x = text_x + _text_width(draw, label, font) + cfg["gap"]
    draw.text((count_x, mid + 1), count_text, font=font_count, fill=theme["meta"], anchor="lm")

    line_start = count_x + _text_width(draw, count_text, font_count) + cfg["gap"] * 2
    line_end = geom["width"] - geom["side"]
    if line_end > line_start + 40:
        draw.line([(line_start, mid), (line_end, mid)], fill=theme["divider"], width=2)


def _draw_tile(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    item,
    geom: dict,
    theme: dict,
    media_type: str,
    show_score: bool,
    show_status: bool,
) -> None:
    x, y = origin
    cell_w = geom["cell_w"]
    poster_h = geom["poster_h"]
    radius = max(10, int(cell_w * 0.06))

    poster = _open_poster(ensure_poster(getattr(item.item, "image", "")))
    if poster is not None:
        tile = _fit_poster(poster, (cell_w, poster_h), radius)
    else:
        tile = _placeholder_poster((cell_w, poster_h), radius, theme, item.item.title)
    canvas.paste(tile, (x, y), tile)

    if show_status:
        _draw_status_badge(draw, (x, y), geom, getattr(item, "status", ""))

    font_title = _load_font(True, geom["title_size"])
    title_y = y + poster_h + TITLE_GAP
    lines = _wrap_lines(draw, item.item.title, font_title, cell_w, geom["title_lines"])
    for line in lines:
        draw.text((x, title_y), line, font=font_title, fill=theme["card_title"])
        title_y += geom["line_h"]

    star, rest = _meta_parts(item, media_type, show_score)
    if star or rest:
        font_meta = _load_font(False, geom["meta_size"])
        meta_y = y + poster_h + TITLE_GAP + geom["title_lines"] * geom["line_h"] + META_GAP
        cursor = x
        if star:
            draw.text((cursor, meta_y), star, font=font_meta, fill=theme["star"])
            cursor += _text_width(draw, star, font_meta) + 10
        if rest:
            draw.text((cursor, meta_y), rest, font=font_meta, fill=theme["meta"])


def _render_page(
    items,
    height: int,
    geom: dict,
    theme: dict,
    media_type: str,
    show_score: bool,
    show_status: bool,
    title: str,
    subtitle: str,
    footer_lines: list[str],
    groups: list | None = None,
) -> Image.Image:
    canvas = Image.new("RGB", (geom["width"], height), theme["bg_top"])
    _paint_background(canvas, theme)
    draw = ImageDraw.Draw(canvas)
    _draw_header(draw, theme, geom, title, subtitle)

    columns = geom["columns"]
    cell_step = geom["cell_w"] + geom["gap_x"]

    if groups:
        # 分栏模式：每一栏自带标题，条目从左往右排，行不满也不居中（保持列表感）
        content_height = _content_height(geom, groups, True)
        spare = max(0, height - geom["header_h"] - content_height - geom["footer_h"])
        y = geom["header_h"] + spare // 2

        for index, (state, group) in enumerate(groups):
            if index:
                y += geom["section"]["gap_top"]
            _draw_section_header(draw, theme, geom, y, state, len(group))
            y += geom["section"]["height"]

            for position, item in enumerate(group):
                row, col = divmod(position, columns)
                x = geom["grid_left"] + col * cell_step
                _draw_tile(
                    canvas,
                    draw,
                    (x, y + row * geom["row_h"]),
                    item,
                    geom,
                    theme,
                    media_type,
                    show_score,
                    show_status,
                )
            y += math.ceil(len(group) / columns) * geom["row_h"] - geom["gap_y"]

        _draw_footer(draw, theme, geom, height, footer_lines)
        return canvas

    total = len(items)
    rows = math.ceil(total / columns) if total else 1

    # 内容不足一屏时（竖屏保底 1920、横屏 16:9），把网格在剩余空间里垂直居中
    content_height = rows * geom["row_h"] - geom["gap_y"]
    spare = max(0, height - geom["header_h"] - content_height - geom["footer_h"])
    grid_top = geom["header_h"] + spare // 2

    for index, item in enumerate(items):
        row, col = divmod(index, columns)
        x = geom["grid_left"] + col * cell_step
        if row == rows - 1:
            in_row = total - row * columns
            if in_row < columns:  # 最后一行没排满时居中，避免孤零零贴在左边
                x += round((columns - in_row) * cell_step / 2)
        y = grid_top + row * geom["row_h"]
        _draw_tile(
            canvas,
            draw,
            (x, y),
            item,
            geom,
            theme,
            media_type,
            show_score,
            show_status,
        )

    _draw_footer(draw, theme, geom, height, footer_lines)
    return canvas


def _header_texts(items, media_type: str, status: str, grouped: bool = False) -> tuple[str, str]:
    label = MEDIA_TYPE_LABELS.get(media_type, "收藏")
    if status in STATUS_TITLES:
        title = f"{STATUS_TITLES[status]}{label}"
    else:
        title = f"我的{label}"

    total_episodes = 0
    counts: Counter[str] = Counter()
    for item in items:
        try:
            total_episodes += int(getattr(item, "progress", 0) or 0)
        except (TypeError, ValueError):
            pass
        state = getattr(item, "status", "")
        if state:
            counts[state] += 1

    parts = [f"共 {len(items)} 部"]
    # 分栏时每栏标题已经写了数量，副标题就不重复列构成
    if len(counts) > 1 and not grouped:
        for state in STATUS_ORDER:
            amount = counts.get(state, 0)
            if amount:
                parts.append(f"{STATUS_BADGES[state]['label']} {amount}")

    if total_episodes and media_type in {"tv", "season", "episode"}:
        parts.append(f"累计 {total_episodes} 集")
    return title, " · ".join(parts)


def _group_items(items) -> list[tuple[str, list]]:
    """按状态切栏，顺序固定；不认识的装到 STATUS_BADGES 之外的状态放最后。"""
    buckets: dict[str, list] = {}
    for item in items:
        state = getattr(item, "status", "") or "Unknown"
        buckets.setdefault(state, []).append(item)

    groups = [(state, buckets[state]) for state in SECTION_ORDER if buckets.get(state)]
    for state, bucket in buckets.items():
        if state not in SECTION_ORDER and bucket:
            groups.append((state, bucket))
    return groups


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def build_share_pages(
    items,
    *,
    media_type: str,
    status: str,
    orientation: str = "portrait",
    theme_name: str = "dark",
    show_score: bool = True,
    show_status: bool = True,
    group_by_status: bool = False,
) -> list[Image.Image]:
    """把条目渲染成图片，永远只返回一张。

    - portrait：1080 宽，4 列；高度保底 1920（一屏手机），条目多时自然变长；
    - landscape：以 1920×1080 为目标，列数按条目数量自动增减，海报随之缩放；
      实在排不下时把画布加高，而不是拆成多张；
    - group_by_status：按 看完 / 在追 / 想看 / 搁置 / 弃掉 分栏。
    """
    items = list(items)
    if not items:
        return []

    orientation = MODE_ALIASES.get(orientation, orientation)
    if orientation not in LAYOUTS:
        orientation = "portrait"

    layout = LAYOUTS[orientation]
    theme = THEMES.get(theme_name, THEMES["dark"])

    # 太长的列表截断，避免画出几万像素的图
    truncated = len(items) > layout["max_items"]
    if truncated:
        items = items[: layout["max_items"]]

    groups = _group_items(items) if group_by_status else []
    grouped = len(groups) > 1
    if not grouped:
        groups = [("", items)]

    title, subtitle = _header_texts(items, media_type, status, grouped)
    when = timezone.localtime(timezone.now()).strftime("%Y-%m-%d")
    footer = [f"由 Yamtrack 生成 · {when}"]
    if truncated:
        footer.append(f"仅展示前 {len(items)} 部")

    if orientation == "landscape":
        geom, height = _fit_landscape(layout, groups, grouped)
    else:
        geom = _grid_geometry(layout, layout["columns"])
        content_height = _content_height(geom, groups, grouped)
        height = max(layout["min_height"], geom["header_h"] + content_height + geom["footer_h"])

    return [
        _render_page(
            items,
            height,
            geom,
            theme,
            media_type,
            show_score,
            show_status,
            title,
            subtitle,
            footer,
            groups=groups if grouped else None,
        ),
    ]


def new_token() -> str:
    """生成一个不可猜的文件名。"""
    return uuid.uuid4().hex


def save_page(image: Image.Image, token: str, filename: str) -> Path:
    """保存图片并记下它的下载文件名。"""
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    path = SHARE_DIR / f"{token}.png"
    image.save(path, "PNG", optimize=True)

    sidecar = SHARE_DIR / f"{token}.json"
    sidecar.write_text(
        json.dumps({"filename": filename}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def read_filename(token: str) -> str:
    """读取图片对应的下载文件名。"""
    sidecar = SHARE_DIR / f"{token}.json"
    if not sidecar.exists():
        return f"yamtrack-{token[:8]}.png"
    try:
        return json.loads(sidecar.read_text(encoding="utf-8")).get("filename") or "yamtrack.png"
    except Exception:  # noqa: BLE001
        return "yamtrack.png"


def cleanup_share_dir(max_age_hours: int = 24) -> None:
    """清掉过期的分享图，避免目录无限膨胀。"""
    if not SHARE_DIR.exists():
        return
    cutoff = timezone.now().timestamp() - max_age_hours * 3600
    for entry in SHARE_DIR.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:  # pragma: no cover
            continue


def image_path_for_token(token: str) -> Path | None:
    """把 token 映射成磁盘路径，顺便挡住路径穿越。"""
    if not TOKEN_RE.match(token):
        return None
    path = SHARE_DIR / f"{token}.png"
    return path if path.exists() else None
