"""
ABI Tracker — Bot d'alerte de prix (Arena Breakout Infinite)

Parcourt toutes les catégories du marché, extrait le prix "plus bas" de
chaque item, compare avec le run précédent (stocké dans prices.json),
et envoie une alerte Discord si la variation dépasse le seuil défini.

Lancement local :
    pip install playwright requests
    playwright install chromium
    python bot.py
"""

import json
import os
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_URL = "https://abi-tracker.azurewebsites.net/Market/View?minorId={}"
HISTORY_FILE = Path("price_history.json")
THRESHOLD_PCT_SHORT = 15  # % de variation vs il y a 30min
THRESHOLD_PCT_LONG = 25   # % de variation vs il y a 2h (fenêtre complète)
HISTORY_LENGTH = 4  # nb de snapshots gardés (4 x 30min = 2h de recul)
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")

MINOR_IDS = [
    "10101", "10102", "10103", "10104", "10105", "10106", "10108", "10201", "104",
    "20101", "20102", "20103", "20104", "20105", "20106", "20107", "20108", "20110",
    "20111", "20112", "20114", "20115", "20116",
    "20201", "20202", "20203", "20204", "20205", "20206", "20208", "20209", "20210",
    "20212", "20213", "20214", "20215", "20217",
    "3010101", "3010102", "30102", "30103", "30104", "30105", "30106", "3011502",
    "40101", "40103", "40104", "40105",
    "40401", "40402",
    "40501", "40502", "40503", "40504", "40505", "40506", "40507",
    "40801", "40802", "40803", "40804", "40805", "40806", "40807", "40808", "40809",
    "40810", "40812", "40813", "40814", "40815",
]


def scrape_all(page):
    """Parcourt toutes les catégories et retourne {item_name: prix_int}."""
    results = {}
    for minor_id in MINOR_IDS:
        url = BASE_URL.format(minor_id)
        try:
            page.goto(url, wait_until="networkidle", timeout=20000)
            page.wait_for_selector(".market-item-card", timeout=8000)
        except Exception as e:
            print(f"  [!] {minor_id}: skip ({e})")
            continue

        cards = page.query_selector_all(".market-item-card")
        for card in cards:
            name_el = card.query_selector(".market-item-name")
            price_el = card.query_selector(".market-item-lowest-value")
            if not name_el or not price_el:
                continue
            name = name_el.inner_text().strip()
            price_text = price_el.inner_text().strip().replace(",", "").replace(" ", "")
            if price_text in ("--", "", "暫無價格資料"):
                continue
            try:
                price = int(price_text)
            except ValueError:
                continue
            # Préfixe par minorId pour éviter les collisions de noms entre catégories
            results[f"{minor_id}::{name}"] = price

        time.sleep(0.3)  # petite pause polie entre les requêtes
    return results


def load_history():
    """Retourne la liste des anciens snapshots [{'prices': {...}}, ...],
    du plus ancien au plus récent."""
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return []


def save_history(history, current):
    history.append({"prices": current})
    # ne garde que les HISTORY_LENGTH derniers snapshots
    history = history[-HISTORY_LENGTH:]
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def send_discord_alert(changes):
    if not DISCORD_WEBHOOK:
        print("[!] Pas de webhook Discord configuré, alerte non envoyée.")
        return

    lines = []
    for key, old, new, pct, window in changes:
        _, name = key.split("::", 1)
        arrow = "📈" if pct > 0 else "📉"
        lines.append(f"{arrow} **{name}** : {old} → {new} ({pct:+.1f}% / {window})")

    # Discord limite les messages à 2000 caractères, on découpe si besoin
    chunk = []
    length = 0
    for line in lines:
        if length + len(line) > 1800:
            _post_discord("\n".join(chunk))
            chunk, length = [], 0
        chunk.append(line)
        length += len(line)
    if chunk:
        _post_discord("\n".join(chunk))


def _post_discord(content):
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
    except Exception as e:
        print(f"[!] Erreur envoi Discord: {e}")


def main():
    print("Scraping en cours...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        current = scrape_all(page)
        browser.close()

    print(f"{len(current)} items récupérés.")
    history = load_history()

    changes = []
    seen = set()  # évite de doubler un item alerté par les 2 paliers

    if history:
        # Palier COURT : vs le snapshot précédent (30 min)
        prev_short = history[-1]["prices"]
        for key, new_price in current.items():
            old_price = prev_short.get(key)
            if old_price is None or old_price == 0:
                continue
            pct = (new_price - old_price) / old_price * 100
            if abs(pct) >= THRESHOLD_PCT_SHORT:
                changes.append((key, old_price, new_price, pct, "30min"))
                seen.add(key)

        # Palier LONG : vs le snapshot le plus ancien (jusqu'à 2h)
        oldest = history[0]["prices"]
        for key, new_price in current.items():
            if key in seen:
                continue  # déjà signalé par le palier court
            old_price = oldest.get(key)
            if old_price is None or old_price == 0:
                continue
            pct = (new_price - old_price) / old_price * 100
            if abs(pct) >= THRESHOLD_PCT_LONG:
                changes.append((key, old_price, new_price, pct, "2h"))

    if changes:
        print(f"{len(changes)} variation(s) détectée(s), envoi alerte Discord.")
        send_discord_alert(changes)
    else:
        print("Aucune variation significative.")

    save_history(history, current)


if __name__ == "__main__":
    main()
