# Installation persistante sur VPS Linux

Exemple Ubuntu 24.04 / Debian 12 ou plus récent, avec systemd et **Python 3.11
minimum** (exigé par le SDK). Aucune connexion au VPS ou activation réelle
n'a été effectuée par l'agent. Le code, les règles et SQLite supportent Linux ;
le calendrier reste America/Toronto, quel que soit le fuseau du VPS.

## Installation une seule fois

Cloner le dépôt public, sans authentification. Le clone reste dans le dossier
personnel, et le service
utilise une copie du code dans `/opt` sans credentials GitHub.

Dans le terminal SSH du VPS :

```bash
sudo apt update
sudo apt install -y git python3 python3-venv rsync
git clone https://github.com/nirre55/btc-m5-polymarket.git ~/btc-m5-polymarket
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ requis"'
sudo useradd --system --user-group --home-dir /opt/btc-m5-polymarket --shell /usr/sbin/nologin polymarket-bot
sudo install -d -m 755 /opt/btc-m5-polymarket
sudo rsync -a --exclude='.git/' --exclude='.venv/' --exclude='state*/' --exclude='config.local.json' --exclude='.env*' --exclude='__pycache__/' ~/btc-m5-polymarket/ /opt/btc-m5-polymarket/
sudo python3 -m venv /opt/btc-m5-polymarket/.venv
sudo /opt/btc-m5-polymarket/.venv/bin/python -m pip install -r /opt/btc-m5-polymarket/requirements.txt
sudo install -d -m 700 -o polymarket-bot -g polymarket-bot /opt/btc-m5-polymarket/state_prepare /opt/btc-m5-polymarket/state_paper /opt/btc-m5-polymarket/state_live
sudo install -d -m 750 -g polymarket-bot /etc/btc-m5-polymarket
sudo install -m 640 -g polymarket-bot /opt/btc-m5-polymarket/config.example.json /etc/btc-m5-polymarket/config.local.json
sudo install -m 600 /opt/btc-m5-polymarket/linux/credentials.env.example /etc/btc-m5-polymarket/credentials.env
sudo install -m 644 /opt/btc-m5-polymarket/linux/btc-m5-polymarket@.service /etc/systemd/system/btc-m5-polymarket@.service
sudo systemctl daemon-reload
```

Ces commandes d'installation initiale supposent que le compte et les fichiers
n'existent pas encore. Pour une mise à jour, **ne pas recopier les exemples sur
la configuration ou les secrets existants** ; conserver aussi les dossiers state.
Le code appartient à root ; seuls les dossiers state sont modifiables par le bot.

La surveillance utilise un horizon roulant de 72 heures. Pour une installation
existante, modifier seulement `horizon_hours` dans le fichier de configuration
local, sans recopier les exemples ni remplacer les credentials.

## Secrets enregistrés une seule fois

```bash
sudo nano /etc/btc-m5-polymarket/credentials.env
```

Remplacer les deux valeurs REPLACE_LOCALLY par la vraie clé privée et le vrai
funder. Conserver les noms de variables de l'autre bot, type 3 et URL CLOB.
Le fichier est réservé à root (`600`) ; systemd le lit et transmet les variables
au processus. Le bot ne doit pas charger le `.env` lui-même. Pas d'`export` manuel
à chaque connexion et aucune clé dans les commandes ou leur historique.
Les credentials CLOB sont créés/dérivés automatiquement à l'initialisation live.

## Observation au boot

```bash
sudo systemctl enable --now btc-m5-polymarket@prepare
sudo systemctl status btc-m5-polymarket@prepare --no-pager
```

Le service continue après fermeture de SSH, redémarre après panne du processus
avec une pause de 30 secondes et se lance au boot. Un arrêt demandé via systemctl
reste un arrêt ; systemd ne le relance pas immédiatement. Une erreur de
configuration reste une erreur : elle apparaîtra dans les logs jusqu'à correction.

## Activation réelle par l'utilisateur, une seule fois

Dans `/etc/btc-m5-polymarket/config.local.json`, définir `enable_live=true` et ses
quatre plafonds personnels : `max_order_cost`, `max_total_committed_cost`,
`max_daily_committed_cost`, `max_open_orders` (entier). Le mode du service est
imposé par son instance `@live`. Ne pas laisser les plafonds à null.

Alternative explicitement choisie : `use_available_balance=true` permet de laisser
ces quatre plafonds à null et d'ouvrir au minimum du marché tant que le solde lu,
net des réservations et avec marge pour frais, suffit. En cas de manque, la pause
commune est de deux heures (`balance_retry_seconds=7200`), persistante après reboot.
L'auto-redeem externe peut restituer des fonds ; le bot attend leur crédit réel.

Dans `credentials.env`, décommenter volontairement la ligne :

```text
POLY_ENABLE_LIVE=I_ACCEPT_LIVE_ORDERS
```

Puis exécuter **soi-même** :

```bash
sudo systemctl disable --now btc-m5-polymarket@prepare
sudo systemctl enable --now btc-m5-polymarket@live
sudo systemctl status btc-m5-polymarket@live --no-pager
```

À partir de là, une ouverture de SSH, un redémarrage du VPS ou une panne du
processus n'exige pas de ressaisir les credentials ni les plafonds. Le réel
redémarre automatiquement au boot tant que le service @live est enabled.
Le lancement peut acheter des marchés futurs immédiatement au meilleur ask.

## Commandes courantes

```bash
# Etat du processus et logs (aucun fichier de secrets affiché)
sudo systemctl status btc-m5-polymarket@live --no-pager
sudo journalctl -u btc-m5-polymarket@live -n 50 --no-pager

# Relancer après un changement de configuration/credentials
sudo systemctl restart btc-m5-polymarket@live

# Arrêter maintenant, conserver l'activation au prochain boot
sudo systemctl stop btc-m5-polymarket@live

# Arrêter maintenant ET empêcher le démarrage au boot
sudo systemctl disable --now btc-m5-polymarket@live

# Suspendre les nouveaux ordres, mais garder le suivi et le service au boot
sudo -u polymarket-bot touch /opt/btc-m5-polymarket/state_live/HALT

# Reprendre les nouveaux ordres : retire seulement le fichier HALT de ce bot
sudo -u polymarket-bot /opt/btc-m5-polymarket/.venv/bin/python /opt/btc-m5-polymarket/bot.py resume --mode live --config /etc/btc-m5-polymarket/config.local.json
```

HALT persiste après un reboot ; le service continue alors ses observations.
**Arrêt et HALT n'annulent pas les ordres déjà soumis.**

Rapports dans `/opt/btc-m5-polymarket/state_live/report.html`, `report.json` et
`status.json`. On peut les récupérer par SSH/SFTP pour lecture locale, sans publier
de serveur web ni exposer les secrets. SQLite et les prix initiaux sont repris
au redémarrage ; ne pas effacer les dossiers state. Un intervalle passé pendant
l'arrêt ne donne jamais lieu à un nouvel ordre rétrospectif.

Le lancement systemd utilise **run**, pas start : systemd supervise le vrai
processus Python au premier plan. SIGTERM/SIGINT déclenchent un arrêt coopératif,
bloquent les nouvelles soumissions et conservent la base. Aucun service n'a été
installé ou démarré sur le VPS depuis ce chat.

Références officielles :
[services systemd](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml),
[environnement du processus](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).
