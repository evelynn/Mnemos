"""CWE → regulatory-framework mapping (PR-56, closes audit B7).

PR-50 attached a CWE id to security-relevant findings. The value
reassessment's B7 finding: a CWE id alone doesn't tell a
compliance officer *which* audit requirement the finding bears
on. This module maps a CWE to the entries it satisfies / violates
across the three frameworks Mnemos operators most often answer
to:

* **OWASP Top 10 (2021)** — the web-app baseline.
* **PCI DSS v4.0** — anyone touching cardholder data.
* **NIST SP 800-53 Rev 5** — the control catalogue most
  enterprise security programmes anchor on.

The mapping is deliberately small and hand-curated — it covers
the CWEs the Phase-1 finding taxonomy actually emits, not the
full 900-entry CWE corpus. Adding a finding kind with a new CWE
means adding one row here.
"""

from __future__ import annotations

# Each CWE maps to a dict of {framework: [requirement labels]}.
_CWE_COMPLIANCE: dict[str, dict[str, list[str]]] = {
    "CWE-1041": {  # Redundant code (duplicate_endpoint)
        "owasp": [],
        "pci_dss": [],
        "nist_800_53": ["CM-2 Baseline Configuration"],
    },
    "CWE-470": {  # Unsafe reflection (dynamic_call_detected)
        "owasp": ["A03:2021 Injection"],
        "pci_dss": ["6.2.4 Secure coding — injection flaws"],
        "nist_800_53": ["SI-10 Information Input Validation"],
    },
    "CWE-561": {  # Dead code (dead_path_suspected)
        # Dead code is a maintenance / attack-surface concern, not a
        # cardholder-data control — no honest PCI DSS requirement maps.
        "owasp": [],
        "pci_dss": [],
        "nist_800_53": ["CM-7 Least Functionality"],
    },
    "CWE-1078": {  # Inappropriate structure (schema_mismatch)
        "owasp": ["A04:2021 Insecure Design"],
        "pci_dss": [],
        "nist_800_53": ["SA-11 Developer Testing and Evaluation"],
    },
    "CWE-89": {  # SQL injection — if a future analyzer emits it
        "owasp": ["A03:2021 Injection"],
        "pci_dss": ["6.2.4 Secure coding — injection flaws"],
        "nist_800_53": ["SI-10 Information Input Validation"],
    },
    "CWE-200": {  # Information exposure
        "owasp": ["A01:2021 Broken Access Control"],
        "pci_dss": ["3.4 Protect stored cardholder data"],
        "nist_800_53": ["AC-3 Access Enforcement"],
    },
    "CWE-1357": {  # Reliance on insufficiently trustworthy component
        "owasp": ["A06:2021 Vulnerable and Outdated Components"],
        "pci_dss": ["6.3.2 Inventory of bespoke and custom software"],
        "nist_800_53": ["SR-3 Supply Chain Controls and Processes"],
    },
}

_FRAMEWORK_LABELS = {
    "owasp": "OWASP Top 10",
    "pci_dss": "PCI DSS v4.0",
    "nist_800_53": "NIST 800-53",
}


def compliance_for(cwe_id: str | None) -> dict[str, list[str]]:
    """Return the framework → requirements mapping for a CWE.

    A finding with no CWE, or a CWE not in the curated set,
    returns an empty mapping — never raises. Empty framework
    lists are dropped so the caller only sees frameworks that
    actually have a requirement.
    """
    if not cwe_id:
        return {}
    raw = _CWE_COMPLIANCE.get(cwe_id, {})
    return {k: v for k, v in raw.items() if v}


def compliance_tags(cwe_id: str | None) -> list[str]:
    """Flat list of ``"FRAMEWORK: requirement"`` strings — the
    shape the findings table renders as small pills."""
    out: list[str] = []
    for framework, reqs in compliance_for(cwe_id).items():
        label = _FRAMEWORK_LABELS.get(framework, framework)
        for req in reqs:
            out.append(f"{label}: {req}")
    return out
