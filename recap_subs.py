"""Recap quotidien des abonnes d'un canal Telegram, poste dans un salon Discord a 21 h (Paris).

Tourne chez GitHub Actions (.github/workflows/recap.yml), donc sans le Mac. Sans etat :
tout est relu dans le journal d'administration du canal (Telegram le garde ~48 h).

    python recap_subs.py --dry-run                 # affiche le message, ne poste rien
    python recap_subs.py --force                   # poste tout de suite, meme hors 21 h
    python recap_subs.py --day 2026-09-14          # journee complete d'un jour passe
    python recap_subs.py --session-file sessions/echanges   # test local avec la session du Mac

Variables : TG_API_ID, TG_API_HASH, TG_SESSION_STRING (ou --session-file), DISCORD_WEBHOOK_URL,
TG_SUBS_CHANNEL (morceau du titre du canal), TZ_NAME (defaut Europe/Paris).
"""

import argparse
import asyncio
import json
import os
import re
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetAdminLogRequest
from telethon.tl.functions.stats import GetBroadcastStatsRequest, LoadAsyncGraphRequest
from telethon.tl.types import ChannelAdminLogEventsFilter

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Paris"))
CHANNEL_HINT = os.environ["TG_SUBS_CHANNEL"]   # secret : morceau du titre du canal
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet",
        "aout", "septembre", "octobre", "novembre", "decembre"]
# Meme nomenclature que subs_ingest.py : PLATEFORME-compte-Recruteur-code
PLATFORMS = {"ig": "Instagram", "insta": "Instagram", "fb": "Facebook", "th": "Threads",
             "tt": "TikTok", "tk": "TikTok", "tiktok": "TikTok", "sc": "Snapchat",
             "x": "X", "tw": "X", "yt": "YouTube", "rd": "Reddit"}
OVERRIDES = {"meitacrush": "Telephone physique", "tachefmei": "Telephone physique", "tik tok": "TikTok"}


def platform_of(title: str | None) -> str:
    if not title:
        return "Sans lien"
    label = re.sub(r"[\[\]()*`]|https?://\S+", "", title).strip()
    if (p := OVERRIDES.get(label.lower())):
        return p
    parts = [p for p in label.split("-") if p]
    if len(parts) >= 2 and parts[0].lower() in PLATFORMS:
        return PLATFORMS[parts[0].lower()]
    return "Hors nomenclature"


# --------------------------------------------------------------------------- Telegram

async def find_channel(c):
    async for d in c.iter_dialogs():
        if CHANNEL_HINT.lower() in (d.name or "").lower():
            return d.entity
    raise SystemExit("Canal introuvable : verifie le secret TG_SUBS_CHANNEL.")


async def fetch_events(c, chan, since_utc: datetime) -> list[tuple[datetime, str, str | None]]:
    """Tous les join/leave depuis since_utc, du plus recent au plus ancien."""
    flt = ChannelAdminLogEventsFilter(join=True, leave=True, invite=True)
    max_id, out = 0, []
    for _ in range(150):                      # 15 000 evenements max, largement au-dessus d'un gros jour
        r = await c(GetAdminLogRequest(channel=chan, q="", min_id=0, max_id=max_id, limit=100,
                                       events_filter=flt, admins=[]))
        if not r.events:
            break
        older = False
        for e in r.events:
            if e.date < since_utc:
                older = True
                continue
            kind = type(e.action).__name__
            if "Join" in kind:
                action = "join"
            elif "Leave" in kind:
                action = "leave"
            else:
                continue
            inv = getattr(e.action, "invite", None)
            out.append((e.date.astimezone(TZ), action, getattr(inv, "title", None) if inv else None))
        max_id = min(e.id for e in r.events)
        if older:
            break
    return out


async def audience_languages(c, chan) -> tuple[dict, int]:
    """Repartition par langue des personnes ayant vu le canal aujourd'hui (dernier point du graphe)."""
    st = await c(GetBroadcastStatsRequest(channel=chan))
    g = st.languages_graph
    if getattr(g, "token", None):
        g = await c(LoadAsyncGraphRequest(token=g.token))
    raw = getattr(g, "json", None)
    if raw is None:
        return {}, 0
    data = json.loads(raw.data)
    names = data.get("names", {})
    counts = {}
    for col in data.get("columns", [])[1:]:
        vals = [v for v in col[1:] if isinstance(v, (int, float))]
        if vals:
            counts[names.get(col[0], col[0])] = vals[-1]
    total = int(sum(counts.values()))
    shares = {k: round(100 * v / total, 1) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])} if total else {}
    return shares, total


# --------------------------------------------------------------------------- Rendu

