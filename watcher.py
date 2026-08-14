#!/usr/bin/env python3
"""
CinéWatch — veille actualité cinéma & séries → Discord.

Usage:
    python watcher.py            # exécution normale
    python watcher.py --seed     # marque tout comme "déjà vu" sans rien envoyer
    python watcher.py --check    # teste chaque flux et affiche un rapport
    python watcher.py --dry-run  # affiche ce qui serait envoyé, sans poster
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sources.yaml"
STATE_PATH = ROOT / "state" / "seen.json"
MAX_STATE = 4000

USER_AGENT = (
    "Mozilla/5.0 (compatible; CineWatch/1.0; +https://github.com/) "
    "feed-reader"
)

# (score minimum, couleur embed, libellé)
TIERS = [
    (9, 0x2ECC71, "🟢 Officiel / Trade"),
    (7, 0x3498DB, "🔵 Presse spécialisée"),
    (5, 0xE67E22, "🟠 À confirmer"),
    (0, 0x95A5A6, "⚪ Rumeur"),
]

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_|source$|si$)")


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def tier_for(score: int):
    for minimum, color, label in TIERS:
        if score >= minimum:
            return color, label
    return TIERS[-1][1], TIERS[-1][2]


def canonical(url: str) -> str:
    """Normalise une URL pour la déduplication."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAMS.match(k)]
    path = p.path.rstrip("/") or "/"
    return urlunparse((
        p.scheme.lower() or "https",
        p.netloc.lower(),
        path,
        "",
        urlencode(query),
        "",
    ))


