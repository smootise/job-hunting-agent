"""Source adapters: one module per job board, each emitting a common shape.

Every adapter (Welcome to the Jungle, France Travail, LinkedIn alert
emails, ...) is responsible only for turning that source's native
response into the shared dict shape:

    {source, external_id, url, title, company, location, contract_type,
     salary_text, description, posted_at, lang}

Keeping that shape identical across sources is what lets everything
downstream (dedupe, filters, enrichment, scoring) stay source-agnostic.
"""
