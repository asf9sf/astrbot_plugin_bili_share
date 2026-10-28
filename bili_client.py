"""B 站视频抓取（只用公开接口，不需要登录）。

- 全站热门榜：/x/web-interface/ranking/v2?rid=0
- 分区榜：      /x/web-interface/ranking/v2?rid=<分区号>
- 综合热门兜底：/x/web-interface/popular

接口会变、会风控，所以所有失败都只记日志不抛，让上层跳过这一轮。
"""

from __future__ import annotations

from typing import Any

import aiohttp

from astrbot.api import logger

RANK_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# 常用分区 rid，配置里填数字即可
RID_NAMES = {
    1: "动画",
    3: "音乐",
    4: "游戏",
    5: "娱乐",
    36: "知识",
    119: "鬼畜",
    129: "舞蹈",
    155: "时尚",
    160: "生活",
    181: "影视",
    188: "科技",
    211: "美食",
    217: "动物",
    223: "汽车",
    234: "运动",
}


def _parse_duration(value: Any) -> int:
    """时长可能是秒数，也可能是 "12:34" / "1:02:03" 这种字符串。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        parts = value.strip().split(":")
        try:
            parts = [int(p) for p in parts]
        except ValueError:
            return 0
        seconds = 0
        for p in parts:
            seconds = seconds * 60 + p
        return seconds
    return 0


def _normalize(item: dict[str, Any]) -> dict[str, Any] | None:
    bvid = item.get("bvid") or item.get("aid")
    title = (item.get("title") or "").strip()
    if not bvid or not title:
        return None
    pic = item.get("pic") or ""
    if pic.startswith("//"):
        pic = "https:" + pic
    elif pic.startswith("http://"):
        pic = "https://" + pic[7:]
    owner = item.get("owner") or {}
    stat = item.get("stat") or {}
    return {
        "bvid": str(bvid),
        "title": title,
        "desc": (item.get("desc") or "").strip()[:200],
        "up": owner.get("name") or "",
        "view": int(stat.get("view") or 0),
        "like": int(stat.get("like") or 0),
        "duration": _parse_duration(item.get("duration")),
        "pic": pic,
        "tname": item.get("tname") or (item.get("rcmd_reason") or {}).get("content", ""),
        "url": f"https://www.bilibili.com/video/{bvid}",
    }


def _extract_list(body: dict[str, Any]) -> list[dict[str, Any]]:
    data = body.get("data") or {}
    for key in ("list", "result", "archives"):
        items = data.get(key)
        if isinstance(items, list):
            return items
    return []


class BiliClient:
    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers=HEADERS,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            session = self._ensure_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"[BiliShare] {url} 返回 HTTP {resp.status}")
                    return None
                return await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BiliShare] 请求 {url} 失败: {exc}")
            return None

    async def fetch_ranking(self, rid: int = 0) -> list[dict[str, Any]]:
        body = await self._get_json(RANK_URL, {"rid": rid, "type": "all"})
        if not body:
            return []
        if body.get("code") not in (0, None):
            logger.warning(f"[BiliShare] 排行榜 code={body.get('code')} {body.get('message')}")
            return []
        out = []
        for item in _extract_list(body):
            v = _normalize(item)
            if v:
                out.append(v)
        return out

    async def fetch_popular(self, ps: int = 50, pn: int = 1) -> list[dict[str, Any]]:
        body = await self._get_json(POPULAR_URL, {"ps": ps, "pn": pn})
        if not body or body.get("code") not in (0, None):
            return []
        out = []
        for item in _extract_list(body):
            v = _normalize(item)
            if v:
                out.append(v)
        return out

    async def collect(self, rids: list[int], limit: int = 60) -> list[dict[str, Any]]:
        """抓候选。

        实测 ranking/v2（排行榜）在无 cookie 环境下会被风控拦截返回 code -352，
        而 popular（综合热门）稳定可用，所以以 popular 为主、翻几页凑量；
        ranking 只在候选不足时补一次，失败就忽略。

        rids 不为空时，因为 popular 不支持按分区请求，改用分区名在本地过滤。
        """
        videos: list[dict[str, Any]] = []

        for pn in range(1, 4):
            batch = await self.fetch_popular(ps=50, pn=pn)
            if not batch:
                break
            videos.extend(batch)
            if len(videos) >= limit * 3:
                break

        if len(videos) < 20:
            videos.extend(await self.fetch_ranking(0))

        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for v in videos:
            if v["bvid"] in seen:
                continue
            seen.add(v["bvid"])
            out.append(v)

        if rids:
            names = [RID_NAMES[r] for r in rids if r in RID_NAMES]
            if names:
                filtered = [
                    v for v in out if any(n in (v.get("tname") or "") for n in names)
                ]
                # 分区匹配不到东西时退回全量，避免整轮没候选
                if filtered:
                    out = filtered

        return out[:limit]
