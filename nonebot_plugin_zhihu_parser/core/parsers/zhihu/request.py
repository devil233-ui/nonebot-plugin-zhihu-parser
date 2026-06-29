import json
import re
import asyncio
from typing import Any, Callable
import httpx
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from nonebot.log import logger

from .zse_signer import sign_zhihu_fetch_request
from ...exception import ParseException

class ZhihuRequestMixin:
    # ==========================================
    # 【补回丢失的 URL 构造器】
    # 旧版 handlers.py 获取各种内容链接的必需方法
    # ==========================================
    def _answer_url(self, question_id: str, answer_id: str, *args, **kwargs) -> str:
        return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"

    def _article_url(self, article_id: str, *args, **kwargs) -> str:
        return f"https://zhuanlan.zhihu.com/p/{article_id}"

    def _question_url(self, question_id: str, *args, **kwargs) -> str:
        return f"https://www.zhihu.com/question/{question_id}"

    def _pin_url(self, pin_id: str, *args, **kwargs) -> str:
        return f"https://www.zhihu.com/pin/{pin_id}"

    def _pin_api_url(self, pin_id: str, *args, **kwargs) -> str:
        return f"https://www.zhihu.com/api/v4/pins/{pin_id}?include=author,content,excerpt,reaction_count,comment_count"
        
    def _zvideo_url(self, zvideo_id: str, *args, **kwargs) -> str:
        return f"https://www.zhihu.com/zvideo/{zvideo_id}"

    # ==========================================
    # 【万能兼容层】
    # 向下兼容旧版 handlers 的所有实体校验与提取调用
    # ==========================================
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

    # ==========================================
    # 【终极降维打击：API 劫持】
    # 强制把网页端带有 WAF JS 验证的 URL 拦截，转换为纯净的 v4 API 请求！
    # ==========================================
    async def _fetch_initial_data(self, url: str, validator: Callable | None = None, **kwargs) -> tuple[dict[str, Any], dict[str, str]]:
        # 1. 劫持“回答”页面
        ans_match = re.search(r"/answer/(\d+)", url)
        if ans_match:
            answer_id = ans_match.group(1)
            api_url = f"https://www.zhihu.com/api/v4/answers/{answer_id}?include=content,author,question,excerpt,voteup_count,comment_count,created_time,updated_time"
            api_data, hdrs = await self._fetch_api_json(api_url)
            
            fake_data = {
                "initialState": {
                    "entities": {
                        "answers": { answer_id: api_data },
                        "questions": { str(api_data.get("question", {}).get("id", "0")): api_data.get("question", {}) },
                        "users": { str(api_data.get("author", {}).get("id", "0")): api_data.get("author", {}) }
                    }
                }
            }
            if validator and not validator(fake_data):
                raise ParseException("API转换数据未能通过旧版校验器！")
            return fake_data, hdrs

        # 2. 劫持“专栏文章”页面
        art_match = re.search(r"/p/(\d+)", url)
        if art_match:
            article_id = art_match.group(1)
            api_url = f"https://www.zhihu.com/api/v4/articles/{article_id}?include=content,author,column,excerpt,voteup_count,comment_count,created_time,updated_time"
            api_data, hdrs = await self._fetch_api_json(api_url)
            
            fake_data = {
                "initialState": {
                    "entities": {
                        "articles": { article_id: api_data },
                        "users": { str(api_data.get("author", {}).get("id", "0")): api_data.get("author", {}) }
                    }
                }
            }
            if validator and not validator(fake_data):
                raise ParseException("API转换数据未能通过旧版校验器！")
            return fake_data, hdrs
            
        # 3. 劫持单纯的“问题”页面
        q_match = re.search(r"zhihu\.com/question/(\d+)(?!/answer)", url)
        if q_match:
            question_id = q_match.group(1)
            api_url = f"https://www.zhihu.com/api/v4/questions/{question_id}?include=detail,author,excerpt"
            api_data, hdrs = await self._fetch_api_json(api_url)
            
            fake_data = {
                "initialState": {
                    "entities": {
                        "questions": { question_id: api_data }
                    }
                }
            }
            return fake_data, hdrs

        logger.error(f"[API劫持] 未知的 URL 格式，无法转换: {url}")
        raise ParseException("当前链接暂不支持 API 级无损转换。")

    # ==========================================
    # 【核心发包与毒素清洗】
    # ==========================================
    async def _fetch_api_json(self, url: str) -> tuple[dict[str, Any], dict[str, str]]:
        proxy = getattr(self, "proxy", None)
        cookie_str = ""
        real_dc0 = ""
        
        try:
            cookie_file = self.cfg.config_dir / "zhihu_cookies.txt"
            if cookie_file.exists():
                raw_cookie = cookie_file.read_text(encoding="utf-8").strip()
                # 剔除过期必死标识（__zse_ck等），只保留最核心的四样免死金牌
                core_keys = ["z_c0=", "d_c0=", "SESSIONID=", "_xsrf="]
                clean_parts = []
                for item in raw_cookie.split(";"):
                    item_stripped = item.strip()
                    if any(item_stripped.startswith(k) for k in core_keys):
                        clean_parts.append(item_stripped)
                    # 顺手将算签名需要的 d_c0 提取出来
                    if item_stripped.startswith("d_c0="):
                        real_dc0 = item_stripped[5:]
                cookie_str = "; ".join(clean_parts)
        except Exception as e:
            logger.warning(f"[知乎 API] 读取 Cookie 文件失败: {e}")

        async with httpx.AsyncClient(proxy=proxy, timeout=getattr(self.cfg, "common_timeout", 10)) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Referer": "https://www.zhihu.com/",
                "x-api-version": "3.0.91",
                "x-requested-with": "fetch",
            }
            if cookie_str:
                headers["Cookie"] = cookie_str

            # 让 httpx 生成精确 URL 并计算纯正的 x-zse-96
            request = client.build_request("GET", url, headers=headers)
            exact_uri = request.url.raw_path.decode("ascii")
            sign_headers = sign_zhihu_fetch_request(exact_uri, dc0=real_dc0)
            request.headers.update(sign_headers)

            try:
                response = await client.send(request)
            except Exception as e:
                logger.error(f"[知乎 API] 网络请求崩溃: {e}")
                raise ParseException("知乎 API 网络请求失败") from e

        if response.status_code >= 400:
            logger.error(f"[知乎 API] 请求被拒: HTTP {response.status_code} - {response.text}")
            raise ParseException(f"知乎 API 拒绝访问: HTTP {response.status_code}")

        try:
            payload = response.json()
        except Exception:
            raise ParseException("知乎 API 返回数据格式异常")

        if "error" in payload and "message" in payload["error"]:
            raise ParseException(f"知乎 API 返回错误: {payload['error']['message']}")

        return payload, dict(request.headers)

    # 【兼容方法】承接旧版 handlers.py 对想法（Pin）的特殊请求调用
    async def _fetch_json_data(self, url: str, *args, **kwargs) -> tuple[dict[str, Any], dict[str, str]]:
        return await self._fetch_api_json(url)