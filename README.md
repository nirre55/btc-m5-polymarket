# BTCUSDT M5 → Polymarket Bitcoin Up or Down 5m

**VPS Linux :** voir [installation persistante systemd](linux/README.md).
Credentials et plafonds enregistrés une fois ; démarrage au boot et reprise après
panne. Les commandes PowerShell ci-dessous concernent seulement Windows.

Bot local séparé du dry run Binance. **Mode par défaut : prepare ; aucune signature,
aucun ordre soumis, aucun fill réel.** Les 567 règles, leurs critères numériques,
les historiques et les deux groupes sont conservés. V prépare BUY Up ; R prépare
BUY Down sur le marché de la bougie **ouverte** au créneau Toronto de la règle.
UTC en base, America/Toronto pour le calendrier, y compris les deux occurrences
de l'heure d'automne et l'absence de l'heure de printemps.

Les fréquences proviennent de l'historique 04/10/2023 00:00 Toronto inclus →
04/10/2026 00:00 exclu. Elles n'avaient pas passé la correction statistique globale.
Ce sont des hypothèses historiques, pas une stratégie validée ou une rentabilité
démontrée. Aucun nouveau backtest, filtre, vote d'ensemble ou choix de règles.

## Commandes PowerShell

Depuis ce dossier :

```powershell
.\control.ps1 start
.\control.ps1 status
.\control.ps1 report
.\control.ps1 probe
.\control.ps1 halt
.\control.ps1 resume
.\control.ps1 stop
.\control.ps1 test
```

Le service est détaché et invisible sous Windows. `start` est idempotent ; `stop`
est coopératif et conserve SQLite. Après un redémarrage du PC, relancer `start`.
Le PC doit rester éveillé. Ce n'est pas une automation Codex.
`halt` écrit un fichier HALT durable et suspend les nouvelles préparations et
soumissions ; le suivi continue. **HALT et stop n'annulent pas les ordres déjà
soumis.** Les GTD sont bornés par leur échéance ; un GTC peut continuer à exister
après l'arrêt du processus. Le bot n'appelle aucune API d'annulation.

Rapport : `state_prepare/report.html`, actualisé toutes les 30 secondes dans le
navigateur. État : `status.json`. Détails : `report.json`, `intents.csv`, `rules.csv`.
Base : `state_prepare/bot.sqlite3`, WAL/FULL, verrou exclusif de processus.
Les tables `rules`, `links`, `intents`, `fills`, `journal`, `meta` conservent les
preuves, la provenance et les transitions. Arrêter avant une sauvegarde manuelle
de SQLite et conserver ensemble la base et ses éventuels WAL/SHM.

## Marché exact et préparation anticipée

Le bot parcourt les créneaux futurs dans un horizon de 26 heures configurable.
Il interroge exclusivement le slug `btc-updown-5m-<ouverture Unix en secondes>`.
Il exige `eventStartTime`/`events.startTime` = ouverture et `endDate` = ouverture
+ 300 secondes ; le `startDate` Gamma est une date de création, pas la bougie.
Il vérifie le mapping Up/Down et des actifs CLOB, l'identifiant de condition,
l'acceptation d'ordres Gamma et CLOB et le carnet du token choisi.

Un marché absent, non prêt ou sans ask est réessayé avec backoff ; **aucun marché
courant de remplacement**. La première préparation se fait dès qu'un marché exact
accepte les ordres avec un carnet valide observé lors d'un cycle de polling.
Polling 15 secondes, traitement borné à 16 intentions par cycle, backoff jusqu'à
300 secondes : la détection n'est pas instantanée et dépend de la connectivité.
Les carnets dont le timestamp dépasse 30 secondes sont refusés. Des marchés
lointains existent parfois avec un carnet inchangé depuis plusieurs minutes :
ils restent en attente `PublicDataError:stale or future book` jusqu'à un snapshot
assez récent. On privilégie ici la fraîcheur vérifiable à un achat sur carnet ancien.

