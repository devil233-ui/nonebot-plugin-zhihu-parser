import time
import hmac
import hashlib
import json
import urllib.parse
from curl_cffi import requests as curl_requests
from nonebot.log import logger
from .zse_signer import ZseSigner
from ....config import PluginConfig

CLIENT_ID = "c3cef7c66a1843f8b3a9e6a1e3160e20"
CLIENT_SECRET = "d1b964811afb40118a12068ff74a12f4"
GRANT_TYPE = "refresh_token"
SOURCE = "com.zhihu.web"

def _hmac_sha1_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha1).hexdigest()

def do_refresh_token(cfg: PluginConfig):
    cookie_file = cfg.config_dir / "zhihu_cookies.txt"
    if not cookie_file.exists():
        logger.warning("[知乎保活] 找不到 Cookie 文件，放弃刷新")
        return
        
    old_cookie_str = cookie_file.read_text(encoding="utf-8").strip()
    if not old_cookie_str:
        logger.warning("[知乎保活] Cookie 为空，放弃刷新")
        return

    # 提取纯正的 z_c0
    z_c0 = ""
    for item in old_cookie_str.split(";"):
        if "z_c0=" in item:
            z_c0 = item.split("z_c0=")[1].strip()
            break
            
    if not z_c0:
        logger.warning("[知乎保活] 找不到关键凭证 z_c0，放弃刷新")
        return

    headers_base = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Origin": "https://www.zhihu.com",
        "Referer": "https://www.zhihu.com/signin",
        "x-requested-with": "fetch",
        "cookie": old_cookie_str
    }

    try:
        # 第一发：白嫖 Refresh Token
        resp1 = curl_requests.post(
            "https://www.zhihu.com/api/account/prod/token/refresh",
            headers=headers_base,
            impersonate="chrome"
        )
        refresh_token = resp1.json().get("refresh_token")
        if not refresh_token:
            logger.error(f"[知乎保活] 第一步失败，未能获取 refresh_token。响应: {resp1.text}")
            return

        # 第二发：融合签名，向 API 发起官方换血请求
        timestamp = int(time.time() * 1000)
        message = f"{GRANT_TYPE}{CLIENT_ID}{SOURCE}{timestamp}"
        signature = _hmac_sha1_hex(CLIENT_SECRET, message)

        payload_map = {
            "client_id": CLIENT_ID,
            "grant_type": GRANT_TYPE,
            "timestamp": str(timestamp),
            "source": SOURCE,
            "signature": signature,
            "refresh_token": refresh_token,
        }

        # 拼接成类似 key=value&key2=value2 的表单
        form_data = "&".join([f"{urllib.parse.quote(k)}={urllib.parse.quote(v)}" for k, v in payload_map.items()])
        
        # [核武器登场] 加密！
        encrypted_data = ZseSigner.encrypt_zse_v4(form_data)

        headers_auth = headers_base.copy()
        headers_auth["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        headers_auth["x-zse-83"] = "3_3.0"  # 告诉服务器我是合法 App

        resp2 = curl_requests.post(
            "https://www.zhihu.com/api/v3/oauth/sign_in",
            headers=headers_auth,
            data=encrypted_data,
            impersonate="chrome"
        )
        
        if resp2.status_code == 200:
            # 捕获新下发的 Cookie
            new_cookies_dict = resp2.cookies.get_dict()
            new_z_c0 = new_cookies_dict.get("z_c0")
            
            if new_z_c0:
                # 狸猫换太子，更新本地文件
                new_full_cookie = old_cookie_str.replace(z_c0, new_z_c0)
                cookie_file.write_text(new_full_cookie, encoding="utf-8")
                logger.info("[知乎保活] 🎉 满血复活成功！已获取并写入全新 z_c0 凭证！")
            else:
                logger.warning("[知乎保活] 换血成功，但未捕获到 Set-Cookie 头。")
        else:
            logger.error(f"[知乎保活] 换血失败，状态码: {resp2.status_code}, 响应: {resp2.text}")

    except Exception as e:
        logger.error(f"[知乎保活] 发生崩溃: {e}")