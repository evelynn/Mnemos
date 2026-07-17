"""Immutable worst-case price contracts for hard project-dollar budgets.

The display-only flat token rate in :mod:`app.extractor.cost` is deliberately
not used here.  A hard authorization needs an exact provider route, exact
model id, an official model input ceiling, and a provider-enforced output
ceiling.  Unknown/custom/agentic routes therefore have no contract and fail
closed before provider dispatch when the project dollar guard is enabled.

Contracts are versioned code facts.  Updating a price or capability requires
adding a new id (and tests), never mutating an existing contract in place.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from urllib.parse import urlsplit


MONEY_QUANTUM = Decimal("0.0000000001")
ONE_MILLION = Decimal(1_000_000)


class PriceContractError(ValueError):
    """A request cannot be authorized by an immutable price contract."""


@dataclass(frozen=True, slots=True)
class PriceContract:
    """One exact, standard text-only provider route and model contract."""

    contract_id: str
    provider: str
    billing_route: str
    model: str
    api_base_url: str
    max_input_tokens: int
    max_output_tokens: int
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    source_url: str
    source_checked_on: str

    def __post_init__(self) -> None:
        for name in (
            "contract_id",
            "provider",
            "billing_route",
            "model",
            "api_base_url",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise PriceContractError(f"{name} must be a non-empty canonical string")
        if len(self.contract_id) > 96:
            raise PriceContractError("contract_id exceeds the ledger bound")
        try:
            parsed_base_url = urlsplit(self.api_base_url)
            parsed_base_port = parsed_base_url.port
        except ValueError:
            raise PriceContractError(
                "api_base_url must be a canonical credential-free HTTPS root"
            ) from None
        if (
            parsed_base_url.scheme.casefold() != "https"
            or not parsed_base_url.hostname
            or parsed_base_port not in {None, 443}
            or parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or parsed_base_url.query
            or parsed_base_url.fragment
            or "?" in self.api_base_url
            or "#" in self.api_base_url
        ):
            raise PriceContractError(
                "api_base_url must be a canonical credential-free HTTPS root"
            )
        for name in ("max_input_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PriceContractError(f"{name} must be a positive integer")
        for name in ("input_usd_per_mtok", "output_usd_per_mtok"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise PriceContractError(f"{name} must be a finite positive Decimal")

    def reserve(self, requested_output_tokens: int) -> "PriceReservation":
        """Reserve the official full input ceiling and requested output cap.

        ``estimated_input_tokens`` is useful telemetry but cannot prove a hard
        upper bound.  The model's official input/context ceiling is used even
        for a small prompt.  Output is safe only because each supported direct
        route sends the same provider-enforced maximum in its request.
        """

        if (
            isinstance(requested_output_tokens, bool)
            or not isinstance(requested_output_tokens, int)
            or requested_output_tokens <= 0
            or requested_output_tokens > self.max_output_tokens
        ):
            raise PriceContractError(
                "requested output exceeds the supported provider ceiling"
            )
        raw = (
            Decimal(self.max_input_tokens) * self.input_usd_per_mtok
            + Decimal(requested_output_tokens) * self.output_usd_per_mtok
        ) / ONE_MILLION
        amount = raw.quantize(MONEY_QUANTUM, rounding=ROUND_CEILING)
        if amount <= 0:
            raise PriceContractError("reservation rounded to a non-positive amount")
        return PriceReservation(
            price_contract_id=self.contract_id,
            reserved_input_tokens=self.max_input_tokens,
            reserved_output_tokens=requested_output_tokens,
            reserved_cost_usd=amount,
        )


@dataclass(frozen=True, slots=True)
class PriceReservation:
    """Canonical no-refund reservation persisted on one physical attempt."""

    price_contract_id: str
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_usd: Decimal


# These profiles match the outbound payloads in Mnemos: standard synchronous
# text generation with no billable tools, grounding, batch, prompt-cache write,
# or custom proxy.  Source URLs are retained for audit and refresh work.
#
# The Sonnet contract deliberately keeps the former official >200K long-context
# rates as a conservative authorization ceiling.  Current standard 1M pricing
# is lower, so this cannot under-reserve if Anthropic changes the transition
# treatment or account routing.  It may over-reserve, which is the safe side of
# a hard no-refund budget.
_CONTRACTS = (
    PriceContract(
        contract_id=(
            "anthropic.messages.claude-sonnet-4-6.conservative-1m.2026-07-16.v2"
        ),
        provider="anthropic",
        billing_route="anthropic_messages_api",
        model="claude-sonnet-4-6",
        api_base_url="https://api.anthropic.com",
        max_input_tokens=1_000_000,
        max_output_tokens=128_000,
        input_usd_per_mtok=Decimal("6"),
        output_usd_per_mtok=Decimal("22.50"),
        source_url="https://platform.claude.com/docs/en/about-claude/pricing",
        source_checked_on="2026-07-16",
    ),
    PriceContract(
        contract_id="openai.chat.gpt-4o-2024-11-20.2026-07-16.v2",
        provider="openai",
        billing_route="openai_chat_completions_api",
        model="gpt-4o-2024-11-20",
        api_base_url="https://api.openai.com/v1",
        max_input_tokens=128_000,
        max_output_tokens=16_384,
        input_usd_per_mtok=Decimal("2.5"),
        output_usd_per_mtok=Decimal("10"),
        source_url="https://developers.openai.com/api/docs/models/gpt-4o",
        source_checked_on="2026-07-16",
    ),
    PriceContract(
        contract_id="google.generate.gemini-2.5-flash.2026-07-16.v2",
        provider="gemini",
        billing_route="gemini_generate_content_api",
        model="gemini-2.5-flash",
        api_base_url="https://generativelanguage.googleapis.com/v1beta",
        max_input_tokens=1_048_576,
        max_output_tokens=65_536,
        input_usd_per_mtok=Decimal("0.30"),
        output_usd_per_mtok=Decimal("2.50"),
        source_url="https://ai.google.dev/gemini-api/docs/pricing",
        source_checked_on="2026-07-16",
    ),
)


PRICE_CONTRACTS = {
    (item.provider, item.billing_route, item.model): item for item in _CONTRACTS
}
PRICE_CONTRACTS_BY_ID = {item.contract_id: item for item in _CONTRACTS}
if len(PRICE_CONTRACTS) != len(_CONTRACTS):  # pragma: no cover - import invariant
    raise RuntimeError("duplicate immutable LLM price-contract key")
if len(PRICE_CONTRACTS_BY_ID) != len(_CONTRACTS):
    raise RuntimeError("duplicate immutable LLM price-contract id")


def _single_contract_api_base_url(*, provider: str, billing_route: str) -> str:
    routes = {
        item.api_base_url
        for item in _CONTRACTS
        if item.provider == provider and item.billing_route == billing_route
    }
    if len(routes) != 1:  # pragma: no cover - import invariant
        raise RuntimeError("billing route has no single attested API base URL")
    return routes.pop()


# Transport adapters import these values from the active immutable contracts;
# they are not independently duplicated endpoint configuration.
OFFICIAL_ANTHROPIC_API_BASE_URL = _single_contract_api_base_url(
    provider="anthropic",
    billing_route="anthropic_messages_api",
)
OFFICIAL_OPENAI_CHAT_API_BASE_URL = _single_contract_api_base_url(
    provider="openai",
    billing_route="openai_chat_completions_api",
)
OFFICIAL_GEMINI_GENERATE_API_BASE_URL = _single_contract_api_base_url(
    provider="gemini",
    billing_route="gemini_generate_content_api",
)


def is_price_attested_openai_chat_base_url(value: object) -> bool:
    """Return whether ``value`` is the exact official priced Chat API root.

    A hostname match is insufficient for a hard-dollar authorization: an
    insecure scheme, custom port/path, or user-info/query/fragment can route
    the request through a different service while looking superficially like
    ``api.openai.com``.  Only the contract-owned HTTPS ``/v1`` root owns
    Mnemos's immutable OpenAI price contract.
    """

    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    try:
        parsed = urlsplit(value)
        attested = urlsplit(OFFICIAL_OPENAI_CHAT_API_BASE_URL)
        parsed_port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == attested.scheme.casefold() == "https"
        and parsed.hostname is not None
        and attested.hostname is not None
        and parsed.hostname.casefold() == attested.hostname.casefold()
        and parsed.netloc.casefold()
        in {
            attested.netloc.casefold(),
            f"{attested.netloc.casefold()}:443",
        }
        and parsed.username is None
        and parsed.password is None
        and parsed_port in {None, 443}
        and parsed.path == attested.path == "/v1"
        and parsed.query == ""
        and parsed.fragment == ""
        # ``urlsplit`` cannot distinguish an absent delimiter from an empty
        # query/fragment, so reject both delimiters explicitly as noncanonical.
        and "?" not in value
        and "#" not in value
    )


def _canonicalize_price_catalog(contracts: tuple[PriceContract, ...]) -> str:
    return "\n".join(
        "\x00".join(
            (
                item.contract_id,
                item.provider,
                item.billing_route,
                item.model,
                item.api_base_url,
                str(item.max_input_tokens),
                str(item.max_output_tokens),
                format(item.input_usd_per_mtok, "f"),
                format(item.output_usd_per_mtok, "f"),
            )
        )
        for item in sorted(contracts, key=lambda value: value.contract_id)
    )


# A project account snapshots this digest-derived version, not merely its
# cap/window.  Any billing-relevant catalog edit therefore makes mixed-version
# workers fail closed until an explicit account-policy rotation is deployed.
_CATALOG_CANONICAL = _canonicalize_price_catalog(_CONTRACTS)
PRICE_CATALOG_SHA256 = hashlib.sha256(
    _CATALOG_CANONICAL.encode("utf-8")
).hexdigest()
PRICE_CATALOG_POLICY_VERSION = f"usd3:{PRICE_CATALOG_SHA256[:27]}"
if len(PRICE_CATALOG_POLICY_VERSION) > 32:  # pragma: no cover - DB invariant
    raise RuntimeError("price-catalog policy version exceeds account bound")


def resolve_price_contract(
    *, provider: str, billing_route: str | None, model: str
) -> PriceContract:
    """Return an exact contract; aliases and opaque routes are unsupported."""

    if not billing_route:
        raise PriceContractError("billing route is not price-attested")
    try:
        return PRICE_CONTRACTS[(provider, billing_route, model)]
    except KeyError:
        raise PriceContractError(
            "provider, billing route, or model has no immutable price contract"
        ) from None


def resolve_price_contract_by_id(contract_id: str) -> PriceContract:
    """Return one immutable historical contract by its persisted identifier."""

    try:
        return PRICE_CONTRACTS_BY_ID[contract_id]
    except KeyError:
        raise PriceContractError(
            "persisted reservation has no immutable price contract"
        ) from None
