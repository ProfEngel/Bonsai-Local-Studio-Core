from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import datetime, timedelta
import gc
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Response
import mlx.core as mx
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from pypdf import PdfReader

from backend.pipeline import (
    BACKEND_TO_FAMILY,
    BACKEND_TO_KIND,
    BACKENDS,
    LOCAL_BACKENDS,
    MODEL_FAMILIES,
    REMOTE_BACKENDS,
    Backend,
    BackendKind,
    DEFAULT_GUIDANCE,
    DEFAULT_HEIGHT,
    DEFAULT_SEED,
    DEFAULT_STEPS,
    DEFAULT_WIDTH,
    FluxPipeline,
    ModelFamily,
    PipelineConfig,
    RemoteGpuPipeline,
    make_pipeline,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)


def _parse_truthy(value: str | None) -> bool:
    """Parse common environment variable truthy values."""
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


# Force-disable is read once at module load: server restart is the override path.
# Per-session UI overrides ride on the ?force_disable=1 query param instead.
_FORCE_DISABLE_GPU_AT_LOAD: bool = _parse_truthy(os.getenv("MFLUX_STUDIO_FORCE_DISABLE_GPU"))
# Backend health is commonly requested by the frontend, so keep the probe result
# briefly cached instead of repeatedly hitting the remote GPU health endpoint.
_BACKENDS_PROBE_TTL_SECONDS: float = float(
    os.getenv("MFLUX_STUDIO_BACKENDS_PROBE_TTL_SECONDS", "30")
)
# Cache key is (effective_force_disable, pipeline_kind). The pipeline_kind is
# fixed per process today, but keying on it keeps the cache correct if a
# future change ever flips it mid-lifetime.
_backends_cache: dict[tuple[bool, BackendKind], tuple[float, dict]] = {}

DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT = (
    "You improve prompts for a Flux2 Klein image model. Return exactly one English "
    "image prompt, one sentence, at most 60 words, with no repetition or explanation. "
    "Preserve the user's subject and intent; add useful visual details such as "
    "composition, lighting, materials and style."
)

DEFAULT_CHAT_SYSTEM_PROMPT = (
    "Du bist Bonsai, ein lokaler Assistent. Antworte immer auf Deutsch, kurz und direkt: "
    "normalerweise ein bis drei Sätze oder höchstens vier kurze Stichpunkte. Beginne mit "
    "der Antwort; wiederhole die Frage nicht und gib keine Denkspur, Selbstbeschreibung, "
    "Prozess- oder Tool-Erklärung aus. Wenn WEB SEARCH RESULTS vorliegen, verwende für "
    "faktische Aussagen ausschließlich diese Quellen und zitiere sie als [1], [2]. Reichen "
    "die Quellen nicht, sage das knapp statt etwas zu erfinden. Interpretiere relative Daten "
    "mit dem gelieferten Zeitstempel Europe/Berlin. Bildanhänge darfst du nur dann beschreiben, "
    "wenn eine lokale Vision-Auswertung als Kontext bereitgestellt wurde."
)

# The 2-bit Bonsai server is configured for 128K tokens. Keep chat history
# below roughly 90K input tokens so system rules, attachments, web results and
# a 4K response still fit safely. Character counting is deliberately
# conservative and avoids loading a second tokenizer into the image backend.
MAX_CHAT_CONTEXT_CHARS = 360_000

BERLIN_TIMEZONE = ZoneInfo("Europe/Berlin")
CHAT_AGENTS_DIR = Path(
    os.getenv(
        "BONSAI_CHAT_AGENTS_DIR",
        str(Path.home() / ".bonsai-studio" / "agents"),
    )
).expanduser()
GOOSE_PROVIDER = os.getenv("BONSAI_GOOSE_PROVIDER", "custom_bonsai27b_2bit")
GOOSE_MODEL = os.getenv("BONSAI_GOOSE_MODEL", "Ternary-Bonsai-27B-mlx-2bit")
MAIL_ACCOUNT = os.getenv("BONSAI_MAIL_ACCOUNT", "Exchange")
MAIL_INBOX = os.getenv("BONSAI_MAIL_INBOX", "Posteingang")
_goose_agent_locks: dict[str, asyncio.Lock] = {}
WEB_SEARCH_CONFIG_PATH = Path(
    os.getenv(
        "BONSAI_WEB_SEARCH_CONFIG",
        str(Path.home() / ".config" / "bonsai-studio" / "web-search.json"),
    )
).expanduser()


def _validate_local_llm_endpoint(value: str) -> str:
    """Allow only an explicitly configured loopback OpenAI-compatible endpoint."""
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The local LLM may only be called through a loopback HTTP endpoint.")
    if parsed.username or parsed.password or not parsed.port:
        raise ValueError("Use a local HTTP endpoint with an explicit port, for example http://127.0.0.1:8081/v1.")
    if not parsed.path.rstrip("/").endswith("/v1"):
        raise ValueError("The endpoint must end in /v1, for example http://127.0.0.1:8081/v1.")
    return normalized


def _clear_backends_cache() -> None:
    """Clear cached backend metadata, mainly for tests."""
    _backends_cache.clear()


def _probe_gpu(host: str, token: str) -> tuple[bool, str | None]:
    """Check whether the configured remote GPU worker is reachable."""
    try:
        resp = httpx.get(
            f"{host.rstrip('/')}/healthz",
            headers={"Authorization": f"Bearer {token}"},
            timeout=2.0,
        )
    except httpx.HTTPError:
        return False, "healthz_unreachable"
    if resp.status_code == 200:
        return True, None
    return False, f"healthz_failed:{resp.status_code}"


def _resolve_backends(
    force_disable: bool, pipeline_kind: BackendKind, current_backend: Backend
) -> dict:
    """Report the relay's single resident kind + which model families it serves.

    The relay is configured for one kind per process — switching MLX↔gemlite
    requires a restart. For the gemlite kind we still probe the remote GPU so
    the frontend can surface a clear unhealthy state instead of empty errors.
    """
    if pipeline_kind == "gemlite":
        gpu_host = os.getenv("MFLUX_STUDIO_GPU_HOST")
        gpu_token = os.getenv("MFLUX_STUDIO_GPU_TOKEN")
        if force_disable:
            healthy, reason = False, "force_disabled"
        elif not gpu_host:
            healthy, reason = False, "no_gpu_host"
        elif not gpu_token:
            healthy, reason = False, "no_gpu_token"
        else:
            healthy, reason = _probe_gpu(gpu_host, gpu_token)
    else:
        # Local MLX backends do not depend on a remote health endpoint.
        healthy, reason = True, None

    kind_backends = [b for b in BACKENDS if BACKEND_TO_KIND[b] == pipeline_kind]
    supported_families: list[ModelFamily] = [
        f for f in MODEL_FAMILIES if any(BACKEND_TO_FAMILY[b] == f for b in kind_backends)
    ]
    default_family = BACKEND_TO_FAMILY[current_backend]

    return {
        "kind": pipeline_kind,
        "supported_families": supported_families,
        "default_family": default_family,
        "healthy": healthy,
        "reason": reason,
    }


