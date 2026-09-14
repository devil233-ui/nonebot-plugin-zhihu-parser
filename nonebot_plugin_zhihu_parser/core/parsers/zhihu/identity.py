"""解析和保活共用的浏览器身份，避免请求头和模拟版本各自漂移。"""
from typing import Final

ZHIHU_USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ZHIHU_CURL_IMPERSONATE: Final = "chrome124"
