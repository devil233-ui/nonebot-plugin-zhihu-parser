import re
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Message, MessageSegment, MessageEvent, Bot, GroupMessageEvent, PrivateMessageEvent
from nonebot.log import logger
from nonebot import get_driver
from nonebot.rule import Rule
from nonebot.typing import T_State

from .core.config import PluginConfig
from .core.parsers.zhihu.parser import ZhihuParser
from .core.download import Downloader
from .core.render import Renderer
from .core.debounce import Debouncer

cfg = PluginConfig()
zhihu_parser = None
zhihu_renderer = None
zhihu_debouncer = Debouncer()

@get_driver().on_startup
async def init_parser():
    global zhihu_parser, zhihu_renderer
    downloader = Downloader(cfg)
    zhihu_parser = ZhihuParser(cfg, downloader)
    Renderer.load_resources()
    zhihu_renderer = Renderer(cfg)

# ---------------------------------------------------------
# 自定义规则：暴力破解 QQ 小程序和富文本卡片中的 JSON 隐藏链接
# ---------------------------------------------------------
def check_zhihu_url():
    async def _check(bot: Bot, event: MessageEvent, state: T_State) -> bool:
        # 获取带 CQ 码的原始消息，并处理 JSON 里的转义斜杠 (比如 https:\/\/www.zhihu.com)
        raw_msg = str(event.get_message()).replace("\\/", "/")
        
        # 使用宽泛的正则在原始字符串里先捞出链接
        pat = re.compile(r"(https?://(?:www\.)?zhihu\.com/[^\s\"\'\\]+|https?://zhuanlan\.zhihu\.com/[^\s\"\'\\]+)")
        match = pat.search(raw_msg)
        if match:
            state['zhihu_match'] = match
            state['zhihu_raw'] = raw_msg
            return True
        return False
    return Rule(_check)

# 改用 on_message 配合自定义 Rule，完美绕过 plaintext 的过滤
zhihu_matcher = on_message(rule=check_zhihu_url(), priority=10, block=False)

