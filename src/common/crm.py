"""The seam where the CRM can be swapped.

Everything that talks to a CRM goes through `get_crm()`. Nothing outside this
module names KeyCRM, so pointing the system at Bitrix24 — or at whatever the
next client already runs — means writing one class and adding one line to
`_PROVIDERS`, not hunting `keycrm` through eleven files.

The interface below is deliberately the seventeen methods KeyCRM already
exposes, not an idealised CRM. Inventing a prettier abstraction and bending
KeyCRM to it would have meant rewriting working, load-bearing code — the CRM
calls are what keep the recruiter's funnel truthful — for no behaviour change.
A new provider implements this surface; where its CRM has no equivalent it
raises or no-ops, and that gap is visible in one file instead of at runtime.

Two concepts a new provider has to map, because they are not universal:

  **lead / card** — the candidate's record inside a funnel. KeyCRM calls it a
  pipeline card and gives it an integer id, which we store on the candidate as
  `keycrm_lead_id`. That column name is historical; treat it as "the CRM's id
  for this person".

  **buyer / contact** — the person themselves, separate from the card. KeyCRM
  keeps them apart, and a card must be linked to a buyer or the recruiter sees
  a nameless row. A CRM with a single entity can point both at one record.

`CRM_PROVIDER` in `.env` selects the implementation; unset means keycrm, which
is what every existing install is.
"""
from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CRMClient(Protocol):
    """What the recruiter pipeline needs from a CRM.

    Async throughout: these run inside call finalisation, where blocking would
    hold a live conversation open.
    """

    # --- lifecycle ---
    async def aclose(self) -> None: ...

    # --- finding people ---
    async def find_lead_by_phone(self, phone: str, *, pipeline_id: int | None = None) -> int | None:
        """Existing card id for this phone, scoped to a funnel when the CRM can.

        MUST fail closed: raise rather than return None when the lookup itself
        failed. Returning None on an error reads as "no duplicate" and creates a
        second card for someone who already has one — this exact bug ran for
        weeks because KeyCRM has no phone filter and the error looked like an
        empty result.
        """

    async def find_buyer_by_phone(self, phone: str) -> int | None: ...

    # --- people ---
    async def create_buyer(self, *args: Any, **kwargs: Any) -> int | None: ...
    async def ensure_buyer(self, *args: Any, **kwargs: Any) -> int | None: ...
    async def write_buyer_call_status(self, buyer_id: int, status_line: str) -> None: ...
    async def write_buyer_dialog(self, buyer_id: int, dialog_text: str) -> None: ...

    # --- cards ---
    async def create_lead(self, *args: Any, **kwargs: Any) -> int | None: ...
    async def get_lead(self, lead_id: int, include: str = ...) -> dict[str, Any]: ...
    async def update_lead(self, lead_id: int, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def get_card_status(self, lead_id: int) -> int | None: ...
    async def card_pipeline(self, lead_id: int) -> int | None: ...
    async def link_card_to_buyer(self, card_id: int, buyer_id: int) -> None: ...

    # --- moving through the funnel ---
    async def move_to_status(self, lead_id: int, status_id: int) -> dict[str, Any]:
        """Move the card to a stage.

        Stage ids come from the vacancy registry, not from here. A provider whose
        stages are named rather than numbered maps them internally.
        """

    async def assign_manager(self, lead_id: int, manager_id: int) -> dict[str, Any]: ...

    # --- what Єва found out ---
    async def write_call_results(self, *args: Any, **kwargs: Any) -> Any:
        """Transcript, summary, recording, region, score onto the card.

        Note for a new provider: KeyCRM stores these in custom fields that must
        be bound to the funnel in its UI. An unbound field accepts the value over
        the API and renders nowhere, and no endpoint reports the binding — it can
        only be confirmed by eye. Budget for that when mapping a new CRM.
        """

    async def append_manager_comment(self, lead_id: int, addition: str) -> dict[str, Any]: ...


def _keycrm_factory():
    from src.common.keycrm import KeyCRMClient

    return KeyCRMClient()


# provider name in .env -> callable returning a fresh client
_PROVIDERS: dict[str, Any] = {
    "keycrm": _keycrm_factory,
}


def provider_name() -> str:
    return (os.getenv("CRM_PROVIDER") or "keycrm").strip().lower()


def get_crm() -> CRMClient:
    """A client for the configured CRM.

    Returns a new instance per call, matching how the code already used
    `KeyCRMClient()` — each holds its own httpx session and the call sites close
    them independently.
    """
    name = provider_name()
    factory = _PROVIDERS.get(name)
    if factory is None:
        raise RuntimeError(
            f"CRM_PROVIDER={name!r} невідомий. Доступні: {', '.join(sorted(_PROVIDERS))}. "
            "Новий провайдер додається класом у src/common/crm.py."
        )
    return factory()
