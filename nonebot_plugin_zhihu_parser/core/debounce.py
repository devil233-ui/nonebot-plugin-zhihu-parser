import time

class Debouncer:
    def __init__(self, ttl_seconds: int = 86400):
        """防抖时间 24 小时"""
        self.ttl = ttl_seconds
        self._url_cache: dict[str, float] = {}
        self._resource_cache: dict[str, float] = {}

    def hit_url(self, session_id: str, url: str) -> bool:
        return self._check_and_set(self._url_cache, f"{session_id}:{url}")

    def hit_resource(self, session_id: str, resource_id: str) -> bool:
        if not resource_id:
            return False
        return self._check_and_set(self._resource_cache, f"{session_id}:{resource_id}")

    def _check_and_set(self, cache: dict[str, float], key: str) -> bool:
        now = time.time()
        if key in cache and now - cache[key] < self.ttl:
            return True
        cache[key] = now
        
        # 简单阈值清理，防止内存泄漏 (防炸内存)
        if len(cache) > 500:
            for k in list(cache.keys()):
                if now - cache[k] >= self.ttl:
                    del cache[k]
        return False