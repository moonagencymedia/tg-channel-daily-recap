"""Miroir d'un canal Telegram vers un autre, sans machine allumee ni base de donnees.

Tourne chez GitHub Actions toutes les 5 minutes (.github/workflows/miroir.yml). A chaque passage :

1. profil : titre, description, photo, signatures, contenu protege, reactions ;
2. posts programmes : chaque post programme de la source a son double programme a la meme
   minute dans le miroir (copie, texte corrige, photo remplacee, re-datation, double orphelin
   retire de la file) ;
3. posts publies : chaque post publie dans la source depuis MIROIR_DEPUIS a son double dans le
   miroir, sinon il y est copie tout de suite (texte corrige si la source a ete modifiee).

Sans etat : les doubles se retrouvent par date, type de media, texte et empreinte de la photo.
Les medias sont telecharges puis renvoyes (jamais transferes) : aucune mention de la source, et
ca marche aussi sur un canal a contenu protege.

    python miroir.py --dry-run                                     # affiche, ne touche a rien
    python miroir.py --session-file ../sessions/echanges --verbose  # test local
    python miroir.py --admins        # une fois : donne au miroir les admins de la source
    python miroir.py --historique    # une fois : recopie les posts publies avant MIROIR_DEPUIS

Variables : TG_API_ID, TG_API_HASH, TG_SESSION_MIROIR (ou --session-file), MIROIR_SOURCE et
MIROIR_CIBLE (id du canal, -100..., ou id:access_hash), MIROIR_DEPUIS (date ISO, ex.
2026-09-16T23:30:00+02:00). Journal neutre (des compteurs, jamais de texte) sauf --verbose.
"""

import argparse
import asyncio
import io
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from PIL import Image
from telethon import TelegramClient, functions, types, utils
from telethon.errors import ChatAdminRequiredError, RPCError
from telethon.sessions import SQLiteSession, StringSession

MAX_PUBLIES = 10                         # garde-fou : jamais plus de 10 posts publies par passage
TOLERANCE = timedelta(seconds=90)        # un post programme sort a quelques secondes pres
SEUIL_PHOTO = 6                          # bits differents sur 64 (mesure : copie 0-1, photos distinctes >= 13)
ORPHELIN_MARGE = timedelta(minutes=10)   # on ne retire jamais un post programme qui va sortir

args = None
stats = {"erreurs": 0}


def log(msg: str) -> None:
    print(msg, flush=True)


def detail(msg: str) -> None:
    if args.verbose:
        print("   " + msg, flush=True)


def compte(cle: str, n: int = 1) -> None:
    stats[cle] = stats.get(cle, 0) + n


# --------------------------------------------------------------------------- posts

def kind(m) -> str:
    media = m.media
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return "texte"
    if isinstance(media, types.MessageMediaPhoto):
        return "photo" if isinstance(media.photo, types.Photo) else "autre"
    if isinstance(media, types.MessageMediaPoll):
        return "sondage"
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        if m.sticker:
            return "autre"
        if m.voice:
            return "vocal"
        if m.video_note:
            return "video_ronde"
        if m.gif:
            return "gif"
        if m.video:
            return "video"
        if m.audio:
            return "audio"
        return "fichier"
    return "autre"


def empreinte(stripped: bytes | None) -> int | None:
    """dHash 8x8 de la miniature floue que Telegram joint a chaque photo (aucun telechargement)."""
    if not stripped:
        return None
    try:
        img = Image.open(io.BytesIO(utils.stripped_photo_to_jpg(stripped))).convert("L").resize((9, 8))
    except Exception:
        return None
    px = img.tobytes()
    return sum(1 << (y * 8 + x) for y in range(8) for x in range(8) if px[y * 9 + x] > px[y * 9 + x + 1])


def empreinte_photo(m) -> int | None:
    photo = getattr(m.media, "photo", None)
    if not isinstance(photo, types.Photo):
        return None
    return next((empreinte(s.bytes) for s in photo.sizes if isinstance(s, types.PhotoStrippedSize)), None)


def ecart(a: int | None, b: int | None) -> int | None:
    return None if a is None or b is None else bin(a ^ b).count("1")