def clean_text(raw: str, limit: int = 280) -> str:
    """Retire le HTML d'un résumé de flux et tronque proprement."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


# --------------------------------------------------------------------------
# État (déduplication)
# --------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen": [], "initialized": False}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"⚠️  État illisible ({exc}), on repart de zéro.")
        return {"seen": [], "initialized": False}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["seen"] = state["seen"][-MAX_STATE:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Récupération des flux
# --------------------------------------------------------------------------

def fetch_feed(url: str, timeout: int = 25):
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def is_relevant(title: str, summary: str, source: dict, config: dict) -> bool:
    text = f"{title} {summary}".lower()

    for muted in config.get("mute_keywords", []):
        if muted.lower() in text:
            return False

    if source.get("needs_filter"):
        keywords = config.get("relevance_keywords", [])
        if keywords and not any(k.lower() in text for k in keywords):
            return False

    custom = source.get("include_any")
    if custom and not any(k.lower() in text for k in custom):
        return False

    return True


def collect(config: dict, state: dict, ignore_age: bool = False) -> list[dict]:
    settings = config.get("settings", {})
    max_age = timedelta(hours=settings.get("max_age_hours", 12))
    now = datetime.now(timezone.utc)
    seen = set(state.get("seen", []))

    items: list[dict] = []

    for source in config.get("sources", []):
        name, url = source["name"], source["url"]
        try:
            feed = fetch_feed(url)
        except Exception as exc:  # noqa: BLE001 — on ne veut jamais bloquer la boucle
            log(f"❌ {name} — injoignable ({type(exc).__name__})")
            continue

        if not feed.entries:
            log(f"⚠️  {name} — flux vide ou illisible")
            continue

        kept = 0
        for entry in feed.entries:
            link = entry.get("link")
            title = clean_text(entry.get("title", ""), 200)
            if not link or not title:
                continue

            key = canonical(link)
            if key in seen:
                continue

            published = entry_datetime(entry)
            if not ignore_age and published and now - published > max_age:
                continue

            summary = clean_text(entry.get("summary", ""), 300)
            if not is_relevant(title, summary, source, config):
                continue

            seen.add(key)
            items.append({
                "key": key,
                "title": title,
                "link": link,
                "summary": summary,
                "source": name,
                "score": int(source.get("score", 5)),
                "tag": source.get("tag", ""),
                "published": published.isoformat() if published else None,
                "ts": published.timestamp() if published else 0,
            })
            kept += 1

        log(f"✅ {name} — {kept} nouveauté(s) sur {len(feed.entries)} entrées")

    # Les plus fiables d'abord, puis les plus récents
    items.sort(key=lambda i: (-i["score"], -i["ts"]))
    return items[: settings.get("max_items_per_run", 25)]


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def build_embed(item: dict) -> dict:
    color, label = tier_for(item["score"])
    tag = f" · {item['tag']}" if item["tag"] else ""

    embed = {
        "title": item["title"][:250],
        "url": item["link"],
        "color": color,
        "author": {"name": f"{item['source']}{tag}"},
        "footer": {"text": f"{label} — Fiabilité {item['score']}/10"},
    }
    if item["summary"]:
        embed["description"] = item["summary"][:600]
    if item["published"]:
        embed["timestamp"] = item["published"]
    return embed


def post_to_discord(webhook: str, embeds: list[dict]) -> None:
    """Envoie par paquets de 10 (limite Discord), avec gestion du rate-limit."""
    for start in range(0, len(embeds), 10):
        batch = embeds[start:start + 10]
        payload = {"embeds": batch, "username": "CinéWatch", "allowed_mentions": {"parse": []}}

        for attempt in range(4):
            resp = requests.post(webhook, json=payload, timeout=20)
            if resp.status_code == 429:
                wait = resp.json().get("retry_after", 2)
                log(f"⏳ Rate-limit Discord, pause de {wait}s")
                time.sleep(float(wait) + 0.5)
                continue
            if resp.status_code >= 400:
                log(f"❌ Discord {resp.status_code} : {resp.text[:200]}")
                if resp.status_code < 500:
                    break
                time.sleep(2 * (attempt + 1))
                continue
            break
        time.sleep(1.2)


def send_notice(webhook: str, text: str) -> None:
    requests.post(
        webhook,
        json={"content": text, "username": "CinéWatch", "allowed_mentions": {"parse": []}},
        timeout=20,
    )


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def mode_check(config: dict) -> int:
    ok, broken = [], []
    for source in config.get("sources", []):
        name, url = source["name"], source["url"]
        try:
            feed = fetch_feed(url, timeout=20)
            count = len(feed.entries)
            if count:
                ok.append(f"  ✅ {name:<32} {count:>3} entrées")
            else:
                broken.append(f"  ⚠️  {name:<32} flux vide / non parsable")
        except Exception as exc:  # noqa: BLE001
            broken.append(f"  ❌ {name:<32} {type(exc).__name__}: {str(exc)[:70]}")

    print("\n=== FLUX FONCTIONNELS ===")
    print("\n".join(ok) or "  (aucun)")
    print("\n=== À CORRIGER OU SUPPRIMER ===")
    print("\n".join(broken) or "  (aucun) 🎉")
    print(f"\n{len(ok)} OK / {len(ok) + len(broken)} sources\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CinéWatch")
    parser.add_argument("--seed", action="store_true",
                        help="marque tout comme vu sans notifier")
    parser.add_argument("--check", action="store_true",
                        help="teste la validité de chaque flux")
    parser.add_argument("--dry-run", action="store_true",
                        help="affiche sans poster sur Discord")
    args = parser.parse_args()

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    if args.check:
        return mode_check(config)

    webhook_main = os.environ.get("DISCORD_WEBHOOK_NEWS", "").strip()
    webhook_rumors = os.environ.get("DISCORD_WEBHOOK_RUMORS", "").strip() or webhook_main

    if not webhook_main and not args.dry_run:
        log("❌ DISCORD_WEBHOOK_NEWS n'est pas défini.")
        return 1

    state = load_state()
    first_run = not state.get("initialized")

    items = collect(config, state, ignore_age=args.seed or first_run)

    # Mémoriser avant d'envoyer : en cas de crash à l'envoi, on ne spammera pas
    state["seen"] = list(dict.fromkeys(state.get("seen", []) + [i["key"] for i in items]))

    if args.seed or first_run:
        state["initialized"] = True
        save_state(state)
        log(f"🌱 Amorçage terminé : {len(items)} articles marqués comme vus, rien envoyé.")
        if webhook_main and not args.dry_run:
            send_notice(
                webhook_main,
                "**CinéWatch est en ligne.** 🎬\n"
                f"{len(items)} articles existants ignorés. "
                "Les prochaines publications arriveront ici.",
            )
        return 0

    if not items:
        save_state(state)
        log("😴 Rien de neuf.")
        return 0

    threshold = config.get("settings", {}).get("rumor_threshold", 7)
    officiel = [i for i in items if i["score"] >= threshold]
    rumeurs = [i for i in items if i["score"] < threshold]

    if args.dry_run:
        for item in items:
            print(f"  [{item['score']}/10] {item['source']} — {item['title']}\n        {item['link']}")
        log(f"🧪 Dry-run : {len(items)} article(s), rien envoyé.")
        return 0

    if officiel:
        post_to_discord(webhook_main, [build_embed(i) for i in officiel])
    if rumeurs:
        post_to_discord(webhook_rumors, [build_embed(i) for i in rumeurs])

    save_state(state)
    log(f"📨 Envoyé : {len(officiel)} officiel(s), {len(rumeurs)} rumeur(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
