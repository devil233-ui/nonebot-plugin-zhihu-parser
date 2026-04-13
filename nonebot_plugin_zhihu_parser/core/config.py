import json
from pathlib import Path
from nonebot.log import logger

class PluginConfig:
    def __init__(self):
        # 初始化 Nonebot 风格的数据目录
        self.data_dir = Path.cwd() / "data" / "nonebot_plugin_zhihu"
        self.temp_dir = self.data_dir / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        # 模拟原项目的配置字典，提供必需的默认值
        self.settings = {
            "proxy": None,
            "bilibili_cookie": "",
            "douyin_cookie": "",
            "weibo_cookie": "",
            "xiaohongshu_cookie": "",
            "twitter_auth_token": "",
        }
        
    def __getitem__(self, key):
        return self.settings.get(key)
        
    def get(self, key, default=None):
        return self.settings.get(key, default)

    # 模拟原版的获取配置方法，防止解析器内部调用时报错
    def get_config(self, key: str, default=None):
        return self.settings.get(key, default)
        
    def set_config(self, key: str, value):
        self.settings[key] = value