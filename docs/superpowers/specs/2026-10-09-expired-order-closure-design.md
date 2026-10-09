# Clôture des intentions passées

Mandat : utilisateur demande annulation dès créneau passé et aucune immobilisation
indéfinie des autres marchés. Annulation exacte à opening pour LIVE/UNKNOWN, jamais
cancel_all et jamais undo des fills. Échec cancel n’empêche pas les autres lectures.

Clôture d’une réservation UNKNOWN : GTD expiré depuis900s, winner officiel connu,
reply cancel terminal (canceled ou already canceled/matched), historique authentifié
paginé réussi, absence de règlement pending, quantité observée égale confirmée,
solde frais lu. CLOSED_UNCONFIRMED trace incertitude, jamais repost. Suivi continue
pour fills tardifs et réservation restaurée si pending. Réveil immédiat du funds
poll après libération. Expiration seule, simple absence ou API indisponible ne
suffisent pas. Les autres intentions utilisent uniquement cash net réellement lu.

Tests : absence de terminal ack, grace, fill confirmé malgré cancel failure, pending
non libéré, fonds réutilisables après clôture et absence de second POST.
