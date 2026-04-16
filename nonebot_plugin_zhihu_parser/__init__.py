import re
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Message, MessageSegment, MessageEvent, Bot, GroupMessageEvent, PrivateMessageEvent
from nonebot.log import logger
from nonebot import get_driver
from nonebot.rule import Rule
from nonebot.typing import T_State

from nonebot_plugin_zhihu_parser.config import PluginConfig
from .core.parsers.zhihu.parser import ZhihuParser
from .core.download import Downloader
from .core.render import Renderer
from .core.debounce import Debouncer
from .core.clean import CacheCleaner

global_config = get_driver().config
# 从 .env 读取配置，如果没有则提供默认值（分批阈值默认10，防抖默认86400秒）
ZHIHU_BATCH_SIZE = getattr(global_config, "zhihu_batch_size", 10)
ZHIHU_DEBOUNCE_TIME = getattr(global_config, "zhihu_debounce_time", 86400)
ZHIHU_CACHE_MAX_AGE = getattr(global_config, "zhihu_cache_max_age", 86400 * 7)
ZHIHU_MAX_IMAGES = getattr(global_config, "zhihu_max_images", 50)

cfg = PluginConfig()
zhihu_parser = None
zhihu_renderer = None
# 将配置好的防抖时间传给防抖器
zhihu_debouncer = Debouncer(ttl_seconds=ZHIHU_DEBOUNCE_TIME)

@get_driver().on_startup
async def init_parser():
    global zhihu_parser, zhihu_renderer
    downloader = Downloader(cfg)
    zhihu_parser = ZhihuParser(cfg, downloader)
    Renderer.load_resources()
    zhihu_renderer = Renderer(cfg)
    max_age = int(ZHIHU_CACHE_MAX_AGE)
    zhihu_cleaner = CacheCleaner(temp_dir=cfg.cache_dir, max_age_seconds=max_age)
    zhihu_cleaner.start()
    
    days = max_age / 86400
    logger.info(f"知乎解析插件：{days:g}天缓存自动清理任务已挂载启动！")

def check_zhihu_url():
    async def _check(bot: Bot, event: MessageEvent, state: T_State) -> bool:
        raw_msg = str(event.get_message()).replace("\\/", "/")
        pat = re.compile(r"(https?://(?:www\.)?zhihu\.com/[^\s\"\'\\]+|https?://zhuanlan\.zhihu\.com/[^\s\"\'\\]+)")
        match = pat.search(raw_msg)
        if match:
            state['zhihu_match'] = match
            state['zhihu_raw'] = raw_msg
            return True
        return False
    return Rule(_check)

zhihu_matcher = on_message(rule=check_zhihu_url(), priority=10, block=False)

