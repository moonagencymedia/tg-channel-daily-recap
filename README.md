# Recap subs Mei — tous les jours a 21 h, sans le Mac

Poste dans Discord `#sub` le recap des subs du canal Telegram de Mei (nouveaux, departs,
variation, par plateforme, par lien, langue de l'audience), **tous les jours a 21 h heure de
Paris**, depuis GitHub Actions. Le Mac peut etre eteint.

## Comment ca marche
- `recap_subs.py` relit le journal d'administration du canal (Telegram le garde ~48 h) : aucune
  base, aucun etat. Compare avec la veille a la meme heure.
- `.github/workflows/recap.yml` tourne a 19 h et 20 h UTC ; le script ne poste que si il est
  21 h a Paris, ce qui couvre l'heure d'ete et l'heure d'hiver sans rien changer.
- Session Telegram **dediee** (secret `TG_SESSION_STRING`, creee par `login_cloud.py` sur le Mac) :
  la session du Mac et celle du cloud ne se marchent pas dessus.

## Secrets (Settings → Secrets → Actions)
`TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`, `DISCORD_WEBHOOK_URL`.

## Lancer a la main
- Onglet Actions → « Recap subs 21h » → Run workflow (case « force » cochee = poste tout de suite).
- Un jour passe complet : meme bouton, champ `day` = `2026-09-14`.
- En ligne de commande : `gh workflow run recap.yml -R moonagencymedia/recap-subs-mei -f force=true`

## Si ca s'arrete
GitHub coupe les crons d'un depot sans commit depuis 60 jours : pousser n'importe quel commit
les relance. Une session Telegram revoquee (Appareils → « recap-cloud ») → refaire `login_cloud.py`
et mettre a jour le secret.
