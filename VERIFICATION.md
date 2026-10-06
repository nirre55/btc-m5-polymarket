# Vérification du 6 octobre 2026

Livraison initiale dans `bots/BTCUSDT_M5_POLYMARKET`, indépendante du service existant.
Ce rapport décrit la vérification dans le workspace d’origine. Les sondes JSON
et données `state_prepare` mentionnées ci-dessous ne sont pas distribuées dans
le dépôt ; seuls les journaux de tests sont inclus.

## Contrôles terminés

- **48 tests passent** via `control.ps1 test` / Python unittest : calendrier exact,
  filtres numériques, DST, 567 règles et deux cohortes, identité AST avec le
  calendrier du dry run, correspondance du marché et version d'actif, carnets,
  Decimal, min/tick, reprises, idempotence, anti-entrée après début, prix conservé,
  contradictions, limites partagées, HALT, réponse ambiguë sans resoumission,
  fills partiels et confirmed, annulation du reliquat, résultat Binance doji
  séparé, résolution retardée, simulation isolée, adaptateur maker/taker et
  interfaces SDK. Sortie : `evidence/tests.txt`.
- Syntaxe Python vérifiée pour les sept fichiers Python ; `pip check` : aucune
  dépendance cassée. SDK officiel installé uniquement dans `.venv` du nouveau bot.
- Sélection identique octet pour octet à la passation ; SHA-256 :
  `5dde1f027cbc7b8fd88f5d02b3cc70777380b8cbfcd7be47d985534f3e407013`.
- Sondes réelles GET Gamma, CLOB time, markets et carnets des deux outcomes.
  Marché `btc-updown-5m-1791294300` : début UTC 13:45, fin 13:50,
  soit 09:45–09:50 America/Toronto le 6 octobre. Il existait et acceptait des
  ordres avant début. Minimum CLOB **5 shares**, tick **0,01** pour UP et DOWN.
  Un second marché futur à une heure a été sondé avec les mêmes contraintes.
  Un créneau à 24 heures n'était pas encore listé. Preuve :
  `evidence/public_probe_initial.json` et `state_prepare/probe.json`.
- Les métadonnées actuelles décrivent un TWAP Chainlink BTC/USD de 60 secondes,
  égalité → Up. Le texte, les heures, l'acceptation et les identifiants ont été
  archivés, sans assimiler ces prix aux bougies Binance.
- Certains carnets de marchés plus lointains avaient un timestamp ancien malgré
  leur disponibilité. La vérification les a effectivement refusés et placés en
  réessai, sans marché de remplacement ni prix inventé.
- Le premier marché sondé, terminé, était encore `closed=false` sans winner lors
  de la dernière lecture : aucun règlement déduit de la fin de période.
  Preuves : `evidence/resolution_probe.json`, `evidence/resolution_final.json`.
- Service `prepare` démarré invisiblement, arrêté coopérativement, redémarré et
  laissé actif. `start` répété retourne déjà actif. Le lancement tient compte du
  processus enfant pythonw des venv Windows via un jeton de lancement distinct.
- Après reprise : les deux prix initiaux **0,50** et **0,51** sont conservés,
  pas de doublon d'intention, `PRAGMA integrity_check=ok`, **0 fill**. Une intention
  passée est correctement `PREPARED_NOT_SUBMITTED`, une autre reste PREPARED.
  Snapshot : 20 intentions / 20 bougies uniques / 24 activations de règles.
- Nouveau processus vérifié : PID **27388**. Ancien dry run vérifié vivant :
  PID **30972**, heartbeat récent. Son code, sa sélection et sa base n'ont pas
  été modifiés par cette implémentation. Ces PID et comptes sont un snapshot,
  pas une garantie après arrêt/redémarrage.

Preuve consolidée : `evidence/final_runtime.json`. Le rapport opérationnel se
trouve dans `state_prepare/report.html`, avec JSON/CSV et état du service.

## Limites de la vérification

Aucun secret existant lu ; aucun client authentifié créé ; aucun ordre réel signé,
soumis, annulé ou modifié ; aucune transaction financière ni exécution live.
Les objets de test contenant le champ `signature` utilisent une valeur vide
`0x`, sans clé ni calcul de signature. Le hash EIP-712 a été comparé hors ligne
à l'encodage standard eth-account, sans signer.

Les API privées, les credentials, allowances, fonds et réponses de soumission
réelles ne sont donc pas validés sur le compte utilisateur. Le code d'exécution
est livré pour configuration et activation **par l'utilisateur**. Le SDK est
figé à 0.12.0 ; les helpers internes de hash nécessitent revalidation avant une
mise à jour. Les simulations ne prouvent aucune performance ni rentabilité.

Le contrôle de fraîcheur peut retarder la préparation de marchés futurs calmes.
Le mode réel refuse toute nouvelle soumission après ouverture. GTC ne possède
aucune échéance locale ; GTD conserve sa date initiale. Un reliquat annulé et les
shares déjà exécutées restent des objets distincts dans le suivi.

## Suivi : configuration avec quatre variables

Adaptateur compatible avec `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`,
`POLYMARKET_SIGNATURE_TYPE=3`, `POLYMARKET_API_URL=https://clob.polymarket.com`.
En live, le SDK crée/dérive automatiquement les credentials CLOB si le trio
manuel n'est pas fourni. Aucune authentification réelle n'a été exécutée pour
cette modification. Tests avec bootstrap entièrement mocké : passage des deux
valeurs signer/funder, absence de credentials imposés, compatibilité ancienne,
refus de trio partiel, URL différente, alias contradictoires et type de signature
discordant. Le client est fermé sans ordre en cas de type discordant.

Suite complète après modification : **53 tests passent**. Le script local
`configure_credentials.ps1` a été vérifié par le parseur PowerShell sans être
exécuté : aucune saisie ou lecture de secret. Mode réel toujours désactivé.

## Suivi : VPS Linux et persistance

Unité systemd `linux/btc-m5-polymarket@.service`, exemple de fichier de credentials
root-only, guide d'installation unique et activation manuelle, bases persistantes
par mode, redémarrage après panne et au boot. Aucun VPS distant contacté et aucune
instance live activée. Le `.env` est chargé par systemd, pas par le bot.

Suite complète : **55 tests passent** sous Windows. Sous Ubuntu/WSL, **44 tests
du calendrier, des données publiques et de l'engine passent** avec Python 3.10.6.
Contrôles Linux supplémentaires hors réseau : arrêt coopératif SIGTERM, libération
du verrou fcntl, remise à zéro du vieux marqueur stop et conservation de HALT.
`systemd-analyze verify` valide les directives de l'unité, en substituant un
exécutable Python installé localement au chemin de production non installé.
Preuves : `evidence/linux_tests.txt`, `evidence/linux_service_check.txt`.
Le déploiement réel du service, les permissions du VPS et ses accès réseau restent
à vérifier sur le VPS utilisateur. Le test AST comparatif saute dans une archive
autonome sans le projet d'origine ; le SHA canonique et les 567 règles restent
vérifiés sans dépendre de ce projet.
