#!/usr/bin/env python3
"""Materialise card artwork for the gacha plugin.

The Bilibili 收藏集 assets ship as Chinese filenames (``<card>_<tier>.png``),
while the catalog keys cards by hash (``<hash>.png``). This script matches the
two by card name plus rarity tier, converts every hit into a display-sized
JPEG named after its hash, and copies the catalog next to the artwork.

Run once after installing the plugin:

    python prepare_cards.py --source D:\\BiliDLC \\
        --catalog ..\\gacha_full\\catalog.json \\
        --out <astrbot>/data/plugin_data/astrbot_plugin_azuregacha
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from PIL import Image

#: Catalog rarity value to the tier token used in the asset filenames.
TIER_TOKEN = {10: "普卡N", 20: "稀有R", 30: "小隐藏SR", 40: "大隐藏SSR"}

#: Only these tiers get their clip copied; the bot sends them after a draw.
VIDEO_TIERS = (30, 40)

#: Longest edge of the stored JPEG. The bot sends this file directly, so it
#: trades a little sharpness for a much smaller upload.
MAX_EDGE = 1080
QUALITY = 88

ASSET_NAME = re.compile(r"^(?P<name>.+)_(?P<tier>普卡N|稀有R|小隐藏SR|大隐藏SSR)(?:_\d+)?$")


def index_assets(source: Path) -> dict[tuple[str, str], Path]:
    """Index the on-disk artwork by card name and tier.

    Args:
        source: Root directory holding the downloaded 收藏集 folders.

    Returns:
        Mapping of ``(card name, tier token)`` to the image file.
    """
    index: dict[tuple[str, str], Path] = {}
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        match = ASSET_NAME.match(path.stem)
        if match:
            index[(match.group("name"), match.group("tier"))] = path
    return index


def index_videos(source: Path) -> dict[tuple[str, str], Path]:
    """Index the animated card clips by card name and tier.

    Args:
        source: Root directory holding the downloaded 收藏集 folders.

    Returns:
        Mapping of ``(card name, tier token)`` to the mp4 file.
    """
    index: dict[tuple[str, str], Path] = {}
    for path in source.rglob("*.mp4"):
        match = ASSET_NAME.match(path.stem)
        if match:
            index[(match.group("name"), match.group("tier"))] = path
    return index


def convert(source: Path, target: Path) -> bool:
    """Resize and re-encode one card image.

    Args:
        source: Original PNG or JPEG.
        target: Destination JPEG path.

    Returns:
        True when the file was written.
    """
    try:
        with Image.open(source) as img:
            img = img.convert("RGB")
            if max(img.size) > MAX_EDGE:
                scale = MAX_EDGE / max(img.size)
                img = img.resize(
                    (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            img.save(target, format="JPEG", quality=QUALITY, optimize=True)
    except (OSError, ValueError) as exc:
        print(f"  ! {source.name}: {exc}")
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Build gacha card artwork")
    parser.add_argument("--source", required=True, help="Folder with the 收藏集 assets")
    parser.add_argument("--catalog", required=True, help="catalog.json from the source plugin")
    parser.add_argument("--out", required=True, help="Plugin data directory")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Keep catalog entries without artwork (they cannot be drawn anyway)",
    )
    parser.add_argument(
        "--skip-videos",
        action="store_true",
        help="Do not copy the animated clips for hidden-tier cards",
    )
    args = parser.parse_args()

    source = Path(args.source)
    out = Path(args.out)
    cards_dir = out / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    videos_dir = out / "videos"

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    assets = index_assets(source)
    print(f"catalog: {len(catalog)} cards | assets indexed: {len(assets)}")

    kept: dict[str, dict] = {}
    missing: list[str] = []
    for card_hash, entry in catalog.items():
        token = TIER_TOKEN.get(int(entry.get("card_scarcity") or 0))
        origin = assets.get((str(entry.get("card_name", "")), token))
        if not origin:
            missing.append(f"{entry.get('card_name')} ({token})")
            continue
        if convert(origin, cards_dir / f"{card_hash}.jpg"):
            kept[card_hash] = entry

    if args.allow_missing:
        kept.update({k: v for k, v in catalog.items() if k not in kept})

    (out / "catalog.json").write_text(
        json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # 隐藏档的动图单独拷贝一份，抽到小隐藏/大隐藏时直接发视频。
    if not args.skip_videos:
        videos = index_videos(source)
        videos_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for card_hash, entry in kept.items():
            tier = int(entry.get("card_scarcity") or 0)
            if tier not in VIDEO_TIERS:
                continue
            token = TIER_TOKEN.get(tier)
            origin = videos.get((str(entry.get("card_name", "")), token))
            if not origin:
                continue
            shutil.copy2(origin, videos_dir / f"{card_hash}.mp4")
            copied += 1
        size = sum(f.stat().st_size for f in videos_dir.glob("*.mp4"))
        print(f"videos: {copied} clips, {size / 1048576:.1f} MB -> {videos_dir}")

    total = sum(f.stat().st_size for f in cards_dir.glob("*.jpg"))
    print(f"written: {len(kept)} cards, {total / 1048576:.1f} MB -> {cards_dir}")
    if missing:
        print(f"no artwork ({len(missing)}): {', '.join(missing[:10])} ...")


if __name__ == "__main__":
    main()