def _get_backends_payload(
    force_disable_query: bool, pipeline_kind: BackendKind, current_backend: Backend
) -> dict:
    """Return backend metadata, using a short TTL cache for health checks."""
    effective = _FORCE_DISABLE_GPU_AT_LOAD or force_disable_query
    cache_key = (effective, pipeline_kind)
    cached = _backends_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _BACKENDS_PROBE_TTL_SECONDS:
        return cached[1]
    payload = _resolve_backends(
        force_disable=effective,
        pipeline_kind=pipeline_kind,
        current_backend=current_backend,
    )
    _backends_cache[cache_key] = (now, payload)
    return payload


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    seed: int = DEFAULT_SEED
    # The Flux flow-matching scheduler interpolates between at least two points.
    steps: int = Field(default=DEFAULT_STEPS, ge=2)
    guidance: float = Field(default=DEFAULT_GUIDANCE, ge=0.0)
    backend: Backend | SkipJsonSchema[None] = Field(default=None)
    height: int = Field(default=DEFAULT_HEIGHT, ge=16)
    width: int = Field(default=DEFAULT_WIDTH, ge=16)
    model_path: str | None = Field(default=None)
    tiled_vae: bool | None = Field(default=None)
    max_sequence_length: int | None = Field(default=None, ge=1)
    lora_adapters: list["LoraAdapter"] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def _validate_lora_adapters(self) -> "GenerateRequest":
        names = [adapter.name for adapter in self.lora_adapters]
        if len(set(names)) != len(names):
            raise ValueError("Each LoRA adapter can be selected only once.")
        return self


class LoraAdapter(BaseModel):
    """One local Flux2/Klein LoRA chosen from the Studio's loras/ directory."""

    name: str = Field(min_length=1, max_length=255)
    scale: float = Field(default=1.0, ge=0.0, le=2.0)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.name != value or candidate.suffix.lower() != ".safetensors":
            raise ValueError("LoRA name must be a .safetensors filename without a path.")
        return value


def _lora_directory() -> Path:
    """Return the explicitly configured local LoRA folder, never a caller path."""
    raw = os.getenv("MFLUX_STUDIO_LORA_DIR")
    if raw is None:
        raise ValueError("MFLUX_STUDIO_LORA_DIR is not configured.")
    directory = Path(raw).expanduser()
    if not directory.is_absolute():
        raise ValueError("MFLUX_STUDIO_LORA_DIR must be an absolute path.")
    return directory.resolve()


def _resolve_lora_adapters(adapters: list[LoraAdapter]) -> tuple[tuple[str, ...], tuple[float, ...]]:
    if not adapters:
        return (), ()
    directory = _lora_directory()
    if not directory.is_dir():
        raise ValueError(f"LoRA folder does not exist: {directory}")
    paths: list[str] = []
    scales: list[float] = []
    for adapter in adapters:
        candidate = (directory / adapter.name).resolve()
        if candidate.parent != directory or not candidate.is_file():
            raise ValueError(f"LoRA adapter not found: {adapter.name}")
        paths.append(str(candidate))
        scales.append(adapter.scale)
    return tuple(paths), tuple(scales)


class PromptOptimizationRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8_000)
    llm_url: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=512)
    system_prompt: str = Field(
        default=DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT,
        min_length=1,
        max_length=4_000,
    )

    @field_validator("llm_url")
    @classmethod
    def _validate_local_llm_url(cls, value: str) -> str:
        return _validate_local_llm_endpoint(value)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=24_000)


def _bounded_chat_history(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """Keep the newest usable history inside the configured long-context budget."""
    selected: list[dict[str, str]] = []
    used = 0
    for message in reversed(messages):
        content = message.content
        remaining = MAX_CHAT_CONTEXT_CHARS - used
        if remaining <= 0:
            break
        if len(content) > remaining:
            marker = "[Früherer Teil dieser Nachricht wurde für das Kontextfenster ausgelassen.]\n"
            content = marker + content[-max(0, remaining - len(marker)):]
        selected.append({"role": message.role, "content": content})
        used += len(content)
        if used >= MAX_CHAT_CONTEXT_CHARS:
            break
    selected.reverse()
    return selected


class ChatAttachment(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: Literal["text", "pdf", "image"]
    excerpt: str = Field(min_length=1, max_length=24_000)
    data_url: str | None = Field(default=None, max_length=12_000_000)

    @field_validator("data_url")
    @classmethod
    def _validate_image_data_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not re.match(r"^data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+$", value):
            raise ValueError("Image attachments must be local base64 image data.")
        return value


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=160)
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=5)
    web_search: bool = False
    web_search_provider: Literal["auto", "tavily", "brave", "fallback"] = "auto"
    agent_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    llm_url: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=512)
    vision_llm_url: str | None = Field(default=None, max_length=512)
    vision_model: str | None = Field(default=None, max_length=512)
    system_prompt: str = Field(
        default=DEFAULT_CHAT_SYSTEM_PROMPT,
        min_length=1,
        max_length=4_000,
    )

    @field_validator("llm_url")
    @classmethod
    def _validate_local_llm_url(cls, value: str) -> str:
        return _validate_local_llm_endpoint(value)

    @field_validator("vision_llm_url")
    @classmethod
    def _validate_local_vision_url(cls, value: str | None) -> str | None:
        return _validate_local_llm_endpoint(value) if value else value


class PdfExtractRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    data_base64: str = Field(min_length=1, max_length=12_000_000)


class ChatAgentProfile(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=280)
    webSearchDefault: bool = False
    systemPrompt: str = Field(min_length=1, max_length=8_000)


class WebSearchConfigUpdate(BaseModel):
    tavily_api_key: str | None = Field(default=None, max_length=1_024)
    brave_api_key: str | None = Field(default=None, max_length=1_024)


