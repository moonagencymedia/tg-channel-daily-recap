# Recap quotidien d'un canal Telegram vers Discord

Poste chaque jour a 21 h (heure de Paris) dans un salon Discord le recap des abonnes d'un
canal Telegram : nouveaux, departs, variation, part par plateforme, detail par lien
d'invitation, langue de l'audience. Tourne sur GitHub Actions, sans machine allumee.

- `recap_subs.py` relit le journal d'administration du canal (Telegram le garde ~48 h) :
  aucune base, aucun etat. Compare avec la veille a la meme heure.
- `.github/workflows/recap.yml` tourne a 19 h et 20 h UTC ; le script ne poste que s'il est
  21 h a Paris, ce qui couvre l'heure d'ete et l'heure d'hiver.
- Session Telegram dediee (compte admin du canal), en secret ; rien n'est ecrit dans les logs.

## Secrets (Settings → Secrets and variables → Actions)
`TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`, `TG_SUBS_CHANNEL` (morceau du titre du canal),
`DISCORD_WEBHOOK_URL`.

## Lancer a la main
Onglet Actions → « Recap subs 21h » → Run workflow : `force` coche = poste tout de suite ;
`day` = `AAAA-MM-JJ` pour la journee complete d'un jour passe.
