import time
import asyncio
from pathlib import Path
from nonebot.log import logger

class CacheCleaner:
    def __init__(self, temp_dir: str | Path, max_age_seconds: int = 86400):
        """默认清理 24小时 (86400秒) 前的缓存文件"""
        self.temp_dir = Path(temp_dir)
        self.max_age = max_age_seconds
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._running = False

    def start(self):
        if not self._running:
            self._running = True
            asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        while self._running:
            try:
                now = time.time()
                count = 0
                # 遍历目录清理过期文件
                for file_path in self.temp_dir.glob('*'):
                    if file_path.is_file() and now - file_path.stat().st_mtime > self.max_age:
                        file_path.unlink(missing_ok=True)
                        count += 1
                if count > 0:
                    logger.info(f"[知乎解析] 后台清理了 {count} 个过期缓存文件")
            except Exception as e:
                logger.error(f"[知乎解析] 缓存清理异常: {e}")
            
            # 每 12 小时执行一次检查
            await asyncio.sleep(43200)