# Bot calendrier Polymarket BTC M5 — 6 octobre 2026

Implémentation de la sélection autorisée, sans recherche ni optimisation. Le fichier
des 567 règles est copié octet pour octet ; les fonctions calendrier du dry run sont
reprises sans changer les critères. Le dry run Binance continue indépendamment.

Choix retenu : processus Python autonome + SQLite WAL, plutôt qu'une extension du
dry run (risque de perturber le service) ou une automation Codex (pas un bot local).
Un marché exact est identifié par slug UTC, eventStartTime et endDate. startDate
Gamma indique sa création, pas l'ouverture de la bougie. Scanner les créneaux
futurs dans un horizon configurable, réessayer les marchés absents sans substitution.

Une intention minimale par ouverture UTC et direction, reliée à toutes les règles.
Contradictions : par défaut préparer les deux directions, sans vote. Option skip_both.
Entrées uniquement avant ouverture ; une activation découverte après ouverture est
manquée. Le prix du premier carnet frais avec ask valide est figé avec Decimal.
Quantité = min_order_size du carnet arrondi au centième supérieur. GTD par défaut,
expiration déclarée = clôture + 60 secondes, expiration effective = clôture selon
la documentation actuelle. GTC configurable et explicitement sans échéance.

Trois modes isolés dans des bases distinctes : prepare (défaut, aucun fill), paper
(simulation de traversée du carnet à la préparation, une seule fois), live (activation
explicite par l'utilisateur, limites obligatoires). Aucun secret lu en prepare/paper.
L'adaptateur SDK officiel ne réalise aucun transfert/approval/redemption. L'utilisateur
configure lui-même le compte et les autorisations. L'agent n'active jamais live.
Suivi du 6 octobre : compatible avec POLYMARKET_PRIVATE_KEY / FUNDER /
SIGNATURE_TYPE / API_URL ; credentials CLOB créés ou dérivés automatiquement à
l'initialisation live. Type de signature vérifié contre le wallet détecté, URL de
production uniquement. Les credentials CLOB manuels restent optionnels.

Journal durable avant soumission. Après réponse ambiguë ou crash, bloquer les nouveaux
ordres et rapprocher par identifiant déterministe du signed order. Jamais resoumettre
automatiquement. Quantités matched et confirmed distinctes ; résultats officiels
uniquement sur fills confirmés. Résolution CLOB : closed et un token winner, jamais
inférée du prix, du titre, de Binance ou de la fin de période. Pas de PnL réel inventé.
Suivi Binance séparé, doji perdant pour V/R ; Polymarket suit son contrat actuel
(TWAP Chainlink 60 s sur marchés sondés, égalité UP).

Tests : calendrier/DST, copie figée, marché exact, carnet frais/vide/tick, idempotence,
reprise, limites, fill partiel, résolution retardée, réponse ambiguë et absence de
signature/soumission en modes publics. Sonde publique et service prepare local.
