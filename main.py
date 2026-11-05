"""Azure gacha: a collectible-card gacha that spends the check-in plugin's points.

Ported from a Koishi plugin. The card catalog and artwork come from Bilibili
"收藏集" (collectible card) assets; every draw is weighted by the official
circulation rates recorded in the catalog.

Points are *not* stored here. They are read from and written back to
``astrbot_plugin_azuresignin`` through AstrBot's shared preference store, so a
single balance is shared by check-in and gacha. Gacha state (owned cards and
the per-chat pool selection) lives in this plugin's own key-value storage.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from pathlib import Path

import jinja2

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Node, Nodes, Plain, Video
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import sp


class HtmlRenderer:
    """Render plugin HTML to a JPEG with a headless Chromium via Playwright.

    AstrBot's built-in t2i either calls a remote service or needs Pillow, and
    both make local artwork awkward. Driving Chromium in-process keeps rendering
    local, which also lets templates reference card files through ``file://``
    URLs instead of inlining megabytes of base64.
    """

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self._playwright = None
        self._browser = None
        self._context = None
        # 允许 2 个页面同时渲染；浏览器复用同一个 context 以保留 HTTP 缓存。
        self._lock = asyncio.Semaphore(2)

    async def _ensure_browser(self):
        """Start Chromium on first use and reuse it afterwards.

        Returns:
            The connected browser instance.

        Raises:
            RuntimeError: When playwright or its browser is not installed.
        """
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "缺少 playwright，请先执行：pip install playwright && playwright install chromium"
            ) from exc
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                # The flag lets canvas read pixels back from file:// images,
                # which the templates use to decide how thick the glass is.
                args=[
                    "--no-sandbox",
                    "--allow-file-access-from-files",
                    "--font-render-hinting=none",
                ],
            )
        except Exception as exc:
            await self._playwright.stop()
            self._playwright = None
            raise RuntimeError(
                "Chromium 未就绪，请执行：playwright install chromium"
            ) from exc
        return self._browser

    async def render(self, html: str, out_path: Path, quality: int = 92) -> Path | None:
        """Render ``html`` to ``out_path``.

        The page is measured after load so the screenshot hugs ``#root``, which
        keeps variable-height handbooks from carrying blank margins.

        Args:
            html: Fully rendered document.
            out_path: Destination JPEG.
            quality: JPEG quality.

        Returns:
            ``out_path`` when the file was written, else None.
        """
        source = self.work_dir / f"page_{uuid.uuid4().hex}.html"
        try:
            source.write_text(html, encoding="utf-8")
        except OSError as exc:
            logger.error(f"Failed to write render page: {exc}")
            return None

        async with self._lock:
            try:
                browser = await self._ensure_browser()
                if self._context is None:
                    self._context = await browser.new_context(
                        viewport={"width": 2200, "height": 1400},
                        device_scale_factor=1,
                    )
                context = self._context
            except RuntimeError:
                source.unlink(missing_ok=True)
                raise
            page = await context.new_page()
            try:
                await page.goto(source.as_uri(), wait_until="load")
                try:
                    await page.wait_for_function(
                        "window.__renderReady === true", timeout=12000
                    )
                except Exception:
                    logger.warning("Template did not report __renderReady; shooting as-is")
                try:
                    await page.evaluate("document.fonts && document.fonts.ready")
                except Exception:
                    pass
                await page.wait_for_timeout(80)

                box = await page.evaluate(
                    "() => { const r = document.getElementById('root').getBoundingClientRect();"
                    " return {w: Math.ceil(r.width), h: Math.ceil(r.height)}; }"
                )
                width, height = int(box["w"]), int(box["h"])
                if width <= 0 or height <= 0:
                    logger.error("Rendered page has an empty #root")
                    return None

                await page.set_viewport_size({"width": width, "height": height})
                await page.wait_for_timeout(60)
                await page.screenshot(
                    path=str(out_path),
                    type="jpeg",
                    quality=quality,
                    clip={"x": 0, "y": 0, "width": width, "height": height},
                )
            except Exception:
                logger.error("Playwright render failed", exc_info=True)
                return None
            finally:
                await page.close()
                source.unlink(missing_ok=True)
        return out_path if out_path.is_file() else None

    async def close(self) -> None:
        """Release the browser and the playwright driver."""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

PLUGIN_NAME = "astrbot_plugin_azuregacha"
TEMPLATE_DIR = Path(__file__).parent / "templates"
ASSETS_DIR = Path(__file__).parent / "assets"
DEFAULT_BACKGROUND_PATH = ASSETS_DIR / "default_bg.jpg"
LIQUID_GLASS_CDN = "https://cdn.jsdmirror.com/npm/@ybouane/liquidglass/dist/index.js"

#: Rarity corner badges, taken from the official collectible-card page.
TIER_TAG = {40: ASSETS_DIR / "tag_40.png", 30: ASSETS_DIR / "tag_30.png"}
#: Placeholder for the 镭射 (holo) variant; replace the file to swap the badge.
LASER_IMAGE = ASSETS_DIR / "laser.png"

#: Handbook canvas. Wide enough that a 6-column grid keeps the artwork sharp.
HANDBOOK_WIDTH = 1800
HANDBOOK_COLUMNS = 6
HANDBOOK_PAGE_CARDS = 36
#: Logical width of one handbook cell. Thumbnails are generated at twice this.
HANDBOOK_CELL_WIDTH = 272

#: LiquidGlass tuning, mirroring the check-in card so the two plugins match.
GLASS_OPTIONS = {
    "blurAmount": 0.72,
    "refraction": 0.42,
    "chromAberration": 0.05,
    "edgeHighlight": 0.09,
    "specular": 0.40,
    "fresnel": 0.95,
    "distortion": 0.02,
    "cornerRadius": 44,
    "zRadius": 36,
    "brightness": 0.16,
    "saturation": -0.10,
    "tintStrength": 0.18,
    "shadowOpacity": 0.20,
    "shadowSpread": 18,
    "shadowOffsetY": 6,
    "floating": False,
    "button": False,
    "bevelMode": 0,
    "opacity": 1.0,
}

#: Where the points come from. ``plugin_id`` is ``author/name`` in lower case.
SIGNIN_PLUGIN_ID = "azure/astrbot_plugin_azuresignin"

#: Points spent per single draw.
COST = 10
#: Hard cap for a single ``/抽卡 N`` command.
MAX_DRAW = 10

#: Pools that must never be drawn: a promo pool and the synthetic "all" pool.
EXCLUDE_POOLS = frozenset({"洛天依福利卡池", "全部奖池"})

#: Hidden tiers are additionally scaled down when drawing from "all pools",
#: because picking a pool first already flattens the official rate table.
HIDDEN_COMPRESS = 0.4

ACT_NAMES = {
    "171": "天依游学记系列",
    "282": "Vsinger集",
    "102550": "洛天依·收藏集",
    "106977": "乐正绫单人集",
    "108477": "洛天依·珍藏集",
    "112860": "vsinger",
    "113552": "洛天依·新像集",
    "113562": "言和·珍藏集",
}

TIER_NAME = {40: "大隐藏", 30: "小隐藏", 20: "稀有", 10: "普通"}
TIER_STAR = {40: "★★★★★", 30: "★★★★", 20: "★★★", 10: "★★"}

#: Tiers whose animated clip is sent as its own message after the draw.
VIDEO_TIERS = frozenset({30, 40})

#: A 镭射 (holo) copy can roll on normal cards of this act only.
LASER_ACT_ID = "113552"
LASER_TIERS = frozenset({10})
LASER_RATE = 0.15

ALL_POOLS = "全部卡池"


class AzureGachaPlugin(Star):
    """Collectible-card gacha backed by the check-in plugin's points."""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.cards_dir = self.data_dir / "cards"
        self.videos_dir = self.data_dir / "videos"
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.catalog: dict[str, dict] = self._load_catalog()
        self._pool_index: dict[str, list[tuple[str, dict]]] = {}
        self._build_pool_index()

        self._env = jinja2.Environment(autoescape=False)
        self.templates: dict[str, str] = {}
        for name in ("gacha_result.html", "handbook.html"):
            try:
                self.templates[name] = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
            except OSError as exc:
                self.templates[name] = ""
                logger.error(f"Failed to read gacha template {name}: {exc}")

        # Rendering happens in-process, so templates can point at local files
        # through file:// URLs instead of inlining base64 copies.
        self.bg_url = self._load_background()
        self.tag_urls = {
            tier: path.as_uri() for tier, path in TIER_TAG.items() if path.is_file()
        }
        self.laser_url = LASER_IMAGE.as_uri() if LASER_IMAGE.is_file() else ""
        self.renderer = HtmlRenderer(self.cache_dir)

    async def terminate(self) -> None:
        """Shut down the headless browser when the plugin unloads."""
        await self.renderer.close()

    def _load_background(self) -> str:
        """Resolve the handbook background.

        A user-supplied ``background.jpg``/``background.png`` in the plugin data
        directory wins over the bundled watercolour plate.

        Returns:
            A ``file://`` URL, or an empty string when no image is usable.
        """
        for candidate in (
            self.data_dir / "background.jpg",
            self.data_dir / "background.png",
            DEFAULT_BACKGROUND_PATH,
        ):
            if candidate.is_file():
                return candidate.as_uri()
        return ""

    # ------------------------------------------------------------------ 数据

    def _load_catalog(self) -> dict[str, dict]:
        """Load the card catalog, keeping only cards whose artwork is present.

        Cards whose image was never downloaded cannot be drawn or displayed, so
        they are dropped here instead of turning into blank tiles later.

        Returns:
            Mapping of card hash to card entry.
        """
        path = self.data_dir / "catalog.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error(f"Failed to load gacha catalog {path}: {exc}")
            return {}
        if not isinstance(raw, dict):
            return {}

        usable = {}
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            if not (self.cards_dir / f"{key}.jpg").is_file():
                continue
            usable[key] = entry
        logger.info(f"Gacha catalog: {len(usable)}/{len(raw)} cards with artwork")
        return usable

    def _build_pool_index(self) -> None:
        """Index cards by pool name, skipping excluded pools and zero rates."""
        index: dict[str, list[tuple[str, dict]]] = {}
        for key, entry in self.catalog.items():
            try:
                rate = float(entry.get("rate_pct") or 0)
            except (TypeError, ValueError):
                continue
            if rate <= 0:
                continue
            for pool in entry.get("pool_list") or []:
                if not pool or pool in EXCLUDE_POOLS:
                    continue
                index.setdefault(pool, []).append((key, entry))
        self._pool_index = index

    async def _load_state(self) -> dict:
        """Read gacha state: owned counts and per-chat pool selections.

        Returns:
            ``{"owned": {uid: {hash: count}}, "laser": {uid: {hash: count}},
            "pools": {chat_key: pool_name}}``.
        """
        data = await self.get_kv_data("state", {})
        if not isinstance(data, dict):
            data = {}
        owned = data.get("owned")
        laser = data.get("laser")
        pools = data.get("pools")
        return {
            "owned": owned if isinstance(owned, dict) else {},
            "laser": laser if isinstance(laser, dict) else {},
            "pools": pools if isinstance(pools, dict) else {},
        }

    async def _save_state(self, state: dict) -> None:
        await self.put_kv_data("state", state)

    # ------------------------------------------------------------- 积分桥

    async def _get_points(self, uid: str) -> int:
        """Read a user's points from the check-in plugin.

        Args:
            uid: Sender ID.

        Returns:
            Current balance; 0 when the record is missing or unusable.
        """
        data = await sp.get_async("plugin", SIGNIN_PLUGIN_ID, f"user_{uid}", {})
        if not isinstance(data, dict):
            return 0
        try:
            return int(data.get("points", 0) or 0)
        except (TypeError, ValueError):
            return 0

    async def _add_points(self, uid: str, delta: int) -> int:
        """Adjust a user's points in the check-in plugin's record.

        Args:
            uid: Sender ID.
            delta: Signed amount to add.

        Returns:
            Balance after the adjustment.
        """
        key = f"user_{uid}"
        data = await sp.get_async("plugin", SIGNIN_PLUGIN_ID, key, {})
        if not isinstance(data, dict):
            data = {}
        try:
            current = int(data.get("points", 0) or 0)
        except (TypeError, ValueError):
            current = 0
        data["points"] = max(0, current + delta)
        await sp.put_async("plugin", SIGNIN_PLUGIN_ID, key, data)
        return data["points"]

    # ------------------------------------------------------------------ 抽卡

    @staticmethod
    def _chat_key(event: AstrMessageEvent) -> str:
        """Build the key used for a chat-scoped pool selection."""
        return event.unified_msg_origin or "default"

    def _resolve_selection(self, selection: str) -> dict:
        """Turn a stored pool selection into a draw scope.

        Args:
            selection: Saved value, either ``全部卡池``, a series name, or a pool.

        Returns:
            ``{"mode": "all"|"series"|"single", "label": str, ...}`` where
            ``pools`` lists pool names and ``list`` holds cards.
        """
        index = self._pool_index
        if selection in ("", ALL_POOLS, "全部") or selection not in self._act_members() | set(index):
            return {"mode": "all", "label": ALL_POOLS, "pools": list(index)}

        # A series name expands to every pool belonging to that act_id.
        for act_id, name in ACT_NAMES.items():
            if name == selection:
                pools = [
                    pool
                    for pool, cards in index.items()
                    if str(cards[0][1].get("act_id")) == act_id
                ]
                if pools:
                    return {"mode": "series", "label": selection, "pools": pools}

        if selection in index:
            return {"mode": "single", "label": selection, "list": index[selection]}
        return {"mode": "all", "label": ALL_POOLS, "pools": list(index)}

    def _act_members(self) -> set[str]:
        return set(ACT_NAMES.values())

    @staticmethod
    def _pick_weighted(cards: list[tuple[str, dict]], weights: list[float]) -> tuple[str, dict]:
        """Pick one card proportionally to its weight."""
        total = sum(weights)
        if total <= 0:
            return random.choice(cards)
        roll = random.random() * total
        for card, weight in zip(cards, weights):
            roll -= weight
            if roll <= 0:
                return card
        return cards[-1]

    def _draw_one(self, scope: dict) -> tuple[str, dict]:
        """Draw a single card from the resolved scope.

        Args:
            scope: Result of :meth:`_resolve_selection`.

        Returns:
            ``(hash, card entry)``.
        """
        if scope["mode"] == "single":
            cards = scope["list"]
            return self._pick_weighted(
                cards, [float(entry.get("rate_pct") or 0) for _, entry in cards]
            )

        # Weight pools by how many cards they hold. A pool whose artwork is
        # mostly missing would otherwise collapse to one card and pay out that
        # single (often hidden) card every time it is picked.
        pool = random.choices(
            scope["pools"], weights=[len(self._pool_index[p]) for p in scope["pools"]]
        )[0]
        cards = self._pool_index[pool]
        weights = []
        for _, entry in cards:
            rate = float(entry.get("rate_pct") or 0)
            if int(entry.get("card_scarcity") or 0) in (20, 30, 40):
                rate *= HIDDEN_COMPRESS
            weights.append(rate)
        return self._pick_weighted(cards, weights)

    # ------------------------------------------------------------------ 渲染

    def _card_url(self, card_hash: str) -> str:
        """Return the ``file://`` URL of a card image.

        Rendering is local now, so the page can pull the original JPEG straight
        off disk and let Chromium scale it; no thumbnail cache is needed.

        Args:
            card_hash: Catalog key.

        Returns:
            A ``file://`` URL, or an empty string when the artwork is missing.
        """
        path = self.cards_dir / f"{card_hash}.jpg"
        return path.as_uri() if path.is_file() else ""

    def _roll_laser(self, entry: dict) -> bool:
        """Decide whether a drawn card is a 镭射 (holo) copy.

        Only normal-tier cards of the 新像集 act can roll it.

        Args:
            entry: Card catalog entry.

        Returns:
            True when this draw is a holo variant.
        """
        if str(entry.get("act_id")) != LASER_ACT_ID:
            return False
        if int(entry.get("card_scarcity") or 0) not in LASER_TIERS:
            return False
        return random.random() < LASER_RATE

    def _glass_json(self) -> str:
        """Build the LiquidGlass config injected into the handbook script.

        The template drops this into ``const cfg = ...`` verbatim, so it has to
        be real JSON: a Python dict literal would be invalid JavaScript and the
        whole module would fail to parse, silently disabling the glass effect.

        Returns:
            A JSON string.
        """
        payload = {
            "enabled": True,
            "hasImage": bool(self.bg_url),
            "cdn": LIQUID_GLASS_CDN,
            "options": GLASS_OPTIONS,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def _render(self, event: AstrMessageEvent, template: str, data: dict) -> str | None:
        """Render a template to an image file.

        Args:
            event: Triggering message event.
            template: Template file name.
            data: Template variables.

        Returns:
            Path to the rendered image, or None on failure.
        """
        source = self.templates.get(template)
        if not source:
            return None
        try:
            html = self._env.from_string(source).render(**data)
        except Exception:
            logger.error(f"Failed to render gacha template {template}", exc_info=True)
            return None
        target = self.cache_dir / f"{template.split('.')[0]}_{uuid.uuid4().hex}.jpg"
        try:
            rendered = await self.renderer.render(html, target)
        except RuntimeError as exc:
            logger.error(f"Renderer unavailable: {exc}")
            return None
        return str(rendered) if rendered else None

    # ------------------------------------------------------------------ 指令

    @filter.command("抽卡")
    async def draw(self, event: AstrMessageEvent):
        """Draw one to ten cards and spend points for them."""
        # The command filter leaves the command word in place, so strip through it.
        raw = re.sub(r"^.*?抽卡\s*", "", event.message_str or "").strip()
        if raw and not raw.isdigit():
            yield event.plain_result(self._help_text(event.is_admin()))
            return
        count = max(1, min(MAX_DRAW, int(raw) if raw else 1))

        uid = event.get_sender_id()
        need = count * COST
        points = await self._get_points(uid)
        if points < need:
            yield event.plain_result(
                f"💠 积分不足\n需要 {need} 分（{count} 抽 × {COST}），你现在 {points} 分\n\n"
                "发 /打卡 可以赚取积分"
            )
            return

        state = await self._load_state()
        scope = self._resolve_selection(state["pools"].get(self._chat_key(event), ALL_POOLS))
        available = len(scope.get("list", [])) if scope["mode"] == "single" else len(scope["pools"])
        if not available:
            yield event.plain_result("当前奖池没有可抽的卡，发 /换池 换一个")
            return

        left = await self._add_points(uid, -need)
        owned = state["owned"].setdefault(uid, {})
        laser_owned = state["laser"].setdefault(uid, {})
        drawn: list[tuple[str, dict, int]] = []
        for _ in range(count):
            card_hash, entry = self._draw_one(scope)
            owned[card_hash] = int(owned.get(card_hash, 0) or 0) + 1
            laser = self._roll_laser(entry)
            if laser:
                laser_owned[card_hash] = int(laser_owned.get(card_hash, 0) or 0) + 1
            drawn.append((card_hash, entry, owned[card_hash], laser))
        await self._save_state(state)

        rows = []
        for card_hash, entry, have, laser in drawn:
            tier = int(entry.get("card_scarcity") or 0)
            rows.append(
                {
                    "name": entry.get("card_name", ""),
                    "tier": TIER_NAME.get(tier, "普通"),
                    "tier_level": tier,
                    "star": TIER_STAR.get(tier, "★★"),
                    "rate": entry.get("rate_pct", 0),
                    "owned": have,
                    "laser": laser,
                    "tag": self.tag_urls.get(tier, ""),
                    "src": self._card_url(card_hash),
                }
            )

        result = await self._render(
            event,
            "gacha_result.html",
            {
                "rows": rows,
                "count": count,
                "cost": need,
                "left": left,
                "pool": scope["label"],
                "total": len(self.catalog),
                "laser_url": self.laser_url,
            },
        )
        if not result:
            names = "、".join(f"{r['name']}×{r['owned']}" for r in rows)
            yield event.plain_result(f"🎴 抽卡 ×{count}（-{need} 分，余 {left} 分）\n{names}")
            return

        yield event.image_result(result)

        # 原图打包成一条合并转发，刷屏友好；隐藏档再单独补发一段动图。
        nodes = []
        for card_hash, entry, have, laser in drawn:
            path = self.cards_dir / f"{card_hash}.jpg"
            if not path.is_file():
                continue
            tier = int(entry.get("card_scarcity") or 0)
            caption = f"{entry.get('card_name', '')} · {TIER_NAME.get(tier, '普通')}"
            if laser:
                caption += " · 镭射款"
            if have > 1:
                caption += f" · ×{have}"
            nodes.append(
                Node(
                    content=[Plain(caption), Image.fromFileSystem(str(path))],
                    name=event.get_sender_name() or "收藏集",
                    uin=str(event.get_sender_id()),
                )
            )
        if nodes:
            yield event.chain_result([Nodes(nodes=nodes)])

        # 小隐藏 / 大隐藏的动图单独发，视频不适合塞进合并转发。
        for card_hash, entry, _have, _laser in drawn:
            tier = int(entry.get("card_scarcity") or 0)
            if tier not in VIDEO_TIERS:
                continue
            video = self.videos_dir / f"{card_hash}.mp4"
            if video.is_file():
                yield event.chain_result(
                    [
                        Plain(f"🎬 {entry.get('card_name', '')} · {TIER_NAME.get(tier, '')}"),
                        Video.fromFileSystem(str(video)),
                    ]
                )

    @filter.command("卡池")
    async def pools(self, event: AstrMessageEvent):
        """List every drawable pool with the caller's collection progress."""
        if not self._pool_index:
            yield event.plain_result("卡池数据为空，请先准备 cards 目录")
            return

        state = await self._load_state()
        owned = state["owned"].get(event.get_sender_id(), {})
        current = state["pools"].get(self._chat_key(event), ALL_POOLS)

        names = sorted(self._pool_index)
        lines = [f"🎴 卡池（当前：{current}）", f"共 {len(names)} 个奖池 · 卡库 {len(self.catalog)} 张", ""]
        for i, name in enumerate(names, 1):
            cards = self._pool_index[name]
            have = sum(1 for h, _ in cards if owned.get(h))
            mark = "▸" if name == current else " "
            lines.append(f"{mark}{i:>2}. {name}  {have}/{len(cards)}")
        lines += ["", "发 /换池 序号 切换奖池", "发 /换池 全部卡池 抽全部"]
        yield event.plain_result("\n".join(lines))

    @filter.command("换池")
    async def switch_pool(self, event: AstrMessageEvent):
        """Switch the pool used by this chat."""
        target = re.sub(r"^.*?换池\s*", "", event.message_str or "").strip()
        if not target:
            yield event.plain_result("用法：/换池 序号 或 /换池 卡池名")
            return

        state = await self._load_state()
        key = self._chat_key(event)
        current = state["pools"].get(key, ALL_POOLS)

        names = sorted(self._pool_index)
        if target.isdigit():
            index = int(target)
            if not 1 <= index <= len(names):
                yield event.plain_result(f"序号超出范围（1-{len(names)}）")
                return
            chosen = names[index - 1]
        elif target in (ALL_POOLS, "全部"):
            chosen = ALL_POOLS
        elif target in self._act_members() or target in self._pool_index:
            chosen = target
        else:
            hits = [n for n in names if target in n]
            if len(hits) != 1:
                hint = "、".join(hits[:6]) if hits else "没有匹配的奖池"
                yield event.plain_result(f"{hint}\n用法：/换池 序号")
                return
            chosen = hits[0]

        state["pools"][key] = chosen
        await self._save_state(state)

        scope = self._resolve_selection(chosen)
        if scope["mode"] == "single":
            detail = f"{len(scope['list'])} 张卡"
        else:
            detail = f"{len(scope['pools'])} 个奖池"
        yield event.plain_result(f"✅ 已切换到「{chosen}」（{detail}）\n上次是「{current}」")

    @filter.command("图鉴")
    async def handbook(self, event: AstrMessageEvent):
        """Render the caller's collection handbook."""
        if not self.catalog:
            yield event.plain_result("卡库为空，请先准备 cards 目录")
            return

        state = await self._load_state()
        uid = event.get_sender_id()
        owned = state["owned"].get(uid, {})
        laser_owned = state["laser"].get(uid, {})

        # Group cards by act, then by the first non-generic pool they belong to.
        groups: dict[str, dict[str, list[tuple[str, dict]]]] = {}
        for card_hash, entry in self.catalog.items():
            pools = [p for p in entry.get("pool_list") or [] if p and p not in EXCLUDE_POOLS]
            if not pools:
                pools = [p for p in entry.get("pool_list") or [] if p]
            if not pools:
                continue
            act_id = str(entry.get("act_id") or "")
            act = ACT_NAMES.get(act_id, f"act {act_id}")
            groups.setdefault(act, {}).setdefault(pools[0], []).append((card_hash, entry))

        # Build one tile per card, then split each act into pages of at most
        # HANDBOOK_PAGE_CARDS entries so a single image stays a sane size.
        sections: list[dict] = []
        for act, pool_map in groups.items():
            for pool, cards in pool_map.items():
                cards.sort(key=lambda kv: -(float(kv[1].get("rate_pct") or 0)))
                tiles = []
                for card_hash, entry in cards:
                    tier = int(entry.get("card_scarcity") or 0)
                    tiles.append(
                        {
                            "name": entry.get("card_name", ""),
                            "tier": TIER_NAME.get(tier, "普通"),
                            "tier_level": tier,
                            "rate": entry.get("rate_pct", 0),
                            "owned": int(owned.get(card_hash, 0) or 0),
                            "laser": int(laser_owned.get(card_hash, 0) or 0),
                            "tag": self.tag_urls.get(tier, ""),
                            "src": self._card_url(card_hash),
                        }
                    )
                sections.append(
                    {
                        "act": act,
                        "pool": pool,
                        "have": sum(1 for t in tiles if t["owned"]),
                        "total": len(tiles),
                        "tiles": tiles,
                    }
                )

        pages: list[dict] = []
        current: list[dict] = []
        budget = 0
        for section in sections:
            tiles = section["tiles"]
            if current and budget + len(tiles) > HANDBOOK_PAGE_CARDS:
                pages.append(self._page(current))
                current, budget = [], 0
            current.append(section)
            budget += len(tiles)
            # A single oversized pool gets its own page rather than being split,
            # which would break the per-pool progress line.
            if budget >= HANDBOOK_PAGE_CARDS:
                pages.append(self._page(current))
                current, budget = [], 0
        if current:
            pages.append(self._page(current))

        collected = sum(1 for card_hash in self.catalog if owned.get(card_hash))
        duplicates = sum(max(0, int(n) - 1) for n in owned.values())
        sender = event.get_sender_name() or uid

        # Each page carries 30+ full-size cards, so rendering them one by one
        # makes /图鉴 crawl. The renderer caps concurrency internally.
        payloads = [
            {
                "page": page,
                "index": index,
                "pages": len(pages),
                "collected": collected,
                "total": len(self.catalog),
                "duplicates": duplicates,
                "sender": sender,
                "bg": self.bg_url,
                "laser_url": self.laser_url,
                "glass_json": self._glass_json(),
            }
            for index, page in enumerate(pages, 1)
        ]
        rendered = await asyncio.gather(
            *(self._render(event, "handbook.html", payload) for payload in payloads)
        )
        images = [image for image in rendered if image]

        if not images:
            yield event.plain_result("图鉴渲染失败")
            return
        for image in images:
            yield event.image_result(image)

    @staticmethod
    def _page(sections: list[dict]) -> dict:
        """Summarise a list of sections into one handbook page.

        Args:
            sections: Sections sharing this page.

        Returns:
            Page payload with the act names and totals it covers.
        """
        acts: list[str] = []
        for section in sections:
            if section["act"] not in acts:
                acts.append(section["act"])
        return {
            "acts": acts,
            "sections": sections,
            "have": sum(s["have"] for s in sections),
            "total": sum(s["total"] for s in sections),
        }

    @filter.command("获取卡")
    async def get_card(self, event: AstrMessageEvent):
        """Send the artwork of a card the caller already owns."""
        keyword = re.sub(r"^.*?获取卡\s*", "", event.message_str or "").strip()
        if not keyword:
            yield event.plain_result("用法：/获取卡 卡名")
            return

        state = await self._load_state()
        owned = state["owned"].get(event.get_sender_id(), {})
        hits = [
            (card_hash, entry)
            for card_hash, entry in self.catalog.items()
            if keyword in str(entry.get("card_name", "")) and owned.get(card_hash)
        ]
        if not hits:
            yield event.plain_result(f"你还没有名字含「{keyword}」的卡")
            return
        if len(hits) > 1:
            names = "、".join(entry.get("card_name", "") for _, entry in hits[:8])
            yield event.plain_result(f"匹配到多张，请写更完整：{names}")
            return

        card_hash, entry = hits[0]
        path = self.cards_dir / f"{card_hash}.jpg"
        if path.is_file():
            yield event.image_result(str(path))
        tier = int(entry.get("card_scarcity") or 0)
        yield event.plain_result(
            f"🎴 {entry.get('card_name', '')}\n"
            f"档位：{TIER_NAME.get(tier, '普通')} ｜ 概率：{entry.get('rate_pct')}% ｜ "
            f"你拥有 ×{owned.get(card_hash, 0)}"
        )

    @filter.command("抽卡帮助")
    async def help_command(self, event: AstrMessageEvent):
        """Print the gacha help text."""
        yield event.plain_result(self._help_text(event.is_admin()))

    # ------------------------------------------------------------- 管理员调试

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发积分")
    async def grant_points(self, event: AstrMessageEvent):
        """Give points to a user, bypassing the check-in loop.

        Usage: ``/发积分 <数量> [@某人]``; negative values deduct.
        """
        tail = re.sub(r"^.*?发积分\s*", "", event.message_str or "").strip()
        target, rest = self._split_target(event, tail)
        match = re.match(r"^([+-]?\d+)", rest)
        if not match:
            yield event.plain_result("用法：/发积分 100 [@某人]（负数即扣除）")
            return

        delta = int(match.group(1))
        left = await self._add_points(target, delta)
        sign = f"+{delta}" if delta >= 0 else str(delta)
        yield event.plain_result(f"✅ 已为 {target} 调整 {sign} 积分，当前余额 {left}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发卡")
    async def grant_card(self, event: AstrMessageEvent):
        """Grant a card directly, recording it as owned.

        Usage: ``/发卡 <卡名> [数量] [@某人]``.
        """
        tail = re.sub(r"^.*?发卡\s*", "", event.message_str or "").strip()
        target, rest = self._split_target(event, tail)
        # Quantity is only read when separated by whitespace or prefixed with ×,
        # so card names containing digits stay intact.
        quantity = 1
        qty_match = re.search(r"\s+[x×*]?\s*(\d+)$", rest)
        if qty_match:
            quantity = max(1, int(qty_match.group(1)))
            rest = rest[: qty_match.start()].strip()
        if not rest:
            yield event.plain_result("用法：/发卡 卡名 [数量] [@某人]")
            return

        exact = [(h, e) for h, e in self.catalog.items() if e.get("card_name") == rest]
        hits = exact or [(h, e) for h, e in self.catalog.items() if rest in str(e.get("card_name"))]
        if not hits:
            yield event.plain_result(f"卡库里没有名字含「{rest}」的卡")
            return
        if len(hits) > 1:
            names = "、".join(entry.get("card_name", "") for _, entry in hits[:8])
            yield event.plain_result(f"匹配到多张，请写更完整：{names}")
            return

        card_hash, entry = hits[0]
        state = await self._load_state()
        owned = state["owned"].setdefault(target, {})
        owned[card_hash] = int(owned.get(card_hash, 0) or 0) + quantity
        await self._save_state(state)

        path = self.cards_dir / f"{card_hash}.jpg"
        if path.is_file():
            yield event.image_result(str(path))
        tier = int(entry.get("card_scarcity") or 0)
        yield event.plain_result(
            f"✅ 已发放 {entry.get('card_name', '')} ×{quantity} 给 {target}\n"
            f"档位：{TIER_NAME.get(tier, '普通')} ｜ 该用户现有 ×{owned[card_hash]}"
        )

    @staticmethod
    def _split_target(event: AstrMessageEvent, text: str) -> tuple[str, str]:
        """Pull an @mention out of an admin command tail.

        Args:
            event: Source message, used to find mention components.
            text: Command tail with the leading command already removed.

        Returns:
            ``(uid, remaining_text)``; the sender is the default target.
        """
        uid = event.get_sender_id()
        for component in event.message_obj.message:
            if isinstance(component, At) and str(component.qq) != "all":
                uid = str(component.qq)
                break
        return uid, re.sub(r"@\S+\s*", "", text).strip()

    def _help_text(self, is_admin: bool = False) -> str:
        """Build the help text shown by ``/抽卡帮助`` and on bad arguments.

        Args:
            is_admin: Whether to append the administrator section.

        Returns:
            The multi-line help text.
        """
        lines = [
            "🎴 收藏集抽卡",
            "",
            f"/抽卡 [1-{MAX_DRAW}]  抽卡，每抽 {COST} 积分",
            "/卡池  查看全部奖池与收集进度",
            "/换池 序号  切换本群奖池（全部卡池 = 抽所有）",
            "/图鉴  生成本人图鉴长图",
            "/获取卡 卡名  发送已拥有卡的原图",
            "",
            f"当前卡库 {len(self.catalog)} 张卡 · {len(self._pool_index)} 个奖池",
            "积分由打卡插件提供：发 /打卡 赚取",
        ]
        if is_admin:
            lines += [
                "",
                "【管理员】",
                "/发积分 数量 [@某人]  增减积分",
                "/发卡 卡名 [数量] [@某人]  直接发卡",
            ]
        return "\n".join(lines)
