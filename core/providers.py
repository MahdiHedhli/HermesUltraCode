"""Provider abstraction over the model call (invariant 6 / startup rule).

The reviewer is routed to a provider that MUST differ from the orchestrator's
provider — a different *lab* (posttraining divergence), not same-family-different-
size. ``validate_distinct_providers`` enforces this at startup and hard-fails if
they match.

The reviewer call is wrapped with a timeout. Error / timeout / quota / empty are
all mapped to a ``ReviewerUnavailable`` not-a-pass signal so the gate fails closed
(invariant 1). No network is imported in the hot path that tests can't mock:
``MockProvider`` is the offline test double; real providers raise unless given live
credentials, so a unit run never touches the wire.
"""

from __future__ import annotations

import abc
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass


class ProviderError(RuntimeError):
    """Base class for provider failures (network, auth, etc.)."""


class ProviderQuotaError(ProviderError):
    """Provider rejected the call for quota / rate-limit reasons."""


class ProviderTimeout(ProviderError):
    """The provider call exceeded its timeout budget."""


class ProviderEmptyResponse(ProviderError):
    """The provider returned an empty / whitespace-only body."""


class ProviderConfigError(RuntimeError):
    """Misconfiguration detected at startup (e.g. reviewer lab == orchestrator lab)."""


class ReviewerUnavailable(Exception):
    """Normalised not-a-pass signal: the reviewer could not produce a usable verdict.

    The gate catches this and degrades to block-and-escalate per tier. It carries the
    underlying ``reason`` for the audit trail / fail-closed counter.
    """

    def __init__(self, reason: str, cause: Exception | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


class Provider(abc.ABC):
    """A model endpoint. ``lab`` identifies the posttraining lineage for distinctness."""

    #: e.g. "nous", "anthropic", "openai", "mistral", "google". NOT the model size.
    lab: str = ""
    #: human-readable model id surfaced in the verdict / audit row.
    model: str = ""

    @abc.abstractmethod
    def complete(self, system: str, prompt: str, *, timeout: float) -> str:
        """Return the model's text completion, or raise a ProviderError subclass."""
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.lab}:{self.model}"


@dataclass
class MockProvider(Provider):
    """Offline test double. Returns a canned response, or simulates a failure mode.

    Pass ``responses`` (a list, consumed per call) or ``response`` (returned every
    call). Set ``raises`` to an exception instance/class to simulate failure, or
    ``delay`` with a small ``timeout`` to simulate a hang via the timeout wrapper.
    """

    lab: str = "mock-lab"
    model: str = "mock-model"
    response: str | None = None
    responses: list[str] | None = None
    raises: BaseException | type[BaseException] | None = None
    delay: float = 0.0
    calls: int = 0

    def complete(self, system: str, prompt: str, *, timeout: float) -> str:
        self.calls += 1
        if self.raises is not None:
            raise self.raises if isinstance(self.raises, BaseException) else self.raises()
        if self.delay:
            # Busy-free sleep; the wrapper's own timeout will fire if delay > timeout.
            import time

            time.sleep(self.delay)
        if self.responses:
            return self.responses.pop(0)
        if self.response is not None:
            return self.response
        return ""


def validate_distinct_providers(orchestrator: Provider, reviewer: Provider) -> None:
    """Hard-fail if the reviewer shares the orchestrator's lab (invariant 6, startup).

    Comparison is on ``lab`` (posttraining lineage), case-insensitive, so
    "openai:gpt-4o" reviewing "openai:gpt-4o-mini" is correctly rejected as the
    same lab — same-family-different-size is not divergence.
    """
    if not orchestrator.lab.strip() or not reviewer.lab.strip():
        # ``.strip()`` so a whitespace-only lab ("  ") can't sneak past the empty check
        # and then collapse to "" in the comparison below.
        raise ProviderConfigError(
            "both orchestrator and reviewer providers must declare a non-empty 'lab'"
        )
    if orchestrator.lab.strip().lower() == reviewer.lab.strip().lower():
        raise ProviderConfigError(
            "reviewer provider must be a DIFFERENT lab from the orchestrator for "
            f"posttraining divergence; both are lab={orchestrator.lab!r} "
            f"(orchestrator={orchestrator.describe()}, reviewer={reviewer.describe()}). "
            "Pick a different lab, not same-family-different-size."
        )


