# Politique de solde disponible et attente de deux heures

Mandat explicite : aucun budget monétaire fixe ; ouvrir les positions au minimum
du marché tant que le solde disponible suffit, attendre deux heures en cas
d'insuffisance, vérifier à nouveau et répéter. L'utilisateur autorise désormais
le trading continu, après un essai borné. Auto-redeem externe conservé.

`use_available_balance=true` autorise des plafonds monétaires/count à null,
sans supprimer les lectures de solde, permissions et réservations avant chaque
achat. Le profil par défaut conserve ses protections d'activation explicite et
ses plafonds obligatoires. Les frais gardent leur marge configurable de 10 %.

Le premier manque de fonds déclenche une attente commune persistée SQLite
`funds_retry_at = server_now + 7200`. Toutes les nouvelles intentions attendent,
même si elles appartiennent à un autre créneau/direction. Une hausse du solde
pendant la pause n'est vérifiée qu'à son échéance. À l'échéance, une lecture
indépendante des carnets/marchés disponibles vérifie le solde : assez pour une
intention minimum encore future -> reprise ; insuffisant -> nouvelle attente
de deux heures. Une lecture en erreur bloque les achats avec réessai technique
après cinq minutes ; elle ne prouve jamais la disponibilité des fonds.

Pendant l'attente : suivi des positions, résolution, calendrier sur 72 heures,
recherche horaire des marchés non créés avec dernier contrôle ouverture-60s.
Les intentions devenues passées ne sont jamais ressuscitées. Prix initial fixe.
Timeout/ambiguïté après POST restent UNKNOWN et interdisent toute nouvelle
soumission tant que le rapprochement ne les clarifie.

Rapport : politique de capital, solde observé, prochain réveil UTC/Toronto et
erreurs de lecture. Déploiement avec sauvegardes et suppression du profil local
monitor uniquement, car le nouveau mandat autorise les nouvelles positions.
