"""Redis fixed-window limits and expiring, token-owned coordination leases."""

import asyncio
import os
import secrets
from contextlib import asynccontextmanager, suppress

from fastapi import HTTPException
from redis import Redis
from redis.exceptions import RedisError

RELEASE = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
RENEW = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('expire',KEYS[1],ARGV[2]) else return 0 end"
RATE = "local n=redis.call('incr',KEYS[1]); if n==1 then redis.call('expire',KEYS[1],ARGV[1]) end; return n"


class Lease:
    def __init__(self, gate, token):
        self.gate, self.token = gate, token
        self.lost = False

    async def ensure_owned(self):
        value = await self.gate.call(self.gate.client.get, self.gate.key)
        if self.lost or value != self.token:
            raise HTTPException(503, '问答协调租约已失效，请重试；本轮未保存。')


class RedisControl:
    def __init__(self, url=None, prefix=None):
        url = url or os.getenv('REDIS_URL', '')
        if not url.startswith(('redis://', 'rediss://')):
            raise ValueError('请设置REDIS_URL。')
        self.client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
        self.prefix = prefix or os.getenv('WANG_REDIS_PREFIX', 'wang')
        self.key = self.prefix + ':chat-lease'
        self.ttl = 120

    async def call(self, function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except RedisError:
            raise HTTPException(503, 'Redis服务不可用，请稍后重试。') from None

    async def busy(self):
        return bool(await self.call(self.client.exists, self.key))

    async def allow(self, identity):
        limit = int(os.getenv('WANG_CHAT_RATE_LIMIT', '60'))
        window = int(os.getenv('WANG_CHAT_RATE_WINDOW', '60'))
        if not 1 <= limit <= 10000 or not 1 <= window <= 3600:
            raise HTTPException(503, '限流配置无效。')
        count = await self.call(self.client.eval, RATE, 1, self.prefix + ':rate:' + identity, window)
        if count > limit:
            raise HTTPException(429, '请求过于频繁，请稍后重试。', headers={'Retry-After': str(window)})

    @asynccontextmanager
    async def hold(self):
        token = secrets.token_urlsafe(32)
        acquired = await self.call(self.client.set, self.key, token, nx=True, ex=self.ttl)
        if not acquired:
            raise HTTPException(429, '正在生成回答，请稍后重试。')
        lease, stop = Lease(self, token), asyncio.Event()

        async def renew():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.ttl / 3)
                except TimeoutError:
                    try:
                        if not await self.call(self.client.eval, RENEW, 1, self.key, token, self.ttl):
                            lease.lost = True
                            return
                    except HTTPException:
                        lease.lost = True
                        return
        task = asyncio.create_task(renew())
        try:
            yield lease
        finally:
            stop.set()
            await task
            with suppress(HTTPException):
                await self.call(self.client.eval, RELEASE, 1, self.key, token)

    def close(self):
        self.client.close()
