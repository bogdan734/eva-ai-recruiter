"""robota.ua employer-cabinet API client.

The public partner API needs a signed agreement, but the cabinet SPA talks to a
plain JSON backend that the employer's own credentials unlock. That is what we
use — no scraping, no DOM selectors to rot:

    POST https://auth-api.robota.ua/Login            -> JWT (valid 7 days)
    POST https://employer-api.robota.ua/apply/list   -> responses ("Відгуки")
    GET  https://employer-api.robota.ua/resume/{id}  -> full CV of one applicant
    GET  https://api.robota.ua/dictionary/city       -> cityId -> city + oblast

⚠️ TRANSPORT: every one of those hosts sits behind Cloudflare, which serves a
challenge page (HTTP 403 "Just a moment…") to Python's TLS stack — httpx,
urllib and curl_cffi were all tested and all get 403 from this VPS, while the
system `curl` binary passes. So requests go through `curl` as a subprocess.
Options and the request body are handed to curl through a config file on stdin
(`curl -K -`) so credentials and the bearer token never appear in `ps` output.
If robota.ua ever drops the challenge, swapping in httpx is a ~20 line change.

⚠️ IPv6: Cloudflare challenges this VPS's IPv4 outright (403 on every call) but
serves its IPv6 normally, so requests are forced over v6 — the compose file
attaches the poller's containers to an IPv6-enabled network for exactly this.
ROBOTAUA_FORCE_IPV6=0 turns that off if the situation ever reverses.

⚠️ CONTACTS: an apply carries the candidate's phone only when the CV is of type
`Notepad` (a CV hosted on robota.ua) or when contacts were already opened in the
cabinet. Type `Interaction` (~85% of the flow) hides the phone until someone
opens contacts on robota.ua's side; `hasPhone: true` says the number exists.
This client never opens contacts — that is a billable action on the client's
account. `robotaua_sync` parks those applies in a pending list and re-checks
them, so they flow in by themselves once a recruiter opens the contact.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path

import structlog

from src.common.settings import get_settings
# Defined here until 2026-08-08, then moved to src/common/state.py so the work.ua
# poller could stop hardcoding `.cache/` and losing its cursor on every build.
# Re-exported because robotaua_sync and robotaua_chat import the name from here.
from src.common.state import state_dir

log = structlog.get_logger()

LOGIN_URL = "https://auth-api.robota.ua/Login"
APPLY_LIST_URL = "https://employer-api.robota.ua/apply/list"
RESUME_URL = "https://employer-api.robota.ua/resume/{resume_id}"
CITY_DICT_URL = "https://api.robota.ua/dictionary/city"
# Contact opening — paths lifted from the cabinet bundle (main.*.js). Reading the
# remaining quota is free; opening a contact spends it, so it stays behind an
# explicit call the client has to authorise.
OPEN_CONTACTS_COUNT_URL = "https://employer-api.robota.ua/resume/open-contacts-count"
OPEN_CONTACT_URL = "https://employer-api.robota.ua/resume/open/{resume_id}"
# Cabinet chat ("24 непрочитані") — same JWT, separate host.
CHAT_CONVERSATIONS_URL = "https://chat-api.robota.ua/v2/conversations/all"
CHAT_COUNTERS_URL = "https://chat-api.robota.ua/v2/conversations/counters"
CHAT_UNREAD_URL = "https://chat-api.robota.ua/v1/not-read-messages"
CHAT_MESSAGES_URL = "https://chat-api.robota.ua/v1/conversations/{conversation_id}/messages"
# Employer-cabinet link a recruiter can open from the CRM card.
CANDIDATE_URL = "https://robota.ua/candidates/{resume_id}"
# CV file of an `AttachedFile` apply. Those have resumeId=0, so /resume/{id}
# can never return them — the file is attached to the apply itself. The apply
# payload carries this exact URL in `filePath`; the template is the fallback.
ATTACHMENT_URL = "https://apply-api.robota.ua/{apply_id}-attach/file"

# Chrome header set — the cabinet backend rejects requests without an Origin,
# and Cloudflare is friendlier to a complete, browser-shaped header list.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_BASE_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/plain, */*",
    "accept-language": "uk-UA,uk;q=0.9,en;q=0.8",
    "origin": "https://robota.ua",
    "referer": "https://robota.ua/",
    "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": _UA,
}

_STATUS_MARK = "RA_STATUS:"
_HTML_TAG_RE = re.compile(r"<[^>]+>")


class RobotaUaError(RuntimeError):
    pass


class RobotaUaAuthError(RobotaUaError):
    pass


class RobotaUaBlockedError(RobotaUaError):
    """Cloudflare served a challenge instead of the API response."""


STATUS_NAME = "robotaua_status.json"


def read_status() -> dict:
    """Last known robota.ua numbers, written by the pollers.

    The admin bot reads this instead of calling robota.ua — a /status command
    must never spend a request against an API that rate-limits us.
    """
    try:
        return json.loads((state_dir() / STATUS_NAME).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_status(**patch) -> None:
    data = read_status()
    data.update({k: v for k, v in patch.items() if v is not None})
    try:
        path = state_dir() / STATUS_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("robotaua.status_write_failed", error=str(e))


def _preferred_family() -> str:
    """Which IP family to try first: 'ipv6', 'ipv4' or '' (whatever curl picks).

    Cloudflare flags this host per address, and the flags rotate: 2026-08-03 the
    IPv4 was challenged all morning while IPv6 worked, and by midday it was the
    other way round. Both addresses are ours, so the client simply tries the one
    that worked last and falls back to the other — normal dual-stack behaviour,
    not IP rotation: it never goes looking for a fresh address to dodge a block.
    """
    explicit = (os.getenv("ROBOTAUA_IP_FAMILY") or "").strip().lower()
    if explicit in ("ipv4", "ipv6"):
        return explicit
    if explicit in ("any", "none", "off"):
        return ""
    legacy = (os.getenv("ROBOTAUA_FORCE_IPV6") or "").strip().lower()
    if legacy in ("0", "false", "no", "off"):
        return "ipv4"
    try:
        cached = json.loads((state_dir() / "robotaua_net.json").read_text(encoding="utf-8"))
        if cached.get("family") in ("ipv4", "ipv6"):
            return cached["family"]
    except Exception:
        pass
    return "ipv6"


def _remember_family(family: str) -> None:
    if not family:
        return
    try:
        path = state_dir() / "robotaua_net.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"family": family}), encoding="utf-8")
    except Exception:
        pass


# Bounds for the ways a curl request stalls without finishing (see _curl).
_CONNECT_TIMEOUT = int(os.getenv("ROBOTAUA_CONNECT_TIMEOUT") or 15)
_STALL_BYTES_PER_SEC = int(os.getenv("ROBOTAUA_STALL_BYTES") or 1)
_STALL_SECONDS = int(os.getenv("ROBOTAUA_STALL_SECONDS") or 20)
# How long we wait for a killed curl to actually die before giving up on it.
_REAP_TIMEOUT = 5


async def _reap(proc, url: str) -> None:
    """Kill a curl subprocess and collect it — without ever blocking forever.

    The previous version called `proc.kill()` then a bare `await proc.wait()`.
    When asyncio's child watcher misses the SIGCHLD, that wait never returns, so
    the "hung curl" the job timeout kept catching was in fact a hung *wait*, not
    a hung curl. Every step here is bounded and nothing raises: the caller is
    already handling an error and must be allowed to continue.
    """
    for signal_step in ("terminate", "kill"):
        if proc.returncode is not None:
            return
        try:
            getattr(proc, signal_step)()
        except ProcessLookupError:
            return  # already gone
        except Exception as e:  # noqa: BLE001
            log.warning("robotaua.curl_signal_failed", step=signal_step, error=str(e))
        try:
            await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT)
            return
        except asyncio.TimeoutError:
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("robotaua.curl_wait_failed", error=str(e))
            return
    if proc.returncode is None:
        # SIGKILL did not collect it within the window. Leaving it unreaped is
        # survivable (it becomes init's problem); blocking the poller is not.
        log.error(
            "robotaua.curl_unreapable", pid=getattr(proc, "pid", None),
            url=url.split("?")[0],
        )


def _esc(value: str) -> str:
    """Escape a value for curl's config-file quoting rules."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    plain = _HTML_TAG_RE.sub(" ", text)
    plain = plain.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", plain).strip()


