"""Notice when a work.ua posting stops existing.

Every other silent failure in this project announced itself as a zero somewhere.
A deleted posting does not even do that: no error, no responses, no counter
moving — the poller reports `new=0 errors=0` exactly as it does on a quiet
Sunday. Both sales postings were deleted on 2026-08-13 and the call queue
starved for five days before anyone looked at the public page.

`unknown_vacancies` in the poller is the *republication* alarm: a new job id
turns up in the feed and gets skipped. This is the *deletion* alarm, and it
cannot be anything but a poll of the public page, because a deleted posting
never sends anything to react to.

Deliberately quiet: no message of its own. A removal is not an emergency you
can act on at 3am — the fix is the client republishing — so the state rides
along in the daily report instead of interrupting anyone. work.ua being
unreachable is not a removal either; a line that appears on a timeout is a line
people learn to skip, and then the real one is skipped too.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog

from src.common import vacancies
from src.common.state import state_dir

log = structlog.get_logger()

ALIVE = "alive"
REMOVED = "removed"
UNKNOWN = "unknown"

# A 404 on a job page is work.ua saying the posting is not there.
UNKNOWN_OR_REMOVED_404 = REMOVED

STATE_NAME = "workua_vacancy_health.json"

# The public page, not the API: `GET /jobs/{id}/` answers 501 on this account,
# so the cabinet cannot tell us whether a posting is still up.
_JOB_URL = "https://www.work.ua/jobs/{job_id}/"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
# Only ever matched against the page BODY, and only once the URL already says we
# were redirected away. `job_removed=1` is deliberately NOT here: work.ua puts a
# "Повернутися до списку" link into every job page carrying the previous search
# URL, so after probing a removed posting the next LIVE page quotes that marker
# in its body. Matching it there declared two live vacancies dead on the very
# first run.
_REMOVAL_NOTICE = "видалена або прихована роботодавцем"


def _state_path() -> Path:
    return state_dir() / STATE_NAME


def classify(job_id: int | str, status_code: int, final_url: str, body: str = "") -> str:
    """alive / removed / unknown for one job page.

    The signal is where we LANDED, not what the page says. A live posting serves
    its own URL; a removed one is redirected to a search listing. Reading the
    body for markers is what produced the first version's false positives — the
    page of a live vacancy quotes the previous search URL in its back-link, so a
    probe run right after a removed posting poisoned the next live one.

    Anything we cannot read confidently is `unknown`, never `removed`: this
    function decides whether a vacancy gets reported as gone.
    """
    if status_code == 404:
        return REMOVED
    if status_code != 200:
        return UNKNOWN
    if _is_job_page(final_url, job_id):
        return ALIVE
    # Redirected off the job page. The notice confirms why, but leaving the page
    # at all is already the answer.
    if "job_removed=1" in final_url or _REMOVAL_NOTICE in body:
        return REMOVED
    # Somewhere else entirely — a challenge page, a login wall, an A/B redirect.
    # Not our call to make.
    return UNKNOWN


def _is_job_page(final_url: str, job_id: int | str) -> bool:
    """Are we still on /jobs/<job_id>/ ?"""
    path = urlparse(final_url).path.rstrip("/")
    return path.endswith(f"/jobs/{job_id}")


def transitions(
    previous: dict[str, str], current: dict[str, str]
) -> list[tuple[str, str | None, str]]:
    """State changes worth telling a human about, as (job_id, old, new).

    Only removals and resurrections. Becoming unreachable is never reported —
    that is noise, and noise is how an alarm gets ignored.
    """
    out: list[tuple[str, str | None, str]] = []
    for job_id, new in current.items():
        if new == UNKNOWN:
            continue
        old = previous.get(job_id)
        if old == new:
            continue
        if new == REMOVED or (new == ALIVE and old == REMOVED):
            out.append((job_id, old, new))
    return out


def merge_states(previous: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    """Carry forward what we knew; only a confident answer overwrites it.

    Without this, one unreachable night blanks every posting and the next good
    run re-alerts on all of them.
    """
    merged = dict(previous)
    for job_id, state in current.items():
        if state != UNKNOWN:
            merged[job_id] = state
    return merged


def route_is_starved(states: dict[str, str]) -> bool:
    """True when every posting a vacancy has on work.ua is gone."""
    if not states:
        return False
    return all(state == REMOVED for state in states.values())


def _load_state() -> dict[str, str]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in (data.get("jobs") or {}).items()}
    except Exception:
        return {}


def _save_state(jobs: dict[str, str]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")


async def _probe(client: httpx.AsyncClient, job_id: int) -> str:
    # Each posting is judged on its own. work.ua threads a "previous search"
    # breadcrumb through the session cookies, which is how one removed posting
    # used to contaminate the page of the next live one.
    client.cookies.clear()
    try:
        r = await client.get(_JOB_URL.format(job_id=job_id))
    except Exception as e:  # noqa: BLE001
        log.info("workua.liveness.unreachable", job_id=job_id, error=str(e))
        return UNKNOWN
    return classify(job_id, r.status_code, str(r.url), r.text)


async def check_vacancies(*, client: httpx.AsyncClient | None = None) -> dict[str, str]:
    """Poll every registered posting and shout once when one disappears.

    Cheap on purpose: five pages a couple of times a day, far under
    WORKUA_SCRAPE_DAILY_LIMIT. Returns the merged state for the caller to log.
    """
    job_ids = sorted(vacancies.workua_ids())
    if not job_ids:
        return {}

    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=25, follow_redirects=True, headers={"User-Agent": _UA}
    )
    try:
        current = {str(job_id): await _probe(client, job_id) for job_id in job_ids}
    finally:
        if owns_client:
            await client.aclose()

    previous = _load_state()
    changed = transitions(previous, current)
    merged = merge_states(previous, current)
    _save_state(merged)

    log.info(
        "workua.liveness.done",
        alive=sum(1 for s in current.values() if s == ALIVE),
        removed=sum(1 for s in current.values() if s == REMOVED),
        unknown=sum(1 for s in current.values() if s == UNKNOWN),
    )

    if not changed:
        return merged

    for job_id, old, new in changed:
        # WARNING so it is greppable in the logs the same day; the human-facing
        # version waits for the daily report.
        log.warning("workua.liveness.changed", job_id=job_id, was=old, now=new)
    return merged


async def run() -> dict[str, str]:
    """Scheduler entry point."""
    if (os.getenv("WORKUA_LIVENESS_ENABLED") or "1").strip() not in ("1", "true", "yes"):
        return {}
    return await check_vacancies()


def report_block(state: dict[str, str] | None = None) -> str:
    """Postings section for the daily report. Empty while everything is up.

    Silence when all is well is the point: a block that prints every day gets
    read as decoration. It appears exactly when work.ua has stopped carrying
    something we recruit for.
    """
    jobs = state if state is not None else _load_state()
    if not jobs:
        return ""

    dead: list[str] = []
    starved: list[str] = []
    for key, vacancy in sorted(vacancies.all_vacancies().items()):
        ids = [str(i) for i in vacancy.workua_ids]
        if not ids:
            continue
        states = {jid: jobs.get(jid, UNKNOWN) for jid in ids}
        gone = [jid for jid, st in states.items() if st == REMOVED]
        if not gone:
            continue
        if route_is_starved(states):
            what = "обдзвін" if getattr(vacancy, "calls_enabled", False) else "збір"
            starved.append(f"«{key}» — жодного оголошення, {what} з work.ua стоїть")
        else:
            dead.append(f"«{key}» — знято {len(gone)} з {len(ids)}")

    if not dead and not starved:
        return ""

    lines = ["", "📋 *work.ua — оголошення*"]
    lines += [f"├ ⚠️ {t}" for t in dead]
    lines += [f"├ 🔴 {t}" for t in starved]
    lines.append(
        "└ Клієнту треба перепублікувати. Новий job_id завести:"
        " /menu → Параметри вакансії → Збір і обдзвін —"
        " пропущені відгуки система добере сама"
    )
    return "\n".join(lines) + "\n"