@dataclass
class Post:
    msgs: list

    @property
    def first(self):
        return self.msgs[0]

    @property
    def date(self) -> datetime:
        return self.first.date

    @property
    def kinds(self) -> tuple:
        return tuple(kind(m) for m in self.msgs)

    @property
    def text(self) -> str:
        return "\n".join((m.message or "").strip() for m in self.msgs).strip()

    @property
    def photos(self) -> list:
        return [empreinte_photo(m) for m in self.msgs]

    @property
    def copiable(self) -> bool:
        return "autre" not in self.kinds

    def label(self) -> str:
        return f"{self.date.astimezone():%d/%m %H:%M} {'+'.join(self.kinds)} {self.text[:45]!r}"


def photos_proches(a: Post, b: Post) -> bool:
    ecarts = [ecart(x, y) for x, y in zip(a.photos, b.photos)]
    connus = [e for e in ecarts if e is not None]
    return bool(connus) and all(e <= SEUIL_PHOTO for e in connus)


def photos_differentes(a: Post, b: Post) -> list[int]:
    """Index des medias dont la photo a change (empreintes connues des deux cotes et eloignees)."""
    return [i for i, (x, y) in enumerate(zip(a.photos, b.photos)) if (e := ecart(x, y)) is not None and e > SEUIL_PHOTO]


def grouper(msgs) -> list[Post]:
    posts, albums = [], {}
    for m in sorted(msgs, key=lambda m: (m.date, m.id)):
        if m.grouped_id and m.grouped_id in albums:
            albums[m.grouped_id].msgs.append(m)
            continue
        post = Post([m])
        posts.append(post)
        if m.grouped_id:
            albums[m.grouped_id] = post
    for p in posts:
        p.msgs.sort(key=lambda m: m.id)
    return posts


def associer(sources: list[Post], cibles: list[Post], regles) -> tuple[list, list, list]:
    """Associe chaque post source a au plus un post cible, regle par regle, la plus stricte d'abord."""
    paires, libres = [], list(cibles)
    for regle in regles:
        reste = []
        for s in sources:
            t = next((t for t in libres if regle(s, t)), None)
            if t is None:
                reste.append(s)
            else:
                paires.append((s, t))
                libres.remove(t)
        sources = reste
    return paires, sources, libres


# --------------------------------------------------------------------------- lecture

async def resoudre(c, spec: str):
    if ":" in spec:
        cid, access_hash = spec.split(":", 1)
        return types.InputPeerChannel(utils.resolve_id(int(cid))[0], int(access_hash))
    cid = int(spec)
    try:
        return await c.get_input_entity(cid)
    except ValueError:
        pass
    async for d in c.iter_dialogs():
        if d.id == cid:
            return utils.get_input_peer(d.entity)
    raise SystemExit(f"Canal {spec} introuvable dans les discussions du compte.")


async def programmes(c, peer) -> list[Post]:
    res = await c(functions.messages.GetScheduledHistoryRequest(peer=peer, hash=0))
    entites = {utils.get_peer_id(x): x for x in res.users + res.chats}
    msgs = []
    for m in res.messages:
        if isinstance(m, types.Message):
            m._finish_init(c, entites, peer)
            msgs.append(m)
    return grouper(msgs)


async def publies(c, peer, depuis: datetime, jusqua: datetime | None = None) -> list[Post]:
    msgs = []
    async for m in c.iter_messages(peer, offset_date=jusqua):
        if m.date < depuis:
            break
        if not m.action:
            msgs.append(m)
    return grouper(msgs)


# --------------------------------------------------------------------------- ecriture

class NonCopiable(Exception):
    pass


