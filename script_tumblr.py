import argparse
import asyncio
import configparser
import logging
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Set

import feedparser
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import RetryAfter, TelegramError


@dataclass
class Settings:
    bot_token: str
    chat_id: str
    media_caption: str
    delay_between_posts: int
    rss_feeds_file: Path
    published_images_file: Path
    log_file: Path


def read_config(config_path: Path) -> Settings:
    parser = configparser.ConfigParser()
    if not parser.read(config_path, encoding="utf-8"):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    base_dir = config_path.parent

    def resolve_path(raw_value: Optional[str], option_name: str) -> Path:
        if not raw_value:
            raise ValueError(f"Missing `{option_name}` in config {config_path}")
        candidate = Path(raw_value)
        return candidate if candidate.is_absolute() else base_dir / candidate

    required_sections = ("telegram", "storage", "script")
    for section in required_sections:
        if section not in parser:
            raise ValueError(f"Missing [{section}] section in config {config_path}")

    telegram_section = parser["telegram"]
    storage_section = parser["storage"]
    script_section = parser["script"]

    return Settings(
        bot_token=telegram_section.get("bot_token", fallback="").strip(),
        chat_id=telegram_section.get("chat_id", fallback="").strip(),
        media_caption=telegram_section.get("media_caption", fallback="").strip(),
        delay_between_posts=script_section.getint("delay_between_posts", fallback=5),
        rss_feeds_file=resolve_path(storage_section.get("rss_feeds_file"), "rss_feeds_file"),
        published_images_file=resolve_path(
            storage_section.get("published_images_file"), "published_images_file"
        ),
        log_file=resolve_path(script_section.get("log_file"), "log_file"),
    )


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )


def read_rss_feeds(file_path: Path) -> List[str]:
    if not file_path.exists():
        logging.error("RSS feeds file not found: %s", file_path)
        return []
    with file_path.open("r", encoding="utf-8") as file:
        return [line.strip() for line in file if line.strip()]


def parse_rss_feed(url: str):
    logging.info("Parsing RSS feed: %s", url)
    return feedparser.parse(url)


def _parse_srcset(srcset: str) -> List[tuple[str, float]]:
    candidates: List[tuple[str, float]] = []
    for part in srcset.split(","):
        item = part.strip()
        if not item:
            continue
        if " " in item:
            url, descriptor = item.rsplit(" ", 1)
            descriptor = descriptor.strip()
            score: Optional[float] = None
            if descriptor.endswith("w"):
                try:
                    score = float(descriptor[:-1])
                except ValueError:
                    score = None
            elif descriptor.endswith("x"):
                try:
                    score = float(descriptor[:-1]) * 1000.0
                except ValueError:
                    score = None
            if score is None:
                score = 0.0
        else:
            url = item
            score = 0.0
        candidates.append((url, score))
    return candidates


def _pick_largest_srcset_url(srcset: str) -> Optional[str]:
    candidates = _parse_srcset(srcset)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1])[0]


def extract_images(feed) -> List[str]:
    images: List[str] = []
    for entry in getattr(feed, "entries", []):
        description = entry.get("description")
        if not description:
            continue
        soup = BeautifulSoup(description, "html.parser")
        for tag in soup.find_all(["img", "a"]):
            if tag.name == "img" and tag.has_attr("src"):
                srcset = tag.get("srcset", "").strip()
                best_src = _pick_largest_srcset_url(srcset) if srcset else None
                img_url = best_src or tag["src"]
                images.append(img_url)
                logging.info("Found image: %s", img_url)
            elif tag.name == "a" and tag.has_attr("href"):
                href = tag["href"]
                if href.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".gifv", ".webp")):
                    images.append(href)
                    logging.info("Found image: %s", href)
    return images


def load_published_images(file_path: Path) -> Set[str]:
    if not file_path.exists():
        return set()
    with file_path.open("r", encoding="utf-8") as file:
        return {line.strip() for line in file if line.strip()}


def append_published_image(file_path: Path, img_url: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as file:
        file.write(img_url + "\n")


def _ensure_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        destination.write_bytes(response.read())


def _get_content_type(url: str) -> Optional[str]:
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.headers.get("Content-Type")
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError:
        return None


def _is_animated_webp(file_path: Path) -> bool:
    data = file_path.read_bytes()
    return b"ANIM" in data


def _run_ffmpeg(args: List[str]) -> None:
    subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _convert_mp4_to_gif(source: Path, output: Path) -> None:
    palette = output.with_suffix(".palette.png")
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vf",
            "fps=20,scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos,palettegen",
            str(palette),
        ]
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-i",
            str(palette),
            "-lavfi",
            "fps=20,scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos[x];[x][1:v]paletteuse",
            "-loop",
            "0",
            str(output),
        ]
    )
    palette.unlink(missing_ok=True)


def _convert_webp_to_mp4(source: Path, output: Path) -> None:
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-an",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def _strip_audio_from_mp4(source: Path, output: Path) -> None:
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def _convert_webp_to_jpg(source: Path, output: Path) -> None:
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(output),
        ]
    )


async def _send_animation_file(
    bot: Bot, chat_id: str, file_path: Path, media_caption: str
) -> None:
    with file_path.open("rb") as file:
        await bot.send_animation(
            chat_id=chat_id, animation=file, caption=media_caption, parse_mode="HTML"
        )


