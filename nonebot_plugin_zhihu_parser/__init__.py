import re
from nonebot import on_regex
from nonebot.adapters.onebot.v11 import Message, MessageSegment, MessageEvent
from nonebot.log import logger

from .core.config import PluginConfig
from .core.parsers.zhihu.parser import ZhihuParser

# 初始化配置（它会自动在你的 nonebot 运行目录下创建 data/nonebot_plugin_zhihu/temp）
cfg = PluginConfig()
zhihu_parser = ZhihuParser(cfg)

# 匹配知乎链接
zhihu_matcher = on_regex(
    r"(https?://(?:www\.)?zhihu\.com/(?:question/\d+(?:/answer/\d+)?|p/\d+|video/\d+|zvideo/\d+)|https?://zhuanlan\.zhihu\.com/p/\d+)", 
    priority=10, 
    block=False
)

@zhihu_matcher.handle()
async def handle_zhihu(event: MessageEvent):
    text = event.get_plaintext()
    match = re.search(r"(https?://[^\s]+zhihu\.com[^\s]+|https?://zhuanlan\.zhihu\.com[^\s]+)", text)
    if not match:
        return
        
    url = match.group(0)
    logger.info(f"检测到知乎链接: {url}")
    
    try:
        # 调用原始解析逻辑 (AstrBot 返回的是 ParseResult 对象)
        parse_res = await zhihu_parser.parse("zhihu", url)
        
        if not parse_res:
            logger.warning("解析返回为空")
            return
            
        reply_msg = Message()
        
        # 按照 AstrBot ParseResult 的结构拼装 OneBot 消息
        if parse_res.title:
            reply_msg += MessageSegment.text(f"【{parse_res.title}】\n")
            
        if parse_res.text:
            content = parse_res.text[:500] + ("..." if len(parse_res.text) > 500 else "")
            reply_msg += MessageSegment.text(f"{content}\n")
            
        if parse_res.images:
            for img_url in parse_res.images:
                reply_msg += MessageSegment.image(img_url)
                
        await zhihu_matcher.finish(reply_msg)

    except Exception as e:
        logger.error(f"解析失败: {e}")