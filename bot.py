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
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_URL = "https://abi-tracker.azurewebsites.net/Market/View?minorId={}"
HISTORY_FILE = Path("price_history.json")
WEEKLY_ANCHOR_FILE = Path("weekly_anchor.json")
WEEKLY_STATE_FILE = Path("weekly_state.json")
THRESHOLD_PCT_SHORT = 15  # % de variation vs il y a 30min
THRESHOLD_PCT_LONG = 25   # % de variation vs il y a 2h (fenêtre complète)
HISTORY_LENGTH = 4  # nb de snapshots gardés (4 x 30min = 2h de recul)
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_WEBHOOK_WEEKLY = os.environ.get("DISCORD_WEBHOOK_WEEKLY_URL", "")
WEEKLY_REPORT_HOUR_UTC = 9  # heure à laquelle envoyer le rapport journalier

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
    # Force la langue anglaise (sinon les noms sont en chinois)
    try:
        page.goto(
            "https://abi-tracker.azurewebsites.net/Home/SetLanguage?lang=en&returnUrl=%2FMarket%2FView",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"  [!] Impossible de forcer la langue anglaise: {e}")

    results = {}
    first_failure_logged = False
    for minor_id in MINOR_IDS:
        url = BASE_URL.format(minor_id)
        cards = []
        for attempt in range(2):  # jusqu'à 2 essais par catégorie
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)  # laisse le JS afficher les prix
                page.wait_for_selector(".market-item-card", timeout=15000)
                cards = page.query_selector_all(".market-item-card")
                break
            except Exception as e:
                if attempt == 1:
                    print(f"  [!] {minor_id}: skip ({e})")
                    if not first_failure_logged:
                        first_failure_logged = True
                        print(f"  [debug] URL actuelle: {page.url}")
                        print(f"  [debug] Titre page: {page.title()}")
                        print(f"  [debug] 500 premiers caractères du body: {page.content()[:500]}")

        cards = page.query_selector_all(".market-item-card")
        for card in cards:
            name_el = card.query_selector(".market-item-name")
            price_el = card.query_selector(".market-item-lowest-value")
            if not name_el or not price_el:
                continue
            name = name_el.inner_text().strip()
            price_text = price_el.inner_text().strip()
            # Supprime tous types d'espaces (normal, insécable \xa0, fine insécable \u202f, etc.)
            price_text = "".join(ch for ch in price_text if not ch.isspace())
            price_text = price_text.replace(",", "")
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


TIERS = [100, 75, 50, 25, 15]  # bornes des tranches (%)

# Couleurs ANSI par tranche (plus fort = plus vif)
# code = (fg_gain, fg_drop) — 92=vert vif, 32=vert normal, 91=rouge vif, 31=rouge normal
TIER_COLORS = {
    100: ("1;92", "1;91"),  # bold bright green / red
    75: ("92", "91"),       # bright green / red
    50: ("1;32", "1;31"),   # bold green / red
    25: ("32", "31"),       # green / red
    15: ("2;32", "2;31"),   # dim green / red
}


def _tier_bound(pct_abs):
    for t in TIERS:
        if pct_abs >= t:
            return t
    return 15


def _ansi(code, text):
    return f"\u001b[{code}m{text}\u001b[0m"


def send_discord_alert(changes):
    if not DISCORD_WEBHOOK:
        print("[!] Pas de webhook Discord configuré, alerte non envoyée.")
        return

    short = [c for c in changes if c[4] == "30min"]
    long_ = [c for c in changes if c[4] == "2h"]

    messages = []  # liste de strings, chacune sera un message Discord séparé

    def build_section(title, items):
        block_lines = []
        if not items:
            return block_lines
        block_lines.append(f"=== {title} ===")
        gains = sorted([c for c in items if c[3] > 0], key=lambda c: c[3], reverse=True)
        drops = sorted([c for c in items if c[3] < 0], key=lambda c: c[3])

        def add_subsection(sub_title, sub_items, is_gain):
            if not sub_items:
                return
            block_lines.append(f"--- {sub_title} ---")
            name_width = max(len(k.split("::", 1)[1]) for k, *_ in sub_items)
            current_tier = None
            for key, old, new, pct, window in sub_items:
                tier = _tier_bound(abs(pct))
                if tier != current_tier:
                    block_lines.append(f"[{tier}%+]")
                    current_tier = tier
                _, name = key.split("::", 1)
                fg_gain, fg_drop = TIER_COLORS[tier]
                color = fg_gain if is_gain else fg_drop
                pct_str = f"{pct:+.1f}%"
                line = f"{name:<{name_width}}  {old:>7} -> {new:<7} {pct_str:>8}"
                block_lines.append(_ansi(color, line))

        add_subsection("Hausses / Gains", gains, True)
        add_subsection("Baisses / Drops", drops, False)
        return block_lines

    all_lines = []
    all_lines += build_section("VARIATIONS 30 MIN / 30 MIN CHANGES", short)
    all_lines += build_section("VARIATIONS 2H / 2H CHANGES", long_)

    # Découpe en blocs ```ansi ... ``` de max ~1900 caractères
    chunk = []
    length = 8  # marge pour les balises ```ansi \n ... \n```
    for line in all_lines:
        if length + len(line) > 1900:
            messages.append("```ansi\n" + "\n".join(chunk) + "\n```")
            chunk, length = [], 8
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        messages.append("```ansi\n" + "\n".join(chunk) + "\n```")

    for msg in messages:
        _post_discord(msg)


