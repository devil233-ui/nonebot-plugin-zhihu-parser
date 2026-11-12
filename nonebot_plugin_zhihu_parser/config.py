"""插件配置与运行时目录。

配置统一走 nonebot 的插件配置机制（pydantic 模型 + ``get_plugin_config``），
数据 / 配置 / 缓存目录统一走 localstore 提供的插件目录，不再读取全局配置，
也不再按工作目录硬拼路径。
"""

from __future__ import annotations

from datetime import timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from nonebot import get_plugin_config
from nonebot.log import logger
from pydantic import BaseModel, ConfigDict, Field

PLUGIN_NAME = "nonebot_plugin_zhihu_parser"

_DEFAULT_EMOJI_CDN = (
    "https://fastly.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/"
)

_DIR_GETTERS = {
    "data": "get_plugin_data_dir",
    "config": "get_plugin_config_dir",
    "cache": "get_plugin_cache_dir",
}


class Config(BaseModel):
    """知乎解析插件配置，键名沿用原有的 ``ZHIHU_*`` 环境变量。"""

    model_config = ConfigDict(extra="ignore")

    zhihu_cookie: str = Field(
        default="",
        description="知乎登录 Cookie，推荐留空并改用插件配置目录下的 CK 文件",
    )
    zhihu_proxy: Optional[str] = Field(default=None, description="访问知乎使用的代理")
    zhihu_batch_size: int = Field(default=10, description="合并转发的单批次图片数量阈值")
    zhihu_debounce_time: int = Field(
        default=86400, description="同一链接的防抖时长（秒）"
    )
    zhihu_cache_max_age: int = Field(
        default=604800, description="缓存文件最大存活时间（秒）"
    )
    zhihu_max_images: int = Field(default=50, description="单条链接最多转发的图片总量")
    emoji_cdn: str = Field(default=_DEFAULT_EMOJI_CDN, description="表情资源 CDN 前缀")
    emoji_style: str = Field(default="twemoji", description="表情资源风格")
    zhihu_refresh_jitter_seconds: int = Field(
        default=900, description="保活任务的随机错峰上限（秒）"
    )
    # 下面两项允许字符串 / 整数 / 列表等多种写法，校验交给接收人解析函数，避免写错格式就起不来
    zhihu_refresh_alert_users: Optional[Any] = Field(
        default=None,
        description="保活失败提醒的私聊接收人；留空表示私聊机器人管理员，空列表表示关闭",
    )
    zhihu_refresh_alert_groups: Optional[Any] = Field(
        default=None, description="保活失败提醒的接收群"
    )


class ParserItem(dict):
    """兼容原代码的替身类，允许通过 .属性名 访问字典键值"""

    def __getattr__(self, item):
        if item not in self:
            # 遇到 enable 系列开关默认返回 True
            if isinstance(item, str) and "enable" in item:
                return True
            return ParserItem()
        return self.get(item)

    def __getitem__(self, item):
        if item not in self:
            if isinstance(item, str) and "enable" in item:
                return True
            return ParserItem()
        return super().__getitem__(item)

    def __bool__(self):
        return True


def _fallback_dir(kind: str) -> Path:
    """不在插件上下文中时的兜底目录，保持与旧版本相同的位置。"""
    path = Path.cwd() / kind / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_plugin_dir(kind: str) -> Path:
    """取 localstore 提供的插件目录。

    正常加载时由 localstore 按插件名与 ``LOCALSTORE_USE_CWD`` 等配置决定实际位置；
    脱离插件上下文（例如单元测试里直接实例化）时回退到工作目录下的同名目录。
    """
    try:
        import nonebot_plugin_localstore as store
    except ImportError:  # pragma: no cover - 依赖已声明，只在异常安装环境触发
        logger.warning("[知乎解析] 未安装 nonebot-plugin-localstore，目录回退到工作目录")
        return _fallback_dir(kind)

    getter = getattr(store, _DIR_GETTERS[kind])
    try:
        return getter()
    except RuntimeError:
        logger.debug(f"[知乎解析] 未能识别插件上下文，{kind} 目录回退到工作目录")
        return _fallback_dir(kind)


class PluginConfig:
    """运行时视图：把配置模型与插件目录组合起来，调用点保持原样。"""

    def __init__(self, config: Optional[Config] = None):
        self.config = config if config is not None else get_plugin_config(Config)

        # --- 目录由 localstore 统一提供 ---
        self.data_dir = resolve_plugin_dir("data")
        self.config_dir = resolve_plugin_dir("config")
        self.cache_dir = resolve_plugin_dir("cache")
        self.temp_dir = self.cache_dir

        # 补上 Downloader 等组件需要的属性
        self.source_max_size = 50
        self.download_retry_times = 5
        self.common_timeout = 60
        self.download_timeout = 60
        self.max_duration = 3600
        self.timezone = timezone(timedelta(hours=8))

        # --- Cookie 文件管理 (位于插件配置目录) ---
        self.cookie_file = self.config_dir / "zhihu_cookies.txt"
        if not self.cookie_file.exists():
            self.cookie_file.write_text("", encoding="utf-8")
            logger.info(f"[知乎解析] 已在配置目录下创建 CK 文件: {self.cookie_file}")

        # 环境变量里的 Cookie 作为 CK 文件为空时的备选
        self.env_cookie = (self.config.zhihu_cookie or "").strip()

        # 优先级：配置目录下的 txt 文件 > 环境配置
        final_cookie = self.get_zhihu_cookie()

        # 旧版本把目录放在工作目录下；目录变化时只提示，不搬运凭据文件
        legacy_config_dir = Path.cwd() / "config" / PLUGIN_NAME
        if (
            not final_cookie
            and self.config_dir != legacy_config_dir
            and (legacy_config_dir / "zhihu_cookies.txt").exists()
        ):
            logger.warning(
                f"[知乎解析] 旧目录中存在 CK 文件 {legacy_config_dir / 'zhihu_cookies.txt'}，"
                f"当前使用 {self.config_dir}；如需沿用旧目录请设置 LOCALSTORE_USE_CWD=true，"
                "或把 CK 文件移动到当前配置目录"
            )

        # 构造 parser.zhihu 结构，供底层解析器通过 self.mycfg 调用
        self.parser = ParserItem({"zhihu": ParserItem({"cookie": final_cookie})})

        # 基础配置项字典
        self.settings = {
            "proxy": self.config.zhihu_proxy,
            "zhihu_cookie": final_cookie,
            "emoji_cdn": self.config.emoji_cdn,
            "emoji_style": self.config.emoji_style,
        }

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def get_config(self, key: str, default=None):
        return self.settings.get(key, default)

    def get_zhihu_cookie(self) -> str:
        """每次读取 Cookie 文件，文件为空或读取失败时回退到环境配置。"""
        try:
            local_cookie = self.cookie_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            logger.warning(f"[知乎解析] 读取 CK 文件失败，将使用环境配置: {error}")
            local_cookie = ""
        return local_cookie or self.env_cookie

    def __getattr__(self, item):
        if item in self.settings:
            return self.settings[item]
        return ParserItem()