def _year_month(raw: str | None) -> tuple[int, int] | None:
    """robota.ua sends '2019-02-01T00:00:00'; '0001-01-01T00:00:00' means empty."""
    if not raw or raw.startswith("0001"):
        return None
    try:
        dt = datetime.fromisoformat(raw[:19])
    except ValueError:
        return None
    return dt.year, dt.month


def parse_add_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw[:26])
    except ValueError:
        try:
            return datetime.fromisoformat(raw[:19])
        except ValueError:
            return None


class RobotaUaClient:
    """Read-only client over the employer cabinet's own endpoints."""

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        *,
        timeout: int = 40,
    ) -> None:
        s = get_settings()
        self._email = email or s.robotaua_employer_email
        self._password = password or s.robotaua_employer_password
        self._timeout = timeout
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._city_map: dict[int, dict[str, str]] | None = None
        if not self._email or not self._password:
            raise RobotaUaAuthError(
                "ROBOTAUA_EMPLOYER_EMAIL / ROBOTAUA_EMPLOYER_PASSWORD not set"
            )

    # ---------------------------------------------------------------- transport

    async def _curl(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        token: str | None = None,
        attempts: int = 2,
        output_path: str | None = None,
    ) -> tuple[int, str]:
        headers = dict(_BASE_HEADERS)
        if token:
            headers["authorization"] = f"Bearer {token}"
        base_config = [
            f'url = "{_esc(url)}"',
            f'request = "{method}"',
            "http2",
            "silent",
            "show-error",
            "compressed",
            f"max-time = {self._timeout}",
            # max-time only bounds the WHOLE transfer. These bound the ways a
            # request can stall without ever finishing: a connect that never
            # completes, and a transfer that trickles bytes forever without
            # tripping max-time. Both showed up as the "curl hangs" symptom.
            f"connect-timeout = {_CONNECT_TIMEOUT}",
            f"speed-limit = {_STALL_BYTES_PER_SEC}",
            f"speed-time = {_STALL_SECONDS}",
            f'write-out = "\\n{_STATUS_MARK}%{{http_code}}"',
        ]
        if output_path:
            # Attachments are binary (PDF/DOC). Sending the body to a file keeps
            # stdout as pure text, so the status marker still parses and we never
            # try to utf-8 decode a PDF.
            base_config.append(f'output = "{_esc(output_path)}"')
            base_config.append("location")  # attachments redirect to a CDN
        base_config += [f'header = "{_esc(k)}: {_esc(v)}"' for k, v in headers.items()]
        if body is not None:
            base_config.append(
                f'data-raw = "{_esc(json.dumps(body, ensure_ascii=False))}"'
            )

        preferred = _preferred_family()
        families = [preferred] if preferred else [""]
        if preferred == "ipv6":
            families.append("ipv4")
        elif preferred == "ipv4":
            families.append("ipv6")

        last: tuple[int, str] = (0, "")
        for family in families[:attempts]:
            config = base_config + ([family] if family else [])
            payload = ("\n".join(config) + "\n").encode()
            proc = await asyncio.create_subprocess_exec(
                "curl",
                "-K",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            reaped = False
            try:
                # curl's own --max-time covers the transfer, but a subprocess can
                # still hang around its pipes. Without this guard one stuck call
                # freezes the whole poll, and APScheduler then skips every later
                # run with "maximum number of running instances reached".
                out, err = await asyncio.wait_for(
                    proc.communicate(payload), timeout=self._timeout + 15
                )
                reaped = True
            except asyncio.TimeoutError:
                await _reap(proc, url)
                reaped = True
                log.warning("robotaua.curl_timeout", url=url.split("?")[0], family=family or "auto")
                last = (0, "curl timeout")
                continue
            finally:
                # Any other exit — most importantly CancelledError raised by the
                # job's own 300s timeout — must not leave a curl behind. A leaked
                # process holds its pipes open and the next poll inherits the mess.
                if not reaped:
                    await _reap(proc, url)
            text = out.decode("utf-8", "replace")
            status = 0
            if _STATUS_MARK in text:
                text, _, tail = text.rpartition(_STATUS_MARK)
                status = int(tail.strip() or 0)
                text = text.rstrip("\n")
            elif proc.returncode != 0:
                text = err.decode("utf-8", "replace")[:300]

            last = (status, text)
            challenged = status in (403, 429) or "Just a moment" in text[:400]
            if not challenged and status:
                if family and family != preferred:
                    log.info("robotaua.family_switched", to=family)
                _remember_family(family)
                return last
            # A challenge means Cloudflare has flagged the address we came from.
            # Try the host's other address once, then stop: hammering a flagged
            # address only keeps the flag alive, so the poller backs off for
            # ROBOTAUA_BLOCK_COOLDOWN_MIN instead of fighting through.
            log.warning(
                "robotaua.request_challenged",
                url=url.split("?")[0],
                status=status,
                family=family or "auto",
            )
            await asyncio.sleep(2)
        return last

    async def _json(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        params: dict | None = None,
        authed: bool = True,
    ):
        if params:
            from urllib.parse import urlencode

            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}{'&' if '?' in url else '?'}{urlencode(clean)}"
        token = await self.token() if authed else None
        status, text = await self._curl(url, method=method, body=body, token=token)
        if status == 401 and authed:
            # Token died early (password change, session revoked) — one clean retry.
            self._token = None
            token = await self.token(force=True)
            status, text = await self._curl(url, method=method, body=body, token=token)
        if status in (403, 429) or "Just a moment" in text[:400]:
            raise RobotaUaBlockedError(f"cloudflare challenge on {url} (status {status})")
        if status == 204 or (status == 200 and not text.strip()):
            # Deleted or hidden CV — the cabinet answers 204 with no body. Not an
            # error: the apply row still carries the name and we park it as usual.
            return {}
        if status != 200:
            raise RobotaUaError(f"{method} {url} -> {status}: {text[:200]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise RobotaUaError(f"{url} returned non-JSON: {text[:200]}") from e

    # -------------------------------------------------------------------- auth

    def _token_path(self) -> Path:
        return state_dir() / "robotaua_token.json"

    def _load_token(self) -> None:
        try:
            data = json.loads(self._token_path().read_text(encoding="utf-8"))
        except Exception:
            return
        token, exp = data.get("token"), float(data.get("exp") or 0)
        # Refresh an hour before expiry so a poll never dies mid-flight.
        if token and exp - 3600 > time.time():
            self._token, self._token_exp = token, exp

    def _store_token(self, token: str, exp: float) -> None:
        self._token, self._token_exp = token, exp
        try:
            path = self._token_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"token": token, "exp": exp}), encoding="utf-8")
            path.chmod(0o600)
        except Exception as e:  # noqa: BLE001 — cache is an optimisation, not a must
            log.warning("robotaua.token_cache_failed", error=str(e))

    @staticmethod
    def _exp_from_jwt(token: str) -> float:
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return float(claims.get("exp") or 0)
        except Exception:
            return time.time() + 3600

    async def token(self, *, force: bool = False) -> str:
        """Cached JWT. robota.ua issues 7-day tokens, and its login endpoint is
        the most aggressively challenged one, so we log in as rarely as possible."""
        if not force:
            if self._token and self._token_exp - 3600 > time.time():
                return self._token
            self._load_token()
            if self._token:
                return self._token

        status, text = await self._curl(
            LOGIN_URL,
            method="POST",
            body={"username": self._email, "password": self._password},
        )
        if status in (403, 429) or "Just a moment" in text[:400]:
            raise RobotaUaBlockedError("cloudflare challenge on login")
        if status != 200:
            raise RobotaUaAuthError(f"login failed: {status} {text[:160]}")
        token = text.strip().strip('"')
        if token.count(".") != 2:
            raise RobotaUaAuthError(f"login returned no JWT: {text[:120]}")
        self._store_token(token, self._exp_from_jwt(token))
        log.info("robotaua.login_ok", expires=datetime.utcfromtimestamp(self._token_exp).isoformat())
        return token

    # ------------------------------------------------------------------- reads

    async def list_applies(self, *, page: int = 0, count: int = 50) -> list[dict]:
        """One page of responses, newest first (the cabinet's own ordering)."""
        data = await self._json(
            APPLY_LIST_URL, method="POST", body={"page": page, "count": count}
        )
        return list(data.get("applies") or [])

    async def get_resume(self, resume_id: int) -> dict:
        return await self._json(RESUME_URL.format(resume_id=resume_id))

    async def city_map(self) -> dict[int, dict[str, str]]:
        """cityId -> {"city": "Львів", "region": "Львівська область"}.

        3.7k entries, static — fetched once per process.
        """
        if self._city_map is not None:
            return self._city_map
        rows = await self._json(CITY_DICT_URL)
        out: dict[int, dict[str, str]] = {}
        for row in rows if isinstance(rows, list) else []:
            try:
                cid = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            region = (row.get("regionName") or {}).get("ua") or ""
            out[cid] = {"city": row.get("ua") or row.get("en") or "", "region": region}
        self._city_map = out
        return out

    async def health_check(self) -> bool:
        try:
            await self.list_applies(page=0, count=1)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("robotaua.health_check_failed", error=str(e))
            return False

    # ------------------------------------------------------- contacts (quota)

    async def open_contacts_count(self):
        """How many contact openings the account still has. FREE to read."""
        return await self._json(OPEN_CONTACTS_COUNT_URL)

    async def open_contact(self, resume_id: int) -> str:
        """⚠️ SPENDS one of the account's contact openings.

        Returns "opened" on success, "hidden" when the candidate has hidden the
        number entirely (robota.ua answers `PhonesAreHidden` and charges nothing
        — such a CV must never be retried).
        """
        status, text = await self._curl(
            OPEN_CONTACT_URL.format(resume_id=resume_id),
            method="POST",
            token=await self.token(),
        )
        if status in (403, 429) or "Just a moment" in text[:400]:
            raise RobotaUaBlockedError(f"cloudflare challenge on open_contact/{resume_id}")
        if "PhonesAreHidden" in text:
            return "hidden"
        if status not in (200, 204):
            raise RobotaUaError(f"open_contact/{resume_id} -> {status}: {text[:160]}")
        return "opened"

    # ---------------------------------------------------------------- chat

    async def chat_counters(self):
        return await self._json(CHAT_COUNTERS_URL)

    async def chat_unread(self):
        return await self._json(CHAT_UNREAD_URL)

    async def list_conversations(self, **params):
        return await self._json(CHAT_CONVERSATIONS_URL, params=params or None)

    async def download_attachment(self, apply_id: int | str, url: str | None = None) -> bytes:
        """Fetch the CV file of an `AttachedFile` apply.

        Those applies carry `resumeId = 0` — there is no resume record to fetch,
        which is why /resume/{id} never found them. The file lives at the
        `filePath` the apply itself carries:
            https://apply-api.robota.ua/{applyId}-attach/file
        Returns b"" on any failure; the caller treats that as "no phone found".
        """
        target = url or ATTACHMENT_URL.format(apply_id=apply_id)
        token = await self.token()
        tmp = Path(tempfile.gettempdir()) / f"robotaua-attach-{apply_id}"
        try:
            status, _ = await self._curl(target, token=token, output_path=str(tmp))
            if status != 200:
                log.warning("robotaua.attachment_http", apply=apply_id, status=status)
                return b""
            data = tmp.read_bytes()
            log.info("robotaua.attachment_fetched", apply=apply_id, bytes=len(data))
            return data
        except Exception as e:  # noqa: BLE001 — one bad file must not kill the poll
            log.warning("robotaua.attachment_failed", apply=apply_id, error=str(e))
            return b""
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    async def get_messages(self, conversation_id: str, **params):
        return await self._json(
            CHAT_MESSAGES_URL.format(conversation_id=conversation_id),
            params=params or None,
        )

    async def send_message(self, conversation_id: str, text: str):
        """⚠️ Writes to a real candidate chat. Gated by the caller — the chat
        poller only sends when ROBOTAUA_CHAT_REPLY_ENABLED is on.

        Body shape is the cabinet's own `sendTextMessage$`:
        {messageType: "Text", text, syncTag}. robota.ua answers 429 when the
        per-account message limit is hit and 403 if the candidate blacklisted
        the company — both are surfaced as RobotaUaError, not retried.
        """
        sync_tag = str(abs(hash((conversation_id, text, time.time()))) % (10**12))
        return await self._json(
            CHAT_MESSAGES_URL.format(conversation_id=conversation_id),
            method="POST",
            body={"messageType": "Text", "text": text, "syncTag": sync_tag},
        )