async def media_de(c, m):
    k, media = kind(m), m.media
    spoiler = getattr(media, "spoiler", None) or None
    if k == "photo":
        data = await c.download_media(m, file=bytes)
        return types.InputMediaUploadedPhoto(file=await c.upload_file(data, file_name="photo.jpg"), spoiler=spoiler)
    if k == "sondage":
        p, r = media.poll, media.results
        if getattr(media, "attached_media", None) or any(getattr(a, "media", None) for a in p.answers):
            raise NonCopiable("sondage illustre")
        correct = solution = entites = None
        if p.quiz:
            bonnes = {v.option for v in ((r.results if r else None) or []) if v.correct}
            correct = [i for i, a in enumerate(p.answers) if a.option in bonnes]
            if not correct:
                raise NonCopiable("quiz dont la bonne reponse n'est pas visible")
            solution, entites = r.solution, r.solution_entities
        fin = p.close_date if p.close_date and p.close_date > datetime.now(timezone.utc) else None
        poll = types.Poll(id=random.getrandbits(62), hash=0, question=p.question,
                          answers=[types.InputPollAnswer(text=a.text) for a in p.answers],
                          public_voters=p.public_voters, multiple_choice=p.multiple_choice, quiz=p.quiz,
                          open_answers=p.open_answers, revoting_disabled=p.revoting_disabled,
                          shuffle_answers=p.shuffle_answers, hide_results_until_close=p.hide_results_until_close,
                          subscribers_only=p.subscribers_only, countries_iso2=p.countries_iso2,
                          close_period=p.close_period if fin or not p.close_date else None, close_date=fin)
        return types.InputMediaPoll(poll=poll, correct_answers=correct, solution=solution, solution_entities=entites)
    if k in ("vocal", "video_ronde", "gif", "video", "audio", "fichier"):
        doc = media.document
        nom = next((a.file_name for a in doc.attributes if isinstance(a, types.DocumentAttributeFilename)), "fichier")
        fichier = await c.upload_file(await c.download_media(m, file=bytes), file_name=nom)
        miniature = None
        tailles = [t for t in (doc.thumbs or []) if isinstance(t, (types.PhotoSize, types.PhotoSizeProgressive))]
        if tailles:
            grande = max(tailles, key=lambda t: getattr(t, "size", 0) or max(getattr(t, "sizes", [0])))
            if (data := await c.download_media(m, file=bytes, thumb=grande)):
                miniature = await c.upload_file(data, file_name="miniature.jpg")
        return types.InputMediaUploadedDocument(file=fichier, mime_type=doc.mime_type, attributes=doc.attributes,
                                                thumb=miniature, spoiler=spoiler,
                                                video_timestamp=getattr(media, "video_timestamp", None))
    raise NonCopiable(f"type de media non gere ({type(media).__name__})")


def entites(m, sans_emoji_perso: bool):
    """Mise en forme du message ; sans les emojis Premium si le compte ne peut pas les envoyer."""
    if not sans_emoji_perso:
        return m.entities
    return [e for e in (m.entities or []) if not isinstance(e, types.MessageEntityCustomEmoji)] or None


async def envoyer(c, peer, post: Post, programme: datetime | None = None) -> None:
    """Recree le post dans le miroir, tout de suite ou programme a la date donnee."""
    m0 = post.first
    kw = dict(schedule_date=programme, silent=m0.silent or None, invert_media=m0.invert_media or None)
    repetition = dict(schedule_repeat_period=m0.schedule_repeat_period) if programme else {}   # absent des albums

    if len(post.msgs) == 1 and kind(m0) == "texte":
        def requete(sans):
            return functions.messages.SendMessageRequest(
                peer=peer, message=m0.message, entities=entites(m0, sans),
                no_webpage=not isinstance(m0.media, types.MessageMediaWebPage), **kw, **repetition)
    elif len(post.msgs) == 1:
        media = await media_de(c, m0)

        def requete(sans):
            return functions.messages.SendMediaRequest(peer=peer, media=media, message=m0.message or "",
                                                       entities=entites(m0, sans), **kw, **repetition)
    else:
        album = []
        for m in post.msgs:
            envoye = await c(functions.messages.UploadMediaRequest(peer=peer, media=await media_de(c, m)))
            media = utils.get_input_media(envoye)
            if getattr(m.media, "spoiler", None) and hasattr(media, "spoiler"):
                media.spoiler = True
            album.append((media, m))

        def requete(sans):
            return functions.messages.SendMultiMediaRequest(peer=peer, multi_media=[
                types.InputSingleMedia(media=media, message=m.message or "", entities=entites(m, sans))
                for media, m in album], **kw)

    try:
        await c(requete(False))
    except RPCError:
        if not any(isinstance(e, types.MessageEntityCustomEmoji) for m in post.msgs for e in (m.entities or [])):
            raise
        await c(requete(True))


