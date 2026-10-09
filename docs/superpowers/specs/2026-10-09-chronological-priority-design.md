# Priorité des créneaux futurs les plus proches

Mandat utilisateur : toujours commencer par le prochain marché avant un marché
plus éloigné. Avant signature, vérifier toutes les intentions, pas uniquement
celles dues dans le cycle. WAITING/PREPARED/WAITING_FUNDS encore signables et plus
proches bloquent uniquement les signatures suivantes, retry15s. Même ouverture
peut acheter les deux directions si policyboth. UNKNOWN/SENDING/LIVE/FILLED ne
font pas patienter la file car déjà soumis ; réserves cash habituelles gardées.
Intention passée/abandonnée/refusée ne bloque pas. Un carnet invalide ou marché
absent du créneau prioritaire fait patienter les achats suivants ; discovery
continue. Cette politique supersède l’ancien saut vers un marché plus éloigné.

Tests réessai futur non dû, absence de blocage UNKNOWN, financement séquentiel
17 intentions, et attente/reprise après insuffisance. Déployer sans vendre ni
modifier position de demain déjà exécutée.
