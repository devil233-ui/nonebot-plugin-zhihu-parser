"""知乎保活结果、持久化提醒与调度配置；不保存登录凭据。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nonebot.log import logger


@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    reason: str = ""

    @property
    def counts_as_failure(self) -> bool:
        return not self.ok and self.reason not in {"busy", "cookie_changed"}


_RESULT_MESSAGES = {
    "login_expired": (
        "知乎登录已失效，链接解析可能无法使用。请在浏览器重新登录并更新登录信息；"
        "不要把Cookie发到群聊里。"
    ),
    "missing_cookie": "知乎登录信息缺失，无法保活。请先登录并配置登录信息。",
    "network_error": "暂时无法连接知乎，未能完成保活。请稍后再试。",
    "request_failed": "知乎暂时拒绝了保活请求，未更新登录信息。请稍后再试。",
    "invalid_response": "知乎未返回完整的登录信息，保活未成功。请稍后再试。",
    "write_error": "登录信息无法读写，保活未完成。请检查服务器存储和文件权限。",
    "cookie_changed": "检测到登录信息已被更新，本次保活已取消，不会覆盖新登录信息。",
    "busy": "知乎保活正在进行，请等待本次结果，不要重复刷新。",
    "unexpected_error": "知乎保活未完成，原有登录信息未被替换。请联系管理员检查。",
}


def format_refresh_result(result: RefreshResult, failures: int = 0) -> str:
    """手动反馈和自动提醒共享文案，不拼接响应体、异常或凭据。"""
    if result.ok:
        return "知乎保活成功，登录信息已更新。"
    message = _RESULT_MESSAGES.get(result.reason, _RESULT_MESSAGES["unexpected_error"])
    if failures:
        return f"知乎保活已连续失败{failures}次。\n{message}"
    return message


class CookieChangedError(RuntimeError):
    """刷新期间用户更新了凭据，旧请求的结果不能覆盖它。"""


def atomic_write_text(path: Path, text: str, *, expected: str | None = None) -> None:
    """在同一目录原子替换；凭据与状态文件仅允许属主读写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        if expected is not None and path.read_text(encoding="utf-8") != expected:
            raise CookieChangedError("login information changed during refresh")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def refresh_job_options(timezone, jitter_seconds: int = 900) -> dict:
    """保留原有日期安排，只使用APScheduler内置的非阻塞随机延迟。"""
    try:
        if isinstance(jitter_seconds, bool):
            raise ValueError
        jitter = int(jitter_seconds)
        if not 0 <= jitter <= 3600:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        logger.warning("[知乎保活] 随机延迟配置无效，使用默认的15分钟上限")
        jitter = 900
    return {
        "trigger": "cron",
        "id": "zhihu_cookie_refresh",
        "day": "*/3",
        "hour": 4,
        "minute": 0,
        "timezone": timezone,
        "jitter": jitter,
        "replace_existing": True,
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 3600,
    }


def _recipient_ids(value) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            logger.warning("[知乎保活] 提醒接收人配置格式无效，已忽略该配置")
            return ()
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    result: set[int] = set()
    for item in value:
        try:
            raw = str(item)
            if isinstance(item, bool) or not raw.isascii() or not raw.isdigit():
                raise ValueError
            recipient = int(item)
            if recipient <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            logger.warning("[知乎保活] 提醒接收人配置包含无效账号，已跳过")
            continue
        result.add(recipient)
    return tuple(sorted(result))


def refresh_alert_targets(config) -> tuple[tuple[str, int], ...]:
    groups = _recipient_ids(getattr(config, "zhihu_refresh_alert_groups", ()))
    users = getattr(config, "zhihu_refresh_alert_users", None)
    # 明确配置群后不额外私聊；无专用目标时才使用现有管理员名单。
    if users is None:
        users = () if groups else getattr(config, "superusers", ())
    return tuple(("group", group) for group in groups) + tuple(
        ("private", user) for user in _recipient_ids(users)
    )