Le prix = ask minimal strictement positif, trié indépendamment de l'ordre de la
réponse, arrondi au tick supérieur si nécessaire. Decimal partout ; pas de float
pour les montants. La quantité = `min_order_size` du carnet, arrondie au centième
supérieur. Les sondes publiques du 6 octobre 2026 ont confirmé **5 shares** et
**tick 0,01** pour plusieurs outcomes de marchés futurs. Cela ne fixe pas un
minimum global : il est relu pour chaque token, puis revérifié avant soumission.
Le champ de récompenses `rewardsMinSize` ne sert pas de minimum d'ordre.

Le prix initial est conservé en base au redémarrage et n'est jamais repricé.
En live, un changement de minimum ou de tick incompatible bloque l'ordre au lieu
d'en changer le prix ou la quantité. Un achat au meilleur ask peut traverser le
carnet et être exécuté immédiatement, **avant le début** : `post_only=False`.
S'il n'y a que 2 shares à cet ask, la demande minimale de 5 peut être exécutée
partiellement ; le reste conserve sa limite initiale.

## Exposition et choix de politique visibles

**Une intention minimum par marché ET direction**, reliée à toutes les règles
concordantes. Deux règles UP ne deviennent pas implicitement deux ordres de
5 shares. C'est une agrégation d'exposition, pas un dédoublonnage des statistiques
par règle. Le rapport distingue bougies uniques, intentions et activations.

Contradictions : `conflict_policy="both"` prépare séparément UP et DOWN, sans vote.
Cela peut représenter deux demandes minimales sur la même bougie. Option
`skip_both` : ignorer les deux intentions contradictoires, en conservant la trace.
Il n'existe aucune politique majoritaire cachée ni multiplication par règle.

**Entrées avant ouverture uniquement.** Une intention qui n'a pas pu être préparée
à temps devient MISSED, jamais un ordre rétrospectif après début. Les ordres déjà
soumis restent suivis pendant la période. Les créneaux manqués pendant une panne
sont enregistrés à la reprise, avec rattrapage borné d'une journée par cycle ;
`missed_backlog_seconds` rend le retard visible.

**GTD par défaut** : expiration déclarée = clôture + 60 secondes ; selon la
documentation actuelle, le seuil de sécurité de 60 secondes donne une échéance
effective à la clôture. La documentation exige aussi une expiration déclarée
au moins 3 minutes dans le futur ; le bot soumet avant l'ouverture, donc sa durée
la satisfait avec marge. Option `order_type="GTC"` : pas de date d'expiration,
prix conservé jusqu'au fill, annulation externe ou fermeture par l'exchange.
Le dépassement d'une heure locale ne prouve pas une annulation réelle : l'état
de l'ordre vient de l'exchange. `CANCELED_MARKET_RESOLVED` annule un reliquat et
ne supprime jamais les exécutions déjà confirmées.

Les paramètres d'exposition sont `max_order_cost`, `max_total_committed_cost`,
`max_daily_committed_cost`, `max_open_orders`. Aucun budget monétaire réel n'est
choisi pour l'utilisateur : ils sont `null` en prepare/paper et **tous obligatoires
en live**. Frais : réserve prudente configurable `fee_reserve_fraction=0.10`,
incluse dans les engagements, pas une estimation de frais réellement payés.
Le total est un plafond conservateur sur les engagements depuis la création de
la base ; il ne recycle pas les règlements ni les refus automatiquement. Le
plafond quotidien utilise le jour UTC de soumission. Les deux directions partagent
les mêmes plafonds. Les plafonds concernent cette base, pas les autres bots ou
ordres du compte ; le plafond du nombre d'ordres doit être un entier.

## Modes, exécutions et résolution

`prepare` : conserve des intentions non signées ; **zéro position**, même si un
ask aurait rendu l'ordre exécutable. Une résolution peut être observée mais ne
crée aucun WIN/LOSS de position sans quantité exécutée.

