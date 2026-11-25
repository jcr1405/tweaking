"""Presence Model Providers Subsystem"""

from runtime.providers.base import IModelProvider, ModelStreamChunk
from runtime.providers.gemini import GeminiModelProvider
from runtime.providers.fake import FakeModelProvider
from runtime.providers.freellmapi import FreeLLMAPIProvider, FreeLLMAPIConfig
from runtime.providers.ollama import OllamaModelProvider, OllamaConfig
from runtime.providers.coordinator import (
    MultiProviderCoordinator,
    InferenceRequestConfig,
    FailureCategory,
    classify_provider_error,
)

__all__ = [
    "IModelProvider",
    "ModelStreamChunk",
    "GeminiModelProvider",
    "FakeModelProvider",
    "FreeLLMAPIProvider",
    "FreeLLMAPIConfig",
    "OllamaModelProvider",
    "OllamaConfig",
    "MultiProviderCoordinator",
    "InferenceRequestConfig",
    "FailureCategory",
    "classify_provider_error",
]