def _chat_agent_profiles() -> list[dict[str, str | bool]]:
    """Load explicit local chat profiles; agent source files are never executed."""
    profiles: list[dict[str, str | bool]] = []
    if not CHAT_AGENTS_DIR.is_dir():
        return profiles
    for profile_path in sorted(CHAT_AGENTS_DIR.rglob("chat/profile.json")):
        try:
            profile = ChatAgentProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Skipping unreadable chat profile %s: %s", profile_path, exc)
            continue
        profiles.append(profile.model_dump())
    return profiles


def _find_chat_agent_profile(agent_id: str) -> tuple[ChatAgentProfile, Path] | None:
    """Resolve an explicitly selected local profile; never execute profile files."""
    if not CHAT_AGENTS_DIR.is_dir():
        return None
    for profile_path in sorted(CHAT_AGENTS_DIR.rglob("chat/profile.json")):
        try:
            profile = ChatAgentProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if profile.id == agent_id:
            return profile, profile_path
    return None


def _agent_skill_context(profile_path: Path, *, limit: int = 12_000) -> str:
    """Load the selected agent's small, declarative rule files for Goose context."""
    # A profile may live in a command subfolder (for example Mail-Agent /
    # commands / mailrundruf / chat). Use the nearest actual agent root so its
    # shared SKILL.md, rules and workflows are not silently skipped.
    agent_dir = next(
        (parent for parent in profile_path.parents if (parent / "agent.yaml").is_file()),
        profile_path.parent.parent,
    )
    candidates = [agent_dir / "SKILL.md"]
    candidates.extend(sorted((agent_dir / "rules").glob("*.md")) if (agent_dir / "rules").is_dir() else [])
    candidates.extend(sorted((agent_dir / "workflows").glob("*.md")) if (agent_dir / "workflows").is_dir() else [])
    parts: list[str] = []
    used = 0
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        excerpt = text[: max(0, limit - used)]
        parts.append(f"## {candidate.name}\n{excerpt}")
        used += len(excerpt)
        if used >= limit:
            break
    return "\n\n".join(parts)


def _is_unread_mail_question(question: str) -> bool:
    lowered = question.casefold()
    mail_terms = ("mail", "e-mail", "email", "posteingang", "inbox", "nachricht")
    unread_terms = ("neu", "ungelesen", "unread", "eingang", "aktuell", "heute")
    return any(term in lowered for term in mail_terms) and any(term in lowered for term in unread_terms)


def _read_unread_mail_summary(max_items: int = 10) -> str:
    """Read subject metadata only. This AppleScript never changes Mail state."""
    safe_max = max(1, min(max_items, 20))
    # Asking Mail for *all* matching message objects is surprisingly slow on a
    # large Exchange mailbox. Count the matches, then inspect only the most
    # recent fixed window. This keeps the connector both read-only and usable.
    recent_window = max(30, safe_max * 4)
    script = f'''
tell application "Mail"
  set mailboxRef to mailbox "{MAIL_INBOX}" of account "{MAIL_ACCOUNT}"
  set itemCount to count of (every message of mailboxRef whose read status is false)
  set outputText to ""
  set shownCount to 0
  set recentMessages to messages 1 thru {recent_window} of mailboxRef
  repeat with currentMessage in recentMessages
    if read status of currentMessage is false then
      set outputText to outputText & (date received of currentMessage as string) & tab & (sender of currentMessage as string) & tab & (subject of currentMessage as string) & linefeed
      set shownCount to shownCount + 1
      if shownCount = {safe_max} then exit repeat
    end if
  end repeat
  return (itemCount as text) & linefeed & outputText
end tell
'''
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Apple Mail konnte nicht lesend abgefragt werden: {exc}"
    rows = [row for row in result.stdout.splitlines() if row.strip()]
    if not rows:
        return "Apple Mail hat keine verwertbare Antwort geliefert."
    try:
        total = int(rows[0])
    except ValueError:
        return "Apple Mail lieferte kein lesbares Ergebnis."
    if total == 0:
        return "Posteingang (nur lesend geprüft): keine ungelesenen Nachrichten."
    messages = []
    for row in rows[1:]:
        date_value, sender, subject = (row.split("\t", 2) + ["", "", ""])[:3]
        messages.append(f"- {date_value}: {sender} — {subject}")
    omitted = f"\nWeitere ungelesene Nachrichten: {total - len(messages)}." if total > len(messages) else ""
    return f"Posteingang (nur lesend geprüft): {total} ungelesene Nachricht(en).\n" + "\n".join(messages) + omitted