def call_reviewer(
    provider: Provider,
    system: str,
    prompt: str,
    *,
    timeout: float,
) -> str:
    """Invoke the reviewer with a hard timeout, normalising every failure mode to
    ``ReviewerUnavailable`` (not-a-pass). Returns the non-empty completion on success.
    """
    if timeout <= 0:
        raise ReviewerUnavailable("invalid non-positive reviewer timeout")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(provider.complete, system, prompt, timeout=timeout)
            try:
                result = future.result(timeout=timeout)
            except FutureTimeout as exc:
                future.cancel()
                raise ReviewerUnavailable(f"reviewer timed out after {timeout}s", exc) from exc
    except ReviewerUnavailable:
        raise
    except ProviderQuotaError as exc:
        raise ReviewerUnavailable(f"reviewer quota/rate-limit: {exc}", exc) from exc
    except ProviderTimeout as exc:
        raise ReviewerUnavailable(f"reviewer timeout: {exc}", exc) from exc
    except ProviderEmptyResponse as exc:
        raise ReviewerUnavailable(f"reviewer empty response: {exc}", exc) from exc
    except ProviderError as exc:
        raise ReviewerUnavailable(f"reviewer provider error: {exc}", exc) from exc
    except Exception as exc:  # noqa: BLE001 - any unexpected error is still not-a-pass
        raise ReviewerUnavailable(f"reviewer unexpected error: {exc}", exc) from exc

    if result is None or not str(result).strip():
        raise ReviewerUnavailable("reviewer returned an empty response")
    return result


# ---------------------------------------------------------------------------
# Real provider skeletons. These never run in unit tests (no creds -> raise).
# Reviewer route is configurable: OpenRouter or Hermes provider routing both fine.
# ---------------------------------------------------------------------------


@dataclass
class OpenRouterProvider(Provider):
    """OpenRouter-routed model. Network call lives behind ``complete``; unit tests
    mock it. Requires an API key at call time or it fails closed (no silent pass).

    neckbeard: thin urllib client, no SDK dependency (stdlib does it -> use it).
    Upgrade path: the official openrouter/openai SDK if streaming/tool-use is needed.
    """

    lab: str = "openrouter"
    model: str = "anthropic/claude-3.5-sonnet"
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1/chat/completions"

    def complete(self, system: str, prompt: str, *, timeout: float) -> str:
        if not self.api_key:
            raise ProviderError("OpenRouterProvider has no api_key; cannot call (fail closed)")
        import json
        import urllib.error
        import urllib.request

        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
            }
        ).encode()
        req = urllib.request.Request(
            self.base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (402, 429):
                raise ProviderQuotaError(f"OpenRouter {exc.code}: {exc.reason}") from exc
            raise ProviderError(f"OpenRouter HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"OpenRouter network error: {exc}") from exc
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderEmptyResponse(f"OpenRouter malformed response: {exc}") from exc


@dataclass
class HermesProvider(Provider):
    """Hermes/Nous provider routing. Same fail-closed contract as OpenRouter.

    Default lab is "nous" — this is the orchestrator's lab, so by default it can ONLY
    be the orchestrator side; using it for both raises in ``validate_distinct_providers``.
    """

    lab: str = "nous"
    model: str = "hermes-4"
    api_key: str = ""
    base_url: str = ""

    def complete(self, system: str, prompt: str, *, timeout: float) -> str:
        raise ProviderError(
            "HermesProvider.complete is a routing skeleton; wire it to your Hermes "
            "provider endpoint. It fails closed until configured (no silent pass)."
        )