async def modifier(c, peer, source: Post, cible: Post, programme: datetime | None, photos: list[int] = ()) -> None:
    """Aligne texte (et photos remplacees) de la copie sur la source. `programme` obligatoire pour
    un post programme : sans lui, Telegram prendrait l'id pour celui d'un post publie."""
    for i, (a, b) in enumerate(zip(source.msgs, cible.msgs)):
        nouveau_texte = (a.message or "") != (b.message or "")
        if not nouveau_texte and i not in photos:
            continue
        media = await media_de(c, a) if i in photos else None
        await c(functions.messages.EditMessageRequest(
            peer=peer, id=b.id, message=a.message or "", entities=a.entities, media=media, schedule_date=programme,
            no_webpage=(kind(a) == "texte" and not isinstance(a.media, types.MessageMediaWebPage)) or None))


async def agir(etiquette: str, action) -> bool:
    if args.dry_run:
        detail(f"[simulation] {etiquette}")
        return True
    try:
        await action()
        detail(etiquette)
        return True
    except NonCopiable as e:
        compte("non_copiables")
        log(f"   ignore : {e}")
    except Exception as e:
        stats["erreurs"] += 1
        log(f"   ECHEC ({type(e).__name__}) : {e.message if isinstance(e, RPCError) else ''}")
        detail(f"   ... sur : {etiquette} : {e}")
    return False


# --------------------------------------------------------------------------- synchronisations

def emojis_reactions(r) -> object:
    if isinstance(r, types.ChatReactionsSome):
        return ("some", sorted(getattr(x, "emoticon", None) or str(getattr(x, "document_id", "")) for x in r.reactions))
    if isinstance(r, types.ChatReactionsAll):
        return ("all", bool(r.allow_custom))
    return ("none",)


async def profil(c, src, dst, full_src, full_dst) -> None:
    cs, cd = full_src.chats[0], full_dst.chats[0]
    fs, fd = full_src.full_chat, full_dst.full_chat

    if cs.title != cd.title:
        compte("profil")
        await agir("titre", lambda: c(functions.channels.EditTitleRequest(dst, cs.title)))
    if (fs.about or "") != (fd.about or ""):
        compte("profil")
        await agir("description", lambda: c(functions.messages.EditChatAboutRequest(dst, fs.about or "")))

    ps = cs.photo if isinstance(cs.photo, types.ChatPhoto) else None
    pd = cd.photo if isinstance(cd.photo, types.ChatPhoto) else None
    if ps and (pd is None or (e := ecart(empreinte(ps.stripped_thumb), empreinte(pd.stripped_thumb))) is None
               or e > SEUIL_PHOTO):
        compte("profil")

        async def photo():
            data = await c.download_profile_photo(src, file=bytes, download_big=True)
            fichier = await c.upload_file(data, file_name="profil.jpg")
            await c(functions.channels.EditPhotoRequest(dst, types.InputChatUploadedPhoto(file=fichier)))
        await agir("photo de profil", photo)

    if (bool(cs.signatures), bool(cs.signature_profiles)) != (bool(cd.signatures), bool(cd.signature_profiles)):
        compte("profil")
        await agir("signatures", lambda: c(functions.channels.ToggleSignaturesRequest(
            dst, signatures_enabled=bool(cs.signatures), profiles_enabled=bool(cs.signature_profiles))))
    if bool(cs.noforwards) != bool(cd.noforwards):
        compte("profil")
        await agir("contenu protege", lambda: c(functions.messages.ToggleNoForwardsRequest(dst, bool(cs.noforwards))))
    if emojis_reactions(fs.available_reactions) != emojis_reactions(fd.available_reactions):
        compte("profil")
        reactions = fs.available_reactions or types.ChatReactionsNone()
        await agir("reactions", lambda: c(functions.messages.SetChatAvailableReactionsRequest(dst, reactions)))