@zhihu_matcher.handle()
async def handle_zhihu(bot: Bot, event: MessageEvent, state: T_State):
    if not zhihu_parser or not zhihu_renderer:
        logger.warning("知乎解析器仍在初始化中，请稍后再试")
        return
        
    text = state['zhihu_raw']
    base_match = state['zhihu_match']
    url = base_match.group(0).replace("&amp;", "&")
    text = text.replace("&amp;", "&")
    session_id = event.get_session_id()
    
    if zhihu_debouncer.hit_url(session_id, url):
        logger.info(f"[{session_id}] 链接 {url} 在防抖时间内，已跳过重复解析")
        await zhihu_matcher.send("24小时内有人水果了，端下去罢")
        return
        
    handlers = getattr(zhihu_parser, "_handlers", {})
    final_keyword = None
    final_match_obj = None

    for kw, func in handlers.items():
        pat = getattr(func, "__parser_pattern__", None)
        if pat:
            if isinstance(pat, str): pat = re.compile(pat)
            match = pat.search(text)
            if match:
                final_keyword = kw
                final_match_obj = match
                break

    if not final_match_obj:
        KNOWN_PATTERNS = [
            ("/answer/", re.compile(r"zhihu\.com/question/(?P<question_id>\d+)/answer/(?P<answer_id>\d+)")),
            ("www.zhihu.com/question/", re.compile(r"zhihu\.com/question/(?P<question_id>\d+)")),
            ("zhuanlan.zhihu.com/p/", re.compile(r"zhuanlan\.zhihu\.com/p/(?P<article_id>\d+)")),
            ("www.zhihu.com/pin/", re.compile(r"zhihu\.com/pin/(?P<pin_id>\d+)")),
        ]
        for exact_kw, pat in KNOWN_PATTERNS:
            match = pat.search(text)
            if match and exact_kw in handlers:
                final_keyword = exact_kw
                final_match_obj = match
                break
                
    if not final_keyword or not final_match_obj:
        return

    try:
        parse_res = await zhihu_parser.parse(final_keyword, final_match_obj)
        if not parse_res: return
            
        # A. 发送预览卡片
        card_path = await zhihu_renderer.render_card(parse_res)
        if card_path and card_path.exists():
            await zhihu_matcher.send(MessageSegment.image(card_path))

        # B. 分批次合并转发逻辑
        bot_id = int(bot.self_id)
        img_counter = 0
        batch_num = 1
        current_combined_msg = Message()
        total_img_counter = 0
        limit_reached = False
        
        # 初始添加标题
        if getattr(parse_res, "title", None):
            current_combined_msg += MessageSegment.text(f"【{parse_res.title}】\n\n")

        async def send_current_batch(msg: Message, is_last: bool = False):
            if not msg: return
            prefix = f"(第{batch_num}部分) " if not (batch_num == 1 and is_last) else ""
            node = [MessageSegment.node_custom(user_id=bot_id, nickname=f"知乎解析 {prefix}", content=msg)]
            try:
                if isinstance(event, GroupMessageEvent):
                    await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=node)
                else:
                    await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=node)
            except Exception as e:
                logger.error(f"发送第 {batch_num} 部分转发失败: {e}")
                
                # [恢复] 降级单发机制：因为现在有总量限制，可以放心拆包单发了
                await zhihu_matcher.send(f"⚠️ 第 {batch_num} 部分触发风控拦截，正在自动拆分单独发送...")
                
                # [新增] 专门处理长文本折叠的内部助手函数
                async def safe_send_text(text_msg: Message):
                    t_str = str(text_msg).strip()
                    if not t_str: return
                    
                    # 纯文字装进合并转发里“折叠”起来
                    if len(t_str): 
                        t_node = [MessageSegment.node_custom(user_id=bot_id, nickname="风控部分纯文字折叠", content=Message(t_str))]
                        try:
                            if isinstance(event, GroupMessageEvent):
                                await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=t_node)
                            else:
                                await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=t_node)
                        except Exception:
                            # 终极兜底：万一纯文本转发都失败，截断只发前500字
                            await zhihu_matcher.send(t_str[:500] + "\n...[字数过多已截断]")
                    else:
                        # 不满 150 字的短句（比如图注、小标题）直接发，不影响阅读连贯性
                        await zhihu_matcher.send(text_msg)

                fallback_msg = Message()
                for seg in msg:
                    if seg.type == 'text':
                        fallback_msg += seg
                    else:
                        # 先发送之前攒着的文字（带折叠判定）
                        try: await safe_send_text(fallback_msg)
                        except Exception: pass
                        fallback_msg = Message()
                        
                        # 单发媒体
                        try: await zhihu_matcher.send(Message(seg))
                        except Exception as sub_e: 
                            logger.error(f"单发媒体失败（严重风控死锁）: {sub_e}")
                            await zhihu_matcher.send("🚫 [一张图片/视频因触发腾讯极其严格的审核被无情抹杀]")
                            
                # 收尾剩余文字
                try: await safe_send_text(fallback_msg)
                except Exception: pass

        # 遍历正文内容
        if hasattr(parse_res, "contents") and parse_res.contents:
            for content in parse_res.contents:
                # 1. 处理文字
                if hasattr(content, "text") and content.text:
                    current_combined_msg += MessageSegment.text(str(content.text) + "\n")
                
                # 2. 处理图片/媒体（增加精准异常捕获）
                if hasattr(content, "get_path"):
                    try:
                        file_path = await content.get_path()
                        if file_path and getattr(file_path, "exists", lambda: False)():
                            # 换完后上一行是：
                            ext = file_path.suffix.lower()
                            if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                                current_combined_msg += MessageSegment.image(file_path)
                                current_combined_msg += MessageSegment.text("\n")
                                img_counter += 1
                                total_img_counter += 1  # [新增] 累加总数
                            elif ext in [".mp4", ".mov", ".avi"]:
                                current_combined_msg += MessageSegment.video(file_path)
                                current_combined_msg += MessageSegment.text("\n")
                                img_counter += 2 
                                total_img_counter += 2  # [新增] 累加总数
                    except RuntimeError:
                        pass
                    except Exception as e:
                        logger.debug(f"读取媒体失败: {e}")

                # [修改] 达到单批次阈值 或 达到总量限制 时触发发包
                if img_counter >= ZHIHU_BATCH_SIZE or total_img_counter >= ZHIHU_MAX_IMAGES:
                    await send_current_batch(current_combined_msg)
                    current_combined_msg = Message() 
                    img_counter = 0
                    batch_num += 1
                    
                # [新增] 总量阻断：超过设定总量直接掐断循环
                if total_img_counter >= ZHIHU_MAX_IMAGES:
                    limit_reached = True
                    break

        # [修改] 如果没有被提前阻断，才发送收尾段落
        if current_combined_msg and not limit_reached:
            await send_current_batch(current_combined_msg, is_last=True)
            
        # [新增] 触发了截断则发送文案提示
        if limit_reached:
            await zhihu_matcher.send("不刷屏了，剩下的请点击原链接跳转阅读")

    except Exception as e:
        logger.exception("解析知乎链接时发生严重错误")