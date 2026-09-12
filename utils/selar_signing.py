# path: utils/selar_signing.py

"""
HMAC signing for the Selar manual-payment web redirect flow
(payments_manual.py -> api/selar_redirect.py -> app/unlock -> api/selar_submit.py).

Same trust shape as pow_captcha.py: POW_SECRET_KEY signs a few fields we
handed out ourselves, so a later request can prove "this reference/buyer/
target combination is one we actually generated" instead of trusting
whatever a client puts in the query string. This is the only thing standing
between "/unlock is reachable exclusively via our own Selar redirect_url"
and "/unlock is reachable by anyone who guesses or copies a reference" —
the reference itself (selar_<type>_<user_id>_<hex>) is visible in the pay
link and in the Selar dashboard, so it must NOT be treated as a secret on
its own.

Not encryption (nothing here is confidential — reference/payment_type/ids
are all fine to appear in a URL bar) — it's tamper-evidence, which is the
actual property needed: reject anything whose fields don't match a
signature only this backend could have produced.
"""
import hashlib
import hmac
import time

from config import POW_SECRET_KEY

# How long a signed /unlock target stays valid, counted from the moment
# payments_manual.py minted the Selar pay link. Generous on purpose — a
# slow checkout on Selar's own pages shouldn't expire the link out from
# under a legitimate buyer — but bounded, so a captured redirect_url
# (browser history, shared screenshot, proxy log) can't be replayed against
# a *different* buyer indefinitely. Replay against the *same* buyer within
# this window is still harmless no-op territory once claim_manual_payment_
# for_review has already flipped the row out of 'pending'.
SELAR_LINK_TTL_SECONDS = 60 * 60


def _signing_string(reference: str, payment_type: str, buyer_id: int,
                     guild_id: int | None, clone_id: int | None, ts: int) -> str:
    # None -> "-" so a guild-scoped and a clone-scoped payment can never
    # collide on the same signed string. ts is part of the signed message
    # (not just compared out-of-band) so a client can't swap in a fresher
    # ts without invalidating the signature.
    return f"{reference}:{payment_type}:{buyer_id}:{guild_id or '-'}:{clone_id or '-'}:{ts}"


def sign_selar_target(reference: str, payment_type: str, buyer_id: int,
                       guild_id: int | None = None, clone_id: int | None = None,
                       ts: int | None = None) -> tuple[str, int]:
    """Returns (signature, ts). Callers must carry both ts and signature
    forward in the URL/body — verify_selar_target needs the exact ts that
    was signed, not wall-clock time, to recompute the HMAC."""
    if ts is None:
        ts = int(time.time())
    msg = _signing_string(reference, payment_type, buyer_id, guild_id, clone_id, ts).encode()
    sig = hmac.new(POW_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return sig, ts


def verify_selar_target(reference: str, payment_type: str, buyer_id: int,
                         guild_id: int | None, clone_id: int | None,
                         ts: int, signature: str) -> bool:
    if not POW_SECRET_KEY or not signature:
        return False
    # Reject non-numeric/garbage ts before it ever reaches the HMAC compare.
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    if ts > now + 60:  # small allowance for clock skew, not a real future ts
        return False
    if now - ts > SELAR_LINK_TTL_SECONDS:
        return False
    expected, _ = sign_selar_target(reference, payment_type, buyer_id, guild_id, clone_id, ts=ts)
    return hmac.compare_digest(expected, signature)
