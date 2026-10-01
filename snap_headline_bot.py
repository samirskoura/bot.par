#!/usr/bin/env python3
"""Low-cost GitHub Actions worker for reviewing rejected Snapchat ad headlines.

The worker is deliberately conservative:
- each manual live start selects one or more ad squads for the active job;
- scheduled runs continue the complete selected ad-squad list;
- it selects Ads whose review status is REJECTED;
- after a PATCH, it waits for review and can safely recover if a brief PENDING
  transition occurred while the worker was offline;
- each Ad/Creative is handled independently, so another pending Ad does not block it;
- it skips creatives shared with ads outside the selected ad squads;
- it rechecks linked Ads and the Creative immediately before each edit;
- it requires every linked Ad to be REJECTED and the Creative DISAPPROVED;
- every PATCH tests the Creative's status and old headline before replacing it;
- it keeps retrying confirmed rejections while the workflow remains enabled.
"""

from __future__ import annotations

import builtins
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from uuid import UUID, uuid4

import requests


_ORIGINAL_PRINT = builtins.print
_PUBLIC_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _sanitize_public_log(message: str) -> str:
    """Remove account routing, internal names, headlines, and API details from logs."""
    if message.startswith("Verified target:"):
        return "Verified private target scope; identifiers and internal names hidden."
    if message.startswith("WOULD UPDATE "):
        return "WOULD UPDATE one eligible Creative; private details hidden."
    if message.startswith("UPDATED squad="):
        return "UPDATED one eligible Creative; submitted for re-review."
    if message.startswith("SKIP model item with unknown creative_id:"):
        return "SKIP model item with an unknown private Creative identifier."
    if "OpenAI declined the headline request:" in message:
        return "ERROR: OpenAI declined the headline request; response text hidden."
    message = _PUBLIC_UUID_RE.sub("[private-id]", message)
    message = re.sub(
        r"request_id=[^;\s]+",
        "request_id=[private-request]",
        message,
        flags=re.IGNORECASE,
    )
    if "detail=" in message:
        message = message.split("detail=", 1)[0] + "detail=[hidden]"
    if "must be a valid UUID:" in message:
        message = message.split("must be a valid UUID:", 1)[0] + "must be a valid UUID."
    return message


def _public_print(
    *values: object,
    sep: str = " ",
    end: str = "\n",
    file: Any = None,
    flush: bool = False,
) -> None:
    rendered = sep.join(str(value) for value in values)
    _ORIGINAL_PRINT(
        _sanitize_public_log(rendered),
        end=end,
        file=file,
        flush=flush,
    )


if os.getenv("PUBLIC_SAFE_LOGS", "").strip().lower() in {"1", "true", "yes", "on"}:
    builtins.print = _public_print


SNAP_API = "https://adsapi.snapchat.com/v1"
SNAP_TOKEN_URL = "https://accounts.snapchat.com/login/oauth2/access_token"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
STATE_PATH = Path(__file__).with_name("state.json")
HEADLINE_POOL_PATH = Path(__file__).with_name("headline_pool.txt")
STATE_VERSION = 11
BOT_VERSION = "2026.09.13-random-per-workflow-run"
HEADLINE_RANDOM = random.SystemRandom()
CHECKPOINT_SCRIPT = Path(__file__).with_name("checkpoint_encrypted_state.py")
STATE_STOP_EXIT_CODE = 90
HARD_MAX_UPDATES_PER_RUN = 80
MAX_HEADLINE_REQUEST_CREATIVES = 30
HARD_MAX_AD_SQUADS = 20
HEADLINE_OPTIONS_PER_CREATIVE = 5
MAX_HEADLINE_GENERATION_ROUNDS = 2
NEAR_DUPLICATE_RATIO = 0.85
MIN_REVIEW_PROPAGATION_SECONDS = 60
MISSED_PENDING_RECOVERY_SECONDS = 300
MAX_SNAP_READ_ATTEMPTS = 4
MAX_OPENAI_REQUEST_ATTEMPTS = 3
OPENAI_RETRY_WINDOW_SECONDS = 180
OPENAI_STOP_EXIT_CODE = 78
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
TIMEOUT = 45

INTERNAL_NAME_FIELDS = (
    "campaign_name",
    "ad_squad_name",
    "ad_name",
    "creative_name",
)
INTERNAL_NAME_STOPWORDS = {
    "active",
    "ad",
    "ads",
    "adset",
    "approved",
    "campaign",
    "copy",
    "creative",
    "image",
    "ksa",
    "new",
    "pending",
    "product",
    "rejected",
    "saudi",
    "snap",
    "snapchat",
    "squad",
    "test",
    "video",
    "اعلان",
    "اعلانات",
    "اختبار",
    "السعودية",
    "المتجر",
    "المنتج",
    "تجربة",
    "جديد",
    "جديدة",
    "حملة",
    "سناب",
    "سنابشات",
    "صنف",
    "صورة",
    "فيديو",
    "مجموعة",
    "متجرنا",
    "مرفوض",
    "مرفوضة",
    "منتج",
    "نسخة",
    "نشط",
    "نشطة",
}
LATIN_NAME_DIGRAPHS = {
    "sh": "ش",
    "ch": "تش",
    "kh": "خ",
    "gh": "غ",
    "th": "ث",
    "dh": "ذ",
    "ph": "ف",
}
LATIN_NAME_CHARACTERS = {
    "a": "ا",
    "b": "ب",
    "c": "ك",
    "d": "د",
    "e": "ي",
    "f": "ف",
    "g": "ج",
    "h": "ه",
    "i": "ي",
    "j": "ج",
    "k": "ك",
    "l": "ل",
    "m": "م",
    "n": "ن",
    "o": "و",
    "p": "ب",
    "q": "ق",
    "r": "ر",
    "s": "س",
    "t": "ت",
    "u": "و",
    "v": "ف",
    "w": "و",
    "x": "كس",
    "y": "ي",
    "z": "ز",
}


class BotError(RuntimeError):
    pass


class OpenAIServiceError(BotError):
    """Stop the monitor after the bounded OpenAI recovery attempt."""


class StatePersistenceError(BotError):
    """Stop before an edit if its history cannot be saved reliably."""


def env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def required_env(name: str) -> str:
    value = env(name)
    if not value:
        raise BotError(f"Missing required GitHub secret or variable: {name}")
    return value


def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise BotError(f"{name} must be a whole number, received: {raw!r}") from exc
    return max(minimum, min(value, maximum))