class RefreshMonitor:
    """串行运行保活，连续失败两次提醒；逐目标去重并跨重启保留状态。"""

    def __init__(
        self,
        state_file: Path,
        targets: tuple[tuple[str, int], ...],
        refresh: Callable,
        get_bots: Callable,
    ) -> None:
        self.state_file = state_file
        self.targets = targets
        self.refresh = refresh
        self.get_bots = get_bots
        self.consecutive_failures = 0
        self.reason = "unexpected_error"
        self.notified: set[str] = set()
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            raw = self.state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as error:
            logger.warning(f"[知乎保活] 无法读取提醒状态：{type(error).__name__}")
            return
        try:
            state = json.loads(raw)
            count = state["consecutive_failures"]
            notified = state.get("notified", [])
            if type(count) is not int or count < 0 or not isinstance(notified, list):
                raise ValueError
            if not all(isinstance(item, str) for item in notified):
                raise ValueError
            reason = state.get("reason", "unexpected_error")
            if not isinstance(reason, str):
                raise ValueError
            # 所有字段验证完成后再整体接收，避免半份损坏状态污染计数。
            self.consecutive_failures = count
            self.notified = set(notified)
            self.reason = reason if reason in _RESULT_MESSAGES else "unexpected_error"
        except (ValueError, TypeError, KeyError, AttributeError):
            logger.warning("[知乎保活] 提醒状态损坏，将重新累计失败次数")

    def _save(self) -> None:
        state = {
            "consecutive_failures": self.consecutive_failures,
            "reason": self.reason,
            "notified": sorted(self.notified),
        }
        try:
            atomic_write_text(self.state_file, json.dumps(state, ensure_ascii=False) + "\n")
        except OSError as error:
            logger.error(
                f"[知乎保活] 无法保存提醒状态，重启后可能重复提醒：{type(error).__name__}"
            )

    async def run(self, config) -> RefreshResult:
        if self._lock.locked():
            return RefreshResult(False, "busy")
        async with self._lock:
            try:
                result = await asyncio.to_thread(self.refresh, config)
                if not isinstance(result, RefreshResult):
                    raise TypeError("refresh did not return a result")
            except Exception as error:
                # 这里是自动/手动任务边界，隔离错误，不能泄漏异常中的请求头。
                logger.error(f"[知乎保活] 执行失败：{type(error).__name__}")
                result = RefreshResult(False, "unexpected_error")
            if result.ok:
                self.consecutive_failures = 0
                self.reason = "unexpected_error"
                self.notified.clear()
            elif result.counts_as_failure:
                self.consecutive_failures += 1
                self.reason = result.reason
            else:
                return result
            self._save()
            await self._notify_pending()
            return result

    async def notify_pending(self) -> None:
        """机器人重新连接后补发未成功送达的提醒，不额外刷新知乎。"""
        async with self._lock:
            await self._notify_pending()

    async def _notify_pending(self) -> None:
        if self.consecutive_failures < 2:
            return
        if not self.targets:
            logger.warning("[知乎保活] 已连续失败，但未设置有效的提醒接收人")
            return
        try:
            bots = list(self.get_bots())
        except Exception as error:
            logger.warning(f"[知乎保活] 无法获取在线机器人：{type(error).__name__}")
            return
        if not bots:
            logger.warning("[知乎保活] 暂无在线机器人，提醒将于重新连接后补发")
            return
        message = format_refresh_result(
            RefreshResult(False, self.reason), self.consecutive_failures
        )
        for kind, recipient in self.targets:
            key = f"{kind}:{recipient}"
            if key in self.notified:
                continue
            api, parameter = (
                ("send_group_msg", "group_id") if kind == "group"
                else ("send_private_msg", "user_id")
            )
            for bot in bots:
                try:
                    await asyncio.wait_for(
                        bot.call_api(api, **{parameter: recipient}, message=message),
                        timeout=15,
                    )
                except Exception as error:
                    logger.warning(f"[知乎保活] 提醒未送达，将保留待发状态：{type(error).__name__}")
                    continue
                self.notified.add(key)
                self._save()
                break