async def _run_goose_harness(
    *,
    profile: ChatAgentProfile,
    profile_path: Path,
    messages: list[dict[str, str]],
    tool_context: str,
    response_instruction: str,
) -> str:
    """Run one constrained, ephemeral Goose job without shell/computer extensions."""
    goose = shutil.which("goose") or "/opt/homebrew/bin/goose"
    if not Path(goose).is_file():
        raise HTTPException(status_code=503, detail="Goose ist lokal nicht verfügbar. Bitte Goose installieren oder den allgemeinen Chat verwenden.")
    history = "\n\n".join(
        f"{'Nutzer' if item['role'] == 'user' else 'Assistent'}: {item['content']}"
        for item in messages[-12:]
    )
    system = "\n\n".join(part for part in (
        "Du arbeitest als eingeschränkter Goose-Harness für einen lokalen BrainVault-Agenten.",
        "Antworte immer auf Deutsch. Gib ausschließlich die fertige Antwort aus: keine Denkspur, keine Selbstanalyse, keine Tool-Planung und keinen englischen Meta-Kommentar.",
        "Du hast absichtlich keine Shell-, Computer- oder Versandwerkzeuge. Behaupte niemals, eine Mail gesendet, Dateien geändert oder andere externe Aktionen ausgeführt zu haben.",
        "Bei Mailentwürfen gilt: nur Entwurf; niemals versenden. Bei fehlenden Daten frage konkret nach.",
        f"# Globale Antwortvorgabe (nur Stil; Sicherheitsregeln bleiben unverändert)\n{response_instruction}",
        f"# Agentenprofil: {profile.name}\n{profile.systemPrompt}",
        _agent_skill_context(profile_path),
        f"# Bereits vom lokalen, eingeschränkten Harness erhobene Fakten\n{tool_context}" if tool_context else "",
    ) if part)
    lock = _goose_agent_locks.setdefault(profile.id, asyncio.Lock())
    async with lock:
        process = await asyncio.create_subprocess_exec(
            goose,
            "run",
            "--no-profile",
            "--no-session",
            "--quiet",
            "--max-turns",
            "4",
            "--provider",
            GOOSE_PROVIDER,
            "--model",
            GOOSE_MODEL,
            "--system",
            system,
            "--text",
            history,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise HTTPException(status_code=504, detail="Der Goose-Agentenlauf hat zu lange gedauert.") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unbekannter Fehler"]
        raise HTTPException(status_code=502, detail=f"Goose-Harness fehlgeschlagen: {detail[0][:400]}")
    answer = stdout.decode("utf-8", "replace").strip()
    answer = re.sub(r"<think>.*?</think>\s*", "", answer, flags=re.IGNORECASE | re.DOTALL).strip()
    if not answer:
        raise HTTPException(status_code=502, detail="Goose-Harness lieferte keine nutzbare Antwort.")
    return answer


async def _vision_summary_for_harness(
    *,
    endpoint: str,
    model: str,
    attachments: list[ChatAttachment],
) -> str:
    """Turn local image pixels into bounded factual context before a Goose run."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Describe only visible image details in German. Do not diagnose, identify people, or expose reasoning."},
            {"role": "user", "content": [
                {"type": "text", "text": "Erstelle eine kurze, sachliche Bildbeschreibung für den nachfolgenden lokalen Agenten."},
                *[{"type": "image_url", "image_url": {"url": item.data_url}} for item in attachments if item.data_url],
            ]},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            response = await client.post(f"{endpoint}/chat/completions", json=body)
        response.raise_for_status()
        summary = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Lokale Vision-Auswertung fehlgeschlagen: {exc}") from exc
    if not isinstance(summary, str) or not summary.strip():
        raise HTTPException(status_code=502, detail="Lokale Vision-Auswertung lieferte keine Bildbeschreibung.")
    return re.sub(r"<think>.*?</think>\s*", "", summary, flags=re.IGNORECASE | re.DOTALL).strip()


def _plain_html(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _decode_duckduckgo_url(value: str) -> str:
    href = html.unescape(value).strip()
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        return unquote(parse_qs(parsed.query).get("uddg", [href])[0])
    return href


def _web_search_query(query: str) -> tuple[str, dict[str, str], str]:
    """Make relative-time questions reproducible for a search engine and the LLM."""
    now = datetime.now(BERLIN_TIMEZONE)
    lowered = query.casefold()
    relative_terms = ("heute", "gestern", "aktuell", "neuest", "latest", "letzte", "today", "yesterday")
    params: dict[str, str] = {}
    search_query = query.strip()
    mentions_today = "heute" in lowered or "today" in lowered
    mentions_yesterday = "gestern" in lowered or "yesterday" in lowered
    if mentions_today and mentions_yesterday:
        search_query = (
            f"{search_query} am {(now - timedelta(days=1)).strftime('%d.%m.%Y')} "
            f"und {now.strftime('%d.%m.%Y')}"
        )
        params["df"] = "d"
    elif mentions_yesterday:
        search_query = f"{search_query} {(now - timedelta(days=1)).date().isoformat()}"
        params["df"] = "d"
    elif mentions_today or any(term in lowered for term in relative_terms):
        search_query = f"{search_query} {now.date().isoformat()}"
        params["df"] = "d"
    return search_query, params, now.strftime("%Y-%m-%d %H:%M %Z")


def _search_topic(query: str) -> Literal["general", "news", "finance"]:
    """Map a natural-language question to the documented Tavily search topics."""
    lowered = query.casefold()
    if any(term in lowered for term in (
        "aktie", "aktien", "etf", "portfolio", "börse", "boerse", "kurs", "dividende",
        "crypto", "krypto", "bitcoin", "ethereum", "finanz", "wirtschaft", "earnings",
    )):
        return "finance"
    if any(term in lowered for term in (
        "news", "nachricht", "politik", "bundestag", "regierung", "wahl", "sport",
        "fussball", "fußball", "spiel", "wm", "wetter", "heute", "gestern", "aktuell",
    )):
        return "news"
    return "general"


def _configured_search_key(name: str) -> str:
    """Read a provider key locally; it is never returned to the browser."""
    return _read_local_web_search_config().get(name, "") or os.getenv(name, "").strip()


def _read_local_web_search_config() -> dict[str, str]:
    """Read only the two supported keys from the private, local config file."""
    try:
        raw = json.loads(WEB_SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        name: value.strip()
        for name, value in raw.items()
        if name in {"TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY"}
        and isinstance(value, str)
        and value.strip()
    }


def _write_local_web_search_config(updates: dict[str, str]) -> None:
    """Persist keys with owner-only permissions, without ever logging their values."""
    current = _read_local_web_search_config()
    for name, value in updates.items():
        cleaned = value.strip()
        if not cleaned:
            continue
        if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
            raise HTTPException(status_code=422, detail="Suchschlüssel dürfen keine Zeilenumbrüche enthalten.")
        current[name] = cleaned
    if not current:
        return
    try:
        WEB_SEARCH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(WEB_SEARCH_CONFIG_PATH.parent, 0o700)
        temporary_path = WEB_SEARCH_CONFIG_PATH.with_name(f".{WEB_SEARCH_CONFIG_PATH.name}.{os.getpid()}.tmp")
        temporary_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, WEB_SEARCH_CONFIG_PATH)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Lokale Suchkonfiguration konnte nicht gespeichert werden.") from exc


def _is_fifa_world_cup_query(query: str) -> bool:
    lowered = query.casefold()
    return "fifa" in lowered and any(term in lowered for term in (" wm", "weltmeisterschaft", "world cup"))


async def _fifa_world_cup_scoreboard(query: str) -> list[dict[str, str]]:
    """Add date-specific FIFA fixtures/results when a query explicitly asks for them."""
    if not _is_fifa_world_cup_query(query):
        return []
    now = datetime.now(BERLIN_TIMEZONE)
    dates = [now.date(), (now - timedelta(days=1)).date()]
    results: list[dict[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            for date in dates:
                response = await client.get(
                    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard",
                    params={"dates": date.strftime("%Y%m%d")},
                )
                response.raise_for_status()
                for event in response.json().get("events", []):
                    competition = (event.get("competitions") or [{}])[0]
                    competitors = competition.get("competitors") or []
                    home = next((item for item in competitors if item.get("homeAway") == "home"), None)
                    away = next((item for item in competitors if item.get("homeAway") == "away"), None)
                    if not home or not away:
                        continue
                    home_name = home.get("team", {}).get("displayName")
                    away_name = away.get("team", {}).get("displayName")
                    status = event.get("status", {}).get("type", {}).get("description", "Status unbekannt")
                    if not home_name or not away_name:
                        continue
                    starts = datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone(BERLIN_TIMEZONE)
                    if status.casefold() == "full time":
                        fixture = f"{home_name} {home.get('score', '?')}:{away.get('score', '?')} {away_name}"
                    else:
                        fixture = f"{home_name} – {away_name}"
                    results.append(
                        {
                            "title": f"FIFA World Cup · {starts.strftime('%d.%m.%Y')}: {fixture}",
                            "url": f"https://www.espn.com/soccer/scoreboard/_/league/fifa.world/date/{date.strftime('%Y%m%d')}",
                            "snippet": f"ESPN-Spielplan, {starts.strftime('%H:%M %Z')}: {status}.",
                            "provider": "ESPN Scoreboard",
                        }
                    )
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        log.info("FIFA scoreboard supplement unavailable: %s", exc)
    return results[:2]


async def _bing_news_fallback(query: str) -> list[dict[str, str]]:
    """Use Bing News RSS only when DuckDuckGo's HTML endpoint rate-limits us."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": "Bonsai-Image-Studio/1.0"},
        ) as client:
            response = await client.get(
                "https://www.bing.com/news/search",
                params={"q": query, "format": "rss", "setlang": "de-DE", "cc": "DE"},
            )
            response.raise_for_status()
        root = ET.fromstring(response.content)
    except (httpx.HTTPError, ET.ParseError) as exc:
        log.info("Bing News fallback unavailable: %s", exc)
        return []

    results: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = _plain_html(item.findtext("title") or "").lstrip(":–- ").strip()
        url = (item.findtext("link") or "").strip()
        parsed_url = urlparse(url)
        if parsed_url.netloc.endswith("bing.com") and parsed_url.path.startswith("/news/apiclick"):
            url = unquote(parse_qs(parsed_url.query).get("url", [url])[0])
        snippet = _plain_html(item.findtext("description") or "")
        if not title or not url.startswith(("https://", "http://")):
            continue
        results.append({"title": title[:240], "url": url[:1_500], "snippet": snippet[:500], "provider": "Bing News"})
        if len(results) == 3:
            break
    return results


