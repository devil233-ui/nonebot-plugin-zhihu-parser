import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache, wraps
from io import BytesIO
from pathlib import Path
from typing import ClassVar, ParamSpec, TypeVar

import aiofiles
from apilmoji import Apilmoji, EmojiCDNSource
from apilmoji.core import get_font_height
from PIL import Image, ImageDraw, ImageFont

# 【修改点 1】：替换为 Nonebot2 的日志模块
from nonebot.log import logger

from .config import PluginConfig
from .data import GraphicsContent, ParseResult

# 定义类型变量
P = ParamSpec("P")
T = TypeVar("T")

Color = tuple[int, int, int]
PILImage = Image.Image


def suppress_exception(
    func: Callable[P, T],
) -> Callable[P, T | None]:
    """装饰器：捕获所有异常并返回 None"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.debug(f"函数 {func.__name__} 执行失败: {e}")
            return None

    return wrapper


def suppress_exception_async(
    func: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T | None]]:
    """装饰器：捕获所有异常并返回 None"""

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.debug(f"函数 {func.__name__} 执行失败: {e}")
            return None

    return wrapper


@dataclass(eq=False, frozen=True, slots=True)
class FontInfo:
    """字体信息数据类"""

    font: ImageFont.FreeTypeFont
    line_height: int
    cjk_width: int

    def __hash__(self) -> int:
        """实现哈希方法以支持 @lru_cache"""
        return hash((id(self.font), self.line_height, self.cjk_width))

    @lru_cache(maxsize=400)
    def get_char_width(self, char: str) -> int:
        """获取字符宽度，使用缓存优化"""
        return int(self.font.getlength(char))

    def get_char_width_fast(self, char: str) -> int:
        """快速获取单个字符宽度"""
        if "\u4e00" <= char <= "\u9fff":
            return self.cjk_width
        else:
            return self.get_char_width(char)

    def get_text_width(self, text: str) -> int:
        if not text:
            return 0
        total_width = 0
        for char in text:
            total_width += self.get_char_width_fast(char)
        return total_width


@dataclass(eq=False, frozen=True, slots=True)
class FontSet:
    """字体集数据类"""
    _FONT_SIZES = (
        ("name", 28),
        ("title", 30),
        ("text", 24),
        ("extra", 24),
        ("indicator", 60),
    )

    name_font: FontInfo
    title_font: FontInfo
    text_font: FontInfo
    extra_font: FontInfo
    indicator_font: FontInfo

    @classmethod
    def new(cls, font_path: Path):
        font_infos: dict[str, FontInfo] = {}
        for name, size in cls._FONT_SIZES:
            font = ImageFont.truetype(font_path, size)
            font_infos[f"{name}_font"] = FontInfo(
                font=font,
                line_height=get_font_height(font),
                cjk_width=size,
            )
        return FontSet(**font_infos)


@dataclass(eq=False, frozen=True, slots=True)
class SectionData:
    height: int

@dataclass(eq=False, frozen=True, slots=True)
class HeaderSectionData(SectionData):
    avatar: PILImage | None
    name_lines: list[str]
    time_lines: list[str]
    text_height: int

@dataclass(eq=False, frozen=True, slots=True)
class TitleSectionData(SectionData):
    lines: list[str]

@dataclass(eq=False, frozen=True, slots=True)
class CoverSectionData(SectionData):
    cover_img: PILImage

@dataclass(eq=False, frozen=True, slots=True)
class TextSectionData(SectionData):
    lines: list[str]

@dataclass(eq=False, frozen=True, slots=True)
class ExtraSectionData(SectionData):
    lines: list[str]

@dataclass(eq=False, frozen=True, slots=True)
class RepostSectionData(SectionData):
    scaled_image: PILImage

@dataclass(eq=False, frozen=True, slots=True)
class ImageGridSectionData(SectionData):
    images: list[PILImage]
    cols: int
    rows: int
    has_more: bool
    remaining_count: int

@dataclass(eq=False, frozen=True, slots=True)
class GraphicsSectionData(SectionData):
    text_lines: list[str]
    image: PILImage
    alt_text: str | None = None

@dataclass
class RenderContext:
    result: ParseResult
    card_width: int
    content_width: int
    image: PILImage
    draw: ImageDraw.ImageDraw
    not_repost: bool = True
    y_pos: int = 0


class Renderer:
    PADDING = 25
    AVATAR_SIZE = 80
    AVATAR_TEXT_GAP = 15
    MAX_COVER_WIDTH = 1000
    MAX_COVER_HEIGHT = 800
    DEFAULT_CARD_WIDTH = 800
    MIN_CARD_WIDTH = 400
    SECTION_SPACING = 15
    NAME_TIME_GAP = 5
    AVATAR_UPSCALE_FACTOR = 2

    MIN_COVER_WIDTH = 300
    MIN_COVER_HEIGHT = 200
    MAX_IMAGE_HEIGHT = 800
    IMAGE_3_GRID_SIZE = 300
    IMAGE_2_GRID_SIZE = 400
    IMAGE_GRID_SPACING = 4
    MAX_IMAGES_DISPLAY = 9
    IMAGE_GRID_COLS = 3

    REPOST_PADDING = 12
    REPOST_SCALE = 0.88

    BG_COLOR: ClassVar[Color] = (255, 255, 255)
    TEXT_COLOR: ClassVar[Color] = (51, 51, 51)
    HEADER_COLOR: ClassVar[Color] = (0, 122, 255)
    EXTRA_COLOR: ClassVar[Color] = (136, 136, 136)
    REPOST_BG_COLOR: ClassVar[Color] = (247, 247, 247)
    REPOST_BORDER_COLOR: ClassVar[Color] = (230, 230, 230)

    _EMOJIS = "emojis"
    _RESOURCES = "resources"
    _LOGOS = "logos"
    _BUTTON_FILENAME = "media_button.png"
    _FONT_FILENAME = "HYSongYunLangHeiW-1.ttf"

    RESOURCES_DIR: ClassVar[Path] = Path(__file__).parent / _RESOURCES
    LOGOS_DIR: ClassVar[Path] = RESOURCES_DIR / _LOGOS
    DEFAULT_FONT_PATH: ClassVar[Path] = RESOURCES_DIR / _FONT_FILENAME
    DEFAULT_VIDEO_BUTTON_PATH: ClassVar[Path] = RESOURCES_DIR / _BUTTON_FILENAME

    def __init__(self, config: PluginConfig):
        self.cfg = config
        # 【修改点 2】：增加兜底默认配置，防止 getattr 拿不到
        self.EMOJI_SOURCE = EmojiCDNSource(
            base_url=self.cfg.get("emoji_cdn", "https://fastly.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/"),
            style=self.cfg.get("emoji_style", "twemoji"),
            cache_dir=self.cfg.cache_dir / self._EMOJIS,
        )

    @classmethod
    def load_resources(cls):
        cls._load_fonts()
        cls._load_video_button()
        cls._load_platform_logos()

    @classmethod
    def _load_fonts(cls):
        font_path = cls.DEFAULT_FONT_PATH
        cls.fontset = FontSet.new(font_path)
        logger.debug(f"加载字体「{font_path.name}」成功")

    @classmethod
    def _load_video_button(cls):
        with Image.open(cls.DEFAULT_VIDEO_BUTTON_PATH) as img:
            cls.video_button_image: PILImage = img.convert("RGBA")
        alpha = cls.video_button_image.split()[-1]
        alpha = alpha.point(lambda x: int(x * 0.3))
        cls.video_button_image.putalpha(alpha)

    @classmethod
    def _load_platform_logos(cls) -> None:
        cls.platform_logos = {}
        for p in cls.LOGOS_DIR.rglob("*.png"):
            try:
                with Image.open(p) as img:
                    cls.platform_logos[p.stem] = img.convert("RGBA")
            except Exception:
                continue

    async def text(self, ctx: RenderContext, xy: tuple[int, int], lines: list[str], font: FontInfo, fill: Color) -> int:
        await Apilmoji.text(ctx.image, xy, lines, font.font, fill=fill, line_height=font.line_height, source=self.EMOJI_SOURCE)
        return font.line_height * len(lines)

    async def _create_card_image(self, result: ParseResult, not_repost: bool = True) -> PILImage:
        card_width = self.DEFAULT_CARD_WIDTH
        content_width = card_width - 2 * self.PADDING
        sections = await self._calculate_sections(result, content_width)
        card_height = sum(section.height for section in sections)
        card_height += self.PADDING * 2 + self.SECTION_SPACING * (len(sections) - 1)
        bg_color = self.BG_COLOR if not_repost else self.REPOST_BG_COLOR
        image = Image.new("RGB", (card_width, card_height), bg_color)
        ctx = RenderContext(result=result, card_width=card_width, content_width=content_width, image=image, draw=ImageDraw.Draw(image), not_repost=not_repost, y_pos=self.PADDING)
        await self._draw_sections(ctx, sections)
        return image

    async def render_card(self, result: ParseResult) -> Path | None:
        cache = self.cfg.cache_dir / f"card_{uuid.uuid4().hex}.png"
        try:
            img = await self._create_card_image(result)
            buf = BytesIO()
            await asyncio.to_thread(img.save, buf, format="PNG")
            async with aiofiles.open(cache, "wb") as fp:
                await fp.write(buf.getvalue())
            return cache
        except Exception as e:
            logger.error(f"渲染卡片失败: {e}")
            return None

    @suppress_exception
    def _load_and_resize_cover(self, cover_path: Path | None, content_width: int) -> PILImage | None:
        if not cover_path or not cover_path.exists():
            return None
        with Image.open(cover_path) as original_img:
            if original_img.mode not in ("RGB", "RGBA"):
                cover_img = original_img.convert("RGB")
            else:
                cover_img = original_img
            target_width = content_width
            if cover_img.width != target_width:
                scale_ratio = target_width / cover_img.width
                new_width = target_width
                new_height = int(cover_img.height * scale_ratio)
                if new_height > self.MAX_COVER_HEIGHT:
                    scale_ratio = self.MAX_COVER_HEIGHT / new_height
                    new_height = self.MAX_COVER_HEIGHT
                    new_width = int(new_width * scale_ratio)
                cover_img = cover_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            elif cover_img is original_img:
                cover_img = cover_img.copy()
            return cover_img

    @suppress_exception
    def _load_and_process_avatar(self, avatar: Path | None) -> PILImage | None:
        if not avatar or not avatar.exists():
            return None
        with Image.open(avatar) as original_img:
            if original_img.mode != "RGBA":
                avatar_img = original_img.convert("RGBA")
            else:
                avatar_img = original_img
            scale = self.AVATAR_UPSCALE_FACTOR
            temp_size = self.AVATAR_SIZE * scale
            avatar_img = avatar_img.resize((temp_size, temp_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (temp_size, temp_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, temp_size - 1, temp_size - 1), fill=255)
            output_avatar = Image.new("RGBA", (temp_size, temp_size), (0, 0, 0, 0))
            output_avatar.paste(avatar_img, (0, 0))
            output_avatar.putalpha(mask)
            output_avatar = output_avatar.resize((self.AVATAR_SIZE, self.AVATAR_SIZE), Image.Resampling.LANCZOS)
            return output_avatar

    async def _calculate_sections(self, result: ParseResult, content_width: int) -> list[SectionData]:
        sections: list[SectionData] = []
        header_section = await self._calculate_header_section(result, content_width)
        if header_section is not None:
            sections.append(header_section)
        if result.title:
            title_lines = self._wrap_text(result.title, content_width, self.fontset.title_font)
            title_height = len(title_lines) * self.fontset.title_font.line_height
            sections.append(TitleSectionData(height=title_height, lines=title_lines))
        if cover_img := self._load_and_resize_cover(await result.cover_path, content_width=content_width):
            sections.append(CoverSectionData(height=cover_img.height, cover_img=cover_img))
        elif result.img_contents:
            img_grid_section = await self._calculate_image_grid_section(result, content_width)
            if img_grid_section:
                sections.append(img_grid_section)
        elif result.graphics_contents:
            for graphics_content in result.graphics_contents:
                graphics_section = await self._calculate_graphics_section(graphics_content, content_width)
                if graphics_section:
                    sections.append(graphics_section)
        if result.text:
            text_lines = self._wrap_text(result.text, content_width, self.fontset.text_font)
            text_height = len(text_lines) * self.fontset.text_font.line_height
            sections.append(TextSectionData(height=text_height, lines=text_lines))
        if result.extra_info:
            extra_lines = self._wrap_text(result.extra_info, content_width, self.fontset.extra_font)
            extra_height = len(extra_lines) * self.fontset.extra_font.line_height
            sections.append(ExtraSectionData(height=extra_height, lines=extra_lines))
        if result.repost:
            repost_section = await self._calculate_repost_section(result.repost)
            sections.append(repost_section)
        return sections

    @suppress_exception_async
    async def _calculate_graphics_section(self, graphics_content: GraphicsContent, content_width: int) -> GraphicsSectionData | None:
        img_path = await graphics_content.get_path()
        with Image.open(img_path) as original_img:
            if original_img.width > content_width:
                ratio = content_width / original_img.width
                new_height = int(original_img.height * ratio)
                image = original_img.resize((content_width, new_height), Image.Resampling.LANCZOS)
            else:
                image = original_img.copy()
            text_lines = []
            if graphics_content.text:
                text_lines = self._wrap_text(graphics_content.text, content_width, self.fontset.text_font)
            text_height = len(text_lines) * self.fontset.text_font.line_height if text_lines else 0
            alt_height = self.fontset.extra_font.line_height if graphics_content.alt else 0
            total_height = text_height + image.height + alt_height
            if text_lines:
                total_height += self.SECTION_SPACING
            if graphics_content.alt:
                total_height += self.SECTION_SPACING
            return GraphicsSectionData(height=total_height, text_lines=text_lines, image=image, alt_text=graphics_content.alt)

    async def _calculate_header_section(self, result: ParseResult, content_width: int) -> HeaderSectionData | None:
        if result.author is None:
            return None
        avatar_img = self._load_and_process_avatar(await result.author.get_avatar_path())
        text_area_width = content_width - (self.AVATAR_SIZE + self.AVATAR_TEXT_GAP)
        name_lines = self._wrap_text(result.author.name, text_area_width, self.fontset.name_font)
        time_text = result.formatted_datetime()
        time_lines = self._wrap_text(time_text, text_area_width, self.fontset.extra_font)
        text_height = len(name_lines) * self.fontset.name_font.line_height
        if time_lines:
            text_height += self.NAME_TIME_GAP + len(time_lines) * self.fontset.extra_font.line_height
        header_height = max(self.AVATAR_SIZE, text_height)
        return HeaderSectionData(height=header_height, avatar=avatar_img, name_lines=name_lines, time_lines=time_lines, text_height=text_height)

    async def _calculate_repost_section(self, repost: ParseResult) -> RepostSectionData:
        repost_image = await self._create_card_image(repost, False)
        scaled_width = int(repost_image.width * self.REPOST_SCALE)
        scaled_height = int(repost_image.height * self.REPOST_SCALE)
        repost_image_scaled = repost_image.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)
        return RepostSectionData(height=scaled_height + self.REPOST_PADDING * 2, scaled_image=repost_image_scaled)

    async def _calculate_image_grid_section(self, result: ParseResult, content_width: int) -> ImageGridSectionData | None:
        if not result.img_contents:
            return None
        total_images = len(result.img_contents)
        has_more = total_images > self.MAX_IMAGES_DISPLAY
        if has_more:
            img_contents = result.img_contents[: self.MAX_IMAGES_DISPLAY]
            remaining_count = total_images - self.MAX_IMAGES_DISPLAY
        else:
            img_contents = result.img_contents[: self.MAX_IMAGES_DISPLAY]
            remaining_count = 0
        processed_images = []
        img_count = len(img_contents)
        for img_content in img_contents:
            img_path = await img_content.get_path()
            img = await self._load_and_process_grid_image(img_path, content_width, img_count)
            if img is not None:
                processed_images.append(img)
        if not processed_images:
            return None
        image_count = len(processed_images)
        if image_count == 1:
            cols, rows = 1, 1
        elif image_count in (2, 4):
            cols, rows = 2, (image_count + 1) // 2
        else:
            cols = self.IMAGE_GRID_COLS
            rows = (image_count + cols - 1) // cols
        max_img_height = max(img.height for img in processed_images)
        if len(processed_images) == 1:
            grid_height = max_img_height
        else:
            grid_height = self.IMAGE_GRID_SPACING + rows * (max_img_height + self.IMAGE_GRID_SPACING)
        return ImageGridSectionData(height=grid_height, images=processed_images, cols=cols, rows=rows, has_more=has_more, remaining_count=remaining_count)

    @suppress_exception_async
    async def _load_and_process_grid_image(self, img_path: Path, content_width: int, img_count: int) -> PILImage | None:
        if not img_path.exists():
            return None
        with Image.open(img_path) as original_img:
            img = original_img
            if img_count >= 2:
                img = self._crop_to_square(img)
            if img_count == 1:
                max_width = content_width
                max_height = min(self.MAX_IMAGE_HEIGHT, content_width)
                if img.width > max_width or img.height > max_height:
                    ratio = min(max_width / img.width, max_height / img.height)
                    new_size = (int(img.width * ratio), int(img.height * ratio))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                elif img is original_img:
                    img = img.copy()
            else:
                if img_count in (2, 4):
                    num_gaps = 3
                    max_size = (content_width - self.IMAGE_GRID_SPACING * num_gaps) // 2
                    max_size = min(max_size, self.IMAGE_2_GRID_SIZE)
                else:
                    num_gaps = self.IMAGE_GRID_COLS + 1
                    max_size = (content_width - self.IMAGE_GRID_SPACING * num_gaps) // self.IMAGE_GRID_COLS
                    max_size = min(max_size, self.IMAGE_3_GRID_SIZE)
                if img.width > max_size or img.height > max_size:
                    ratio = min(max_size / img.width, max_size / img.height)
                    new_size = (int(img.width * ratio), int(img.height * ratio))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                elif img is original_img:
                    img = img.copy()
            return img

    def _crop_to_square(self, img: PILImage) -> PILImage:
        width, height = img.size
        if width == height:
            return img
        if width > height:
            left = (width - height) // 2
            right = left + height
            return img.crop((left, 0, right, height))
        else:
            top = (height - width) // 2
            bottom = top + width
            return img.crop((0, top, width, bottom))

    async def _draw_sections(self, ctx: RenderContext, sections: list[SectionData]) -> None:
        for section in sections:
            match section:
                case HeaderSectionData() as header:
                    await self._draw_header(ctx, header)
                case TitleSectionData() as title:
                    await self._draw_title(ctx, title.lines)
                case CoverSectionData() as cover:
                    self._draw_cover(ctx, cover.cover_img)
                case TextSectionData() as text:
                    await self._draw_text(ctx, text.lines)
                case GraphicsSectionData() as graphics:
                    await self._draw_graphics(ctx, graphics)
                case ExtraSectionData() as extra:
                    await self._draw_extra(ctx, extra.lines)
                case RepostSectionData() as repost:
                    self._draw_repost(ctx, repost)
                case ImageGridSectionData() as image_grid:
                    self._draw_image_grid(ctx, image_grid)

    def _create_avatar_placeholder(self) -> PILImage:
        placeholder_bg_color = (230, 230, 230, 255)
        placeholder_fg_color = (200, 200, 200, 255)
        head_ratio = 0.35
        head_radius_ratio = 1 / 6
        shoulder_y_ratio = 0.55
        shoulder_width_ratio = 0.55
        shoulder_height_ratio = 0.6
        placeholder = Image.new("RGBA", (self.AVATAR_SIZE, self.AVATAR_SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(placeholder)
        draw.ellipse((0, 0, self.AVATAR_SIZE - 1, self.AVATAR_SIZE - 1), fill=placeholder_bg_color)
        center_x = self.AVATAR_SIZE // 2
        head_radius = int(self.AVATAR_SIZE * head_radius_ratio)
        head_y = int(self.AVATAR_SIZE * head_ratio)
        draw.ellipse((center_x - head_radius, head_y - head_radius, center_x + head_radius, head_y + head_radius), fill=placeholder_fg_color)
        shoulder_y = int(self.AVATAR_SIZE * shoulder_y_ratio)
        shoulder_width = int(self.AVATAR_SIZE * shoulder_width_ratio)
        shoulder_height = int(self.AVATAR_SIZE * shoulder_height_ratio)
        draw.ellipse((center_x - shoulder_width // 2, shoulder_y, center_x + shoulder_width // 2, shoulder_y + shoulder_height), fill=placeholder_fg_color)
        mask = Image.new("L", (self.AVATAR_SIZE, self.AVATAR_SIZE), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, self.AVATAR_SIZE - 1, self.AVATAR_SIZE - 1), fill=255)
        placeholder.putalpha(mask)
        return placeholder

    async def _draw_header(self, ctx: RenderContext, section: HeaderSectionData) -> None:
        x_pos = self.PADDING
        avatar = section.avatar if section.avatar else self._create_avatar_placeholder()
        ctx.image.paste(avatar, (x_pos, ctx.y_pos), avatar)
        text_x = self.PADDING + self.AVATAR_SIZE + self.AVATAR_TEXT_GAP
        avatar_center = ctx.y_pos + self.AVATAR_SIZE // 2
        text_start_y = avatar_center - section.text_height // 2
        text_y = text_start_y
        text_y += await self.text(ctx, (text_x, text_y), section.name_lines, self.fontset.name_font, fill=self.HEADER_COLOR)
        if section.time_lines:
            text_y += self.NAME_TIME_GAP
            text_y += await self.text(ctx, (text_x, text_y), section.time_lines, self.fontset.extra_font, fill=self.EXTRA_COLOR)
        if ctx.not_repost:
            platform_name = ctx.result.platform.name
            if platform_name in self.platform_logos:
                logo_img = self.platform_logos[platform_name]
                logo_x = ctx.image.width - self.PADDING - logo_img.width
                logo_y = ctx.y_pos + (self.AVATAR_SIZE - logo_img.height) // 2
                ctx.image.paste(logo_img, (logo_x, logo_y), logo_img)
        ctx.y_pos += section.height + self.SECTION_SPACING

    async def _draw_title(self, ctx: RenderContext, lines: list[str]) -> None:
        ctx.y_pos += await self.text(ctx, (self.PADDING, ctx.y_pos), lines, self.fontset.title_font, self.TEXT_COLOR)
        ctx.y_pos += self.SECTION_SPACING

    def _draw_cover(self, ctx: RenderContext, cover_img: PILImage) -> None:
        x_pos = self.PADDING
        ctx.image.paste(cover_img, (x_pos, ctx.y_pos))
        button_size = 128
        button_x = x_pos + (cover_img.width - button_size) // 2
        button_y = ctx.y_pos + (cover_img.height - button_size) // 2
        ctx.image.paste(self.video_button_image, (button_x, button_y), self.video_button_image)
        ctx.y_pos += cover_img.height + self.SECTION_SPACING

    async def _draw_text(self, ctx: RenderContext, lines: list[str]) -> None:
        ctx.y_pos += await self.text(ctx, (self.PADDING, ctx.y_pos), lines, self.fontset.text_font, fill=self.TEXT_COLOR)
        ctx.y_pos += self.SECTION_SPACING

    async def _draw_graphics(self, ctx: RenderContext, section: GraphicsSectionData) -> None:
        if section.text_lines:
            ctx.y_pos += await self.text(ctx, (self.PADDING, ctx.y_pos), section.text_lines, self.fontset.text_font, fill=self.TEXT_COLOR)
            ctx.y_pos += self.SECTION_SPACING
        x_pos = self.PADDING + (ctx.content_width - section.image.width) // 2
        ctx.image.paste(section.image, (x_pos, ctx.y_pos))
        ctx.y_pos += section.image.height
        if section.alt_text:
            ctx.y_pos += self.SECTION_SPACING
            extra_font_info = self.fontset.extra_font
            text_width = extra_font_info.get_text_width(section.alt_text)
            text_x = self.PADDING + (ctx.content_width - text_width) // 2
            ctx.y_pos += await self.text(ctx, (text_x, ctx.y_pos), [section.alt_text], self.fontset.extra_font, fill=self.EXTRA_COLOR)
        ctx.y_pos += self.SECTION_SPACING

    async def _draw_extra(self, ctx: RenderContext, lines: list[str]) -> None:
        ctx.y_pos += await self.text(ctx, (self.PADDING, ctx.y_pos), lines, self.fontset.extra_font, fill=self.EXTRA_COLOR)

    def _draw_repost(self, ctx: RenderContext, section: RepostSectionData) -> None:
        repost_image = section.scaled_image
        repost_x = self.PADDING
        repost_y = ctx.y_pos
        repost_width = ctx.content_width
        repost_height = section.height
        self._draw_rounded_rectangle(ctx.image, (repost_x, repost_y, repost_x + repost_width, repost_y + repost_height), self.REPOST_BG_COLOR, radius=8)
        self._draw_rounded_rectangle_border(ctx.draw, (repost_x, repost_y, repost_x + repost_width, repost_y + repost_height), self.REPOST_BORDER_COLOR, radius=8, width=1)
        card_x = repost_x + (repost_width - repost_image.width) // 2
        card_y = repost_y + self.REPOST_PADDING
        ctx.image.paste(repost_image, (card_x, card_y))
        ctx.y_pos += repost_height + self.SECTION_SPACING

    def _draw_image_grid(self, ctx: RenderContext, section: ImageGridSectionData) -> None:
        images = section.images
        cols = section.cols
        rows = section.rows
        has_more = section.has_more
        remaining_count = section.remaining_count
        if not images:
            return
        available_width = ctx.content_width
        img_spacing = self.IMAGE_GRID_SPACING
        if len(images) == 1:
            max_img_size = available_width
        else:
            num_gaps = cols + 1
            calculated_size = (available_width - img_spacing * num_gaps) // cols
            max_img_size = self.IMAGE_2_GRID_SIZE if cols == 2 else self.IMAGE_3_GRID_SIZE
            max_img_size = min(calculated_size, max_img_size)
        current_y = ctx.y_pos
        for row in range(rows):
            row_start = row * cols
            row_end = min(row_start + cols, len(images))
            row_images = images[row_start:row_end]
            max_height = max(img.height for img in row_images)
            for i, img in enumerate(row_images):
                img_x = self.PADDING + img_spacing + i * (max_img_size + img_spacing)
                img_y = current_y + img_spacing
                y_offset = (max_height - img.height) // 2
                ctx.image.paste(img, (img_x, img_y + y_offset))
                if has_more and row == rows - 1 and i == len(row_images) - 1 and len(images) == self.MAX_IMAGES_DISPLAY:
                    self._draw_more_indicator(ctx.image, img_x, img_y, max_img_size, max_height, remaining_count)
            current_y += img_spacing + max_height
        ctx.y_pos = current_y + img_spacing + self.SECTION_SPACING

    def _draw_more_indicator(self, image: PILImage, img_x: int, img_y: int, img_width: int, img_height: int, count: int):
        draw = ImageDraw.Draw(image)
        overlay = Image.new("RGBA", (img_width, img_height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle((0, 0, img_width - 1, img_height - 1), fill=(0, 0, 0, 100))
        image.paste(overlay, (img_x, img_y), overlay)
        text = f"+{count}"
        font_info = self.fontset.indicator_font
        text_width = font_info.get_text_width(text)
        text_x = img_x + (img_width - text_width) // 2
        text_y = img_y + (img_height - font_info.line_height) // 2
        draw.text((text_x, text_y), text, fill=(255, 255, 255), font=font_info.font)

    def _draw_rounded_rectangle(self, image: PILImage, bbox: tuple[int, int, int, int], fill_color: Color, radius: int = 8):
        x1, y1, x2, y2 = bbox
        draw = ImageDraw.Draw(image)
        draw.rectangle((x1 + radius, y1, x2 - radius, y2), fill=fill_color)
        draw.rectangle((x1, y1 + radius, x2, y2 - radius), fill=fill_color)
        draw.pieslice((x1, y1, x1 + 2 * radius, y1 + 2 * radius), 180, 270, fill=fill_color)
        draw.pieslice((x2 - 2 * radius, y1, x2, y1 + 2 * radius), 270, 360, fill=fill_color)
        draw.pieslice((x1, y2 - 2 * radius, x1 + 2 * radius, y2), 90, 180, fill=fill_color)
        draw.pieslice((x2 - 2 * radius, y2 - 2 * radius, x2, y2), 0, 90, fill=fill_color)

    def _draw_rounded_rectangle_border(self, draw: ImageDraw.ImageDraw, bbox: tuple[int, int, int, int], border_color: Color, radius: int = 8, width: int = 1):
        x1, y1, x2, y2 = bbox
        draw.rectangle((x1 + radius, y1, x2 - radius, y1 + width), fill=border_color)
        draw.rectangle((x1 + radius, y2 - width, x2 - radius, y2), fill=border_color)
        draw.rectangle((x1, y1 + radius, x1 + width, y2 - radius), fill=border_color)
        draw.rectangle((x2 - width, y1 + radius, x2, y2 - radius), fill=border_color)
        draw.arc((x1, y1, x1 + 2 * radius, y1 + 2 * radius), 180, 270, fill=border_color, width=width)
        draw.arc((x2 - 2 * radius, y1, x2, y1 + 2 * radius), 270, 360, fill=border_color, width=width)
        draw.arc((x1, y2 - 2 * radius, x1 + 2 * radius, y2), 90, 180, fill=border_color, width=width)
        draw.arc((x2 - 2 * radius, y2 - 2 * radius, x2, y2), 0, 90, fill=border_color, width=width)

    def _wrap_text(self, text: str | None, max_width: int, font_info: FontInfo) -> list[str]:
        if not text:
            return []
        lines: list[str] = []
        paragraphs = text.splitlines()
        def is_punctuation(char: str) -> bool:
            return char in "，。！？；：、）】》〉」』〕〗〙〛…—·" or char in ",.;:!?)]}"
        for paragraph in paragraphs:
            if not paragraph:
                lines.append("")
                continue
            current_line = ""
            current_line_width = 0
            remaining_text = paragraph
            while remaining_text:
                next_char = remaining_text[0]
                char_width = font_info.get_char_width_fast(next_char)
                if not current_line:
                    current_line = next_char
                    current_line_width = char_width
                    remaining_text = remaining_text[1:]
                    continue
                if is_punctuation(next_char):
                    current_line += next_char
                    current_line_width += char_width
                    remaining_text = remaining_text[1:]
                    continue
                test_width = current_line_width + char_width
                if test_width <= max_width:
                    current_line += next_char
                    current_line_width = test_width
                    remaining_text = remaining_text[1:]
                else:
                    lines.append(current_line)
                    current_line = next_char
                    current_line_width = char_width
                    remaining_text = remaining_text[1:]
            if current_line:
                lines.append(current_line)
        return lines