`paper` : bases et rapports séparés dans `state_paper`. Simulation explicitement
étiquetée `PAPER_SIMULATED`, une seule traversée du carnet lors de la préparation,
limitée à la profondeur disponible au prix initial. Le reliquat reste simulé non
exécuté puis expire en GTD. Aucun fill ultérieur de queue n'est inventé. En GTC,
le reliquat reste ouvert dans la simulation. Cette simulation ignore latence,
file d'attente et frais réels ; elle n'est pas un test de qualité d'exécution.

```powershell
.\control.ps1 start -Mode paper
.\control.ps1 status -Mode paper
.\control.ps1 stop -Mode paper
```

`live` : `state_live`, accessible uniquement après configuration explicite locale.
Le SDK officiel `polymarket-client==0.12.0` signe l'ordre puis `post_order` le soumet.
Un identifiant de hash d'ordre EIP-712 est journalisé durablement **avant** le POST.
Crash en SENDING → UNKNOWN. Réponse réseau ambiguë → UNKNOWN. **Aucune
resoumission automatique**, même si GET retourne 404 ; le bot tente le rapprochement
du même identifiant et bloque les nouveaux ordres tant que l'ambiguïté persiste.
Une absence temporaire de réponse n'est ni un refus ni la preuve d'un ordre absent.
Une ambiguïté durable nécessite une inspection locale du compte et du journal.

Le rapprochement lit l'ordre authentifié et les trades exacts associés, y compris
les legs maker en cas d'exécution ultérieure. `matched_qty` n'est pas une position
confirmée. MATCHED/MINED/RETRYING restent en attente ; seules les exécutions
CONFIRMED sont utilisées pour les résultats réels. Les fills sont idempotents par
intention + trade + leg, indépendants de l'état du reliquat. Les tailles REST sont
déjà en shares et ne sont pas divisées par 1e6.

La phase du marché est distincte : BEFORE_START → IN_PROGRESS →
ENDED_AWAITING_OFFICIAL_RESOLUTION → RESOLVED. Une fin de période ne règle rien.
Résolution officielle : endpoint CLOB de la condition exacte, `closed=true` et
exactement un token `winner=true`. Les prix 0/1 ne servent pas de preuve.
Un résultat en retard reste en attente. Les sondes ont effectivement rencontré
un marché terminé dont la réponse CLOB n'était pas encore résolue.

Le contrat actuel des marchés sondés utilise **TWAP Chainlink BTC/USD de 60 s**
et attribue Up à l'égalité. Le texte complet et `resolutionSource` sont archivés
pour chaque préparation. Binance BTCUSDT SPOT est une autre mesure : ses prix
open/close et son doji perdant sont conservés dans un diagnostic séparé, récupéré
seulement après clôture selon l'heure publique Binance. Aucun règlement Polymarket
n'est inféré de Binance et la politique doji n'est pas transposée.

WIN/LOSS et taux par règle concernent uniquement les positions exécutées résolues
(simulations explicitement étiquetées en paper). Les positions partagées sont
attribuées aux règles liées ; ne pas additionner leurs quantités ou coûts. Les
cohortes dédoublonnent les intentions et affichent aussi les activations. Les
quantités exécutées et valeurs de payout sont **brutes** : elles ne constituent
pas un solde net de tokens, un remboursement reçu ou un PnL réel après frais.
Aucun redemption, approval, transfert ou déploiement de wallet n'est automatisé.

## Configuration et activation par l'utilisateur

Copier `config.example.json` vers `config.local.json` et changer les paramètres
souhaités. Ce fichier et `.env*` sont exclus de Git. Le bot ne charge pas de fichier
`.env` et ne lit aucune clé en prepare/paper. Le SDK et tzdata sont déjà installés
dans `.venv` pour cette livraison. Pour une nouvelle installation :

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Pour activer **soi-même** le réel :

