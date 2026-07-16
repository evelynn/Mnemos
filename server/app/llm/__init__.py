"""Provider-neutral LLM accounting contracts.

The package deliberately contains no provider clients.  Adapters normalize
their provider-owned usage dialect at this boundary, while prompts and model
outputs remain in the purpose-specific caller.
"""

from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMPhysicalAttemptV1,
    LLMPurpose,
    LLMUsageContractError,
    LLMUsageV1,
    ResultStatus,
    UsageSource,
    UsageStatus,
    normalize_agent_sdk_usage,
    normalize_anthropic_usage,
    normalize_gemini_usage,
    normalize_openai_chat_usage,
    unavailable_usage,
)

__all__ = [
    "AttemptKind",
    "AttemptStatus",
    "LLMPhysicalAttemptV1",
    "LLMPurpose",
    "LLMUsageContractError",
    "LLMUsageV1",
    "ResultStatus",
    "UsageSource",
    "UsageStatus",
    "normalize_agent_sdk_usage",
    "normalize_anthropic_usage",
    "normalize_gemini_usage",
    "normalize_openai_chat_usage",
    "unavailable_usage",
]
