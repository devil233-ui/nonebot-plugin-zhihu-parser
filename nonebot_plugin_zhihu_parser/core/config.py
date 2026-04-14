import json
from pathlib import Path
from nonebot.log import logger
from nonebot import get_driver

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

class PluginConfig:
    def __init__(self):
        # --- 严格执行用户定义的路径规范 ---
        self.data_dir = Path.cwd() / "data" / "nonebot_plugin_zhihu_parser"
        self.config_dir = Path.cwd() / "config" / "nonebot_plugin_zhihu_parser"
        self.temp_dir = Path.cwd() / "cache" / "nonebot_plugin_zhihu_parser"
        
        self.cache_dir = self.temp_dir

        # 确保所有目录存在
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        # 补上 Downloader 等组件需要的属性
        self.source_max_size = 50
        self.download_retry_times = 5
        self.common_timeout = 60
        self.download_timeout = 60
        self.max_duration = 3600
        
        # --- Cookie 文件管理 (位于 config 目录) ---
        self.cookie_file = self.config_dir / "zhihu_cookies.txt"
        if not self.cookie_file.exists():
            self.cookie_file.write_text("", encoding="utf-8")
            logger.info(f"[知乎解析] 已在 config 目录下创建 CK 文件: {self.cookie_file}")
            
        local_cookie = self.cookie_file.read_text(encoding="utf-8").strip()
        
        # 读取 .env.dev 配置作为备选
        nb_config = get_driver().config
        env_cookie = getattr(nb_config, "zhihu_cookie", "").strip()
        
        # 优先级：config 目录下的 txt 文件 > .env.dev
        final_cookie = local_cookie if local_cookie else env_cookie

        # 构造 parser.zhihu 结构，供底层解析器通过 self.mycfg 调用
        self.parser = ParserItem({
            "zhihu": ParserItem({
                "cookie": final_cookie
            })
        })
        
        # 基础配置项字典
        self.settings = {
            "proxy": getattr(nb_config, "zhihu_proxy", None),
            "zhihu_cookie": final_cookie,
        }
        
    def get(self, key, default=None):
        return self.settings.get(key, default)

    def get_config(self, key: str, default=None):
        return self.settings.get(key, default)

    def __getattr__(self, item):
        if item in self.settings:
            return self.settings[item]
        return ParserItem()