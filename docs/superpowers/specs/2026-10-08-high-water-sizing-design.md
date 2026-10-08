# Taille de position sur maximum de solde

Design explicitement approuvé : 3 % ajustable du maximum en espèces observé,
sans diminution après perte ou achat ; minimum marché comme plancher ; maximum
persistant après redémarrage. Activation démarre avec lecture authentifiée actuelle.

Configuration position_balance_percent string numérique (0,100], null désactive.
Meta balance_high_water monotone, commit durable même si un carnet échoue ensuite.
Lecture avant achats et périodique300s indépendamment des opportunités. Quantité
max(minimum, floor(cible/prix,0.01share)) ; frais10 % réservés en supplément et cash
net contrôlé avant signature. Prix fixe, intentions envoyées jamais redimensionnées.
Si insuffisance, attente5min sans diminution de taille. Aucun reset de UNKNOWN.

Plan réalisé : config/validation, maximum durable, polling, sizing avant signature,
documentation et tests maximum/baisse/restart, arrondi, frais, rollback et cash
insuffisant. Déploiement avec service arrêté, backup code/config/SQLite cohérente,
config3, maximum initialisé par fonds réels, tests Linux et vérification santé.
