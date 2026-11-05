#!/usr/bin/env python3
"""Materialise card artwork for the gacha plugin.

Two source layouts are supported:

1. **Official release** (``assets-cards.tar`` from
   ``zb23333ww/koishi-plugin-vsinger-gacha``): a flat ``<hash>.png`` set plus a
   ``holo/<hash>.jpg`` set of holographic variants. Detected automatically.
2. **Community art dump** (``D:\\BiliDLC`` style): ``<card name>_<tier>.png``
   files nested in folders, matched to the catalog by name plus tier.

In both cases the output is::

    <out>/catalog.json          catalog filtered to the cards we can show
    <out>/cards/<hash>.jpg      display-sized JPEG
    <out>/holo/<hash>.jpg       holographic variant (official release only)
    <out>/videos/<hash>.mp4     clips for the hidden tiers only

Example::

    python prepare_cards.py --source D:\\BiliDLC \\
        --catalog path/to/catalog.json \\
        --out <astrbot>/data/plugin_data/astrbot_plugin_azuregacha
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from PIL import Image

#: Catalog rarity value to the tier token used in the legacy asset filenames.
TIER_TOKEN = {10: "普卡N", 20: "稀有R", 30: "小隐藏SR", 40: "大隐藏SSR"}

#: Only these tiers get their clip copied; the bot sends them after a draw.
VIDEO_TIERS = (30, 40)

#: Width cap for the stored JPEG. The official art is 1242x1863 and is kept
#: as-is; wider sources are scaled down, narrower ones are never upscaled.
MAX_WIDTH = 1242
QUALITY = 92

ASSET_NAME = re.compile(r"^(?P<name>.+)_(?P<tier>普卡N|稀有R|小隐藏SR|大隐藏SSR)(?:_\d+)?$")

#: 官方 catalog 随插件一起分发，省得新机器还要单独找。
BUNDLED_CATALOG = Path(__file__).parent / "catalog.json"


def index_assets(source: Path) -> dict[tuple[str, str], Path]:
    """Index legacy artwork by card name and tier.

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
    """Index legacy clips by card name and tier.

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
            if img.width > MAX_WIDTH:
                scale = MAX_WIDTH / img.width
                img = img.resize(
                    (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            img.save(target, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    except (OSError, ValueError) as exc:
        print(f"  ! {source.name}: {exc}")
        return False
    return True


def is_official_layout(source: Path, catalog: dict) -> bool:
    """Tell the two source layouts apart.

    Args:
        source: Candidate asset directory.
        catalog: Parsed catalog keyed by card hash.

    Returns:
        True when the directory holds ``<hash>.png`` files from the release.
    """
    hits = sum(1 for card_hash in catalog if (source / f"{card_hash}.png").is_file())
    return hits > len(catalog) * 0.5


def build_from_official(source: Path, catalog: dict, out: Path, videos_dir: Path | None) -> dict:
    """Convert the official flat release layout.

    Args:
        source: Directory with ``<hash>.png`` and ``holo/``.
        catalog: Full catalog.
        out: Plugin data directory.
        videos_dir: Extracted ``videos/`` directory, if any.

    Returns:
        The catalog entries that ended up with artwork.
    """
    cards_dir = out / "cards"
    kept: dict[str, dict] = {}
    for card_hash, entry in catalog.items():
        origin = source / f"{card_hash}.png"
        if not origin.is_file():
            print(f"  ! 缺图 {card_hash} {entry.get('card_name')}")
            continue
        if convert(origin, cards_dir / f"{card_hash}.jpg"):
            kept[card_hash] = entry
    print(f"cards: {len(kept)}/{len(catalog)} -> {cards_dir}")

    holo_src = source / "holo"
    if holo_src.is_dir():
        holo_dir = out / "holo"
        holo_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for origin in holo_src.glob("*.jpg"):
            if origin.stem in kept:
                shutil.copy2(origin, holo_dir / origin.name)
                count += 1
        print(f"holo: {count} -> {holo_dir}")

    if videos_dir:
        copy_videos(videos_dir, kept, out / "videos")
    return kept


def copy_videos(source: Path, kept: dict, target_dir: Path) -> None:
    """Copy the clips the bot will actually send.

    Args:
        source: Directory holding ``<hash>.mp4``.
        kept: Cards with artwork.
        target_dir: Destination directory.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for origin in source.glob("*.mp4"):
        entry = kept.get(origin.stem)
        if not entry or int(entry.get("card_scarcity") or 0) not in VIDEO_TIERS:
            continue
        shutil.copy2(origin, target_dir / origin.name)
        count += 1
    size = sum(f.stat().st_size for f in target_dir.glob("*.mp4"))
    print(f"videos: {count} clips, {size / 1048576:.1f} MB -> {target_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build gacha card artwork")
    parser.add_argument("--source", required=True, help="Folder with the card artwork")
    parser.add_argument(
        "--catalog",
        help="catalog.json; defaults to the one bundled next to this script",
    )
    parser.add_argument("--out", required=True, help="Plugin data directory")
    parser.add_argument(
        "--videos-dir",
        help="Folder with <hash>.mp4 clips; defaults to <source>/videos when present",
    )
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
    catalog_path = Path(args.catalog) if args.catalog else BUNDLED_CATALOG
    if not catalog_path.is_file():
        raise SystemExit(f"catalog.json 不存在：{catalog_path}")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    if args.videos_dir:
        videos_dir = Path(args.videos_dir)
    else:
        # 两个 tar 解压到同一目录时，视频就在 <source>/videos 下。
        videos_dir = source / "videos"
    if not videos_dir.is_dir():
        videos_dir = None

    if is_official_layout(source, catalog):
        print(f"检测到官方素材包布局: {source}")
        kept = build_from_official(
            source, catalog, out, None if args.skip_videos else videos_dir
        )
    else:
        cards_dir = out / "cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        assets = index_assets(source)
        print(f"catalog: {len(catalog)} cards | assets indexed: {len(assets)}")

        kept = {}
        missing: list[str] = []
        for card_hash, entry in catalog.items():
            token = TIER_TOKEN.get(int(entry.get("card_scarcity") or 0))
            origin = assets.get((str(entry.get("card_name", "")), token))
            if not origin:
                missing.append(f"{entry.get('card_name')} ({token})")
                continue
            if convert(origin, cards_dir / f"{card_hash}.jpg"):
                kept[card_hash] = entry
        if args.skip_videos is False:
            copy_videos(source, kept, out / "videos")
        if missing:
            print(f"no artwork ({len(missing)}): {', '.join(missing[:10])} ...")

    if args.allow_missing:
        kept.update({k: v for k, v in catalog.items() if k not in kept})

    (out / "catalog.json").write_text(
        json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    total = sum(f.stat().st_size for f in (out / "cards").glob("*.jpg"))
    print(f"written: {len(kept)} cards, {total / 1048576:.1f} MB -> {out / 'cards'}")


if __name__ == "__main__":
    main()