async def _send_photo_file(bot: Bot, chat_id: str, file_path: Path, media_caption: str) -> None:
    with file_path.open("rb") as file:
        await bot.send_photo(
            chat_id=chat_id, photo=file, caption=media_caption, parse_mode="HTML"
        )


async def _send_gifv(
    bot: Bot, chat_id: str, url: str, media_caption: str, ffmpeg_available: bool
) -> None:
    original_url = url
    mp4_url = url[:-5] + ".mp4" if url.lower().endswith(".gifv") else url
    content_type = _get_content_type(original_url) or _get_content_type(mp4_url)
    is_gif = content_type is not None and content_type.lower().startswith("image/gif")
    if not ffmpeg_available:
        fallback_url = original_url if is_gif else mp4_url
        logging.warning("ffmpeg not available, sending gifv as URL: %s", fallback_url)
        await bot.send_animation(
            chat_id=chat_id, animation=fallback_url, caption=media_caption, parse_mode="HTML"
        )
        return
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        if is_gif:
            source_path = tmp_dir_path / "source.gif"
            _download_file(original_url, source_path)
            await _send_animation_file(bot, chat_id, source_path, media_caption)
        else:
            source_path = tmp_dir_path / "source.mp4"
            output_path = tmp_dir_path / "output.mp4"
            _download_file(mp4_url, source_path)
            _strip_audio_from_mp4(source_path, output_path)
            await _send_animation_file(bot, chat_id, output_path, media_caption)


async def _send_webp(
    bot: Bot, chat_id: str, url: str, media_caption: str, ffmpeg_available: bool
) -> None:
    if not ffmpeg_available:
        logging.warning("ffmpeg not available, sending webp as URL: %s", url)
        await bot.send_photo(chat_id=chat_id, photo=url, caption=media_caption, parse_mode="HTML")
        return
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        source_path = tmp_dir_path / "source.webp"
        _download_file(url, source_path)
        if _is_animated_webp(source_path):
            output_path = tmp_dir_path / "output.mp4"
            _convert_webp_to_mp4(source_path, output_path)
            await _send_animation_file(bot, chat_id, output_path, media_caption)
        else:
            output_path = tmp_dir_path / "output.jpg"
            _convert_webp_to_jpg(source_path, output_path)
            await _send_photo_file(bot, chat_id, output_path, media_caption)


async def send_images(
    bot: Bot,
    chat_id: str,
    images: Iterable[str],
    published_images: Set[str],
    published_images_file: Path,
    media_caption: str,
    delay_seconds: int,
) -> None:
    ffmpeg_available = _ensure_ffmpeg()
    for img_url in images:
        if img_url in published_images:
            logging.info("Image already published: %s", img_url)
            continue
        while True:
            try:
                url_lower = img_url.lower()
                if url_lower.endswith(".gifv"):
                    await _send_gifv(bot, chat_id, img_url, media_caption, ffmpeg_available)
                elif url_lower.endswith(".webp"):
                    await _send_webp(bot, chat_id, img_url, media_caption, ffmpeg_available)
                else:
                    await bot.send_photo(
                        chat_id=chat_id, photo=img_url, caption=media_caption, parse_mode="HTML"
                    )
                logging.info("Sent media: %s", img_url)
                append_published_image(published_images_file, img_url)
                published_images.add(img_url)
                await asyncio.sleep(delay_seconds)
                break
            except RetryAfter as exc:
                logging.warning("Flood control exceeded. Retrying in %s seconds.", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
            except TelegramError as exc:
                logging.error("Error sending photo %s: %s", img_url, exc)
                break
            except Exception as exc:
                logging.error("Error processing media %s: %s", img_url, exc)
                break


async def run(config_path: Path) -> None:
    settings = read_config(config_path)
    if not settings.bot_token or not settings.chat_id:
        raise ValueError("Both `bot_token` and `chat_id` must be configured.")

    configure_logging(settings.log_file)
    logging.info("Starting Tumblr RSS to Telegram bridge")

    rss_feeds = read_rss_feeds(settings.rss_feeds_file)
    if not rss_feeds:
        logging.error("No RSS feeds loaded. Nothing to process.")
        return
    logging.info("Loaded %d RSS feeds", len(rss_feeds))

    settings.published_images_file.parent.mkdir(parents=True, exist_ok=True)
    published_images = load_published_images(settings.published_images_file)

    bot = Bot(token=settings.bot_token)
    for rss_feed in rss_feeds:
        feed = parse_rss_feed(rss_feed)
        images = extract_images(feed)
        await send_images(
            bot,
            settings.chat_id,
            images,
            published_images,
            settings.published_images_file,
            settings.media_caption,
            settings.delay_between_posts,
        )

    logging.info("Script execution completed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post images from Tumblr RSS feeds to Telegram.")
    parser.add_argument(
        "--config",
        default="config.ini",
        help="Path to configuration file (default: config.ini located next to the script).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path(__file__).resolve().parent / config_path
    try:
        asyncio.run(run(config_path))
    except Exception as exc:  # pragma: no cover
        logging.error("Fatal error: %s", exc)
        raise


if __name__ == "__main__":
    main()