def _post_discord(content, webhook=None):
    hook = webhook or DISCORD_WEBHOOK
    try:
        requests.post(hook, json={"content": content}, timeout=10)
    except Exception as e:
        print(f"[!] Erreur envoi Discord: {e}")


def load_weekly_anchor():
    if WEEKLY_ANCHOR_FILE.exists():
        return json.loads(WEEKLY_ANCHOR_FILE.read_text(encoding="utf-8"))
    return None


def save_weekly_anchor(monday_date, prices):
    WEEKLY_ANCHOR_FILE.write_text(
        json.dumps({"date": monday_date, "prices": prices}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_weekly_state():
    if WEEKLY_STATE_FILE.exists():
        return json.loads(WEEKLY_STATE_FILE.read_text(encoding="utf-8"))
    return {"last_sent_date": None}


def save_weekly_state(state):
    WEEKLY_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def handle_weekly_report(current):
    if not DISCORD_WEBHOOK_WEEKLY:
        return  # pas configuré, on ignore silencieusement

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    monday_str = (now.date().isoformat()
                  if now.weekday() == 0
                  else None)

    anchor = load_weekly_anchor()

    # Chaque lundi (première exécution du jour), on repart d'une nouvelle référence
    if now.weekday() == 0 and (anchor is None or anchor["date"] != today_str):
        save_weekly_anchor(today_str, current)
        anchor = {"date": today_str, "prices": current}
        # Pas d'alerte le jour même de la remise à zéro : rien à comparer

    if anchor is None:
        # Pas encore de référence (premier lancement avant le premier lundi)
        return

    state = load_weekly_state()
    if state.get("last_sent_date") == today_str:
        return  # déjà envoyé aujourd'hui
    if now.hour < WEEKLY_REPORT_HOUR_UTC:
        return  # trop tôt dans la journée

    if anchor["date"] == today_str:
        # C'est lundi et on vient tout juste de poser la référence : rien à comparer
        state["last_sent_date"] = today_str
        save_weekly_state(state)
        return

    changes = []
    for key, new_price in current.items():
        old_price = anchor["prices"].get(key)
        if old_price is None or old_price == 0:
            continue
        pct = (new_price - old_price) / old_price * 100
        changes.append((key, old_price, new_price, pct))

    send_weekly_report(anchor["date"], today_str, changes)
    state["last_sent_date"] = today_str
    save_weekly_state(state)


def send_weekly_report(monday_date, today_date, changes):
    gains = sorted([c for c in changes if c[3] > 0], key=lambda c: c[3], reverse=True)
    drops = sorted([c for c in changes if c[3] < 0], key=lambda c: c[3])

    lines = [f"───────── {today_date} (réf. lundi {monday_date}) / ref. Monday {monday_date} ─────────"]

    def add_subsection(sub_title, sub_items, is_gain):
        if not sub_items:
            return
        lines.append(f"--- {sub_title} ---")
        name_width = max(len(k.split("::", 1)[1]) for k, *_ in sub_items)
        current_tier = None
        for key, old, new, pct in sub_items:
            tier = _tier_bound(abs(pct))
            if tier != current_tier:
                lines.append(f"[{tier}%+]")
                current_tier = tier
            _, name = key.split("::", 1)
            fg_gain, fg_drop = TIER_COLORS[tier]
            color = fg_gain if is_gain else fg_drop
            pct_str = f"{pct:+.1f}%"
            line = f"{name:<{name_width}}  {old:>7} -> {new:<7} {pct_str:>8}"
            lines.append(_ansi(color, line))

    add_subsection("Hausses / Gains", gains, True)
    add_subsection("Baisses / Drops", drops, False)

    if len(lines) == 1:
        lines.append("(aucune variation notable / no notable change)")

    chunk = []
    length = 8
    messages = []
    for line in lines:
        if length + len(line) > 1900:
            messages.append("```ansi\n" + "\n".join(chunk) + "\n```")
            chunk, length = [], 8
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        messages.append("```ansi\n" + "\n".join(chunk) + "\n```")

    for msg in messages:
        _post_discord(msg, webhook=DISCORD_WEBHOOK_WEEKLY)


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
    handle_weekly_report(current)


if __name__ == "__main__":
    main()