async def _tavily_search(query: str, api_key: str) -> list[dict[str, str]]:
    """Use Tavily's supported API rather than scraping a public search page."""
    search_query, _, _ = _web_search_query(query)
    body = {
        "query": search_query,
        "topic": _search_topic(query),
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Tavily search unavailable: %s", exc)
        return []

    results: list[dict[str, str]] = []
    for item in payload.get("results", []):
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("content") or "").strip()
        published = str(item.get("published_date") or "").strip()
        if not title or not url.startswith(("https://", "http://")):
            continue
        if published:
            snippet = f"{published} · {snippet}"
        results.append({"title": title[:240], "url": url[:1_500], "snippet": snippet[:800], "provider": "Tavily"})
        if len(results) == 5:
            break
    return results


async def _brave_search(query: str, api_key: str) -> list[dict[str, str]]:
    """Use Brave's supported news/web API with German locale and freshness hints."""
    search_query, _, _ = _web_search_query(query)
    topic = _search_topic(query)
    is_news = topic in {"news", "finance"}
    params = {"q": search_query, "count": "5", "country": "DE", "search_lang": "de", "spellcheck": "1"}
    if any(term in query.casefold() for term in ("heute", "gestern", "aktuell", "neueste", "neues", "today", "yesterday")):
        params["freshness"] = "pd"
    endpoint = "https://api.search.brave.com/res/v1/news/search" if is_news else "https://api.search.brave.com/res/v1/web/search"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.get(
                endpoint,
                headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Brave search unavailable: %s", exc)
        return []

    raw_results = payload.get("results", []) if is_news else payload.get("web", {}).get("results", [])
    results: list[dict[str, str]] = []
    for item in raw_results:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("description") or item.get("page_age") or "").strip()
        if not title or not url.startswith(("https://", "http://")):
            continue
        results.append({"title": title[:240], "url": url[:1_500], "snippet": snippet[:800], "provider": "Brave Search"})
        if len(results) == 5:
            break
    return results


