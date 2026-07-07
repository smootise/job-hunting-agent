"""Address resolution and commute estimation for filtered survivors.

Runs only on offers that already passed the hard filters (it costs API
calls). Deterministic code except for the address-research agent, which
is invoked as a last resort when the posting, the company's WTTJ profile,
and the official company registry all fail to yield a usable address.

Invariant (do not violate): an inferred, registry-derived, or
approximate address may never hard-reject an offer on commute time. Only
a confirmed address (source = posting or wttj) can trigger that filter.
"""
