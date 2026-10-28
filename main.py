"""AstrBot 插件：让角色刷 B 站，把它自己觉得有意思的视频分享给你。

和"定时转发热榜"的区别在于：每条视频都会先由角色本人（LLM）按口味打一次分，
并写出想分享的理由；分数不够就不推。宁缺毋滥。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

try:
    from astrbot.api.event import MessageChain
except ImportError:  # 兼容旧版导出路径
    from astrbot.core.message.message_chain import MessageChain

from .bili_client import RID_NAMES, BiliClient

TOOL_SHARE = "share_video"

DEFAULT_BLACKLIST = "时政,政治,军事,战争,恐怖,凶杀,案件,医疗,减肥药,带货,广告,擦边"

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(text: str) -> Any:
    """从可能带解释、带代码块的回复里抠出 JSON。"""
    if not text:
        return None
    m = _JSON_BLOCK.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:  # noqa: BLE001
                continue
    return None


def _fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "未知"
    m, s = divmod(seconds, 60)
    return f"{m}分{s}秒" if m else f"{s}秒"


class BiliSharePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        def cfg(key: str, default):
            value = self.config.get(key, default)
            return default if value is None else value

        self.enable_proactive: bool = bool(cfg("enable_proactive", True))
        self.daily_limit: int = int(cfg("daily_limit", 2) or 2)
        self.min_interval_hours: float = float(cfg("min_interval_hours", 4) or 4)
        self.quiet_hours: str = str(cfg("quiet_hours", "0-8") or "")
        self.score_threshold: int = int(cfg("score_threshold", 7) or 7)
        self.candidate_count: int = int(cfg("candidate_count", 8) or 8)
        self.min_duration: int = int(cfg("min_duration", 20) or 0)
        self.max_duration: int = int(cfg("max_duration", 600) or 0)
        self.check_interval: int = int(cfg("check_interval_minutes", 20) or 20)
        self.taste_hint: str = str(cfg("taste_hint", "") or "").strip()
        self.enable_tool: bool = bool(cfg("enable_tool", True))
        self.inject_prompt: bool = bool(cfg("inject_prompt", True))
        self.active_hours: float = float(cfg("active_hours", 24) or 24)

        self.blacklist = [
            w.strip()
            for w in str(cfg("blacklist", DEFAULT_BLACKLIST) or "").replace("，", ",").split(",")
            if w.strip()
        ]
        rid_raw = str(cfg("rids", "") or "").replace("，", ",")
        self.rids: list[int] = []
        for part in rid_raw.split(","):
            part = part.strip()
            if part.isdigit():
                self.rids.append(int(part))

        self.client = BiliClient(timeout=int(cfg("timeout", 20) or 20))

        try:
            data_dir = Path(StarTools.get_data_dir())
        except Exception:  # noqa: BLE001
            data_dir = Path("data/plugin_data/astrbot_plugin_bili_share")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.state_path = data_dir / "state.json"

        self.state: dict[str, Any] = self._load_state()
        self._persona_cache: dict[str, str] = {}
        self._task: asyncio.Task | None = None

        logger.info(
            f"[BiliShare] 已加载：主动={'开' if self.enable_proactive else '关'} "
            f"每日上限={self.daily_limit} 间隔={self.min_interval_hours}h "
            f"阈值={self.score_threshold} 分区={self.rids or '全站'}",
        )

    # ---------------- 状态 ----------------

    def _load_state(self) -> dict[str, Any]:
        default = {"sessions": {}, "pushed": [], "taste": {}}
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in default.items():
                        data.setdefault(k, v)
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BiliShare] 读取状态失败，使用空状态: {exc}")
        return default

    def _save_state(self) -> None:
        try:
            # 推送历史只保留最近 800 条，防止文件无限增长
            self.state["pushed"] = self.state["pushed"][-800:]
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BiliShare] 保存状态失败: {exc}")

    def _session(self, umo: str) -> dict[str, Any]:
        return self.state["sessions"].setdefault(
            umo,
            {"enabled": True, "last_active": 0, "last_push": 0, "push_date": "", "push_count": 0},
        )

    def _umo(self, event: AstrMessageEvent) -> str:
        try:
            umo = getattr(event, "unified_msg_origin", "")
            if umo:
                return str(umo)
        except Exception:  # noqa: BLE001
            pass
        try:
            return str(event.get_session_id())
        except Exception:  # noqa: BLE001
            return "unknown"

    def _touch(self, umo: str) -> None:
        """记录会话活跃时间。主动推送只发给最近还在聊的会话，避免打扰冷掉的群。"""
        try:
            sess = self._session(umo)
            now = time.time()
            # 一分钟内不重复写盘，避免每条消息都触发一次文件写入
            if now - float(sess.get("last_active", 0)) < 60:
                return
            sess["last_active"] = now
            self._save_state()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        if self.enable_proactive:
            self._task = asyncio.create_task(self._loop())

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        await self.client.close()

    async def _loop(self) -> None:
        # 启动后先等一会儿，别和 AstrBot 自身启动抢资源
        await asyncio.sleep(90)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[BiliShare] 定时检查出错: {exc}")
            await asyncio.sleep(max(5, self.check_interval) * 60)

    # ---------------- 时间与过滤 ----------------

    def _in_quiet_hours(self) -> bool:
        raw = (self.quiet_hours or "").strip()
        if not raw or "-" not in raw:
            return False
        try:
            a, b = raw.split("-", 1)
            start, end = int(a), int(b)
        except ValueError:
            return False
        if start == end:
            return False
        hour = datetime.now().hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _hard_filter(self, videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pushed = set(self.state.get("pushed", []))
        out = []
        for v in videos:
            if v["bvid"] in pushed:
                continue
            d = v["duration"]
            if self.min_duration and d and d < self.min_duration:
                continue
            if self.max_duration and d and d > self.max_duration:
                continue
            text = f"{v['title']} {v['desc']} {v['tname']}"
            if any(word and word in text for word in self.blacklist):
                continue
            if v["view"] and v["view"] < 10000:
                continue
            out.append(v)
        return out

    # ---------------- 口味 ----------------

    async def _provider(self):
        try:
            return self.context.get_using_provider()
        except Exception:  # noqa: BLE001
            return None

    def _persona_text(self, umo: str) -> str:
        if self._persona_cache.get(umo):
            return self._persona_cache[umo]
        try:
            conf = self.context.get_config()
        except Exception:  # noqa: BLE001
            return ""
        if not conf:
            return ""
        for key in ("persona", "personas"):
            try:
                items = conf.get(key) or []
            except Exception:  # noqa: BLE001
                continue
            if isinstance(items, str):
                return items.strip()
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    for f in ("system_prompt", "prompt", "content", "text"):
                        val = item.get(f)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
                elif isinstance(item, str) and item.strip():
                    return item.strip()
        return ""

    async def _ensure_taste(self, umo: str) -> dict[str, Any]:
        taste = self.state.get("taste") or {}
        if taste.get("watch"):
            return taste

        provider = await self._provider()
        persona = self._persona_text(umo)
        if not provider:
            taste = {
                "watch": ["搞笑", "生活", "动物"],
                "avoid": ["时政", "恐怖"],
                "tone": "喜欢轻松有梗的短视频",
            }
            self.state["taste"] = taste
            self._save_state()
            return taste

        prompt = (
            "下面是一个角色的设定。请推断她/他刷 B 站时会喜欢什么、会避开什么。\n\n"
            f"角色设定：\n{persona[:1500] if persona else '（未提供，按一个普通年轻人推断）'}\n\n"
            "只输出 JSON，不要解释：\n"
            '{"watch": ["最多6个中文兴趣标签"], "avoid": ["最多4个不看的标签"], '
            '"tone": "一句话描述这个人的观看口味，20字以内"}'
        )
        if self.taste_hint:
            prompt += f"\n补充要求：{self.taste_hint}"

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt="你擅长根据人物设定推断内容偏好。只输出 JSON。",
            )
            data = _extract_json((getattr(resp, "completion_text", "") or "").strip())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BiliShare] 生成口味失败: {exc}")
            data = None

        if not isinstance(data, dict) or not data.get("watch"):
            data = {
                "watch": ["搞笑", "生活", "动物"],
                "avoid": ["时政", "恐怖"],
                "tone": "喜欢轻松有梗的短视频",
            }
        data.setdefault("avoid", [])
        data.setdefault("tone", "")
        self.state["taste"] = data
        self._save_state()
        logger.info(f"[BiliShare] 口味已生成：{data.get('watch')} / {data.get('tone')}")
        return data

    # ---------------- 评分 ----------------

    async def _score(
        self,
        videos: list[dict[str, Any]],
        taste: dict[str, Any],
        persona: str,
    ) -> list[dict[str, Any]]:
        """让角色给候选打分，并给最高分写一句分享语。"""
        provider = await self._provider()
        if not provider or not videos:
            return []

        lines = []
        for i, v in enumerate(videos):
            lines.append(
                f"{i}. 标题：{v['title'][:60]}｜UP：{v['up'][:20]}｜"
                f"时长：{_fmt_duration(v['duration'])}｜分区：{v['tname']}｜"
                f"播放：{v['view']}｜简介：{v['desc'][:60]}"
            )

        prompt = (
            "你就是下面这个角色本人，正在刷 B 站。\n\n"
            f"你的人设：{persona[:1200] if persona else '（普通年轻人）'}\n"
            f"你的口味：{'、'.join(taste.get('watch') or [])}\n"
            f"你不感兴趣的：{'、'.join(taste.get('avoid') or [])}\n\n"
            "下面是几条候选视频：\n" + "\n".join(lines) + "\n\n"
            "请对每条打 0-10 分（你自己会不会点开看），并给分数最高的那条写一句分享语。\n"
            "分享语要求：第一人称、口语、贴合人设、25 字以内、像随手发给朋友的一句话，"
            "不要用「推荐」「值得一看」这类书面词，不要复述标题，不要剧透内容。\n\n"
            '只输出 JSON 数组，不要解释：[{"i":0,"score":8,"line":"分享语"}]'
        )

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt="你是一个有自己品味的普通年轻人，说话口语化。只输出 JSON。",
            )
            data = _extract_json((getattr(resp, "completion_text", "") or "").strip())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BiliShare] 评分失败: {exc}")
            return []

        if not isinstance(data, list):
            return []

        scored: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("i"))
                score = float(item.get("score") or 0)
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(videos):
                continue
            scored.append(
                {
                    "video": videos[idx],
                    "score": score,
                    "line": str(item.get("line") or "").strip()[:80],
                }
            )
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored

    # ---------------- 推送 ----------------

    async def _send_video(self, umo: str, video: dict[str, Any], line: str) -> bool:
        text = f"{line}\n{video['url']}" if line else video["url"]
        chain = []
        if video.get("pic"):
            try:
                chain.append(Image.fromURL(video["pic"]))
            except Exception:  # noqa: BLE001
                chain = []
        chain.append(Plain(text))

        try:
            ok = await self.context.send_message(umo, MessageChain(chain))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BiliShare] 发送失败，降级纯文字: {exc}")
            try:
                ok = await self.context.send_message(umo, MessageChain([Plain(text)]))
            except Exception as exc2:  # noqa: BLE001
                logger.error(f"[BiliShare] 纯文字也发不出去: {exc2}")
                return False
        return bool(ok)

    async def _pick(self, umo: str, count: int | None = None) -> tuple[dict[str, Any], str] | None:
        """抓一轮候选、过滤、打分，返回最值得推的一条。"""
        videos = await self.client.collect(self.rids, limit=60)
        if not videos:
            logger.info("[BiliShare] 本轮没抓到候选")
            return None

        videos = self._hard_filter(videos)
        if not videos:
            logger.info("[BiliShare] 硬筛后没有剩余候选")
            return None

        random.shuffle(videos)
        taste = await self._ensure_taste(umo)
        # 先按兴趣标签做一次粗排，减少送进模型的条数
        watch = [w for w in (taste.get("watch") or []) if w]
        if watch:
            videos.sort(key=lambda v: 0 if any(w in f"{v['title']}{v['tname']}{v['desc']}" for w in watch) else 1)

        n = count or self.candidate_count
        candidates = videos[: max(1, n)]
        persona = self._persona_text(umo)
        scored = await self._score(candidates, taste, persona)

        if not scored:
            # 模型没给出结果时，退化成"随机挑一条"，但仍然推
            v = candidates[0]
            return v, ""

        best = scored[0]
        if best["score"] < self.score_threshold:
            logger.info(f"[BiliShare] 最高分 {best['score']} 未达阈值 {self.score_threshold}，本轮不推")
            return None
        return best["video"], best["line"]

    def _count_today(self, sess: dict[str, Any]) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        return sess.get("push_count", 0) if sess.get("push_date") == today else 0

    def _mark_pushed(self, umo: str, bvid: str) -> None:
        sess = self._session(umo)
        today = datetime.now().strftime("%Y-%m-%d")
        sess["last_push"] = time.time()
        sess["push_date"] = today
        sess["push_count"] = self._count_today(sess) + 1
        self.state["pushed"].append(bvid)
        self._save_state()

    async def _tick(self) -> None:
        if not self.enable_proactive:
            return
        if self._in_quiet_hours():
            return

        now = time.time()
        targets = []
        for umo, sess in list(self.state.get("sessions", {}).items()):
            if not sess.get("enabled", True):
                continue
            if now - float(sess.get("last_active", 0)) > self.active_hours * 3600:
                continue
            if now - float(sess.get("last_push", 0)) < self.min_interval_hours * 3600:
                continue
            if self._count_today(sess) >= self.daily_limit:
                continue
            targets.append(umo)

        if not targets:
            return

        # 一次只给一个会话推，避免同时刷屏
        umo = random.choice(targets)
        picked = await self._pick(umo)
        if not picked:
            return
        video, line = picked
        if await self._send_video(umo, video, line):
            self._mark_pushed(umo, video["bvid"])
            logger.info(f"[BiliShare] 已推送给 {umo}：{video['title'][:30]}")

    # ---------------- 钩子 ----------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        umo = self._umo(event)
        # 记录活跃时间：主动推送只发给最近说过话的会话
        self._touch(umo)
        # 缓存人格，供生成"口味"用
        try:
            sp = getattr(req, "system_prompt", "") or ""
            if sp:
                self._persona_cache[umo] = sp[:2000]
        except Exception:  # noqa: BLE001
            pass

        if not (self.enable_tool and self.inject_prompt):
            return
        hint = (
            f"你平时会刷 B 站。当用户说「有什么好玩的」「给我看点东西」「你最近在看什么」"
            f"「无聊」这类话时，调用 `{TOOL_SHARE}` 把你觉得有意思的视频分享给他。"
            "分享时用你自己的口吻说一句，不要用「推荐」这种书面词。"
            "用户没提这茬时不要主动调用。"
        )
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=hint)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            req.extra_user_content_parts.append(part)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            req.system_prompt = f"{req.system_prompt or ''}\n{hint}"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[BiliShare] 注入提示失败: {exc}")

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool(TOOL_SHARE)
    async def share_video(self, event: AstrMessageEvent):
        """从 B 站挑一个你自己觉得有意思的短视频分享给用户。

        当用户说「有什么好玩的」「给我看点东西」「你最近在看什么」「好无聊」时调用。
        用户没有主动要求时不要调用。
        """
        umo = self._umo(event)
        picked = await self._pick(umo)
        if not picked:
            yield "这次没刷到什么值得分享的，换个时间再说吧。"
            return
        video, line = picked
        ok = await self._send_video(umo, video, line)
        if not ok:
            yield f"视频找到了但没发出去，你可以自己看：{video['url']}"
            return
        self._mark_pushed(umo, video["bvid"])
        yield "视频已经发出去了。不要再用文字重复这个链接或标题，接着聊就行。"

    # ---------------- 命令 ----------------

    @filter.command("推一个")
    async def cmd_push_now(self, event: AstrMessageEvent):
        """立刻推一条：/推一个"""
        umo = self._umo(event)
        self._touch(umo)
        yield event.plain_result("我去刷一会儿…")
        picked = await self._pick(umo)
        if not picked:
            yield event.plain_result("这一轮没刷到想分享的，等会儿再试。")
            return
        video, line = picked
        ok = await self._send_video(umo, video, line)
        if ok:
            self._mark_pushed(umo, video["bvid"])
        else:
            yield event.plain_result("视频找到了但没发出去。")

    @filter.command("不推了")
    async def cmd_off(self, event: AstrMessageEvent):
        """关闭本会话的主动推送"""
        umo = self._umo(event)
        self._touch(umo)
        sess = self._session(umo)
        sess["enabled"] = False
        self._save_state()
        yield event.plain_result("好，不主动推了。想看了发 /推一个。")

    @filter.command("推送开")
    async def cmd_on(self, event: AstrMessageEvent):
        """重新开启本会话的主动推送"""
        umo = self._umo(event)
        self._touch(umo)
        sess = self._session(umo)
        sess["enabled"] = True
        self._save_state()
        yield event.plain_result(
            f"好，以后我刷到有意思的就发给你（每天最多 {self.daily_limit} 条）。",
        )

    @filter.command("口味")
    async def cmd_taste(self, event: AstrMessageEvent):
        """查看当前的口味档案"""
        umo = self._umo(event)
        self._touch(umo)
        taste = await self._ensure_taste(umo)
        lines = [
            "我现在的口味：",
            f"· 爱看：{'、'.join(taste.get('watch') or []) or '（空）'}",
            f"· 不爱看：{'、'.join(taste.get('avoid') or []) or '（空）'}",
            f"· 概括：{taste.get('tone') or '（空）'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("口味重写")
    async def cmd_taste_reset(self, event: AstrMessageEvent):
        """清空口味档案，按当前人设重新推断"""
        umo = self._umo(event)
        self._touch(umo)
        self.state["taste"] = {}
        self._save_state()
        yield event.plain_result("正在重新想我到底爱看什么…")
        taste = await self._ensure_taste(umo)
        yield event.plain_result(
            f"好了。爱看：{'、'.join(taste.get('watch') or [])}\n{taste.get('tone') or ''}",
        )
