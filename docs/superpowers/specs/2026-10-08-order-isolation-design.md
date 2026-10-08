# Isolation des ordres incertains et solde à cinq minutes

Mandat approuvé le 8 octobre 2026 : appliquer les recommandations présentées et
réduire la pause de solde insuffisant de deux heures à cinq minutes.

Un ordre UNKNOWN/SENDING interdit seulement une nouvelle signature de sa propre
intention. Les autres intentions futures continuent après lecture authentifiée du
solde net de toutes les réservations, dont le coût maximal de chaque ordre incertain.
L’intention passée ne peut jamais être rachetée. Aucun budget fixe ajouté.

Réconciliation : lecture exacte de l’ordre ; en cas d’erreur, recherche des ordres
ouverts exacts puis transactions paginées filtrées par marché/token/date, chaque
fill associé par hash exact taker/maker. Exécution complète CONFIRMED permet FILLED.
Historique vide/partiel/indisponible et expiration ne prouvent jamais l’absence :
UNKNOWN et réservation conservés. Aucun renvoi aveugle, aucun changement de prix.
Refus explicites SDK et HTTP authentification/validation sont REJECTED ; refus de
fonds revient en attente. Les erreurs HTTP 400 inconnues restent ambiguës.

Plan : modifier guard, rapprochement, config exemple et documentation ; tester
redémarrage sans double POST, continuité autre intention et réservation, historique
vide/partiel, fills exacts et incohérences ; publier le code ; sauvegarder code,
config et SQLite VPS service arrêté ; déployer et passer balance_retry_seconds=300.
Réduire les échéances futures de solde déjà persistées à au plus cinq minutes.
Tester Linux puis redémarrer et vérifier la santé et les premières réconciliations.