async def _duckduckgo_search(query: str) -> tuple[list[dict[str, str]], str, str, str]:
    """Return DuckDuckGo results, with an explicit news-RSS fallback on a DDG challenge."""
    search_query, params, timestamp = _web_search_query(query)
    params["q"] = search_query
    scoreboard_sources = await _fifa_world_cup_scoreboard(query)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": "Bonsai-Image-Studio/1.0"},
        ) as client:
            response = await client.get("https://html.duckduckgo.com/html/", params=params)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        log.info("DuckDuckGo search unavailable: %s", exc)
        news_sources = await _bing_news_fallback(search_query)
        provider = "ESPN Scoreboard + Bing News fallback (DuckDuckGo unavailable)" if scoreboard_sources else "Bing News fallback (DuckDuckGo unavailable)"
        return (scoreboard_sources + news_sources)[:3], search_query, timestamp, provider

    # DuckDuckGo answers bot/rate-limit challenges with HTTP 202 and an HTML
    # page without result links. Treat that as an unavailable search, never as
    # an empty factual result set.
    if response.status_code != 200:
        log.info("DuckDuckGo search returned HTTP %s", response.status_code)
        news_sources = await _bing_news_fallback(search_query)
        provider = "ESPN Scoreboard + Bing News fallback (DuckDuckGo unavailable)" if scoreboard_sources else "Bing News fallback (DuckDuckGo unavailable)"
        return (scoreboard_sources + news_sources)[:3], search_query, timestamp, provider

    results: list[dict[str, str]] = []
    # DuckDuckGo's nested result divs vary frequently. Match result links in the
    # complete document and look only forward for their associated snippet.
    # This deliberately avoids a brittle "two closing divs" HTML regex.
    title_pattern = re.compile(
        r'<a[^>]+class="[^\"]*result__a[^\"]*"[^>]+href="([^\"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    for title_match in title_pattern.finditer(response.text):
        nearby_html = response.text[title_match.end(): title_match.end() + 4_000]
        snippet_match = re.search(
            r'<(?:a|div)[^>]+class="[^\"]*result__snippet[^\"]*"[^>]*>(.*?)</(?:a|div)>',
            nearby_html,
            re.DOTALL,
        )
        title = _plain_html(title_match.group(2))
        url = _decode_duckduckgo_url(title_match.group(1))
        if not title or not url:
            continue
        results.append({"title": title[:240], "url": url[:1_500], "snippet": _plain_html(snippet_match.group(1))[:500] if snippet_match else "", "provider": "DuckDuckGo"})
        if len(results) == 8:
            break
    results.sort(
        key=lambda item: (
            bool(re.search(r"\b\d{1,2}\s*:\s*\d{1,2}\b", f"{item['title']} {item['snippet']}")),
            urlparse(item["url"]).hostname in {"www.zdf.de", "www.zdfheute.de", "www.sportschau.de", "www.tagesschau.de", "www.espn.com", "www.espn.co.uk"},
        ),
        reverse=True,
    )
    if results:
        provider = "ESPN Scoreboard + DuckDuckGo" if scoreboard_sources else "DuckDuckGo"
        return (scoreboard_sources + results)[:3], search_query, timestamp, provider
    news_sources = await _bing_news_fallback(search_query)
    provider = "ESPN Scoreboard + Bing News fallback (DuckDuckGo returned no results)" if scoreboard_sources else "Bing News fallback (DuckDuckGo returned no results)"
    return (scoreboard_sources + news_sources)[:3], search_query, timestamp, provider


async def _web_search(query: str, provider_choice: str) -> tuple[list[dict[str, str]], str, str, str]:
    """Choose a supported provider, keeping the legacy public search only as opt-in fallback."""
    search_query, _, timestamp = _web_search_query(query)
    scoreboard_sources = await _fifa_world_cup_scoreboard(query)
    tavily_key = _configured_search_key("TAVILY_API_KEY")
    brave_key = _configured_search_key("BRAVE_SEARCH_API_KEY")

    choices = {
        "auto": (["tavily", "brave", "fallback"], "Automatisch"),
        "tavily": (["tavily"], "Tavily"),
        "brave": (["brave"], "Brave Search"),
        "fallback": (["fallback"], "Öffentliche Fallback-Suche"),
    }
    selected, label = choices.get(provider_choice, choices["auto"])
    for provider in selected:
        if provider == "tavily":
            if not tavily_key:
                if provider_choice == "tavily":
                    raise HTTPException(status_code=422, detail="Tavily ist ausgewählt, aber TAVILY_API_KEY ist im Studio-Start nicht gesetzt.")
                continue
            results = await _tavily_search(query, tavily_key)
            if results:
                return (scoreboard_sources + results)[:5], search_query, timestamp, "ESPN Scoreboard + Tavily" if scoreboard_sources else "Tavily"
            if provider_choice == "tavily":
                return scoreboard_sources, search_query, timestamp, "Tavily lieferte keine verwertbaren Treffer"
        elif provider == "brave":
            if not brave_key:
                if provider_choice == "brave":
                    raise HTTPException(status_code=422, detail="Brave Search ist ausgewählt, aber BRAVE_SEARCH_API_KEY ist im Studio-Start nicht gesetzt.")
                continue
            results = await _brave_search(query, brave_key)
            if results:
                return (scoreboard_sources + results)[:5], search_query, timestamp, "ESPN Scoreboard + Brave Search" if scoreboard_sources else "Brave Search"
            if provider_choice == "brave":
                return scoreboard_sources, search_query, timestamp, "Brave Search lieferte keine verwertbaren Treffer"
        else:
            results, _, _, used_provider = await _duckduckgo_search(query)
            return results, search_query, timestamp, used_provider
    return scoreboard_sources, search_query, timestamp, f"{label}: kein konfigurierter Suchanbieter"


def _ground_unambiguous_score(answer: str, question: str, sources: list[dict[str, str]]) -> str:
    """Keep a tiny text model from changing one explicit, unanimous result score."""
    if not any(term in question.casefold() for term in ("gespielt", "ergebnis", "score", "result")):
        return answer
    score_pattern = re.compile(r"\b\d{1,2}\s*:\s*\d{1,2}\b")
    source_scores = {
        re.sub(r"\s+", "", match.group(0))
        for source in sources
        for match in score_pattern.finditer(f"{source['title']} {source['snippet']}")
    }
    if len(source_scores) != 1:
        return answer
    grounded_score = next(iter(source_scores))
    if score_pattern.search(answer):
        return score_pattern.sub(grounded_score, answer)
    return f"Nach den Suchquellen lautet das Ergebnis **{grounded_score}**.\n\n{answer}"


def _attachment_context(attachments: list[ChatAttachment], *, vision_enabled: bool) -> str:
    if not attachments:
        return ""
    parts = ["Attached local material:"]
    for attachment in attachments:
        if attachment.kind == "image":
            if vision_enabled and attachment.data_url:
                parts.append(
                    f"[Image attachment: {attachment.name}]\n"
                    "Analyze the actual attached image. Do not invent details that are not visible."
                )
                continue
            parts.append(
                f"[Image attachment: {attachment.name}]\n"
                "No image pixels, OCR text, or visual description are available to this text-only model. "
                "Do not infer or describe the image content."
            )
            continue
        label = {"text": "Text", "pdf": "PDF", "image": "Image"}[attachment.kind]
        parts.append(f"[{label}: {attachment.name}]\n{attachment.excerpt}")
    return "\n\n".join(parts)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pipeline = make_pipeline(PipelineConfig.from_env())
    app.state.pipeline = pipeline
    app.state.swap_lock = asyncio.Lock()
    try:
        yield
    finally:
        if isinstance(pipeline, RemoteGpuPipeline):
            pipeline.close()


app = FastAPI(lifespan=lifespan)


@app.get("/backends")
async def get_backends(force_disable: bool = False) -> dict:
    pipeline: FluxPipeline | RemoteGpuPipeline = app.state.pipeline
    pipeline_kind: BackendKind = "gemlite" if pipeline.is_remote else "mlx"
    return _get_backends_payload(
        force_disable_query=force_disable,
        pipeline_kind=pipeline_kind,
        current_backend=pipeline.backend,
    )


@app.get("/loras")
async def get_loras() -> dict:
    """List only local, selectable Flux2/Klein adapter files.

    The frontend receives filenames rather than paths so a browser client cannot
    point the renderer at arbitrary files on the Mac.
    """
    try:
        directory = _lora_directory()
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not directory.is_dir():
        return {"directory": str(directory), "loras": []}
    adapters = [
        {"name": item.name, "size_bytes": item.stat().st_size}
        for item in sorted(directory.glob("*.safetensors"), key=lambda path: path.name.lower())
        if item.is_file()
    ]
    return {"directory": str(directory), "loras": adapters}


@app.post("/optimize-prompt")
async def optimize_prompt(request: PromptOptimizationRequest) -> dict:
    """Ask an explicitly configured loopback OpenAI-compatible LLM for one prompt."""
    body = {
        "model": request.model,
        "messages": [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 128,
        # Bonsai-27B supports this MLX-LM template option. Other compatible
        # local servers may simply ignore it.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=5.0)) as client:
            response = await client.post(f"{request.llm_url}/chat/completions", json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Local LLM could not be reached: {exc}") from exc
    if response.status_code != 200:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        raise HTTPException(
            status_code=502,
            detail=f"Local LLM returned {response.status_code}: {detail}",
        )
    try:
        payload = response.json()
        optimized = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Local LLM returned no usable prompt.") from exc
    if not isinstance(optimized, str) or not optimized.strip():
        raise HTTPException(status_code=502, detail="Local LLM returned an empty prompt.")
    return {"prompt": optimized.strip()}


@app.post("/extract-pdf")
async def extract_pdf(request: PdfExtractRequest) -> dict:
    """Extract a bounded, local text excerpt from a user-selected PDF."""
    try:
        raw = base64.b64decode(request.data_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="The PDF data is not valid base64.") from exc
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF files are limited to 8 MB in chat.")
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = reader.pages[:40]
        text = "\n\n".join(page.extract_text() or "" for page in pages).strip()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="The PDF could not be read locally.") from exc
    if not text:
        raise HTTPException(
            status_code=422,
            detail="No selectable text was found in this PDF. Scanned PDFs need OCR before Bonsai-27B can use them.",
        )
    return {"filename": request.filename, "pages": len(reader.pages), "text": text[:24_000]}


@app.get("/chat/agents")
async def get_chat_agents() -> dict:
    """Expose curated local chat profiles to the Studio chat selector."""
    return {"agents": _chat_agent_profiles()}


@app.get("/web-search/config")
async def get_web_search_config() -> dict:
    """Expose provider availability, never a provider key or its prefix."""
    return {
        "tavilyConfigured": bool(_configured_search_key("TAVILY_API_KEY")),
        "braveConfigured": bool(_configured_search_key("BRAVE_SEARCH_API_KEY")),
    }


@app.put("/web-search/config")
async def update_web_search_config(request: WebSearchConfigUpdate) -> dict:
    """Store user-entered provider keys in an owner-only local file."""
    updates = {
        "TAVILY_API_KEY": request.tavily_api_key or "",
        "BRAVE_SEARCH_API_KEY": request.brave_api_key or "",
    }
    _write_local_web_search_config(updates)
    return await get_web_search_config()


@app.post("/chat")
async def chat(request: ChatRequest) -> dict:
    """Run a local Bonsai LLM conversation with optional local and web context."""
    messages = [{"role": "system", "content": request.system_prompt}]
    messages.extend(_bounded_chat_history(request.messages))

    sources: list[dict[str, str]] = []
    search_provider = ""
    image_attachments = [item for item in request.attachments if item.kind == "image" and item.data_url]
    vision_enabled = bool(image_attachments and request.vision_llm_url and request.vision_model)
    attachment_context_items = [item for item in request.attachments if item.kind != "image"] if request.agent_id else request.attachments
    extra_context = _attachment_context(attachment_context_items, vision_enabled=vision_enabled)
    if request.web_search:
        latest_question = next(
            (message.content for message in reversed(request.messages) if message.role == "user"),
            "",
        )
        sources, search_query, timestamp, search_provider = await _web_search(
            latest_question,
            request.web_search_provider,
        )
        if sources:
            web_context = (
                f"WEB SEARCH RESULTS\nCurrent local time: {timestamp}\n"
                f"Search query: {search_query}\n"
                f"Search provider: {search_provider}\n"
                "Use only these sources for factual claims and cite their matching source number. "
                "For dated sports fixtures/results, copy the date, teams, score and status exactly from the source title; "
                "never add a team, score or match event that is absent from the sources.\n" + "\n".join(
                    f"[{index}] {source['title']}\n{source['snippet']}\n{source['url']}"
                    for index, source in enumerate(sources, start=1)
                )
            )
            extra_context = "\n\n".join(part for part in (extra_context, web_context) if part)
        else:
            extra_context = "\n\n".join(part for part in (
                extra_context,
                f"WEB SEARCH RESULTS\nCurrent local time: {timestamp}\nSearch query: {search_query}\n"
                f"Search provider: {search_provider}\nNo usable results were found. State this plainly; do not answer from memory.",
            ) if part)
    if extra_context:
        for message in reversed(messages):
            if message["role"] == "user":
                message["content"] = f"{message['content']}\n\n{extra_context}"
                break

    if request.agent_id:
        resolved = _find_chat_agent_profile(request.agent_id)
        if not resolved:
            raise HTTPException(status_code=404, detail="Der ausgewählte lokale Agent wurde nicht gefunden.")
        profile, profile_path = resolved
        latest_question = next(
            (message.content for message in reversed(request.messages) if message.role == "user"),
            "",
        )
        tool_context = ""
        if profile.id == "mailrundruf" and _is_unread_mail_question(latest_question):
            tool_context = await asyncio.to_thread(_read_unread_mail_summary)
        if image_attachments:
            if not vision_enabled:
                raise HTTPException(status_code=422, detail="Bildanalyse im Goose-Agentenlauf benötigt den lokalen Vision-Endpunkt in den Studio-Einstellungen.")
            vision_summary = await _vision_summary_for_harness(
                endpoint=request.vision_llm_url or request.llm_url,
                model=request.vision_model or request.model,
                attachments=image_attachments,
            )
            tool_context = "\n\n".join(part for part in (tool_context, f"Lokale Vision-Auswertung:\n{vision_summary}") if part)
        answer = await _run_goose_harness(
            profile=profile,
            profile_path=profile_path,
            messages=[item for item in messages if item["role"] != "system"],
            tool_context=tool_context,
            response_instruction=request.system_prompt,
        )
        grounded_answer = _ground_unambiguous_score(answer, latest_question, sources)
        source_links = ""
        if sources:
            provider_note = f"*Recherche: {search_provider}.*\n\n" if search_provider else ""
            source_links = f"\n\n{provider_note}### Quellen\n" + "\n".join(
                f"[{index}] [{source['title']}]({source['url']})"
                for index, source in enumerate(sources, start=1)
            )
        return {"message": f"{grounded_answer}{source_links}", "sources": sources, "runner": "Goose-Harness"}

    endpoint = request.llm_url
    model = request.model
    if image_attachments:
        if not vision_enabled:
            raise HTTPException(status_code=422, detail="Bildanalyse benötigt einen lokal gestarteten Vision-Server und ein Vision-Modell in den Studio-Einstellungen.")
        endpoint = request.vision_llm_url or request.llm_url
        model = request.vision_model or request.model
        for message in reversed(messages):
            if message["role"] == "user":
                text = message["content"]
                message["content"] = [
                    {"type": "text", "text": text},
                    *[
                        {"type": "image_url", "image_url": {"url": attachment.data_url}}
                        for attachment in image_attachments
                    ],
                ]
                break

    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1_024,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0)) as client:
            response = await client.post(f"{endpoint}/chat/completions", json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Local LLM could not be reached: {exc}") from exc
    if response.status_code != 200:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=502, detail=f"Local LLM returned {response.status_code}: {detail}")
    try:
        answer = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Local LLM returned no usable chat response.") from exc
    if not isinstance(answer, str) or not answer.strip():
        raise HTTPException(status_code=502, detail="Local LLM returned an empty chat response.")
    latest_question = next(
        (message.content for message in reversed(request.messages) if message.role == "user"),
        "",
    )
    grounded_answer = _ground_unambiguous_score(answer.strip(), latest_question, sources)
    source_links = ""
    if sources:
        provider_note = f"*Recherche: {search_provider}.*\n\n" if search_provider else ""
        source_links = f"\n\n{provider_note}### Quellen\n" + "\n".join(
            f"[{index}] [{source['title']}]({source['url']})"
            for index, source in enumerate(sources, start=1)
        )
    return {"message": f"{grounded_answer}{source_links}", "sources": sources}


@app.post(
    "/generate",
    response_class=Response,
    responses={
        200: {
            "content": {
                "image/png": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                    }
                }
            },
            "description": "Generated PNG image.",
        }
    },
)
async def generate(request: GenerateRequest) -> Response:
    pipeline: FluxPipeline | RemoteGpuPipeline = app.state.pipeline
    lock: asyncio.Lock = app.state.swap_lock
    target_backend: Backend = request.backend if request.backend is not None else pipeline.backend
    if target_backend not in BACKENDS:
        raise HTTPException(status_code=400, detail=f"Unknown backend {target_backend!r}.")
    try:
        lora_paths, lora_scales = _resolve_lora_adapters(request.lora_adapters)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with AsyncExitStack() as stack:
        if not pipeline.is_remote:
            # Why: lock guards in-process MLX swap; remote arm has no resident model so concurrency is safe.
            await stack.enter_async_context(lock)
        try:
            pipeline.ensure_backend(
                backend=target_backend,
                model_path=request.model_path,
                lora_paths=lora_paths,
                lora_scales=lora_scales,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            gen_start = time.perf_counter()
            image_bytes = pipeline.generate_png(
                prompt=request.prompt,
                seed=request.seed,
                steps=request.steps,
                height=request.height,
                width=request.width,
                guidance=request.guidance,
                tiled_vae=request.tiled_vae,
                max_sequence_length=request.max_sequence_length,
            )
            wall_seconds = time.perf_counter() - gen_start
            headers = {"X-Wall-Seconds": f"{wall_seconds:.3f}"}
            if pipeline.last_peak_memory_mb is not None:
                headers["X-Peak-Memory-MB"] = f"{pipeline.last_peak_memory_mb:.1f}"
            return Response(
                content=image_bytes,
                media_type="image/png",
                headers=headers,
            )
        finally:
            if not pipeline.is_remote:
                mx.clear_cache()
                gc.collect()


class CompareRequest(BaseModel):
    prompt: str = Field(min_length=1)
    seed: int = DEFAULT_SEED
    steps: int = Field(default=DEFAULT_STEPS, ge=2)
    guidance: float = Field(default=DEFAULT_GUIDANCE, ge=0.0)
    height: int = Field(default=DEFAULT_HEIGHT, ge=16)
    width: int = Field(default=DEFAULT_WIDTH, ge=16)
    # Why: cross-arm compare is incoherent (one resident pipeline arm at a time);
    # default to the three MLX backends so legacy callers behave unchanged.
    backends: list[Backend] = Field(default_factory=lambda: list(LOCAL_BACKENDS))
    tiled_vae: bool | None = Field(default=None)
    max_sequence_length: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_backends(self) -> "CompareRequest":
        if not self.backends:
            raise ValueError("backends must contain at least one entry.")
        unknown = [b for b in self.backends if b not in BACKENDS]
        if unknown:
            raise ValueError(f"Unknown backend(s): {unknown}; expected subset of {list(BACKENDS)}.")
        if len(set(self.backends)) != len(self.backends):
            raise ValueError("backends must not contain duplicates.")
        return self


@app.post("/generate/compare")
async def generate_compare(request: CompareRequest) -> dict:
    pipeline: FluxPipeline | RemoteGpuPipeline = app.state.pipeline
    lock: asyncio.Lock = app.state.swap_lock

    results = []
    async with AsyncExitStack() as stack:
        if not pipeline.is_remote:
            # Why: same as /generate — lock guards in-process MLX swap; remote arm holds no model.
            await stack.enter_async_context(lock)
        try:
            for target_backend in request.backends:
                swap_start = time.perf_counter()
                try:
                    pipeline.ensure_backend(backend=target_backend, model_path=None)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                swap_seconds = time.perf_counter() - swap_start

                gen_start = time.perf_counter()
                image_bytes = pipeline.generate_png(
                    prompt=request.prompt,
                    seed=request.seed,
                    steps=request.steps,
                    height=request.height,
                    width=request.width,
                    guidance=request.guidance,
                    tiled_vae=request.tiled_vae,
                    max_sequence_length=request.max_sequence_length,
                )
                wall_seconds = time.perf_counter() - gen_start

                results.append(
                    {
                        "backend": target_backend,
                        "png_b64": base64.b64encode(image_bytes).decode("ascii"),
                        "wall_seconds": wall_seconds,
                        "swap_seconds": swap_seconds,
                    }
                )
                if not pipeline.is_remote:
                    mx.clear_cache()
                    gc.collect()
        finally:
            if not pipeline.is_remote:
                mx.clear_cache()
                gc.collect()

    return {"results": results}


__all__ = [
    "app",
    "GenerateRequest",
    "CompareRequest",
    "generate",
    "generate_compare",
    "get_backends",
]
