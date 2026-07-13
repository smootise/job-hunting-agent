"""Enrichment: turn a stored offer into an offer with a resolved office
address and realistic commute times.

Three small, single-purpose modules, deterministic on purpose (the online
address-research *agent* is a Phase 3 concern):

  * ``geocode``  — free-text address → coordinates, via Base Adresse Nationale,
                   with Île-de-France region validation.
  * ``address``  — resolve an offer's best office address (posting/WTTJ, else a
                   city-centroid fallback flagged ``approximate``).
  * ``routing``  — coordinates → commute minutes, via Google Routes, composed
                   into the three commute strategies (bike_only / bike_hybrid /
                   no_bike) the owner reviews.

The pipeline orchestration that reads offers from the DB and writes the results
back lives in ``jobscout.pipeline.enrich_commute`` — these modules stay I/O-thin
and independently testable (each takes an injectable HTTP client).
"""
