"""Hierarchical summarizer (L1~L5) with evidence validator (spec §10).

No eager re-exports: importing ``app.extractor.cost`` from ``app.llm.
lifecycle`` must not drag in ``agent``/``agent_sdk``, which import
``app.llm.lifecycle`` back (the package-init edge closed that cycle and
broke any entry order that loaded ``app.llm.lifecycle`` first). Import
``Extractor``/``validate_claims`` from their defining submodules.
"""
