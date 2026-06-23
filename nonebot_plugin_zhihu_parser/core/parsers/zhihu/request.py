from __future__ import annotations

import asyncio
import json
from typing import Any, Callable
from http.cookies import SimpleCookie

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from nonebot.log import logger

from ...exception import ParseException
from .common import RequestContext

from .zse_signer import sign_zhihu_fetch_request

import httpx

class ZhihuRequestMixin:
    async def _fetch_initial_data(self, url: str, validator: Callable | None = None, **kwargs) -> tuple[dict[str, Any], dict[str, str]]:
        cookie_str = ""
        try:
            cookie_file = self.cfg.config_dir / "zhihu_cookies.txt"
            if cookie_file.exists():
                cookie_str = cookie_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning(f"[知乎 API] 读取 Cookie 文件失败: {e}")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.zhihu.com/",
        }
        # 绝不篡改！原封不动地发送洗血得到的完整 Cookie，保证底层签名校验通过
        if cookie_str:
            headers["Cookie"] = cookie_str

        def do_req():
            return curl_requests.get(
                url,
                headers=headers,
                impersonate="chrome",
                timeout=getattr(self.cfg, "common_timeout", 10)
            )

        try:
            response = await asyncio.to_thread(do_req)
        except Exception as e:
            logger.error(f"[知乎 HTML] 网络请求崩溃: {e}")
            raise ParseException("知乎网页请求失败") from e

        if response.status_code >= 400:
            logger.error(f"[知乎 HTML] 请求被拒: HTTP {response.status_code}")
            raise ParseException(f"知乎网页拒绝访问: HTTP {response.status_code}")

        soup = BeautifulSoup(response.text, "html.parser")
        tag = soup.find("script", id="js-initialData")
        if not tag:
            raise ParseException("未找到 js-initialData，可能 Cookie 失效，请发送指令【刷新知乎ck】")
        
        try:
            data = json.loads(tag.text)
        except Exception:
            raise ParseException("解析 initialData 失败")

        if validator and not validator(data):
            raise ParseException("获取到的数据未能通过旧版校验器，可能需要发送指令【刷新知乎ck】！")
            
        return data, dict(response.headers)

    def _has_answer_entities(self, payload: dict, question_id: str, answer_id: str, *args, **kwargs) -> bool:
        return str(answer_id) in payload.get("initialState", {}).get("entities", {}).get("answers", {})
    _has_answer_entity = _has_answer_entities

    def _has_article_entities(self, payload: dict, article_id: str, *args, **kwargs) -> bool:
        return str(article_id) in payload.get("initialState", {}).get("entities", {}).get("articles", {})
    _has_article_entity = _has_article_entities

    def _has_question_entities(self, payload: dict, question_id: str, *args, **kwargs) -> bool:
        return str(question_id) in payload.get("initialState", {}).get("entities", {}).get("questions", {})
    _has_question_entity = _has_question_entities

    def _has_pin_entities(self, payload: dict, pin_id: str, *args, **kwargs) -> bool:
        return str(pin_id) in payload.get("initialState", {}).get("entities", {}).get("pins", {})
    _has_pin_entity = _has_pin_entities

    def _has_zvideo_entities(self, payload: dict, zvideo_id: str, *args, **kwargs) -> bool:
        return str(zvideo_id) in payload.get("initialState", {}).get("entities", {}).get("zvideos", {})
    _has_zvideo_entity = _has_zvideo_entities

    def _entities(self, payload: dict) -> dict:
        return payload.get("initialState", {}).get("entities", {})

    def _answers(self, entities: dict) -> dict:
        return entities.get("answers", {})

    def _articles(self, entities: dict) -> dict:
        return entities.get("articles", {})

    def _questions(self, entities: dict) -> dict:
        return entities.get("questions", {})

    def _users(self, entities: dict) -> dict:
        return entities.get("users", {})

    def _pins(self, entities: dict) -> dict:
        return entities.get("pins", {})

    def _zvideos(self, entities: dict) -> dict:
        return entities.get("zvideos", {})

    async def _fetch_api_json(self, url: str) -> tuple[dict[str, Any], dict[str, str]]:
        proxy = getattr(self, "proxy", None)
        full_cookie = ""
        try:
            cookie_file = self.cfg.config_dir / "zhihu_cookies.txt"
            if cookie_file.exists():
                full_cookie = cookie_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

        dc0_only = ""
        real_dc0 = ""
        if full_cookie:
            for item in full_cookie.split(";"):
                if "d_c0=" in item:
                    dc0_only = item.strip()
                    real_dc0 = item.split("d_c0=")[1].strip()
                    break

        cookie_str = ""
        if "/answer" in url or "answers" in url:
            cookie_str = full_cookie
        elif dc0_only:
            cookie_str = dc0_only

        async with httpx.AsyncClient(proxy=proxy, timeout=getattr(self.cfg, "common_timeout", 10)) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.zhihu.com/",
                "x-api-version": "3.0.91",
                "x-requested-with": "fetch",
            }
            if cookie_str:
                headers["Cookie"] = cookie_str

            request = client.build_request("GET", url, headers=headers)
            exact_uri = request.url.raw_path.decode("ascii")
            sign_headers = sign_zhihu_fetch_request(exact_uri, dc0=real_dc0)
            request.headers.update(sign_headers)

            try:
                response = await client.send(request)
            except Exception as e:
                raise ParseException("知乎 API 网络请求失败") from e

        if response.status_code >= 400:
            raise ParseException(f"知乎 API 拒绝访问: HTTP {response.status_code}")
        return response.json(), dict(request.headers)
    
    # 【新增兼容方法】承接旧版 handlers.py 对想法（Pin）的特殊请求调用
    async def _fetch_json_data(self, url: str, *args, **kwargs) -> tuple[dict[str, Any], dict[str, str]]:
        return await self._fetch_api_json(url)
        
    @staticmethod
    def _article_url(article_id: str) -> str:
        return f"https://zhuanlan.zhihu.com/p/{article_id}"

    @staticmethod
    def _pin_url(pin_id: str) -> str:
        return f"https://www.zhihu.com/pin/{pin_id}"

    @staticmethod
    def _pin_api_url(pin_id: str) -> str:
        return (
            "https://www.zhihu.com/api/v4/pins/"
            f"{pin_id}?include=content,content_html,created_time,updated_time,author,origin_pin"
        )

    @staticmethod
    def _answer_url(question_id: str, answer_id: str) -> str:
        return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"

    @staticmethod
    def _question_url(question_id: str) -> str:
        return f"https://www.zhihu.com/question/{question_id}"