# ------------------------------------------------------------------- parsing


def experience_years(experiences: list[dict] | None) -> int | None:
    """Total months across all jobs, rounded down to years. None when unknown."""
    if not experiences:
        return None
    months = 0
    now = datetime.utcnow()
    for exp in experiences:
        start = _year_month(exp.get("startWork"))
        if not start:
            continue
        end = _year_month(exp.get("endWork")) or (now.year, now.month)
        span = (end[0] - start[0]) * 12 + (end[1] - start[1])
        if span > 0:
            months += span
    return months // 12 if months else None


def birth_year(raw: str | None) -> int | None:
    ym = _year_month(raw)
    return ym[0] if ym else None


def build_resume_text(apply: dict, resume: dict | None = None) -> str:
    """Flatten the CV into the plain text Єва reads on the call and the CRM card
    shows under «AI Резюме»."""
    src = resume or {}
    parts: list[str] = []
    speciality = apply.get("speciality") or src.get("speciality")
    if speciality:
        parts.append(f"Бажана посада: {speciality}")
    salary = apply.get("salary") or src.get("salary")
    if salary:
        parts.append(f"Очікувана зарплата: {salary}")

    experiences = src.get("experiences") or apply.get("experiences") or []
    if experiences:
        parts.append("Досвід:")
        for exp in experiences[:6]:
            start = _year_month(exp.get("startWork"))
            end = _year_month(exp.get("endWork"))
            period = ""
            if start:
                period = f" ({start[0]}.{start[1]:02d} — " + (
                    f"{end[0]}.{end[1]:02d})" if end else "дотепер)"
                )
            head = " · ".join(x for x in (exp.get("position"), exp.get("company")) if x)
            parts.append(f"— {head}{period}")
            desc = _strip_html(exp.get("description"))
            if desc:
                parts.append(f"  {desc[:600]}")

    educations = src.get("educations") or []
    if educations:
        parts.append("Освіта:")
        for edu in educations[:3]:
            bits = [edu.get("name"), edu.get("speciality"), str(edu.get("yearOfGraduation") or "")]
            parts.append("— " + ", ".join(b.strip() for b in bits if b and b.strip()))

    skills = apply.get("skillsSummary") or ""
    if not skills and src.get("skills"):
        skills = " ".join(s.get("description") or "" for s in src["skills"])
    skills_text = _strip_html(skills)
    if skills_text:
        parts.append(f"Навички: {skills_text[:400]}")

    return "\n".join(parts).strip()