@zhihu_matcher.handle()
async def handle_zhihu(bot: Bot, event: MessageEvent, state: T_State):
    if not zhihu_parser or not zhihu_renderer:
        logger.warning("知乎解析器仍在初始化中，请稍后再试")
        return
        
    text = state['zhihu_raw']
    base_match = state['zhihu_match']
    # 将 QQ 富文本中被转义的 &amp; 还原为 &，否则请求和正则都会出大问题
    url = base_match.group(0).replace("&amp;", "&")
    text = text.replace("&amp;", "&")
    session_id = event.get_session_id()
    
    # ==========================================
    # 防抖拦截：终于能正常工作了！
    # ==========================================
    if zhihu_debouncer.hit_url(session_id, url):
        logger.info(f"[{session_id}] 链接 {url} 在防抖时间内，已跳过重复解析")
        await zhihu_matcher.send("该知乎链接一小时内有人水果了，跳过解析。")
        return
        
    final_keyword = None
    final_match_obj = None
    
    handlers = getattr(zhihu_parser, "_handlers", {})
    if not handlers:
        logger.error("ZhihuParser 未能加载任何处理函数，请检查核心代码。")
        return

    # 1. 尝试从注册的函数上动态提取正则
    for kw, func in handlers.items():
        pat = getattr(func, "__parser_pattern__", None)
        if pat:
            if isinstance(pat, str):
                pat = re.compile(pat)
            match = pat.search(text)
            if match:
                final_keyword = kw
                final_match_obj = match
                break

    # 2. 尝试备用硬编码正则
    if not final_match_obj:
        # 左侧的 key 必须与 handlers.py 中 @handle 注册的字符串一字不差！
        KNOWN_PATTERNS = [
            ("/answer/", re.compile(r"zhihu\.com/question/(?P<question_id>\d+)/answer/(?P<answer_id>\d+)")),
            ("www.zhihu.com/question/", re.compile(r"zhihu\.com/question/(?P<question_id>\d+)")),
            ("zhuanlan.zhihu.com/p/", re.compile(r"zhuanlan\.zhihu\.com/p/(?P<article_id>\d+)")),
            ("www.zhihu.com/pin/", re.compile(r"zhihu\.com/pin/(?P<pin_id>\d+)")),
        ]
        for exact_kw, pat in KNOWN_PATTERNS:
            match = pat.search(text)
            # 如果匹配成功且该路由钥匙确实存在于处理器中
            if match and exact_kw in handlers:
                final_keyword = exact_kw
                final_match_obj = match
                break
                
    if not final_keyword or not final_match_obj:
        logger.warning(f"提取到了链接 {url}，但未匹配到支持的具体知乎格式")
        return

    logger.info(f"使用专业正则钥匙 '{final_keyword}' 匹配成功，开始提取数据...")

    try:
        parse_res = await zhihu_parser.parse(final_keyword, final_match_obj)
        
        if not parse_res:
            logger.warning("解析返回为空，可能是知乎风控或该内容需要登录")
            return
            
        logger.info("数据提取完毕，开始渲染图片...")
        card_path = await zhihu_renderer.render_card(parse_res)
        
        if card_path and card_path.exists():
            await zhihu_matcher.send(MessageSegment.image(card_path))
        else:
            await zhihu_matcher.send("渲染卡片失败，下方为你尝试直接发送内容")

        # ==========================================
        # 步骤 B：打包所有长文本和原图构建合并转发（图文混排版）
        # ==========================================
        nodes = []
        bot_id = int(bot.self_id)
        
        def add_node(msg_content):
            nodes.append(MessageSegment.node_custom(user_id=bot_id, nickname="知乎解析", content=Message(msg_content)))
            
        # 我们把所有内容拼装到同一个 Message 对象里，实现真正的“图文混排”
        combined_msg = Message()
        
        if getattr(parse_res, "title", None):
            combined_msg += MessageSegment.text(f"【{parse_res.title}】\n\n")
            
        has_content = False
        if hasattr(parse_res, "contents") and parse_res.contents:
            for content in parse_res.contents:
                try:
                    # 拼接文字
                    if hasattr(content, "text") and content.text:
                        combined_msg += MessageSegment.text(str(content.text) + "\n")
                        has_content = True
                        
                    # 拼接图片或视频
                    if hasattr(content, "get_path"):
                        file_path = await content.get_path()
                        if file_path and hasattr(file_path, "exists") and file_path.exists():
                            ext = file_path.suffix.lower()
                            if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                                combined_msg += MessageSegment.image(file_path)
                                combined_msg += MessageSegment.text("\n") # 图片后加个换行防止文字挤在一起
                                has_content = True
                            elif ext in [".mp4", ".mov", ".avi"]:
                                combined_msg += MessageSegment.video(file_path)
                                combined_msg += MessageSegment.text("\n")
                                has_content = True
                except Exception as e:
                    logger.debug(f"组合图文消息失败: {e}")
                    
        # 兜底：如果没提取到富文本内容，就把纯文本摘要塞进去
        if not has_content and getattr(parse_res, "text", None):
            combined_msg += MessageSegment.text(str(parse_res.text))

        # 将这条组装好的超长图文混排消息作为一个整体节点插入
        if combined_msg:
            add_node(combined_msg)

        # 执行 OneBot 合并转发发送请求

        if nodes:
            try:
                if isinstance(event, GroupMessageEvent):
                    await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
                else:
                    await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes)
            except Exception as e:
                logger.error(f"发送合并转发失败: {e}")
                await zhihu_matcher.send("发送长文合并转发失败，这通常是因为内容被腾讯风控或字数过多。")
                
    except Exception as e:
        logger.exception("解析知乎链接时发生严重错误，完整堆栈如下：")