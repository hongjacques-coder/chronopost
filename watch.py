#!/usr/bin/env python3
"""Surveille un colis Chronopost et notifie Discord à chaque nouvel événement.

Source : le web service public de suivi Chronopost (trackSkybillV2), qui renvoie
du XML structuré. La page www.chronopost.fr/tracking-no-cms/... est derrière un
challenge Cloudflare et n'est pas utilisable sans navigateur.

Bibliothèque standard uniquement (Python >= 3.8).

Variables d'environnement :
  TRACKING_NUMBER      numéro de suivi (défaut : XW609350053TS)
  DISCORD_WEBHOOK_URL  webhook Discord (obligatoire sauf en --dry-run)
  STATE_FILE           fichier d'état JSON (défaut : state.json)
  INTERVAL             secondes entre deux vérifications en mode boucle (défaut : 30)
  DURATION             durée totale de la boucle en secondes (défaut : 0 = une seule vérification)

Usage :
  python watch.py --dry-run            # une vérification, affiche le message au lieu de l'envoyer
  DURATION=18000 python watch.py       # boucle pendant 5 h, une vérification toutes les 30 s
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

API_URL = "https://ws.chronopost.fr/tracking-cxf/TrackingServiceWS/trackSkybillV2"
PAGE_URL = "https://www.chronopost.fr/tracking-no-cms/suivi-page?langue=fr&listeNumerosLT={}"
USER_AGENT = "chronopost-watcher/1.0 (+suivi personnel d'un colis)"
DELIVERED_PREFIXES = ("Colis livré", "Livraison effectuée")


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fetch_events(number, timeout=20):
    """Renvoie la liste des événements, du plus ancien au plus récent."""
    query = urllib.parse.urlencode({"language": "fr_FR", "skybillNumber": number})
    req = urllib.request.Request(f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        root = ET.fromstring(resp.read())

    ret = next(root.iter("return"), None)
    if ret is None:
        raise RuntimeError("réponse inattendue (pas de <return>)")
    error_code = (ret.findtext("errorCode") or "").strip()
    if error_code not in ("", "0"):
        raise RuntimeError(f"errorCode={error_code}")

    events = []
    for ev in ret.iter("events"):
        events.append({
            "code": (ev.findtext("code") or "").strip(),
            "date": (ev.findtext("eventDate") or "").strip(),
            "label": (ev.findtext("eventLabel") or "").strip(),
            "office": (ev.findtext("officeLabel") or "").strip(),
            "zip": (ev.findtext("zipCode") or "").strip(),
            "info": {
                (ic.findtext("name") or "").strip(): (ic.findtext("value") or "").strip()
                for ic in ev.findall("infoCompList")
            },
        })
    events.sort(key=lambda e: e["date"])
    return events


def event_key(ev):
    return f"{ev['date']}|{ev['code']}|{ev['label']}"


def fingerprint(events):
    return hashlib.sha256("\n".join(event_key(e) for e in events).encode()).hexdigest()


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def format_date(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return iso


def build_payload(number, new_events, first_run):
    title = "Suivi Chronopost démarré" if first_run else "Nouvel événement Chronopost"
    embeds = []
    for ev in new_events[-10:]:  # Discord : 10 embeds max par message
        lines = [f"**{format_date(ev['date'])}**"]
        place = ev["office"]
        if ev["zip"] and ev["zip"] not in place:
            place = f"{place} ({ev['zip']})"
        if place:
            lines.append(place)
        lines += [f"{k} : {v}" for k, v in ev["info"].items() if k and v]
        delivered = ev["label"].startswith(DELIVERED_PREFIXES)
        embeds.append({
            "title": ev["label"][:256] or ev["code"],
            "description": "\n".join(lines)[:4000],
            "color": 0x2ECC71 if delivered else 0x1F6FEB,
        })
    return {
        "content": f"📦 {title} — [{number}]({PAGE_URL.format(number)})",
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }


def post_discord(url, payload, retries=3):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=20):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                retry_after = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                time.sleep(retry_after)
                continue
            raise


def check_once(number, state_path, webhook, dry_run):
    """Une vérification. Renvoie True si un changement a été notifié."""
    events = fetch_events(number)
    if not events:
        log("aucun événement renvoyé (numéro inconnu ou pas encore flashé) ; état inchangé")
        return False

    state = load_state(state_path)
    fp = fingerprint(events)
    if state.get("number") == number and state.get("fingerprint") == fp:
        log(f"inchangé : {events[-1]['label']} ({format_date(events[-1]['date'])})")
        return False

    first_run = state.get("number") != number or not state.get("seen")
    seen = set(state.get("seen", [])) if not first_run else set()
    new_events = [e for e in events if event_key(e) not in seen]
    if first_run:
        new_events = events[-1:]  # au démarrage, seulement le statut actuel
    if new_events:
        payload = build_payload(number, new_events, first_run)
        if dry_run or not webhook:
            log("dry-run, message Discord qui serait envoyé :")
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        else:
            post_discord(webhook, payload)
            log(f"notifié : {', '.join(e['label'] for e in new_events)}")
    else:
        log("empreinte modifiée sans nouvel événement (réordonnancement ?) ; pas de notification")

    # L'état n'est écrit qu'après l'envoi : en cas d'échec, on réessaiera au tour suivant.
    save_state(state_path, {
        "number": number,
        "fingerprint": fp,
        "seen": [event_key(e) for e in events],
        "last": events[-1],
        "updated": datetime.now().isoformat(timespec="seconds"),
    })
    return bool(new_events)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="affiche le message au lieu de l'envoyer")
    ap.add_argument("--number", default=os.environ.get("TRACKING_NUMBER") or "XW609350053TS")
    ap.add_argument("--state", default=os.environ.get("STATE_FILE") or "state.json")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("INTERVAL") or 30))
    ap.add_argument("--duration", type=float, default=float(os.environ.get("DURATION") or 0))
    args = ap.parse_args()

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and not args.dry_run:
        sys.exit("DISCORD_WEBHOOK_URL manquant (ou utiliser --dry-run)")

    deadline = time.monotonic() + args.duration
    failures = 0
    while True:
        try:
            check_once(args.number, args.state, webhook, args.dry_run)
            failures = 0
        except Exception as e:  # réseau, XML, Discord : on loggue et on continue
            failures += 1
            log(f"erreur ({failures} d'affilée) : {type(e).__name__}: {e}")
            if args.duration <= 0:
                sys.exit(1)
        if time.monotonic() + args.interval >= deadline:
            break
        # Recul progressif si l'API échoue en boucle (max 5 min).
        time.sleep(min(args.interval * (2 ** min(failures, 4)), 300) if failures else args.interval)


if __name__ == "__main__":
    main()
