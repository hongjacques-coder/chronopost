# chronopost-watcher

Envoie une notification Discord dès que le suivi d'un colis Chronopost change.
Vérification toutes les ~30 s, via GitHub Actions. Aucune dépendance : Python 3 et sa bibliothèque standard.

## Source des données

La page `www.chronopost.fr/tracking-no-cms/suivi-page?...` est protégée par un challenge
Cloudflare (« Just a moment... ») : impossible à lire sans un vrai navigateur.
Le script interroge donc le **web service public de suivi** de Chronopost, qui renvoie les
mêmes événements en XML structuré :

```
https://ws.chronopost.fr/tracking-cxf/TrackingServiceWS/trackSkybillV2?language=fr_FR&skybillNumber=XW609350053TS
```

À chaque vérification, le script calcule une empreinte (SHA-256) de la liste des événements
et la compare à l'état précédent (`state.json`). Si elle change, il envoie les nouveaux
événements sur Discord (date, lieu, infos complémentaires). Au premier lancement, il
envoie seulement le statut actuel.

## Mise en place

1. **Créer le webhook Discord** : sur le serveur, Paramètres du salon → Intégrations →
   Webhooks → Nouveau webhook → Copier l'URL du webhook.
2. **Créer un repo GitHub** avec le contenu de ce dossier (`watch.py`, `.github/workflows/watch.yml`, …).
   Le workflow doit se trouver à la racine du repo, dans `.github/workflows/`.
3. **Ajouter le secret** : Settings → Secrets and variables → Actions → *New repository secret*,
   nom `DISCORD_WEBHOOK_URL`, valeur = l'URL du webhook.
4. *(Optionnel)* **Variables** (même page, onglet *Variables*) :
   - `TRACKING_NUMBER` : autre numéro de suivi (défaut `XW609350053TS`) ;
   - `INTERVAL` : secondes entre deux vérifications (défaut `30`) ;
   - `DURATION` : durée d'un run en secondes (défaut `18000`, soit 5 h).
5. **Activer le workflow** : onglet Actions → autoriser les workflows si demandé →
   « Suivi Chronopost » → *Run workflow* pour démarrer tout de suite sans attendre le cron.
6. **Arrêter** une fois le colis livré : Actions → « Suivi Chronopost » → ⋯ → *Disable workflow*.

## Pourquoi une boucle et pas un cron toutes les 30 s ?

Le cron de GitHub Actions a une fréquence minimale de **5 minutes**, et les runs planifiés
partent souvent avec plusieurs minutes de retard. Donc :

- chaque run fait tourner `watch.py` en boucle (une vérification toutes les 30 s) pendant 5 h,
  puis s'arrête proprement (un job est limité à 6 h) ;
- le cron (toutes les 15 min) ne sert qu'à relancer un run. Avec `concurrency`, un seul
  run tourne à la fois et un seul attend derrière, prêt à démarrer dès la fin du précédent.
  Les runs planifiés suivants remplacent celui en attente : ils apparaissent comme
  « cancelled » dans l'onglet Actions, c'est normal ;
- l'état (`state.json`) est conservé entre deux runs grâce au cache Actions
  (restauré au début, sauvegardé à la fin).

## Tester en local

```bash
python3 watch.py --dry-run
```

Affiche le message Discord qui serait envoyé, sans rien envoyer. Relancer la commande
affiche « inchangé ». Supprimer `state.json` pour repartir de zéro. Pour un vrai envoi :

```bash
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." python3 watch.py
```

## Limites

- **Minutes Actions** : la boucle consomme environ 24 h de runner par jour, soit environ
  43 000 min par mois. C'est gratuit et illimité sur un **repo public**. Sur un repo
  **privé**, le quota gratuit (2 000 min/mois sur le plan Free) est épuisé en moins de
  2 jours. Dans ce cas, augmenter `INTERVAL` ne change rien (c'est la durée du job qui compte) :
  il faut un repo public, ou remplacer la boucle par un simple cron toutes les 5 à 10 min
  avec `DURATION=0`.
  Le numéro de suivi est visible dans un repo public : le mettre en variable plutôt qu'en dur ne
  le cache pas (les variables apparaissent dans les logs). Le webhook, lui, reste secret.
- **Blocage** : le web service n'est pas une API documentée pour cet usage. Chronopost peut le
  modifier, le limiter ou le placer derrière Cloudflare à tout moment, surtout pour les IP des
  runners GitHub. Le script loggue les erreurs, continue, et espace ses appels si les échecs
  se répètent.
- **Cron** : GitHub désactive les workflows planifiés d'un repo public après 60 jours sans
  activité, et les runs planifiés peuvent être retardés aux heures chargées.
- **Cache** : le cache Actions est supprimé après 7 jours sans accès. En cas de coupure plus
  longue, le prochain run repart de zéro et renvoie simplement le statut actuel.
- Si un run est tué brutalement (panne du runner), l'état n'est pas sauvegardé : les derniers
  événements peuvent être notifiés une deuxième fois.