def parse_apply(
    apply: dict,
    *,
    cities: dict[int, dict[str, str]] | None = None,
    resume: dict | None = None,
) -> dict:
    """Normalize one apply (+ optional full CV) into intake-ready fields."""
    cities = cities or {}
    resume = resume or {}
    city_id = apply.get("cityId") or resume.get("cityId")
    geo = cities.get(int(city_id)) if city_id else None
    phone = (apply.get("phone") or resume.get("phone") or "").strip()
    email = (apply.get("eMail") or resume.get("email") or "").strip()
    contacts = apply.get("contacts") or {}
    if not phone:
        phones = (contacts.get("phones") or []) if isinstance(contacts, dict) else []
        phone = (phones[0].get("value") or "").strip() if phones else ""
    resume_id = int(apply.get("resumeId") or 0)

    return {
        "apply_id": int(apply.get("id") or 0),
        "resume_id": resume_id,
        "vacancy_id": apply.get("vacancyId"),
        "full_name": (apply.get("name") or "Кандидат robota.ua").strip(),
        "phone_raw": phone,
        "email": email or None,
        "city": (geo or {}).get("city"),
        "region_raw": (geo or {}).get("region"),
        "desired_position": apply.get("speciality") or resume.get("speciality"),
        "birth_year": birth_year(apply.get("birthDate") or resume.get("birthDate")),
        "experience_years": experience_years(
            resume.get("experiences") or apply.get("experiences")
        ),
        "resume_text": build_resume_text(apply, resume),
        "resume_url": CANDIDATE_URL.format(resume_id=resume_id) if resume_id else None,
        "applied_at": apply.get("addDate"),
        "resume_type": apply.get("resumeType"),
        # robota.ua knows a number exists but keeps it behind "open contacts".
        "has_hidden_phone": bool(resume.get("hasPhone")) and not phone,
    }
