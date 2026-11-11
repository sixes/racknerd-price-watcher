#!/usr/bin/env python3
"""Watch RackNerd special promos and email when a plan's price drops."""

import argparse
import html as html_lib
import json
import logging
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
PROMO_URL = "https://my.racknerd.com/index.php?rp=/store/special-promos"
HEALTHCHECK_URL = "https://hc-ping.com/a772c9bb-de82-40e5-a482-6b7b185464a3"
HEALTHCHECK_FAIL_URL = HEALTHCHECK_URL + "/fail"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SHANGHAI = timezone(timedelta(hours=8))

DEFAULT_CONFIG: Dict[str, Any] = {
    "url": PROMO_URL,
    "timeout_seconds": 30,
    "retries": 2,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "gmail_email": "",
    "gmail_app_password": "",
    "recipient_email": "",
    "healthcheck_url": HEALTHCHECK_URL,
    "healthcheck_fail_url": HEALTHCHECK_FAIL_URL,
    "state_file": "state.json",
    "log_file": "logs/racknerd.log",
}


class WatcherError(Exception):
    pass


def load_config(path: Path) -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WatcherError(f"Cannot read config {path}: {error}") from None
        if not isinstance(raw, dict):
            raise WatcherError(f"Config {path} must contain a JSON object.")
        config.update(raw)
    for key, env in (
        ("gmail_email", "GMAIL_EMAIL"),
        ("gmail_app_password", "GMAIL_APP_PASSWORD"),
        ("recipient_email", "RECIPIENT_EMAIL"),
    ):
        value = os.environ.get(env)
        if value:
            config[key] = value
    base = path.resolve().parent
    for key in ("state_file", "log_file"):
        value = Path(str(config[key]))
        if not value.is_absolute():
            config[key] = str(base / value)
    return config


def setup_logging(log_path: str, level: str) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    root.handlers.clear()
    file_handler = TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(file_handler)
    root.addHandler(console)


def fetch_page(url: str, timeout: int, retries: int) -> str:
    if not shutil.which("curl"):
        raise WatcherError("curl is required to fetch the page over HTTP/2.")
    command = [
        "curl", "-sS", "--http2", "--max-time", str(timeout),
        "-A", USER_AGENT,
        "-H", "Accept: text/html,application/xhtml+xml",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-w", "\n%{http_code}",
        url,
    ]
    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 2):
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout + 5
            )
        except (OSError, subprocess.SubprocessError) as error:
            last_error = error
        else:
            body, _, status = result.stdout.rpartition("\n")
            if result.returncode == 0 and status.strip() == "200":
                if "Just a moment..." in body or "cf-chl" in body:
                    raise WatcherError(
                        "The site returned a Cloudflare challenge instead of the "
                        "promo page; a browser session is required."
                    )
                return body
            last_error = WatcherError(
                f"curl exited {result.returncode} with HTTP {status.strip()}: "
                f"{result.stderr.strip()[:200]}"
            )
        logging.warning("Fetch attempt %s/%s failed: %s",
                        attempt, retries + 1, last_error)
        if attempt <= retries:
            time.sleep(3)
    raise WatcherError(f"Could not fetch {url}: {last_error}")