async def sync_programmes(c, dst, sources: list[Post], cibles: list[Post]) -> None:
    maintenant = datetime.now(timezone.utc)
    meme_date = lambda s, t: abs(s.date - t.date) <= TOLERANCE and s.kinds == t.kinds
    paires, a_copier, orphelins = associer(sources, cibles, [
        lambda s, t: meme_date(s, t) and s.text == t.text,                       # identique
        meme_date,                                                               # texte modifie
        lambda s, t: s.kinds == t.kinds and s.text == t.text and not photos_differentes(s, t),  # deplace
    ])

    async def redater(s: Post, t: Post) -> None:
        for m in t.msgs:
            await c(functions.messages.EditMessageRequest(peer=dst, id=m.id, schedule_date=s.date,
                                                          schedule_repeat_period=s.first.schedule_repeat_period))

    for s, t in paires:
        photos = photos_differentes(s, t)
        if abs(s.date - t.date) > TOLERANCE or s.first.schedule_repeat_period != t.first.schedule_repeat_period:
            compte("programmes_redates")
            await agir(f"re-date {t.label()} -> {s.date.astimezone():%d/%m %H:%M}", lambda s=s, t=t: redater(s, t))
        if s.text != t.text or photos:
            compte("programmes_modifies")
            await agir(f"modifie {s.label()}", lambda s=s, t=t, p=photos: modifier(c, dst, s, t, s.date, p))

    for s in a_copier:
        if not s.copiable:
            compte("non_copiables")
            continue
        if s.date <= maintenant + timedelta(seconds=30):
            continue                                   # il sort a l'instant : la synchro des publies le prendra
        compte("programmes_copies")
        await agir(f"programme {s.label()}", lambda s=s: envoyer(c, dst, s, programme=s.date))

    # Double dont la source a quitte la file (supprimee, ou envoyee en avance : la synchro des publies
    # la recopie juste apres). Garde-fous : jamais un post sur le point de sortir, jamais une vague
    # (une file source vide ressemble plus a une lecture ratee qu'a 65 suppressions).
    retirables = [t for t in orphelins if t.date > maintenant + ORPHELIN_MARGE]
    if retirables and len(retirables) <= max(3, len(cibles) // 5):
        compte("programmes_retires", len(retirables))
        for t in retirables:
            detail(f"orphelin {t.label()}")
        ids = [m.id for t in retirables for m in t.msgs]
        await agir(f"retire {len(ids)} message(s) programme(s)", lambda: c(
            functions.messages.DeleteScheduledMessagesRequest(peer=dst, id=ids)))
    elif retirables:
        compte("orphelins_gardes", len(retirables))
        log(f"   {len(retirables)} posts programmes du miroir sans source : trop nombreux, laisses en place.")


async def sync_publies(c, dst, sources: list[Post], cibles: list[Post], programmes_dst: list[Post],
                       full_src, full_dst) -> None:
    apres = lambda s, t: s.kinds == t.kinds and t.date - s.date >= -TOLERANCE   # une copie n'est jamais avant
    meme_contenu = lambda s, t: s.text == t.text and not photos_differentes(s, t)
    paires, a_copier, _ = associer(sources, cibles, [
        lambda s, t: abs(s.date - t.date) <= TOLERANCE and s.kinds == t.kinds and s.text == t.text,
        lambda s, t: abs(s.date - t.date) <= TOLERANCE and s.kinds == t.kinds,
        lambda s, t: apres(s, t) and meme_contenu(s, t),
        lambda s, t: apres(s, t) and photos_proches(s, t),                       # legende modifiee
        lambda s, t: apres(s, t) and t.date - s.date <= timedelta(minutes=30),     # texte modifie, sans photo
    ])

    for s, t in paires:
        if s.text != t.text:
            compte("publies_modifies")
            await agir(f"modifie {s.label()}", lambda s=s, t=t: modifier(c, dst, s, t, None))

    envoyes = 0
    for s in a_copier:
        if not s.copiable:
            compte("non_copiables")
            continue
        if any(abs(s.date - p.date) <= TOLERANCE and p.kinds == s.kinds for p in programmes_dst):
            continue                                   # sa copie programmee sort en ce moment meme
        if any(apres(s, t) and meme_contenu(s, t) for t in cibles):
            continue                                   # deja copie (dernier filet contre les doublons en boucle)
        if envoyes >= MAX_PUBLIES:
            compte("publies_reportes")
            continue
        envoyes += 1
        compte("publies_copies")
        await agir(f"publie {s.label()}", lambda s=s: envoyer(c, dst, s))

    epingle = full_src.full_chat.pinned_msg_id
    double = next((t for s, t in paires if any(m.id == epingle for m in s.msgs)), None)
    if epingle and double and full_dst.full_chat.pinned_msg_id != double.first.id:
        compte("epingles")
        await agir("epingle", lambda: c(functions.messages.UpdatePinnedMessageRequest(dst, double.first.id, silent=True)))


async def copier_admins(c, src, dst) -> None:
    deja = {u.id async for u in c.iter_participants(dst, filter=types.ChannelParticipantsAdmins())}
    moi = (await c.get_me()).id
    async for u in c.iter_participants(src, filter=types.ChannelParticipantsAdmins()):
        if u.id in deja or u.id == moi:
            continue
        p = u.participant
        droits = p.admin_rights
        if isinstance(p, types.ChannelParticipantCreator):   # proprietaire de la source : admin complet ici
            droits.add_admins = True
        compte("admins")
        nom = f"@{u.username}" if u.username else f"id {u.id}"
        await agir(f"admin {nom}", lambda u=u, d=droits, r=getattr(p, "rank", None): c(
            functions.channels.EditAdminRequest(dst, u, d, r or "")))


async def historique(c, src, dst, depuis: datetime) -> None:
    anciens = await publies(c, src, datetime(2000, 1, 1, tzinfo=timezone.utc), jusqua=depuis)
    deja = await publies(c, dst, datetime(2000, 1, 1, tzinfo=timezone.utc))
    _, a_copier, _ = associer(anciens, deja, [lambda s, t: s.kinds == t.kinds and s.text == t.text
                                              and not photos_differentes(s, t)])
    log(f"Historique : {len(anciens)} posts avant la mise en miroir, {len(a_copier)} a recopier.")
    for s in a_copier:
        if s.copiable:
            compte("historique_copies")
            await agir(f"historique {s.label()}", lambda s=s: envoyer(c, dst, s))
        else:
            compte("non_copiables")


# --------------------------------------------------------------------------- main

def session():
    if args.session_file:
        base = SQLiteSession(args.session_file)
        texte = StringSession.save(base)
        base.close()
        return StringSession(texte), {}
    return StringSession(os.environ["TG_SESSION_MIROIR"]), dict(
        device_model="miroir-cloud", system_version="GitHub Actions", app_version="1.0")


async def run() -> int:
    depuis = datetime.fromisoformat(os.environ["MIROIR_DEPUIS"]).astimezone(timezone.utc)
    sess, appareil = session()
    c = TelegramClient(sess, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"],
                       flood_sleep_threshold=600, **appareil)
    await c.connect()
    try:
        if not await c.is_user_authorized():
            log("Session Telegram invalide ou revoquee.")
            return 2
        src = await resoudre(c, os.environ["MIROIR_SOURCE"])
        dst = await resoudre(c, os.environ["MIROIR_CIBLE"])
        full_src = await c(functions.channels.GetFullChannelRequest(src))
        full_dst = await c(functions.channels.GetFullChannelRequest(dst))

        try:
            prog_dst = await programmes(c, dst)
        except ChatAdminRequiredError:
            log("Le compte n'est pas admin du canal miroir (droit de publier requis).")
            return 3
        prog_src = await programmes(c, src)

        maintenant = datetime.now(timezone.utc)
        borne = max(depuis, maintenant - timedelta(days=7))
        pub_src = await publies(c, src, borne)
        pub_dst = await publies(c, dst, borne - timedelta(days=1))

        await profil(c, src, dst, full_src, full_dst)
        if args.admins:
            await copier_admins(c, src, dst)
        await sync_programmes(c, dst, prog_src, prog_dst)
        await sync_publies(c, dst, pub_src, pub_dst, prog_dst, full_src, full_dst)
        if args.historique:
            await historique(c, src, dst, depuis)
    finally:
        await c.disconnect()

    resume = ", ".join(f"{k} {v}" for k, v in stats.items() if v) or "rien a faire"
    log(f"{'[simulation] ' if args.dry_run else ''}Miroir : {len(prog_src)} programmes / {len(pub_src)} publies "
        f"cote source ; {resume}.")
    return 1 if stats["erreurs"] else 0


def main() -> None:
    global args
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="affiche ce qui serait fait, ne modifie rien")
    ap.add_argument("--verbose", action="store_true", help="detail post par post (textes compris : pas dans le cloud)")
    ap.add_argument("--session-file", help="session Telethon SQLite locale au lieu de TG_SESSION_MIROIR")
    ap.add_argument("--admins", action="store_true", help="donne au miroir les admins de la source")
    ap.add_argument("--historique", action="store_true", help="recopie les posts publies avant MIROIR_DEPUIS")
    args = ap.parse_args()
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