1. Configurer localement un wallet Polymarket existant, les fonds et les permissions
   nécessaires selon la documentation officielle. Le bot refuse de déployer un
   wallet et ne corrige pas les allowances par transaction.
2. Définir dans l'environnement du processus les variables de ton autre bot :
   `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE=3`,
   `POLYMARKET_API_URL=https://clob.polymarket.com`. Aucun trio API à saisir :
   le SDK crée ou dérive les credentials CLOB automatiquement à l'initialisation
   live (signature d'authentification L1, pas signature d'ordre). Les credentials
   restent en mémoire et ne sont pas imprimés ou enregistrés par le bot.
   Le type 3 correspond à DEPOSIT_WALLET ; le bot le vérifie contre le wallet
   détecté et refuse une discordance. L'URL doit être celle du CLOB de production.
   Pour une saisie locale masquée, depuis ce dossier, lancer
   `.\configure_credentials.ps1` dans la même fenêtre PowerShell que le bot.
   Ce script renseigne les quatre variables sans fichier et sans activer live.
   Le bot ne charge toujours pas automatiquement de fichier `.env`.
   L'ancien format `POLY_PRIVATE_KEY`, `POLY_WALLET_ADDRESS` et le trio complet
   `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` reste compatible en
   option. Un trio partiel ou deux alias contradictoires est refusé. Ne pas coller
   les secrets dans un chat, les versionner ou les afficher en logs.
3. Définir des plafonds personnels explicites pour les quatre limites obligatoires,
   choisir la politique de contradiction et la durée d'ordre. `max_open_orders`
   doit être entier. Mettre `mode="live"` et `enable_live=true` dans le fichier local.
4. Dans l'environnement de lancement, définir volontairement
   `POLY_ENABLE_LIVE=I_ACCEPT_LIVE_ORDERS`, puis exécuter soi-même :

```powershell
.\control.ps1 start -Mode live
.\control.ps1 status -Mode live
.\control.ps1 halt -Mode live
.\control.ps1 stop -Mode live
```

La préparation publique ne se convertit pas en ordres live : chaque mode possède
sa propre base et l'activation réelle commence par ses propres créneaux futurs.
Une erreur d'authentification, de balance, de permissions ou de disponibilité ne
déclenche aucun transfert ni achat alternatif. Les messages SDK complets ne sont
pas imprimés, car ils peuvent contenir des détails de requêtes authentifiées.

**L'agent n'a jamais activé live, lu les secrets existants, signé, soumis, annulé
ou modifié un ordre réel.** L'adaptateur est testé hors authentification avec des
fixtures ; l'authentification et l'exécution privées ne sont pas validées sur un
compte réel. Une activation utilisateur peut donc rencontrer un refus réel.

## Vérifications et sources

Voir `VERIFICATION.md` pour les tests et preuves de cette livraison. `evidence/`
conserve les réponses publiques et les pages officielles consultées. Le SHA-256
de la sélection est contrôlé à chaque ouverture de base ; toute modification
refuse la reprise. Les fonctions de calendrier ont aussi été comparées par AST
à celles du dry run existant.

- [Ordres, contraintes, GTC/GTD et seuil d'expiration](https://docs.polymarket.com/trading/place-orders)
- [SDK Python officiel actuel](https://docs.polymarket.com/getting-started/python)
- [Suivi des ordres](https://docs.polymarket.com/trading/manage-orders)
- [Ordre par hash, quantités normalisées et reliquat annulé](https://docs.polymarket.com/api-reference/trade/get-single-order-by-id)
- [Frais](https://docs.polymarket.com/trading/fees)
- [Marché sondé 9:45–9:50 Toronto, 6 octobre 2026](https://polymarket.com/event/btc-updown-5m-1791294300)
- [Source Chainlink de ce marché](https://data.chain.link/streams/btc-usd-twap-60s-streams)