def _clean_text(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return " ".join(text.split())


def _parse_specs(description_html: str) -> List[str]:
    text = re.sub(r"<br\s*/?>", "\n", description_html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    specs = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line or line.upper().startswith("JUST $") or line.startswith("Available in"):
            continue
        specs.append(line)
    return specs[:8]


def parse_promos(page: str, base_url: str) -> List[Dict[str, Any]]:
    parts = re.split(
        r'<div class="product clearfix" id="product(\d+)">', page, flags=re.I
    )
    promos: List[Dict[str, Any]] = []
    for index in range(1, len(parts), 2):
        product_id, chunk = parts[index], parts[index + 1]
        name_match = re.search(
            rf'<span id="product{product_id}-name">(.*?)</span>', chunk, re.S | re.I
        )
        price_block = re.search(
            rf'<div class="product-pricing" id="product{product_id}-price">(.*?)</div>',
            chunk, re.S | re.I,
        )
        if not name_match or not price_block:
            logging.warning("Skipping product %s: name or price block missing.", product_id)
            continue
        name = _clean_text(name_match.group(1))
        price_match = re.search(r"\$([0-9][0-9,]*(?:\.[0-9]{1,2})?)", price_block.group(1))
        if not name or not price_match:
            logging.warning("Skipping product %s: unreadable name or price.", product_id)
            continue
        price = float(price_match.group(1).replace(",", ""))
        currency_match = re.search(r"\$[0-9][0-9,.]*\s*([A-Z]{3})", price_block.group(1))
        period_match = re.search(
            r"</span>.*?<br\s*/?>\s*([A-Za-z]+)", price_block.group(1), re.S | re.I
        )
        link_match = re.search(
            rf'href="([^"]+)"[^>]*id="product{product_id}-order-button"', chunk, re.I
        ) or re.search(rf'id="product{product_id}-order-button"', chunk, re.I)
        href = ""
        if link_match and link_match.lastindex:
            href = html_lib.unescape(link_match.group(1))
        url = urllib.parse.urljoin(base_url, href) if href else base_url
        description_match = re.search(
            rf'<p id="product{product_id}-description">(.*?)</p>', chunk, re.S | re.I
        )
        promos.append({
            "product_id": product_id,
            "name": name,
            "price": price,
            "currency": currency_match.group(1) if currency_match else "USD",
            "period": period_match.group(1) if period_match else "",
            "url": url,
            "specs": _parse_specs(description_match.group(1)) if description_match else [],
        })
    if not promos:
        raise WatcherError("No promotional products found; the page layout may have changed.")
    logging.info("Parsed %d promo plan(s): %s", len(promos), ", ".join(
        f"{item['name']} ${item['price']:.2f}" for item in promos))
    return promos


def load_state(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WatcherError(f"Cannot read state {path}: {error}") from None
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise WatcherError(f"State {path} has an unexpected structure.")
    return state


def save_state(path: Path, promos: List[Dict[str, Any]], previous: Optional[Dict[str, Any]],
               now: datetime) -> None:
    old_items = (previous or {}).get("items") or {}
    items: Dict[str, Any] = {}
    for promo in promos:
        old = old_items.get(promo["name"]) or {}
        items[promo["name"]] = {
            **promo,
            "first_seen": old.get("first_seen") or now.isoformat(timespec="seconds"),
            "last_seen": now.isoformat(timespec="seconds"),
        }
    payload = {
        "version": 1,
        "url": PROMO_URL,
        "updated_at": now.isoformat(timespec="seconds"),
        "items": items,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def find_drops(promos: List[Dict[str, Any]],
               previous_items: Dict[str, Any]) -> List[Dict[str, Any]]:
    drops = []
    for promo in promos:
        old = previous_items.get(promo["name"])
        if not old:
            continue
        try:
            old_price = float(old["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if promo["price"] < old_price:
            drops.append({"previous": old, "current": promo,
                          "saving": old_price - promo["price"]})
    return drops


def format_price(item: Dict[str, Any]) -> str:
    currency = item.get("currency") or "USD"
    period = item.get("period")
    suffix = f" / {period}" if period else ""
    return f"${float(item['price']):,.2f} {currency}{suffix}"


def render_email(drops: List[Dict[str, Any]], total: int, now: datetime) -> str:
    lines = [
        f"RackNerd promo price drop detected on "
        f"{now.strftime('%Y-%m-%d %H:%M')} (UTC+8).",
        "",
    ]
    for drop in drops:
        current = drop["current"]
        lines.append(current["name"])
        lines.append(f"  Previous: {format_price(drop['previous'])}")
        lines.append(f"  Now:      {format_price(current)}")
        lines.append(f"  Saving:   ${drop['saving']:,.2f}")
        specs = " / ".join(current.get("specs") or [])
        if specs:
            lines.append(f"  Specs:    {specs}")
        lines.append(f"  Link:     {current['url']}")
        lines.append("")
    lines.append(f"{total - len(drops)} other promo plan(s) unchanged.")
    lines.append(PROMO_URL)
    return "\n".join(lines)


def send_email(config: Dict[str, Any], subject: str, body: str) -> None:
    sender = (config.get("gmail_email") or "").strip()
    password = (config.get("gmail_app_password") or "").strip()
    recipient = (config.get("recipient_email") or sender).strip()
    if not sender or not password or not recipient:
        raise WatcherError("Gmail credentials or recipient are not configured.")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)
    host = config.get("smtp_host") or "smtp.gmail.com"
    port = int(config.get("smtp_port") or 587)
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(sender, password)
            smtp.send_message(message)
    logging.info("Alert email sent to %s.", recipient)


def ping(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            response.read()
        logging.info("Healthcheck pinged: %s", url)
    except (urllib.error.URLError, OSError) as error:
        logging.error("Healthcheck ping failed (%s): %s", url, error)


def collect(config: Dict[str, Any], url: str, dry_run: bool) -> List[Dict[str, Any]]:
    page = fetch_page(url, int(config["timeout_seconds"]), int(config["retries"]))
    promos = parse_promos(page, url)
    if dry_run:
        for promo in promos:
            print(json.dumps(promo, ensure_ascii=False, indent=2))
    return promos


def run(config: Dict[str, Any], url: str, dry_run: bool, healthcheck: bool) -> int:
    now = datetime.now(SHANGHAI)
    state_path = Path(config["state_file"])
    try:
        promos = collect(config, url, dry_run)
        if dry_run:
            logging.info("Dry run: fetched %d promo(s); no state, email, or ping.",
                         len(promos))
            return 0
        previous = load_state(state_path)
        if previous is None:
            save_state(state_path, promos, None, now)
            logging.info("Baseline recorded for %d promo(s); no email on first run.",
                         len(promos))
        else:
            drops = find_drops(promos, previous["items"])
            if drops:
                subject = (f"[RackNerd] Price drop: {len(drops)} plan(s) "
                           f"{now.strftime('%Y-%m-%d')}")
                send_email(config, subject, render_email(drops, len(promos), now))
            else:
                logging.info("No price drops; email skipped.")
            save_state(state_path, promos, previous, now)
            logging.info("State saved with %d promo(s).", len(promos))
        logging.info("Daily check finished successfully.")
        if healthcheck:
            ping(config["healthcheck_url"])
        return 0
    except Exception as error:
        logging.exception("Daily check failed: %s", error)
        if healthcheck:
            ping(config["healthcheck_fail_url"])
        return 1


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json")
    parser.add_argument("--url", default=None, help="Override the promo page URL.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and print promos without state, email, or pings.")
    parser.add_argument("--no-healthcheck", action="store_true",
                        help="Skip healthchecks.io pings.")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except WatcherError as error:
        print(error, file=sys.stderr)
        return 1
    setup_logging(config["log_file"], args.log_level)
    logging.info("RackNerd promo check starting.")
    url = args.url or config["url"]
    return run(config, url, args.dry_run, not args.no_healthcheck)


if __name__ == "__main__":
    sys.exit(main())
