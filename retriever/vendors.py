"""The single vendor vocabulary for the last mile — carriers, aliases, and how a name is folded.

This used to live in ``make_delivery_topology.py`` alone, which was fine while only the build step
needed it. The consumption side now needs the same vocabulary at query time (``delivery_chain``
turns a use case's declared channels + route/router values into the exit path), and two copies of a
carrier whitelist is exactly how a vendor silently splits into two buckets again (RUNBOOK-49/51).
So the vocabulary lives here and the build script imports it.

Editing rule (AGENTS.md §4): this is CODE vocabulary, not a data knob — a new carrier appearing in
repo names is a code change here, deliberately, because every consumer's behaviour shifts with it.
"""

# A few vendors appear under more than one token in repo names. 3HK's repos carry its legal name
# "htcl" (Hutchison Telecommunications) while the diagram and the business call it "3hk"; left
# unaliased they split into two vendor buckets (RUNBOOK-49).
VENDOR_ALIASES = {"htcl": "3hk"}

# The real last-mile carriers/vendors that legitimately appear as a token in a delivery-job name.
# The vendor is the RIGHTMOST *known* token (see pick_vendor); a name whose only pre-channel tokens
# are segment/mode/system words (hase, hr, rt, bat, svc, gen, …) has NO vendor and buckets under
# "unknown" rather than mis-promoting one of those words. This stops both phantom vendor buckets
# (`hr`, `iccm*` — RUNBOOK-50/51) and the `mc-hk-hase-sms-deli-job` → "hase" regression.
# Names are CANONICAL (post-canon_vendor): htcl folds to 3hk, the iccm* family folds to iccm.
KNOWN_VENDORS = frozenset({
    "csl", "sinch", "3hk", "cm", "lx", "aurora", "awssg", "awshk",  # sms / push carriers
    "pfp", "pps", "sfmc",                                            # email
    "iccm", "otx",                                                  # letter
    "haro",                                                         # whatsapp
    "sns", "apns", "fcm",                                           # push infra / terminals
})
UNKNOWN_VENDOR = "unknown"


def canon_vendor(vendor):
    """Fold a raw vendor token onto its canonical name (identity when no alias applies).

    The ICCM letter platform appears under family variants in repo names (iccms, iccmh, iccmt,
    iccmv, iccmpt); collapse the whole `iccm*` family to `iccm` so they don't split into phantom
    per-variant vendor buckets (RUNBOOK-51).

    RUNBOOK-52 investigated whether `iccm` and `3hk` should really be one vendor for SMS (an
    earlier finding suggested some ICCM-SMS jobs silently route through 3HK's gateway). CLOSED,
    kept separate: real HTCL/CSL/Sinch/CM traffic goes straight into its own topic/job, never
    through an ICCM job. Only 2 of 13 ICCM-SMS repos have a default-only fallback to
    HUTCHISON_GW_SMS (triggers on an empty/unrecognized router), the fallback's own live-ness is
    unconfirmed (no matching Router bean found), and the real DB config snapshot showed 0/79 empty
    routers — the trigger condition doesn't occur in practice. Do not re-merge iccm into 3hk on a
    future re-read of this same evidence; see RUNBOOK-52 for the full trace."""
    vendor = (vendor or "").strip().lower()
    if vendor.startswith("iccm"):
        return "iccm"
    return VENDOR_ALIASES.get(vendor, vendor)


def pick_vendor(tokens):
    """The vendor is the rightmost token whose canonical form is a KNOWN carrier; if none of the
    tokens is a known vendor, there is no identifiable carrier and this returns ``unknown``."""
    for token in reversed(list(tokens or [])):
        canon = canon_vendor(token)
        if canon in KNOWN_VENDORS:
            return canon
    return UNKNOWN_VENDOR


def vendors_in(text, separators="-_. /,:"):
    """Every KNOWN vendor named anywhere in a free-text value, canonicalized, in name order.

    Used on `tbl_use_case_channel_rule.route` / `.router` / `.sender`, whose values look like
    `CSL_SVC_RT_SMS` / `CM_HTTP_SMS` (RUNBOOK-56, read off a screenshot of the colleagues' AIOps
    output — the shape is real, but that these columns carry the carrier is NOT owner-confirmed;
    RUNBOOK-54 question 1 is still open). Callers must therefore label anything derived from this
    a HINT, never the authoritative vendor.

    Unlike ``pick_vendor`` this returns every match rather than one winner: a route value naming
    two carriers is a real (if odd) possibility, and silently keeping one would be a fabricated
    narrowing.
    """
    table = str.maketrans({character: " " for character in separators})
    found, seen = [], set()
    for token in str(text or "").translate(table).split():
        canon = canon_vendor(token)
        if canon in KNOWN_VENDORS and canon not in seen:
            seen.add(canon)
            found.append(canon)
    return found
