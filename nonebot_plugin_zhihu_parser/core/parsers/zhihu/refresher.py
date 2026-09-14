import time
import hmac
import hashlib
import json
import urllib.parse
from threading import Lock
from curl_cffi import requests as curl_requests
from nonebot.log import logger
from .zse_signer import ZseSigner
from .identity import ZHIHU_CURL_IMPERSONATE, ZHIHU_USER_AGENT
from .maintenance import CookieChangedError, RefreshResult, atomic_write_text
from ....config import PluginConfig

CLIENT_ID = "c3cef7c66a1843f8b3a9e6a1e3160e20"
CLIENT_SECRET = "d1b964811afb40118a12068ff74a12f4"
GRANT_TYPE = "refresh_token"
SOURCE = "com.zhihu.web"
_REFRESH_LOCK = Lock()

def _hmac_sha1_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha1).hexdigest()

def do_refresh_token(cfg: PluginConfig) -> RefreshResult:
    if not _REFRESH_LOCK.acquire(blocking=False):
        return RefreshResult(False, "busy")
    try:
        return _refresh_token(cfg)
    except CookieChangedError:
        logger.warning("[知乎保活] 登录信息已变化，保留用户的新凭据")
        return RefreshResult(False, "cookie_changed")
    except OSError as error:
        logger.error(f"[知乎保活] 登录信息读写失败：{type(error).__name__}")
        return RefreshResult(False, "write_error")
    finally:
        _REFRESH_LOCK.release()


def _refresh_token(cfg: PluginConfig) -> RefreshResult:
    cookie_file = cfg.config_dir / "zhihu_cookies.txt"
    if not cookie_file.exists():
        logger.warning("[知乎保活] 找不到 Cookie 文件，放弃刷新")
        return RefreshResult(False, "missing_cookie")
        
    old_cookie_raw = cookie_file.read_text(encoding="utf-8")
    old_cookie_str = old_cookie_raw.strip()
    if not old_cookie_str:
        logger.warning("[知乎保活] Cookie 为空，放弃刷新")
        return RefreshResult(False, "missing_cookie")

    # 提取纯正的 z_c0
    z_c0 = ""
    for item in old_cookie_str.split(";"):
        if item.strip().startswith("z_c0="):
            z_c0 = item.strip().partition("=")[2].strip()
            break
            
    if not z_c0:
        logger.warning("[知乎保活] 找不到关键凭证 z_c0，放弃刷新")
        return RefreshResult(False, "missing_cookie")

    headers_base = {
        "User-Agent": ZHIHU_USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
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
            impersonate=ZHIHU_CURL_IMPERSONATE,
            timeout=30,
            proxy=cfg.get("proxy", None),
            allow_redirects=False,
        )
        try:
            resp1_data = resp1.json()
        except ValueError:
            resp1_data = {}
        refresh_token = (
            resp1_data.get("refresh_token")
            if resp1.status_code == 200 and isinstance(resp1_data, dict) else None
        )
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            logger.error(f"[知乎保活] 未能获取刷新凭证，HTTP {resp1.status_code}")
            reason = (
                "login_expired" if resp1.status_code == 401
                else "invalid_response" if resp1.status_code == 200
                else "request_failed"
            )
            return RefreshResult(False, reason)

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
            impersonate=ZHIHU_CURL_IMPERSONATE,
            timeout=30,
            proxy=cfg.get("proxy", None),
            allow_redirects=False,
        )
        
        if resp2.status_code == 200:
            # 捕获新下发的 Cookie
            new_cookies_dict = resp2.cookies.get_dict()
            new_z_c0 = new_cookies_dict.get("z_c0")
            
            if (
                isinstance(new_z_c0, str) and new_z_c0.strip()
                and new_z_c0.lower() not in {"deleted", "null"}
            ):
                # 狸猫换太子，更新本地文件
                # 仅替换精确的登录Cookie键，不误改碰巧同值的其他Cookie。
                new_full_cookie = ";".join(
                    part.partition("=")[0] + "=" + new_z_c0
                    if part.strip().startswith("z_c0=") else part
                    for part in old_cookie_str.split(";")
                )
                atomic_write_text(cookie_file, new_full_cookie, expected=old_cookie_raw)
                logger.info("[知乎保活] 🎉 满血复活成功！已获取并写入全新 z_c0 凭证！")
                return RefreshResult(True)
            else:
                logger.warning("[知乎保活] 未收到新的登录凭证，保活未完成")
                return RefreshResult(False, "invalid_response")
        else:
            logger.error(f"[知乎保活] 更新登录凭证失败，HTTP {resp2.status_code}")

            return RefreshResult(
                False, "login_expired" if resp2.status_code == 401 else "request_failed"
            )

    except (CookieChangedError, OSError):
        raise
    except Exception as e:
        logger.error(f"[知乎保活] 请求或响应处理失败：{type(e).__name__}")
        return RefreshResult(
            False, "network_error" if isinstance(e, curl_requests.exceptions.RequestException)
            else "unexpected_error"
        )
