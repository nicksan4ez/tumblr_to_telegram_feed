import argparse
import asyncio
import configparser
import logging
import sys
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


def extract_images(feed) -> List[str]:
    images: List[str] = []
    for entry in getattr(feed, "entries", []):
        description = entry.get("description")
        if not description:
            continue
        soup = BeautifulSoup(description, "html.parser")
        for tag in soup.find_all(["img", "a"]):
            if tag.name == "img" and tag.has_attr("src"):
                images.append(tag["src"])
                logging.info("Found image: %s", tag["src"])
            elif tag.name == "a" and tag.has_attr("href"):
                href = tag["href"]
                if href.lower().endswith((".png", ".jpg", ".jpeg", ".gif")):
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


async def send_images(
    bot: Bot,
    chat_id: str,
    images: Iterable[str],
    published_images: Set[str],
    published_images_file: Path,
    media_caption: str,
    delay_seconds: int,
) -> None:
    for img_url in images:
        if img_url in published_images:
            logging.info("Image already published: %s", img_url)
            continue
        while True:
            try:
                await bot.send_photo(
                    chat_id=chat_id, photo=img_url, caption=media_caption, parse_mode="HTML"
                )
                logging.info("Sent image: %s", img_url)
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
