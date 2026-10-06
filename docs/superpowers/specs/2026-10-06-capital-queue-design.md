# File d'attente et capital disponible

Mandat approuvé : l'utilisateur conserve son auto-redeem existant et délègue
les autres choix de gestion. Aucun redeem supplémentaire, activation live,
changement des 567 règles ou choix de budget réel.

## Comportement retenu

- Horizon roulant de 72 heures. Marchés exacts absents recherchés avec backoff.
- Les prix préparés restent fixes et les ordres ne sont jamais soumis après
  l'ouverture. Priorité aux créneaux disponibles les plus proches.
- Lecture authentifiée du collateral et de tous les ordres BUY ouverts avant
  chaque soumission live. Soustraire les réservations ouvertes et les engagements
  récents encore susceptibles de ne pas être reflétés dans les lectures.
- Manque de fonds ou de permissions : état WAITING_FUNDS, aucun ordre signé,
  nouvelle lecture après 30 secondes. Échec de lecture : aucune soumission.
- Refus explicite de balance/allowance : remettre en attente sans engagement.
  Timeout, réponse ambiguë ou crash après POST : UNKNOWN, rapprochement avant
  toute nouvelle soumission ; jamais de resoumission aveugle.
- Le plafond total porte sur le capital simultanément engagé ; les pertes et
  gains réglés sortent de ce plafond uniquement après résolution et confirmation
  des fills et de la fin de l'ordre. Le solde cash lu reste une condition distincte,
  aucune anticipation du crédit de l'auto-redeem.
- Plafond quotidien UTC non recyclé ; nombre maximum de positions/ordres
  engagés simultanément. Refus explicites non comptés comme engagements.
- Suivre les ordres et positions déjà engagés avant les nouvelles soumissions.
  Afficher attente, solde observé et capital engagé dans le rapport.

## Validation et livraison

Tests hors ligne : manque de fonds puis crédit et reprise, réservations d'autres
ordres du compte, lectures indisponibles, refus explicite versus timeout, reprise
SQLite, prix conservé, créneau expiré, résolution sans crédit, plafond quotidien,
plusieurs soumissions face à un snapshot retardé et marchés absents sur 72 heures.
Tests Linux et redémarrage du vrai service prepare après sauvegarde de SQLite.
Déployer sur le VPS et publier sur le dépôt ; aucune clé lue par l'agent.
