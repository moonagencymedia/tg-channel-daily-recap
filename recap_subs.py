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
    """Le canal dont le titre contient le secret. Un canal miroir peut porter le meme titre :
    a titre egal, on garde celui qui a le plus d'abonnes."""
    from telethon.tl.functions.channels import GetFullChannelRequest
    matches = [d.entity async for d in c.iter_dialogs()
               if d.is_channel and CHANNEL_HINT.lower() in (d.name or "").lower()]
    if not matches:
        raise SystemExit("Canal introuvable : verifie le secret TG_SUBS_CHANNEL.")
    if len(matches) == 1:
        return matches[0]
    sizes = [(await c(GetFullChannelRequest(ch))).full_chat.participants_count or 0 for ch in matches]
    return max(zip(sizes, matches), key=lambda sm: sm[0])[1]


async def fetch_events(c, chan, since_utc: datetime):
    """(evenements, couvert) : tous les join/leave depuis since_utc. `couvert` est faux si le journal
    Telegram (~48 h de retention) s'arrete AVANT since_utc : la periode la plus ancienne est alors
    incomplete et ne doit servir a aucune comparaison."""
    flt = ChannelAdminLogEventsFilter(join=True, leave=True, invite=True)
    max_id, out, covered = 0, [], False
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
            covered = True
            break
    return out, covered


async def audience_languages(c, chan, day) -> tuple[dict, int]:
    """Repartition par langue des personnes ayant vu le canal LE JOUR VISE (point date du graphe).
    Rien si ce jour n'est pas dans le graphe : jamais le point d'un autre jour sous la mauvaise etiquette."""
    st = await c(GetBroadcastStatsRequest(channel=chan))
    g = st.languages_graph
    if getattr(g, "token", None):
        g = await c(LoadAsyncGraphRequest(token=g.token))
    raw = getattr(g, "json", None)
    if raw is None:
        return {}, 0
    data = json.loads(raw.data)
    names = data.get("names", {})
    cols = data.get("columns", [])
    xs = next((col[1:] for col in cols if col[0] == "x"), [])
    days = [datetime.fromtimestamp(t / 1000, tz=timezone.utc).date() for t in xs]
    if day not in days:
        return {}, 0
    i = days.index(day)
    counts = {names.get(col[0], col[0]): col[1:][i] for col in cols
              if col[0] != "x" and i < len(col) - 1 and isinstance(col[1:][i], (int, float))}
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

CUTOFF_HOUR = 21   # les chiffres sont arretes a 21 h (Paris), quelle que soit l'heure reelle d'execution


def choose_target(now: datetime, done_today: bool, done_yesterday: bool):
    """(jour, borne) a poster, ou None. GitHub lance les crons avec 0 a 3 h de retard : on ne se fie
    donc jamais a l'heure d'execution. Apres 21 h -> le jour meme ; entre minuit et midi -> rattrapage
    de la veille si elle n'a pas ete postee. La borne est toujours 21 h du jour vise."""
    today = now.date()
    if now.hour >= CUTOFF_HOUR and not done_today:
        return today, datetime.combine(today, datetime.min.time(), TZ).replace(hour=CUTOFF_HOUR)
    if now.hour < 12 and not done_yesterday:
        y = today - timedelta(days=1)
        return y, datetime.combine(y, datetime.min.time(), TZ).replace(hour=CUTOFF_HOUR)
    return None


async def run(args) -> None:
    now = datetime.fromisoformat(args.now).replace(tzinfo=TZ) if args.now else datetime.now(TZ)
    scheduled = False
    if args.day:
        day, until, full_day = datetime.strptime(args.day, "%Y-%m-%d").date(), None, True
    elif args.force or args.dry_run and not args.now:
        day, until, full_day = now.date(), now, False
    else:
        target = choose_target(now, os.getenv("DONE_TODAY") == "true", os.getenv("DONE_YESTERDAY") == "true")
        if target is None:
            print(f"Il est {now:%H:%M} a Paris : rien a poster (avant {CUTOFF_HOUR} h, ou recap deja fait).")
            return
        (day, until), full_day, scheduled = target, False, True

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
        events, veille_complete = await fetch_events(c, chan, since)
        today = stats(events, day, until)
        hier = stats(events, veille, None if full_day else until - timedelta(days=1))
        langs, audience = {}, 0
        if not full_day:
            try:
                langs, audience = await audience_languages(c, chan, day)
            except Exception as e:  # les stats ne sont pas vitales
                print(f"langues indisponibles ({type(e).__name__})")
    finally:
        await c.disconnect()

    if not veille_complete:
        # veille tronquee par la retention Telegram : pas de comparaison plutot qu'un chiffre faux
        hier = {**hier, "joins": 0}
        print("Comparaison avec la veille omise : journal Telegram incomplet pour la veille.")
    embed = build_embed(today, hier, day, now, full_day, langs, audience)
    if not full_day:
        embed["footer"]["text"] = (f"Chiffres arretes a {until:%H:%M} le {until:%d/%m} · poste a {now:%H:%M} le "
                                   f"{now:%d/%m} · comptage brut du journal Telegram")
    print("Journal Telegram lu.")
    if args.dry_run:
        print(render_text(embed))
        return
    post(embed)
    print("Recap poste dans Discord.")
    if scheduled and os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"posted_day={day:%Y-%m-%d}\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="poster meme hors 21 h")
    ap.add_argument("--day", help="YYYY-MM-DD : journee complete d'un jour passe")
    ap.add_argument("--session-file", help="session SQLite locale (test), sinon TG_SESSION_STRING")
    ap.add_argument("--now", help="AAAA-MM-JJTHH:MM : simule l'heure de Paris (test de la logique horaire)")
    args = ap.parse_args()
    if os.getenv("FORCE") == "1":
        args.force = True
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