def parse_ad_squad_ids(raw: str, label: str) -> list[str]:
    """Parse comma, semicolon, whitespace, or newline separated UUIDs."""
    values: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;]+", raw.strip()):
        candidate = token.strip()
        if not candidate:
            continue
        try:
            normalized = str(UUID(candidate))
        except ValueError as exc:
            raise BotError(
                f"{label} contains an invalid Ad Squad UUID: {candidate!r}"
            ) from exc
        if normalized not in seen:
            values.append(normalized)
            seen.add(normalized)
    if len(values) > HARD_MAX_AD_SQUADS:
        raise BotError(
            f"{label} contains {len(values)} Ad Squads; the safe maximum is "
            f"{HARD_MAX_AD_SQUADS}."
        )
    return values


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_headline(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def headline_key(value: Any) -> str:
    # Decompose composed accents too; invisible formatting and diacritics must
    # not make the same wording appear unused after a file is pasted or edited.
    text = unicodedata.normalize("NFKD", clean_headline(value)).casefold()
    normalized: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if character == "ـ" or category.startswith("M") or category == "Cf":
            continue
        normalized.append(character if category[0] in {"L", "N"} else " ")
    return " ".join("".join(normalized).split())


def latin_name_to_arabic(value: str) -> str:
    """Create a conservative Arabic transliteration used only for name blocking."""
    token = re.sub(r"[^a-z]", "", value.lower())
    output: list[str] = []
    position = 0
    while position < len(token):
        pair = token[position : position + 2]
        if pair in LATIN_NAME_DIGRAPHS:
            output.append(LATIN_NAME_DIGRAPHS[pair])
            position += 2
            continue
        output.append(LATIN_NAME_CHARACTERS.get(token[position], ""))
        position += 1
    return "".join(output)


def candidate_internal_name_terms(candidate: dict[str, Any]) -> set[str]:
    """Return internal resource-name terms that must never enter a headline."""
    terms: set[str] = set()
    for field in INTERNAL_NAME_FIELDS:
        normalized_name = headline_key(candidate.get(field))
        if not normalized_name:
            continue
        if len(normalized_name) >= 4 and normalized_name not in INTERNAL_NAME_STOPWORDS:
            terms.add(normalized_name)
        for token in normalized_name.split():
            if len(token) < 4 or token in INTERNAL_NAME_STOPWORDS:
                continue
            terms.add(token)
            if re.fullmatch(r"[a-z0-9]+", token):
                transliterated = headline_key(latin_name_to_arabic(token))
                if len(transliterated) >= 3:
                    terms.add(transliterated)
    return terms


def headline_contains_internal_name(
    headline: str, candidate: dict[str, Any]
) -> bool:
    key = headline_key(headline)
    tokens = set(key.split())
    for term in candidate_internal_name_terms(candidate):
        if " " in term:
            if term in key:
                return True
            continue
        if term in tokens:
            return True
        if len(term) >= 5 and any(
            len(token) >= 5 and (term in token or token in term) for token in tokens
        ):
            return True
    return False


def openai_candidate_payload(
    candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Expose only opaque IDs; never send Snapchat resource names to OpenAI."""
    return [
        {"creative_id": str(candidate.get("creative_id") or "")}
        for candidate in candidates
    ]


def configured_headline_pool() -> list[str]:
    if not HEADLINE_POOL_PATH.exists():
        return []
    # Accept Windows UTF-8 files with or without a BOM. Deduplicate by wording;
    # selection later chooses randomly from this Creative's unused options.
    pool: list[str] = []
    seen: set[str] = set()
    for line in HEADLINE_POOL_PATH.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        headline = clean_headline(line)
        key = headline_key(headline)
        if key and key not in seen:
            pool.append(headline)
            seen.add(key)
    return pool


def manual_suggestions(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pool = configured_headline_pool()
    return [
        {"creative_id": candidate["creative_id"], "action": "UPDATE", "headlines": pool}
        for candidate in candidates
    ]


def remember_headlines(state: dict[str, Any], values: list[Any]) -> None:
    """Preserve the legacy account audit; it never blocks headline selection."""
    history = state.setdefault("global_headline_history", [])
    if not isinstance(history, list):
        history = []
    known_keys = {headline_key(item) for item in history if headline_key(item)}
    for value in values:
        headline = clean_headline(value)
        key = headline_key(headline)
        if headline and key and key not in known_keys:
            history.append(headline)
            known_keys.add(key)
    # Retain legacy data for compatibility. Repeat protection uses this workflow
    # run's per-Creative usage, not this account-wide audit list.
    state["global_headline_history"] = history


def creative_headline_history(record: dict[str, Any]) -> list[str]:
    """Read this Creative's known headlines, including legacy/reserved values."""
    if not isinstance(record, dict):
        raise BotError("Saved Creative history has an invalid structure; stopping.")
    history = record.get("headline_history", [])
    if not isinstance(history, list) or any(not isinstance(item, str) for item in history):
        raise BotError("Saved Creative headline history is invalid; stopping without clearing it.")
    values = list(history)
    for field in ("last_headline", "planned_previous_headline", "planned_headline"):
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            raise BotError("A saved Creative headline has an invalid structure; stopping.")
        if value and value not in values:
            values.append(value)
    return values


def remember_creative_headlines(
    state: dict[str, Any], creative_id: str, values: list[Any]
) -> None:
    """Keep observed wording in this run's usage and the per-Creative audit."""
    record = state["creatives"].setdefault(creative_id, {})
    history = creative_headline_history(record)
    known_keys = {headline_key(value) for value in history}
    for value in values:
        if value is not None and not isinstance(value, str):
            raise BotError("Snapchat returned an invalid Creative headline; stopping.")
        headline = clean_headline(value)
        key = headline_key(headline)
        if key and key not in known_keys:
            history.append(headline)
            known_keys.add(key)
    record["headline_history"] = history
    if "headline_usage" in state:
        for value in values:
            if headline_key(value):
                remember_run_headline(state, creative_id, clean_headline(value))


def current_headline_run_id() -> str:
    """All checks share one Actions run/attempt; a new launch gets a fresh pool."""
    if env("GITHUB_ACTIONS").lower() == "true":
        run_id = env("GITHUB_RUN_ID")
        attempt = env("GITHUB_RUN_ATTEMPT")
        if not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt):
            raise BotError("Cannot identify this GitHub workflow run; stopping without resetting history.")
        return f"github:{run_id}:{attempt}"
    # A local monitor may provide a stable token to its repeated worker calls.
    # A standalone local invocation otherwise represents a new run.
    return f"local:{env('HEADLINE_RUN_ID') or uuid4().hex}"


def prepare_headline_run(state: dict[str, Any]) -> None:
    current_run = current_headline_run_id()
    usage = state.get("headline_usage")
    if "headline_usage" in state:
        if (
            not isinstance(usage, dict)
            or not isinstance(usage.get("run_id"), str)
            or not usage["run_id"]
            or not isinstance(usage.get("creatives"), dict)
        ):
            raise BotError("Saved workflow headline usage is invalid; stopping without clearing it.")
        for creative_id, history in usage["creatives"].items():
            if (
                not isinstance(creative_id, str) or not creative_id
                or not isinstance(history, list)
                or any(not isinstance(value, str) for value in history)
            ):
                raise BotError("Saved workflow headline usage is invalid; stopping without clearing it.")
    if usage is not None and usage["run_id"] == current_run:
        print("Continuing this workflow run: headline usage retained.")
        return
    # Only headline eligibility resets. Approval records, review waits, in-flight
    # reservations, target selections and historical audit data stay intact.
    state["headline_usage"] = {"run_id": current_run, "creatives": {}}
    print("New workflow run: headline usage reset; review protections retained.")


def run_headline_history(state: dict[str, Any], creative_id: str) -> list[str]:
    usage = state.get("headline_usage")
    if not isinstance(usage, dict) or not isinstance(usage.get("creatives"), dict):
        raise BotError("Workflow headline usage has not been initialized; stopping.")
    history = usage["creatives"].get(creative_id, [])
    if not isinstance(history, list) or any(not isinstance(value, str) for value in history):
        raise BotError("A Creative's workflow headline usage is invalid; stopping.")
    return list(history)


def remember_run_headline(state: dict[str, Any], creative_id: str, headline: str) -> None:
    history = run_headline_history(state, creative_id)
    if headline_key(headline) not in {headline_key(value) for value in history}:
        history.append(clean_headline(headline))
    state["headline_usage"]["creatives"][creative_id] = history


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        data: dict[str, Any] = {
            "version": STATE_VERSION,
            "active_ad_account_id": "",
            "active_ad_squad_ids": [],
            "active_jobs": {},
            "creatives": {},
            "global_headline_history": [],
        }
    else:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BotError(f"Cannot read {STATE_PATH.name}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("creatives"), dict):
        raise BotError(f"{STATE_PATH.name} has an invalid structure")
    active_ids = data.get("active_ad_squad_ids")
    if active_ids is not None and not isinstance(active_ids, list):
        raise BotError(f"{STATE_PATH.name} has an invalid active_ad_squad_ids structure")
    active_jobs = data.get("active_jobs")
    if active_jobs is not None and not isinstance(active_jobs, dict):
        raise BotError(f"{STATE_PATH.name} has an invalid active_jobs structure")

    # Migrate the previous one-Ad-Squad state without deleting attempt history.
    if not active_ids:
        legacy_active_job = data.get("active_job")
        if isinstance(legacy_active_job, dict):
            legacy_id = str(legacy_active_job.get("ad_squad_id") or "").strip()
            if legacy_id:
                active_ids = parse_ad_squad_ids(legacy_id, "legacy active_job")
                data["active_ad_squad_ids"] = active_ids
                data["active_jobs"] = {
                    legacy_id: {
                        "ad_squad_id": legacy_id,
                        "started_at": legacy_active_job.get("started_at") or utc_now(),
                    }
                }
    legacy_headlines: list[Any] = []
    for record in data["creatives"].values():
        history = creative_headline_history(record)
        record["headline_history"] = history
        legacy_headlines.extend(history)
    remember_headlines(data, legacy_headlines)
    data["version"] = STATE_VERSION
    data.setdefault("active_ad_account_id", "")
    data.setdefault("active_ad_squad_ids", [])
    data.setdefault("active_jobs", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=STATE_PATH.parent,
            prefix=".state-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_PATH)
    except (OSError, TypeError, ValueError) as exc:
        raise StatePersistenceError("Could not save headline history; stopping before further edits.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def checkpoint_before_edit(state: dict[str, Any]) -> None:
    """On Actions, the reservation must reach GitHub before Snapchat is changed."""
    save_state(state)
    if env("GITHUB_ACTIONS").lower() != "true":
        return
    try:
        result = subprocess.run(
            [sys.executable, str(CHECKPOINT_SCRIPT)],
            cwd=STATE_PATH.parent, capture_output=True, timeout=180, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StatePersistenceError(
            "Could not confirm encrypted history was saved to GitHub; this edit was not sent."
        ) from exc
    if result.returncode != 0:
        raise StatePersistenceError(
            "Could not save encrypted history to GitHub; this edit was not sent. "
            "Check STATE_ENCRYPTION_KEY and the workflow's repository write access."
        )
    print("Encrypted headline reservation saved before edit.")


def reserve_headline(
    state: dict[str, Any], candidate: dict[str, Any], headline: str,
) -> str | None:
    """Recheck authoritative history and consume the chosen wording before PATCH."""
    creative_id = candidate["creative_id"]
    record = state["creatives"].setdefault(creative_id, {})
    if (
        record.get("last_review_outcome") == "APPROVED"
        or record.get("patch_in_flight")
        or record.get("awaiting_review")
    ):
        print(f"SKIP {creative_id}: approval or an outstanding edit protects this Creative.")
        return None
    forbidden = {headline_key(value) for value in run_headline_history(state, creative_id)}
    forbidden.add(headline_key(candidate.get("current_headline")))
    if not headline_key(headline) or headline_key(headline) in forbidden:
        print(f"SKIP {creative_id}: the selected headline is current or was used in this workflow run.")
        return None
    remember_creative_headlines(state, creative_id, [candidate.get("current_headline"), headline])
    reservation_id = uuid4().hex
    record.update(
        patch_in_flight=True,
        patch_reservation_id=reservation_id,
        planned_headline=headline,
        planned_previous_headline=candidate.get("current_headline"),
        patch_started_at=utc_now(),
        awaiting_review=True,
        last_review_outcome="PENDING_REVIEW",
    )
    return reservation_id


def release_unsubmitted_reservation(
    state: dict[str, Any], creative_id: str, reservation_id: str,
) -> None:
    """A fresh safety check skipped this edit; retain its consumed wording."""
    record = state["creatives"][creative_id]
    if record.get("patch_reservation_id") != reservation_id:
        raise StatePersistenceError("The active headline reservation changed; stopping.")
    for field in (
        "patch_in_flight", "patch_reservation_id", "planned_headline",
        "planned_previous_headline", "patch_started_at",
    ):
        record.pop(field, None)
    record["awaiting_review"] = False
    if record.get("last_review_outcome") == "PENDING_REVIEW":
        record["last_review_outcome"] = "RECHECK_REQUIRED"


def seconds_since(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def parse_single_uuid(raw: str, label: str) -> str:
    candidate = raw.strip()
    try:
        return str(UUID(candidate))
    except ValueError as exc:
        raise BotError(f"{label} must be a valid UUID: {candidate!r}") from exc


def select_ad_account(
    state: dict[str, Any],
    configured_account_raw: str,
) -> str:
    """Select and persist the one Ad Account allowed for this repository."""
    if not configured_account_raw:
        raise BotError(
            "No Ad Account was configured. Set the GitHub Variable SNAP_AD_ACCOUNT_ID."
        )
    account_id = parse_single_uuid(configured_account_raw, "SNAP_AD_ACCOUNT_ID")
    saved_account = str(state.get("active_ad_account_id") or "").strip()
    if saved_account:
        saved_account = parse_single_uuid(saved_account, "saved active Ad Account")
    if saved_account and saved_account != account_id:
        raise BotError(
            "Safety check failed: state.json belongs to a different Ad Account. "
            "Use a separate repository for each Ad Account."
        )
    state["active_ad_account_id"] = account_id
    print(f"Using configured Ad Account: {account_id}")
    return account_id


def select_active_ad_squads(
    state: dict[str, Any], requested_raw: str, fallback_raw: str
) -> list[str]:
    requested = parse_ad_squad_ids(requested_raw, "ad_squad_ids") if requested_raw else []
    existing = [
        value
        for value in state.get("active_ad_squad_ids", [])
        if isinstance(value, str) and value.strip()
    ]
    existing = parse_ad_squad_ids(",".join(existing), "saved active Ad Squads") if existing else []
    fallback = parse_ad_squad_ids(fallback_raw, "SNAP_AD_SQUAD_IDS") if fallback_raw else []
    selected = requested or existing or fallback
    if not selected:
        raise BotError(
            "No Ad Squad was selected. Enter one or more UUIDs in ad_squad_ids."
        )

    jobs = state.setdefault("active_jobs", {})
    if requested:
        now = utc_now()
        previous = set(existing)
        state["active_ad_squad_ids"] = selected
        state["active_jobs"] = {
            ad_squad_id: {
                "ad_squad_id": ad_squad_id,
                "started_at": (
                    jobs.get(ad_squad_id, {}).get("started_at")
                    if isinstance(jobs.get(ad_squad_id), dict)
                    else None
                )
                or now,
                "last_manual_start_at": now,
            }
            for ad_squad_id in selected
        }
        action = "Continuing" if set(selected) == previous else "Selected"
        print(f"{action} {len(selected)} active Ad Squad job(s):")
    else:
        state["active_ad_squad_ids"] = selected
        print(f"Scheduled run is continuing {len(selected)} active Ad Squad job(s):")

    for ad_squad_id in selected:
        print(f"  - {ad_squad_id}")
    return selected


def response_json(response: requests.Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok:
        request_id = response.headers.get("x-request-id")
        if not request_id and isinstance(payload, dict):
            request_id = payload.get("request_id")
        detail = payload or response.text[:500] or "no response body"
        if response.status_code == 403:
            raise BotError(
                f"{label} returned 403 Forbidden. Check the Snapchat user's ad-account "
                f"permission and OAuth app scope. request_id={request_id}; detail={detail}"
            )
        raise BotError(
            f"{label} failed with HTTP {response.status_code}. "
            f"request_id={request_id}; detail={detail}"
        )
    if not isinstance(payload, dict):
        raise BotError(f"{label} returned an unexpected response")
    return payload


def refresh_snap_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    response = requests.post(
        SNAP_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=TIMEOUT,
    )
    # Token errors can echo credentials in their body, description or headers.
    # Log only a recognized OAuth code and our own fixed guidance, even when
    # PUBLIC_SAFE_LOGS is disabled. Unknown server text must never be printed.
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    guidance = {
        "invalid_client": (
            "Snapchat rejected app authentication. Check SNAP_CLIENT_ID and "
            "SNAP_CLIENT_SECRET from the same OAuth app."
        ),
        "invalid_grant": (
            "The refresh token is invalid, revoked, expired, or issued to another app. "
            "Confirm the app credentials, then authorize that app again with "
            "get_snap_token.ps1 and update SNAP_REFRESH_TOKEN."
        ),
        "invalid_request": (
            "Snapchat rejected the token request format. Check the three Snapchat "
            "Secrets contain only their raw values, with no labels or quotes."
        ),
        "unauthorized_client": "The OAuth app is not authorized to use this grant type.",
        "unsupported_grant_type": "Snapchat did not accept the refresh-token grant type.",
        "invalid_scope": "Check the OAuth authorization includes snapchat-marketing-api.",
        "server_error": "Snapchat reported a server error. Try one test again later.",
        "temporarily_unavailable": "Snapchat reported temporary unavailability. Try one test later.",
    }
    raw_code = payload.get("error") if isinstance(payload, dict) else None
    code = raw_code if isinstance(raw_code, str) and raw_code in guidance else "unknown"
    if not response.ok or (isinstance(payload, dict) and "error" in payload):
        hint = guidance.get(
            code,
            "Snapchat did not return a recognized OAuth code. Check the matching "
            "Client ID, Client Secret and Refresh Token; the response body remains private.",
        )
        raise BotError(
            f"Snapchat token refresh failed with HTTP {response.status_code}; "
            f"oauth_error={code}. {hint}"
        )
    if not isinstance(payload, dict):
        raise BotError("Snapchat token response had an unexpected structure; response body hidden.")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise BotError("Snapchat token response did not contain access_token")
    return access_token


class SnapClient:
    def __init__(self, access_token: str) -> None:
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def get(self, path_or_url: str, label: str) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{SNAP_API}{path_or_url}"
        for attempt in range(1, MAX_SNAP_READ_ATTEMPTS + 1):
            try:
                response = requests.get(url, headers=self.headers, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == MAX_SNAP_READ_ATTEMPTS:
                    raise BotError(
                        f"{label} failed after {attempt} attempts because Snapchat reset "
                        f"the connection: {exc}"
                    ) from exc
                delay = min(2 ** (attempt - 1), 8)
                print(
                    f"RETRY {label}: temporary network error on attempt "
                    f"{attempt}/{MAX_SNAP_READ_ATTEMPTS}; waiting {delay}s."
                )
                time.sleep(delay)
                continue

            if (
                response.status_code in TRANSIENT_HTTP_STATUSES
                and attempt < MAX_SNAP_READ_ATTEMPTS
            ):
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(1, min(int(float(retry_after)), 30))
                except (TypeError, ValueError):
                    delay = min(2 ** (attempt - 1), 8)
                print(
                    f"RETRY {label}: Snapchat returned HTTP {response.status_code} on "
                    f"attempt {attempt}/{MAX_SNAP_READ_ATTEMPTS}; waiting {delay}s."
                )
                time.sleep(delay)
                continue

            return response_json(response, label)

        raise BotError(f"{label} failed after transient retries")

    @staticmethod
    def checked_entities(
        payload: dict[str, Any], collection: str, entity_key: str, label: str
    ) -> list[dict[str, Any]]:
        """Never interpret a failed or incomplete response as a safe empty list."""
        if payload.get("request_status") != "SUCCESS":
            raise BotError(f"{label}: Snapchat did not confirm a successful request")
        wrappers = payload.get(collection)
        if not isinstance(wrappers, list):
            raise BotError(f"{label}: Snapchat omitted the expected entity array")
        entities = []
        for wrapper in wrappers:
            if not isinstance(wrapper, dict) or wrapper.get("sub_request_status") != "SUCCESS":
                raise BotError(f"{label}: Snapchat returned an unsuccessful entity result")
            entity = wrapper.get(entity_key)
            if (
                not isinstance(entity, dict)
                or not isinstance(entity.get("id"), str)
                or not entity["id"]
            ):
                raise BotError(f"{label}: Snapchat returned an invalid entity")
            entities.append(entity)
        return entities

    def get_all(self, path: str, collection: str, entity_key: str) -> list[dict[str, Any]]:
        url = f"{SNAP_API}{path}"
        entities: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        while url:
            if url in seen_urls or not url.startswith(f"{SNAP_API}/"):
                raise BotError("Safety check failed: invalid or repeated Snapchat pagination")
            seen_urls.add(url)
            label = f"Snapchat list {collection}"
            payload = self.get(url, label)
            for entity in self.checked_entities(payload, collection, entity_key, label):
                if entity["id"] in seen_ids:
                    raise BotError("Safety check failed: duplicate entity in Snapchat pagination")
                seen_ids.add(entity["id"])
                entities.append(entity)
            paging = payload.get("paging")
            if paging is None:
                paging = {}
            if not isinstance(paging, dict):
                raise BotError("Safety check failed: invalid Snapchat paging information")
            next_link = paging.get("next_link")
            if next_link is not None and not isinstance(next_link, str):
                raise BotError("Safety check failed: invalid Snapchat next-page link")
            url = urljoin(url, next_link) if next_link else ""
        return entities

    def one(self, path: str, collection: str, entity_key: str, label: str) -> dict[str, Any]:
        payload = self.get(path, label)
        entities = self.checked_entities(payload, collection, entity_key, label)
        if len(entities) != 1:
            raise BotError(f"{label} did not return exactly one {entity_key}")
        return entities[0]

    def patch_headline(
        self, ad_account_id: str, creative_id: str, headline: str, *, expected_headline: str
    ) -> dict[str, Any]:
        url = f"{SNAP_API}/adaccounts/{ad_account_id}/creatives/{creative_id}"
        headers = {
            **self.headers,
            "Content-Type": "application/json-patch+json",
        }
        # Snapchat JSON Patch supports test. Both conditions must pass in the
        # same request as the edit. Never retry without these conditions.
        body = [
            {"op": "test", "path": "/review_status", "value": "DISAPPROVED"},
            {"op": "test", "path": "/headline", "value": expected_headline},
            {"op": "replace", "path": "/headline", "value": headline},
        ]
        response = requests.patch(url, headers=headers, json=body, timeout=TIMEOUT)
        label = f"Snapchat guarded PATCH creative {creative_id}"
        payload = response_json(response, label)
        entities = self.checked_entities(payload, "creatives", "creative", label)
        if (
            len(entities) != 1
            or entities[0]["id"] != creative_id
            or entities[0].get("headline") != headline
        ):
            raise BotError("Snapchat did not confirm the requested headline edit; stopping")
        return payload


def verify_scope(
    snap: SnapClient,
    ad_account_id: str,
    ad_squad_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ad_squad = snap.one(
        f"/adsquads/{ad_squad_id}", "adsquads", "adsquad", "Get selected ad squad"
    )
    campaign_id = str(ad_squad.get("campaign_id") or "")
    if not campaign_id:
        raise BotError("Selected Ad Squad did not contain a Campaign ID")
    campaign = snap.one(
        f"/campaigns/{campaign_id}",
        "campaigns",
        "campaign",
        "Get parent campaign",
    )
    if str(campaign.get("id") or campaign_id) != campaign_id:
        raise BotError("Safety check failed: the Ad Squad parent Campaign was not returned")
    if str(campaign.get("ad_account_id") or "") != ad_account_id:
        raise BotError(
            "Safety check failed: selected Ad Squad is not inside SNAP_AD_ACCOUNT_ID"
        )
    print(
        "Verified target: "
        f"ad_account={ad_account_id!r}; "
        f"campaign={campaign.get('name', campaign_id)!r}; "
        f"ad_squad={ad_squad.get('name', ad_squad_id)!r}"
    )
    return campaign, ad_squad


def creative_stays_inside_selected_squads(
    creative_id: str,
    selected_ad_squad_ids: set[str],
    all_account_ads: list[dict[str, Any]],
) -> bool:
    linked_squads = {
        str(ad.get("ad_squad_id") or "")
        for ad in all_account_ads
        if ad.get("creative_id") == creative_id and not ad.get("deleted", False)
    }
    linked_squads.discard("")
    return not linked_squads or linked_squads.issubset(selected_ad_squad_ids)


def remember_approval(state: dict[str, Any], creative_id: str, evidence: str) -> None:
    record = state["creatives"].setdefault(creative_id, {})
    record["awaiting_review"] = False
    record["last_review_outcome"] = "APPROVED"
    record["last_review_completed_at"] = utc_now()
    record["last_review_completion_evidence"] = evidence


def recheck_before_edit(
    snap: SnapClient,
    ad_account_id: str,
    selected_ad_squad_ids: set[str],
    state: dict[str, Any],
    candidate: dict[str, Any],
    *, reservation_id: str | None = None,
) -> str | None:
    """Return the exact current headline only after fresh reads confirm eligibility.

    A Creative can be shared by multiple Ads. Refresh the complete account list
    to discover new links, then re-read each linked Ad and finally the Creative.
    These separate reads cannot form an atomic transaction with the later PATCH;
    the PATCH adds server-side conditions on the Creative itself.
    """
    creative_id = candidate["creative_id"]
    record = state["creatives"].setdefault(creative_id, {})
    if record.get("last_review_outcome") == "APPROVED":
        print(f"SKIP {creative_id}: approval was previously observed; permanently protected.")
        return None
    owns_reservation = (
        reservation_id is not None
        and record.get("patch_reservation_id") == reservation_id
        and record.get("patch_in_flight") is True
        and record.get("awaiting_review") is True
    )
    if (record.get("patch_in_flight") or record.get("awaiting_review")) and not owns_reservation:
        print(f"WAIT {creative_id}: a previous edit still needs a confirmed review result.")
        return None

    account_ads = snap.get_all(
        f"/adaccounts/{ad_account_id}/ads?limit=1000&read_deleted_entities=true",
        "ads", "ad",
    )
    linked_ads = []
    for ad in account_ads:
        if not isinstance(ad.get("deleted", False), bool):
            raise BotError("Safety check failed: an Ad has an unknown deletion state")
        if ad.get("deleted", False):
            continue
        if not isinstance(ad.get("creative_id"), str) or not ad["creative_id"]:
            raise BotError("Safety check failed: an Ad's Creative link could not be verified")
        if ad["creative_id"] == creative_id:
            linked_ads.append(ad)

    def linked_ads_are_safe(ads: list[dict[str, Any]]) -> bool:
        if any(ad.get("review_status") == "APPROVED" for ad in ads):
            remember_approval(state, creative_id, "LINKED_AD_APPROVED_BEFORE_EDIT")
            print(f"SKIP {creative_id}: a linked Ad is APPROVED; permanently protected.")
            return False
        if any(ad.get("review_status") != "REJECTED" for ad in ads):
            print(f"WAIT {creative_id}: a linked Ad is pending or not confirmed REJECTED.")
            return False
        if any(ad.get("ad_squad_id") not in selected_ad_squad_ids for ad in ads):
            print(f"SKIP {creative_id}: a linked Ad is outside the selected Ad Squads.")
            return False
        return True

    if not linked_ads_are_safe(linked_ads):
        return None
    if not linked_ads or candidate.get("ad_id") not in {ad["id"] for ad in linked_ads}:
        print(f"SKIP {creative_id}: the original Ad-to-Creative link is no longer confirmed.")
        return None
    for ad in linked_ads:
        fresh_ad = snap.one(
            f"/ads/{ad['id']}?read_deleted_entities=true", "ads", "ad",
            "Recheck linked Ad before edit",
        )
        if (
            fresh_ad.get("id") != ad["id"]
            or fresh_ad.get("creative_id") != creative_id
            or fresh_ad.get("deleted", False) is not False
        ):
            print(f"SKIP {creative_id}: a linked Ad changed during the safety check.")
            return None
        if not linked_ads_are_safe([fresh_ad]):
            return None

    creative = snap.one(
        f"/creatives/{creative_id}", "creatives", "creative",
        "Recheck Creative before edit",
    )
    if (
        creative.get("id") != creative_id
        or creative.get("ad_account_id") != ad_account_id
        or creative.get("deleted", False) is not False
    ):
        raise BotError("Safety check failed: the current Creative's identity could not be verified")
    if creative.get("review_status") == "APPROVED":
        remember_approval(state, creative_id, "CREATIVE_APPROVED_BEFORE_EDIT")
        print(f"SKIP {creative_id}: the Creative is APPROVED; permanently protected.")
        return None
    if creative.get("review_status") != "DISAPPROVED":
        print(f"WAIT {creative_id}: the Creative is pending or not confirmed DISAPPROVED.")
        return None
    current_headline = creative.get("headline")
    if (
        not isinstance(current_headline, str)
        or clean_headline(current_headline) != candidate.get("current_headline")
    ):
        print(f"SKIP {creative_id}: the headline changed after candidate selection.")
        return None
    return current_headline


def collect_candidates(
    ad_squad_id: str,
    selected_ad_squad_ids: set[str],
    state: dict[str, Any],
    max_updates: int,
    all_account_ads: list[dict[str, Any]],
    creative_by_id: dict[str, dict[str, Any]],
    globally_seen: set[str],
) -> tuple[list[dict[str, Any]], Counter[str], int]:
    selected_ads = [
        ad
        for ad in all_account_ads
        if str(ad.get("ad_squad_id") or "") == ad_squad_id
    ]
    live_ads = [ad for ad in selected_ads if not ad.get("deleted", False)]
    status_counts = Counter(
        str(ad.get("review_status", "")).upper() or "UNKNOWN"
        for ad in live_ads
    )
    print(
        f"Ad review statuses for {ad_squad_id}: "
        + ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items()))
    )

    state_creatives = state["creatives"]
    for ad in live_ads:
        creative_id = str(ad.get("creative_id") or "")
        if creative_id and ad.get("review_status") == "APPROVED":
            remember_approval(state, creative_id, "LINKED_AD_APPROVED")
        record = state_creatives.get(creative_id)
        if not isinstance(record, dict):
            continue
        ad_status = str(ad.get("review_status", "")).upper()
        if ad_status in {"PENDING", "PENDING_REVIEW"} and record.get("awaiting_review"):
            record.setdefault("pending_seen_at", utc_now())
            record["last_observed_review_status"] = ad_status
        elif ad_status == "APPROVED" and (
            record.get("awaiting_review") or record.get("last_review_outcome") != "APPROVED"
        ):
            record["awaiting_review"] = False
            record["last_review_outcome"] = "APPROVED"
            record["last_review_completed_at"] = utc_now()
        elif record.get("awaiting_review"):
            record["last_observed_review_status"] = ad_status or "UNKNOWN"

    rejected = [
        ad
        for ad in live_ads
        if str(ad.get("review_status", "")).upper() == "REJECTED"
        and ad.get("creative_id")
    ]
    print(
        f"Selected Ad Squad {ad_squad_id} contains {len(live_ads)} Ads; "
        f"{len(rejected)} are REJECTED."
    )

    if not rejected:
        return [], status_counts, len(live_ads)
    if max_updates <= 0:
        print(
            f"DEFER {ad_squad_id}: this check reached the {HARD_MAX_UPDATES_PER_RUN}-"
            "creative total limit; rejected Ads remain eligible for the next check."
        )
        return [], status_counts, len(live_ads)

    candidates: list[dict[str, Any]] = []

    for ad in rejected:
        creative_id = str(ad["creative_id"])
        if creative_id in globally_seen:
            continue
        globally_seen.add(creative_id)

        linked_live_ads = [
            linked_ad
            for linked_ad in all_account_ads
            if linked_ad.get("creative_id") == creative_id
            and not linked_ad.get("deleted", False)
        ]
        linked_statuses = {
            str(linked_ad.get("review_status", "")).upper() or "UNKNOWN"
            for linked_ad in linked_live_ads
        }
        if "APPROVED" in linked_statuses:
            remember_approval(state, creative_id, "LINKED_AD_APPROVED")
            print(
                f"SKIP {creative_id}: this Creative is connected to an APPROVED Ad; "
                "the approved headline will not be touched."
            )
            continue
        if linked_statuses.intersection({"PENDING", "PENDING_REVIEW"}):
            print(
                f"WAIT {creative_id}: this Creative is still connected to an Ad "
                "under review."
            )
            continue
        if linked_statuses != {"REJECTED"}:
            print(
                f"SKIP {creative_id}: linked Ad status is "
                f"{', '.join(sorted(linked_statuses)) or 'UNKNOWN'}."
            )
            continue

        record = state_creatives.setdefault(creative_id, {})
        if record.get("last_review_outcome") == "APPROVED":
            print(
                f"SKIP {creative_id}: this Creative has already reached an APPROVED "
                "review result; it will never be edited again."
            )
            continue
        attempts = int(record.get("attempts", 0))
        if not creative_stays_inside_selected_squads(
            creative_id, selected_ad_squad_ids, all_account_ads
        ):
            print(
                f"SKIP {creative_id}: this creative is also connected to an ad outside "
                "the selected Ad Squad list."
            )
            continue

        creative = creative_by_id.get(creative_id)
        if not creative:
            print(
                f"SKIP {creative_id}: linked Creative was not returned by the Ad Account."
            )
            continue
        current_headline = clean_headline(creative.get("headline"))
        if record.get("patch_in_flight"):
            planned_headline = clean_headline(record.get("planned_headline"))
            elapsed = seconds_since(record.get("patch_started_at"))
            if planned_headline and current_headline == planned_headline:
                # A previous run may have lost its connection after Snapchat accepted
                # the PATCH. Reconstruct the successful edit from the observed headline
                # before waiting for its review transition.
                previous_headline = clean_headline(
                    record.get("planned_previous_headline")
                )
                history = record.setdefault("headline_history", [])
                if previous_headline and previous_headline not in history:
                    history.append(previous_headline)
                if planned_headline not in history:
                    history.append(planned_headline)
                record["headline_history"] = history
                remember_headlines(state, [previous_headline, planned_headline])
                record["attempts"] = int(record.get("attempts", 0)) + 1
                record["last_headline"] = planned_headline
                record["last_patch_at"] = record.get("patch_started_at") or utc_now()
                record["awaiting_review"] = True
                record["last_review_outcome"] = "PENDING_REVIEW"
                record.pop("pending_seen_at", None)
                record.pop("patch_in_flight", None)
                record.pop("patch_reservation_id", None)
                record.pop("planned_headline", None)
                record.pop("planned_previous_headline", None)
                record.pop("patch_started_at", None)
                print(
                    f"RECOVERED PATCH {creative_id}: Snapchat already has the planned "
                    "headline; waiting for its review transition."
                )
            else:
                wait_seconds = MIN_REVIEW_PROPAGATION_SECONDS
                if elapsed is not None and elapsed >= wait_seconds:
                    wait_seconds = 60
                print(
                    f"WAIT {creative_id}: the previous PATCH result is uncertain; "
                    f"checking again in about {wait_seconds}s before any new edit."
                )
                continue
        creative_status = str(creative.get("review_status", "")).upper()
        if creative_status == "PENDING_REVIEW":
            if record.get("awaiting_review"):
                record.setdefault("pending_seen_at", utc_now())
                record["last_observed_review_status"] = "PENDING_REVIEW"
            print(f"WAIT {creative_id}: creative is still PENDING_REVIEW.")
            continue
        if creative_status == "APPROVED":
            remember_approval(state, creative_id, "CREATIVE_APPROVED")
            print(
                f"SKIP {creative_id}: the Creative is APPROVED; its headline will "
                "never be edited."
            )
            continue
        if creative_status != "DISAPPROVED":
            print(f"SKIP {creative_id}: creative status is {creative_status or 'UNKNOWN'}.")
            continue

        if record.get("awaiting_review"):
            elapsed = seconds_since(record.get("last_patch_at"))
            if elapsed is None or elapsed < MIN_REVIEW_PROPAGATION_SECONDS:
                remaining = (
                    MIN_REVIEW_PROPAGATION_SECONDS
                    if elapsed is None
                    else int(MIN_REVIEW_PROPAGATION_SECONDS - elapsed)
                )
                print(
                    f"WAIT {creative_id}: the headline was just submitted; "
                    f"allow about {max(1, remaining)} more second(s) for status propagation."
                )
                continue

            completion_evidence = "PENDING_OBSERVED"
            if not record.get("pending_seen_at"):
                last_submitted_headline = clean_headline(record.get("last_headline"))
                if not last_submitted_headline or current_headline != last_submitted_headline:
                    print(
                        f"WAIT {creative_id}: PENDING was not observed and the current "
                        "headline does not match the bot's last submitted headline; no "
                        "automatic edit is safe."
                    )
                    continue
                if elapsed < MISSED_PENDING_RECOVERY_SECONDS:
                    remaining = max(
                        1, int(MISSED_PENDING_RECOVERY_SECONDS - elapsed)
                    )
                    print(
                        f"WAIT {creative_id}: PENDING was not observed. The Ad is "
                        "REJECTED and the Creative is DISAPPROVED, but the bot will "
                        f"wait about {remaining} more second(s) before recovering the "
                        "missed transition."
                    )
                    continue
                completion_evidence = "FINAL_STATUSES_AFTER_MISSED_PENDING"
                record["missed_pending_recovered_at"] = utc_now()
                print(
                    f"MISSED PENDING RECOVERED {creative_id}: the submitted headline "
                    "is present and Snapchat now reports final REJECTED/DISAPPROVED "
                    "statuses after the safety delay."
                )
            record["awaiting_review"] = False
            record["last_review_outcome"] = "REJECTED"
            record["last_review_completed_at"] = utc_now()
            record["last_review_completion_evidence"] = completion_evidence
            record.pop("pending_seen_at", None)
            print(
                f"REJECTED AGAIN {creative_id}: review completion is confirmed; "
                "generating the next headline immediately."
            )

        candidates.append(
            {
                "creative_id": creative_id,
                "ad_id": str(ad.get("id") or ""),
                "ad_name": str(ad.get("name") or ""),
                "ad_squad_id": ad_squad_id,
                "creative_name": str(creative.get("name") or ""),
                "current_headline": current_headline,
                "ad_review_reasons": (
                    ad.get("review_status_reasons")
                    or ad.get("review_status_reason")
                    or []
                ),
                "creative_review_reasons": (
                    creative.get("review_status_reasons")
                    or creative.get("review_status_reason")
                    or creative.get("review_status_details")
                    or []
                ),
                "attempts_used": attempts,
                "previous_headlines": run_headline_history(state, creative_id),
            }
        )
        if len(candidates) >= max_updates:
            break

    return candidates, status_counts, len(live_ads)


def extract_openai_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    refusals: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                texts.append(content["text"])
            if content.get("type") == "refusal" and isinstance(content.get("refusal"), str):
                refusals.append(content["refusal"])
    if refusals:
        raise BotError(f"OpenAI declined the headline request: {'; '.join(refusals)}")
    text = "".join(texts).strip()
    if not text:
        raise BotError("OpenAI response did not contain structured output text")
    return text


OPENAI_ERROR_GUIDANCE = {
    "credit_balance_exhausted": "Check the API organization's prepaid credit balance.",
    "organization_spend_limit_exceeded": "Check the API organization's spend limit.",
    "project_spend_limit_exceeded": "Check the API key's project spend limit.",
    "organization_usage_limit_exceeded": "Check the API organization's approved usage limit.",
    "insufficient_quota": "Check API billing, available credits and usage limits.",
    "billing_hard_limit_reached": "Check API billing and spend limits.",
    "rate_limit_exceeded": "Check the model's request and token limits; reduce concurrent bot runs.",
    "slow_down": "Reduce the request rate and increase it gradually.",
    "server_is_overloaded": "The model is temporarily overloaded; try one test later.",
    "server_error": "The API reported a server error; try one test later.",
    "invalid_api_key": "Check the OPENAI_API_KEY Secret.",
    "model_not_found": "Check the model name and the API project's model access.",
    "permission_denied": "Check the API key's project permissions.",
}
OPENAI_SAFE_ERROR_TYPES = {
    "rate_limit_error", "requests", "tokens", "insufficient_quota",
    "invalid_request_error", "authentication_error", "permission_error",
    "server_error", "service_unavailable_error",
}
OPENAI_QUOTA_CODES = {
    "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded",
    "insufficient_quota", "billing_hard_limit_reached",
}


def openai_retry_delay(response: requests.Response, attempt: int) -> float:
    """Honor a valid Retry-After without shortening the server's wait."""
    raw = response.headers.get("Retry-After", "")
    if isinstance(raw, str) and raw.strip():
        try:
            delay = float(raw)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                delay = -1.0
        if math.isfinite(delay) and delay >= 0:
            return max(1.0, delay)
    return 15.0 * (2 ** (attempt - 1)) + random.uniform(0.0, 3.0)


def request_openai_json(api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    """Expose only allowlisted diagnostics; never print API response text."""
    deadline = time.monotonic() + OPENAI_RETRY_WINDOW_SECONDS
    for attempt in range(1, MAX_OPENAI_REQUEST_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenAIServiceError("OpenAI retry window ended; monitor stopped.")
        try:
            response = requests.post(
                OPENAI_RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=min(90.0, remaining),
            )
        except requests.RequestException:
            # A timeout can have an ambiguous billing outcome; do not resend it.
            raise OpenAIServiceError(
                "OpenAI network request did not complete; monitor stopped. "
                "Check API usage before starting one test again; private details hidden."
            ) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.ok and isinstance(payload, dict) and not payload.get("error"):
            return payload

        error = payload.get("error") if isinstance(payload, dict) else None
        error = error if isinstance(error, dict) else {}
        raw_code, raw_type = error.get("code"), error.get("type")
        code = raw_code if isinstance(raw_code, str) and raw_code in OPENAI_ERROR_GUIDANCE else "unknown"
        error_type = raw_type if isinstance(raw_type, str) and raw_type in OPENAI_SAFE_ERROR_TYPES else "unknown"
        hint = OPENAI_ERROR_GUIDANCE.get(
            code,
            "Check the API key's project billing and limits. "
            "This response does not identify the cause; private details hidden.",
        )
        diagnostic = (
            f"OpenAI Responses API failed with HTTP {response.status_code}; "
            f"openai_error={code}; openai_type={error_type}. {hint}"
        )
        quota_error = code in OPENAI_QUOTA_CODES or error_type == "insufficient_quota"
        temporary_rate_limit = code in {"rate_limit_exceeded", "slow_down"} or (
            raw_code in (None, "") and error_type in {"rate_limit_error", "requests", "tokens"}
        )
        retryable = not quota_error and (
            (response.status_code == 429 and temporary_rate_limit)
            or response.status_code in {500, 502, 503, 504}
        )
        if not retryable:
            raise OpenAIServiceError(diagnostic + " No automatic retry; monitor stopped.")
        if attempt >= MAX_OPENAI_REQUEST_ATTEMPTS:
            raise OpenAIServiceError(diagnostic + " Retry attempts exhausted; monitor stopped.")
        delay = openai_retry_delay(response, attempt)
        if delay >= deadline - time.monotonic():
            raise OpenAIServiceError(
                diagnostic + " Required wait exceeds this retry window; monitor stopped."
            )
        print(
            f"RETRY OpenAI: HTTP {response.status_code}; openai_error={code}; "
            f"waiting {math.ceil(delay)} seconds before attempt "
            f"{attempt + 1}/{MAX_OPENAI_REQUEST_ATTEMPTS}.",
            flush=True,
        )
        time.sleep(delay)
    raise OpenAIServiceError("OpenAI request did not finish; monitor stopped.")


def generate_headlines(
    api_key: str,
    model: str,
    product_context: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Preserve the previous request size while allowing more edits per check.
    # The caller checks each Creative's own history; different IDs may share copy.
    if len(candidates) > MAX_HEADLINE_REQUEST_CREATIVES:
        items: list[dict[str, Any]] = []
        for start in range(0, len(candidates), MAX_HEADLINE_REQUEST_CREATIVES):
            items.extend(generate_headlines(
                api_key, model, product_context,
                candidates[start:start + MAX_HEADLINE_REQUEST_CREATIVES],
            ))
        return items

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "creative_id": {"type": "string"},
                        "action": {"type": "string", "enum": ["UPDATE"]},
                        "headlines": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 34},
                            "minItems": HEADLINE_OPTIONS_PER_CREATIVE,
                            "maxItems": HEADLINE_OPTIONS_PER_CREATIVE,
                        },
                    },
                    "required": ["creative_id", "action", "headlines"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    instructions = """
Generate five genuinely different, natural Arabic Snapchat ad headline options for every
supplied creative_id. The bot chooses the first option not previously used on that Creative.

Goal: create a neutral, natural headline that can suit any product or niche while
accurately representing the actual product. Use wording that reduces avoidable ad-review
problems, but never promise or guarantee approval.

Rules:
- Return one item for every supplied creative_id.
- Return exactly five headline options inside headlines for every item.
- Use Arabic only.
- Maximum 34 Unicode characters, including spaces and punctuation.
- Prefer 3 to 6 words and approximately 18 to 28 characters.
- Match product_context, the advertisement, and the landing page.
- Write in clear, natural White Arabic suitable for Saudi Arabia; avoid robotic,
  over-poetic, vague, or translated-sounding wording.
- Prefer clear wording about the product, availability, discovery, details, or ordering.
  Mention delivery, payment, location, price, or an offer only when product_context
  explicitly confirms that exact fact.
- Across the five options, use different angles. Do not make all five about payment,
  cash on delivery, shipping, or the same call to action.
- Never invent benefits, guarantees, medical results, discounts, delivery terms, or facts.
- Never hide or misrepresent the product category.
- Avoid exaggerated claims, before-and-after claims, pressure tactics, and approval promises.
- Avoid empty quality claims such as "الأفضل" or "جودة مضمونة" and avoid awkward filler.
- Never use a Campaign name, Ad Squad name, Ad name, Creative name, internal label,
  resource ID, SKU, file name, brand name, or transliteration of any such name.
- Never infer a public product name from creative_id. Treat every creative_id as an opaque
  routing value that must be copied only into the JSON creative_id field.
- Preferred tone examples are: "متوفر الآن داخل السعودية",
  "اطلب بسهولة داخل السعودية", "اكتشف التفاصيل الآن", and "لمسة فاخرة ليومك".
  Use their concise, natural tone.
- Each Creative must receive wording it has not used before. Its own exact and
  near-duplicate history is checked locally and is intentionally not exposed to you.
- Make the five options meaningfully different from one another. Changing only punctuation,
  one small word, or word order is not a fresh headline.
- The same suitable headline CAN be offered to 10, 80, or more DIFFERENT Creative IDs.
  Sharing across Creative IDs is allowed, including across separate requests.
  Do not force different Creatives to use different wording.
- Always return action UPDATE with five fresh, truthful, non-empty options.
- Follow the required JSON schema and return no additional text.
""".strip()
    user_input = json.dumps(
        {
            "product_context": product_context,
            # Resource names, ad names, prior headlines, and review metadata are
            # deliberately excluded so internal labels cannot leak into copy.
            "ads": openai_candidate_payload(candidates),
        },
        ensure_ascii=False,
    )
    body = {
        "model": model,
        "store": False,
        "reasoning": {"effort": "low"},
        "max_output_tokens": 8000,
        "input": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_input},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "snapchat_headline_result",
                "strict": True,
                "schema": schema,
            }
        },
    }
    payload = request_openai_json(api_key, body)
    try:
        parsed = json.loads(extract_openai_text(payload))
    except json.JSONDecodeError as exc:
        raise BotError(f"OpenAI returned invalid JSON: {exc}") from exc
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        raise BotError("OpenAI structured output did not contain an items array")
    return [item for item in items if isinstance(item, dict)]


def validate_suggestions(
    candidates: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
    *,
    reject_near_duplicates: bool = True,
) -> list[tuple[dict[str, Any], str]]:
    candidate_by_id = {item["creative_id"]: item for item in candidates}
    accepted: list[tuple[dict[str, Any], str]] = []
    handled: set[str] = set()

    for suggestion in suggestions:
        creative_id = suggestion.get("creative_id")
        if not isinstance(creative_id, str) or creative_id not in candidate_by_id:
            print(f"SKIP model item with unknown creative_id: {creative_id!r}")
            continue
        if creative_id in handled:
            print(f"SKIP duplicate model item for {creative_id}")
            continue
        handled.add(creative_id)
        if suggestion.get("action") != "UPDATE":
            print(f"SKIP {creative_id}: model determined headline-only correction is unsuitable.")
            continue
        options = suggestion.get("headlines")
        if not isinstance(options, list):
            print(f"RETRY {creative_id}: model did not return headline options.")
            continue
        candidate = candidate_by_id[creative_id]
        previous = candidate.get("previous_headlines", [])
        if not isinstance(previous, list) or any(not isinstance(value, str) for value in previous):
            raise BotError("A Creative's headline history could not be checked; stopping.")
        # Only this workflow run's usage and the current headline block wording.
        # Never pool history or reserve accepted wording across Creative IDs.
        forbidden_keys = {headline_key(candidate.get("current_headline"))}
        forbidden_keys.update(headline_key(value) for value in previous if headline_key(value))
        suitable: dict[str, str] = {}
        for option in options:
            if not isinstance(option, str):
                continue
            headline = clean_headline(option)
            key = headline_key(headline)
            if not headline or len(headline) > 34 or not key:
                continue
            if headline_contains_internal_name(headline, candidate):
                continue
            if key in forbidden_keys:
                continue
            if reject_near_duplicates and any(
                SequenceMatcher(None, key, old_key).ratio() >= NEAR_DUPLICATE_RATIO
                for old_key in forbidden_keys
                if old_key
            ):
                continue
            suitable.setdefault(key, headline)
        if not suitable:
            print(
                f"RETRY {creative_id}: no option passed this Creative's workflow headline "
                "history, length and internal-name checks. Sharing with other Creatives is allowed."
            )
            continue
        # Independent random choice per Creative, without replacement over its
        # current-run history. Duplicate input lines never increase their weight.
        accepted.append((candidate, HEADLINE_RANDOM.choice(list(suitable.values()))))
    return accepted


def record_update(
    state: dict[str, Any], candidate: dict[str, Any], new_headline: str, result: dict[str, Any]
) -> None:
    creative_id = candidate["creative_id"]
    records = state["creatives"]
    record = records.setdefault(creative_id, {})
    history = record.setdefault("headline_history", [])
    current = candidate.get("current_headline")
    if current and current not in history:
        history.append(current)
    if new_headline not in history:
        history.append(new_headline)
    record["headline_history"] = history
    remember_headlines(state, [current, new_headline])
    record["attempts"] = int(record.get("attempts", 0)) + 1
    record["last_ad_id"] = candidate.get("ad_id")
    record["last_ad_squad_id"] = candidate.get("ad_squad_id")
    record["last_headline"] = new_headline
    record["last_patch_at"] = utc_now()
    record["last_result_request_id"] = result.get("request_id")
    record["awaiting_review"] = True
    record["last_review_outcome"] = "PENDING_REVIEW"
    record.pop("pending_seen_at", None)
    record.pop("patch_in_flight", None)
    record.pop("patch_reservation_id", None)
    record.pop("planned_headline", None)
    record.pop("planned_previous_headline", None)
    record.pop("patch_started_at", None)


def main() -> int:
    print(f"Bot version: {BOT_VERSION}")
    run_mode = env("RUN_MODE", "test").lower()
    dry_run = run_mode != "live"
    max_updates = bounded_int("MAX_UPDATES", 30, 1, HARD_MAX_UPDATES_PER_RUN)

    client_id = required_env("SNAP_CLIENT_ID")
    client_secret = required_env("SNAP_CLIENT_SECRET")
    refresh_token = required_env("SNAP_REFRESH_TOKEN")
    configured_ad_account_id = env("SNAP_AD_ACCOUNT_ID")
    requested_ad_squad_ids = env("REQUESTED_AD_SQUAD_IDS") or env(
        "REQUESTED_AD_SQUAD_ID"
    )
    fallback_ad_squad_ids = env("SNAP_AD_SQUAD_IDS") or env("SNAP_AD_SQUAD_ID")
    headline_source = env("HEADLINE_SOURCE", "manual").lower()
    if headline_source not in {"manual", "openai", "manual_then_openai"}:
        raise BotError("HEADLINE_SOURCE must be manual, openai, or manual_then_openai")
    product_context = required_env("PRODUCT_CONTEXT") if headline_source != "manual" else ""
    manual_pool = configured_headline_pool()
    if headline_source in {"manual", "manual_then_openai"} and not manual_pool:
        raise BotError("headline_pool.txt is empty; add your headline lines first")
    openai_api_key = (
        required_env("OPENAI_API_KEY")
        if headline_source in {"openai", "manual_then_openai"}
        else ""
    )
    openai_model = env("OPENAI_MODEL", "gpt-5.4-nano")

    state = load_state()
    prepare_headline_run(state)
    ad_account_id = select_ad_account(
        state,
        configured_ad_account_id,
    )
    ad_squad_ids = select_active_ad_squads(
        state, requested_ad_squad_ids, fallback_ad_squad_ids
    )

    print(
        f"Mode={'TEST (no Snapchat edits)' if dry_run else 'LIVE'}; "
        f"ad_account={ad_account_id}; "
        f"ad_squads={len(ad_squad_ids)}; max_updates_per_check={max_updates}; "
        f"attempt_limit=NONE; headline_source={headline_source}; "
        f"model={openai_model if headline_source != 'manual' else 'not_used'}"
    )
    print("Headline sharing: allowed across Creatives; repeat protection: each Creative's own history in this workflow run.")
    print("Headline selection: RANDOM; no repeats per Creative within this workflow run; resets on a new run.")
    if headline_source == "manual":
        print(f"Manual headline pool: {len(manual_pool)} distinct lines loaded; OpenAI is disabled.")

    access_token = refresh_snap_access_token(client_id, client_secret, refresh_token)
    snap = SnapClient(access_token)

    verified_scopes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for ad_squad_id in ad_squad_ids:
        verified_scopes[ad_squad_id] = verify_scope(
            snap, ad_account_id, ad_squad_id
        )

    all_account_ads = snap.get_all(
        f"/adaccounts/{ad_account_id}/ads?limit=1000&read_deleted_entities=true",
        "ads",
        "ad",
    )
    account_creatives = snap.get_all(
        f"/adaccounts/{ad_account_id}/creatives?limit=1000",
        "creatives",
        "creative",
    )
    creative_by_id = {
        str(creative.get("id")): creative
        for creative in account_creatives
        if creative.get("id")
    }
    for creative in account_creatives:
        remember_creative_headlines(state, str(creative["id"]), [creative.get("headline")])
    creative_status_counts = Counter(
        str(creative.get("review_status", "")).upper() or "UNKNOWN"
        for creative in account_creatives
    )
    print(
        f"Fetched {len(all_account_ads)} account Ad(s) and "
        f"{len(account_creatives)} Creative(s). Creative review statuses: "
        + ", ".join(
            f"{status}={count}"
            for status, count in sorted(creative_status_counts.items())
        )
    )

    selected_set = set(ad_squad_ids)
    globally_seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    overall_status_counts: Counter[str] = Counter()
    all_selected_complete = True

    for ad_squad_id in ad_squad_ids:
        print(f"--- Checking Ad Squad {ad_squad_id} ---")
        remaining_capacity = max_updates - len(candidates)
        squad_candidates, status_counts, _live_count = collect_candidates(
            ad_squad_id,
            selected_set,
            state,
            max(0, remaining_capacity),
            all_account_ads,
            creative_by_id,
            globally_seen,
        )
        campaign, ad_squad = verified_scopes[ad_squad_id]
        for candidate in squad_candidates:
            candidate["campaign_name"] = str(campaign.get("name") or "")
            candidate["ad_squad_name"] = str(ad_squad.get("name") or "")
        candidates.extend(squad_candidates)
        overall_status_counts.update(status_counts)
        if any(status != "APPROVED" for status in status_counts):
            all_selected_complete = False

    print(
        "Overall selected Ad statuses: "
        + (
            ", ".join(
                f"{status}={count}"
                for status, count in sorted(overall_status_counts.items())
            )
            or "NO_LIVE_ADS=0"
        )
    )
    if not dry_run:
        save_state(state)
    if not candidates:
        print("Nothing to update.")
        print(f"MONITOR_COMPLETE={'true' if all_selected_complete else 'false'}")
        return 0

    accepted: list[tuple[dict[str, Any], str]] = []
    accepted_ids: set[str] = set()
    remaining = candidates

    generation_rounds = 1 if headline_source == "manual" else MAX_HEADLINE_GENERATION_ROUNDS
    for generation_round in range(1, generation_rounds + 1):
        use_manual_pool = headline_source == "manual" or (
            headline_source == "manual_then_openai" and generation_round == 1
        )
        if use_manual_pool:
            suggestions = manual_suggestions(remaining)
        else:
            suggestions = generate_headlines(
                openai_api_key, openai_model, product_context, remaining
            )
        round_accepted = validate_suggestions(
            remaining,
            suggestions,
            reject_near_duplicates=not use_manual_pool,
        )
        accepted.extend(round_accepted)
        for candidate, _headline in round_accepted:
            accepted_ids.add(candidate["creative_id"])
        remaining = [
            candidate
            for candidate in remaining
            if candidate["creative_id"] not in accepted_ids
        ]
        if not remaining:
            break
        if generation_round < generation_rounds:
            print(
                f"RETRY OpenAI: requesting fresh alternatives for "
                f"{len(remaining)} creative(s)."
            )

    if not accepted:
        print("No headline passed the checks for its individual Creative; nothing was updated.")
        if headline_source == "manual":
            print("No unused suitable lines remain; used lines are not recycled during this workflow run. A new run resets headline usage.")
        print(f"MONITOR_COMPLETE={'true' if all_selected_complete else 'false'}")
        return 0
    for candidate in remaining:
        print(
            f"SKIP {candidate['creative_id']}: no unused suitable headline was found "
            f"for this Creative after {generation_rounds} suggestion round(s)."
        )

    print("Safety protection enabled: fresh linked-Ad checks and conditional Creative PATCH.")
    updates_succeeded = 0
    for candidate, headline in accepted:
        if dry_run:
            expected_headline = recheck_before_edit(
                snap, ad_account_id, selected_set, state, candidate
            )
            if expected_headline is None:
                continue
            print(
                f"WOULD UPDATE squad={candidate.get('ad_squad_id')} "
                f"creative={candidate['creative_id']} "
                f"from={candidate.get('current_headline')!r} to={headline!r}"
            )
            continue
        creative_id = candidate["creative_id"]
        reservation_id = reserve_headline(state, candidate, headline)
        if reservation_id is None:
            continue
        try:
            checkpoint_before_edit(state)
            # Persisting can take time. Re-read all status/link guards AFTER the
            # checkpoint, authorizing only this invocation's own reservation.
            expected_headline = recheck_before_edit(
                snap, ad_account_id, selected_set, state, candidate,
                reservation_id=reservation_id,
            )
        except Exception:
            # No PATCH has been attempted yet. The workflow can persist this
            # known skip, keeping the wording consumed without blocking review.
            release_unsubmitted_reservation(state, creative_id, reservation_id)
            save_state(state)
            raise
        if expected_headline is None:
            release_unsubmitted_reservation(state, creative_id, reservation_id)
            save_state(state)
            continue
        result = snap.patch_headline(
            ad_account_id, creative_id, headline, expected_headline=expected_headline
        )
        record_update(state, candidate, headline, result)
        save_state(state)
        updates_succeeded += 1
        print(
            f"UPDATED squad={candidate.get('ad_squad_id')} creative={creative_id}: "
            f"{headline!r}; submitted for re-review."
        )

    if dry_run:
        print("Test mode finished. Snapchat was not changed.")
    else:
        print(f"Live run finished: {updates_succeeded} creative(s) updated.")
    print(f"MONITOR_COMPLETE={'true' if all_selected_complete else 'false'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpenAIServiceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(OPENAI_STOP_EXIT_CODE)
    except StatePersistenceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(STATE_STOP_EXIT_CODE)
    except BotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        if os.getenv("PUBLIC_SAFE_LOGS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            print("UNEXPECTED ERROR: private diagnostic details hidden.", file=sys.stderr)
            raise SystemExit(1)
        raise