def stats(events, day, until: datetime | None) -> dict:
    sel = [e for e in events if e[0].date() == day and (until is None or e[0] <= until)]
    joins = [e for e in sel if e[1] == "join"]
    leaves = [e for e in sel if e[1] == "leave"]
    by_link = Counter((t or "Non attribue") for _, _, t in joins)
    by_platform = Counter(platform_of(t) for _, _, t in joins)
    return {"joins": len(joins), "leaves": len(leaves), "net": len(joins) - len(leaves),
            "by_link": by_link.most_common(25), "by_platform": by_platform.most_common()}


def build_embed(today: dict, hier: dict, day, now: datetime, full_day: bool,
                langs: dict, audience: int) -> dict:
    lignes = [
        f"📈 **{today['joins']}** nouveaux subs",
        f"📉 **{today['leaves']}** departs",
        f"⚖️ Variation nette : **{today['net']:+d}**",
    ]
    if hier["joins"]:
        ecart = today["joins"] - hier["joins"]
        quand = "la veille" if full_day else "hier a la meme heure"
        lignes.append(f"↔️ vs **{hier['joins']}** {quand} ({'+' if ecart >= 0 else ''}{ecart})")
    if today["by_platform"]:
        lignes.append("")
        lignes.append("**Par plateforme :** " + " · ".join(
            f"{p} **{n}** ({100 * n // max(today['joins'], 1)} %)" for p, n in today["by_platform"]))
    lignes.append("")
    lignes.append("**Detail par lien :**")
    for k, n in today["by_link"]:
        lignes.append(f"• **{n}** — {k}")
    if not today["by_link"]:
        lignes.append("_aucune entree_")
    if langs:
        top = list(langs.items())[:7]
        lignes.append("")
        lignes.append(f"**Langue de l'audience du jour** ({audience} personnes) — "
                      "ceux qui ont vu le canal, pas les nouveaux subs :")
        lignes.append(" · ".join(f"{k} **{v}%**" for k, v in top))

    if full_day:
        title = f"📅 {JOURS[day.weekday()].capitalize()} {day.day} {MOIS[day.month - 1]} — journee complete"
    else:
        title = f"🌙 Recap 21 h — {JOURS[day.weekday()]} {day.day} {MOIS[day.month - 1]}"
    return {
        "title": title,
        "description": "\n".join(lignes)[:4000],
        "color": 0x3DDC97 if today["net"] >= 0 else 0xFF6B6B,
        "footer": {"text": f"Recap quotidien · comptage brut du journal Telegram, leger ecart avec "
                           f"le recap officiel · poste a {now:%H:%M} depuis GitHub Actions"},
        "timestamp": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def render_text(embed: dict) -> str:
    return "\n".join([embed["title"], embed["description"], "", embed["footer"]["text"]])


def post(embed: dict) -> str:
    url = os.environ["DISCORD_WEBHOOK_URL"].rstrip("/") + "?wait=true"
    req = urllib.request.Request(url, data=json.dumps({"embeds": [embed]}).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (recap-subs-mei, 1.0)"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode()).get("id", "?")


# --------------------------------------------------------------------------- Main

async def run(args) -> None:
    now = datetime.now(TZ)
    if args.day:
        day = datetime.strptime(args.day, "%Y-%m-%d").date()
        full_day = True
    else:
        day, full_day = now.date(), False
        if not args.force and not args.dry_run and now.hour != 21:
            print(f"Il est {now:%H:%M} a Paris, pas 21 h : rien a faire (les deux crons UTC couvrent ete/hiver).")
            return

    api_id, api_hash = int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]
    session = args.session_file or StringSession(os.environ["TG_SESSION_STRING"])
    c = TelegramClient(session, api_id, api_hash)
    await c.connect()
    try:
        if not await c.is_user_authorized():
            raise SystemExit("Session Telegram non autorisee : refais login_cloud.py et le secret TG_SESSION_STRING.")
        chan = await find_channel(c)
        veille = day - timedelta(days=1)
        since = datetime.combine(veille, datetime.min.time(), TZ).astimezone(timezone.utc)
        events = await fetch_events(c, chan, since)
        until = None if full_day else now
        today = stats(events, day, until)
        hier = stats(events, veille, None if full_day else now - timedelta(days=1))
        langs, audience = {}, 0
        if not full_day:
            try:
                langs, audience = await audience_languages(c, chan)
            except Exception as e:  # les stats ne sont pas vitales
                print(f"langues indisponibles ({type(e).__name__})")
    finally:
        await c.disconnect()

    embed = build_embed(today, hier, day, now, full_day, langs, audience)
    print("Journal Telegram lu.")
    if args.dry_run:
        print(render_text(embed))
        return
    post(embed)
    print("Recap poste dans Discord.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="poster meme hors 21 h")
    ap.add_argument("--day", help="YYYY-MM-DD : journee complete d'un jour passe")
    ap.add_argument("--session-file", help="session SQLite locale (test), sinon TG_SESSION_STRING")
    args = ap.parse_args()
    if os.getenv("FORCE") == "1":
        args.force = True
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
