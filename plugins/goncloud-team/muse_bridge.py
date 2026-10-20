"""Safe Windows-to-WSL bridge for delegated Muse Code work."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from .subscriptions.process import SubscriptionProcessError, run_cli
    from .subscriptions.adapters import classify_failure
    from .subscriptions.types import FailureKind
    from .subscriptions.redaction import redact_data
except ImportError:
    from subscriptions.process import SubscriptionProcessError, run_cli
    from subscriptions.adapters import classify_failure
    from subscriptions.types import FailureKind
    from subscriptions.redaction import redact_data


_jev_resuelto = None


def _importar_jev_cliente():
    """jev_cliente como hermano: relativo en modo paquete, absoluto si no."""
    try:
        from . import jev_cliente as modulo

        return modulo
    except ImportError:
        plugin_dir = Path(__file__).resolve().parent
        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))
        import jev_cliente as modulo

        return modulo


def _jev_muse(jev_cliente_inyectado=None, jev_config_inyectada=None):
    """(módulo, cliente, config) de JEV, o None si no se pudo cargar."""
    global _jev_resuelto
    if jev_cliente_inyectado is not None or jev_config_inyectada is not None:
        try:
            modulo = _importar_jev_cliente()
        except Exception:
            return None
        config = jev_config_inyectada
        cliente = jev_cliente_inyectado
        if cliente is None:
            if config is None:
                config = modulo.config_jev()
            cliente = modulo.ClienteJev(config=config)
        elif config is None:
            config = getattr(cliente, "config", None) or modulo.config_jev()
        return modulo, cliente, config
    if _jev_resuelto is None:
        try:
            modulo = _importar_jev_cliente()
            config = modulo.config_jev()
            _jev_resuelto = (modulo, modulo.ClienteJev(config=config), config)
        except Exception:
            _jev_resuelto = False
    return _jev_resuelto or None


class MuseBridgeError(RuntimeError):
    """Muse could not accept or complete a delegated task."""

    def __init__(self, message: str, failure=FailureKind.TASK_FAILURE):
        super().__init__(message)
        self.failure = failure


def windows_path_to_wsl(path: Path) -> str:
    raw = str(path)
    if len(raw) < 3 or raw[1:3] not in {":\\", ":/"}:
        raise MuseBridgeError("Muse workspace must be an absolute Windows path")
    drive = raw[0].lower()
    remainder = raw[3:].replace("\\", "/")
    return f"/mnt/{drive}/{remainder}"


def build_muse_command(
    workspace: Path,
    prompt_file: Path,
    max_steps: int = 80,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> list[str]:
    if not 1 <= max_steps <= 500:
        raise MuseBridgeError("max_steps must be between 1 and 500")
    return [
        "wsl.exe",
        "-d",
        "Ubuntu",
        "--exec",
        "/home/goncloud/.local/bin/muse",
        "exec",
        "--json",
        "--prompt-file",
        windows_path_to_wsl(prompt_file),
        "--workspace",
        windows_path_to_wsl(workspace),
        "--trust-workspace",
        "--approval-mode",
        "never",
        "--user-input-auto-resolve",
        *(["--model", model] if model is not None else []),
        *(
            ["--reasoning-effort", reasoning_effort]
            if reasoning_effort is not None
            else []
        ),
        "--max-model-steps",
        str(max_steps),
    ]


def parse_muse_jsonl(output: str) -> dict:
    model = None
    session_id = None
    terminal_payload = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        stream = event.get("stream") or {}
        if isinstance(stream, dict) and stream.get("kind") == "session":
            session_id = stream.get("id") or session_id
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if event.get("payload_type") == "run.model.configured":
            if model and payload.get("model_id") != model:
                raise MuseBridgeError(
                    "Muse observed model changed", FailureKind.MODEL_UNAVAILABLE
                )
            model = payload.get("model_id") or model
        if event.get("payload_type") == "run.terminal.completed":
            terminal_payload = payload
    if terminal_payload is None:
        raise MuseBridgeError("Muse output did not contain a terminal record")
    return {
        "session_id": session_id,
        "model": model,
        "terminal": terminal_payload.get("terminal"),
        "text": terminal_payload.get("text", ""),
    }


def delegate_to_muse(
    prompt: str,
    workspace: Path,
    actor_role: str,
    max_steps: int = 80,
    timeout: int | None = None,
    runner: Callable[..., Any] = subprocess.run,
    model: str | None = None,
    reasoning_effort: str | None = None,
    jev_cliente=None,
    jev_config=None,
    clasificar_con_jev: bool = True,
) -> dict:
    """Delegate one task to Muse without interpolating user input into a shell.

    ``timeout=None`` — the default — lets the delegation run without a time
    limit, exactly as the stage adapters pass it on from their unbounded stage.
    """
    if not prompt.strip():
        raise MuseBridgeError("Muse delegation prompt cannot be empty")

    prompt_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="goncloud-muse-",
            delete=False,
        ) as prompt_handle:
            prompt_handle.write(prompt)
            prompt_path = Path(prompt_handle.name)

        command = build_muse_command(
            workspace,
            prompt_path,
            max_steps=max_steps,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        try:
            completed = run_cli(
                command,
                "",
                workspace,
                timeout=timeout,
                runner=runner,
            )
        except SubscriptionProcessError as exc:
            failure = exc.failure
            raise MuseBridgeError(
                f"Muse delegation failed ({failure.value})", failure
            ) from exc
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        regla = classify_failure(
            completed.returncode, completed.stream, completed.stderr
        )
        failure = regla
        # Dentro del router clasifica solo el router (punto
        # router_clasificacion): el adaptador Muse pasa
        # clasificar_con_jev=False y esta delegación devuelve la heurística.
        # La herramienta directa goncloud_muse_delegate sí clasifica aquí.
        resuelto = _jev_muse(jev_cliente, jev_config) if clasificar_con_jev else None
        if resuelto is not None:
            modulo, cliente, config = resuelto
            estado = "\n".join(
                (
                    "adapter: muse",
                    f"model: {model or ''}",
                    f"returncode: {completed.returncode}",
                    f"heuristic_classification: {regla.value}",
                    "evidence (tail, already redacted):",
                    (completed.stderr or "")[-1000:],
                    (completed.stream or "")[-1000:],
                )
            )
            nueva = modulo.reclasificar_falla(
                cliente,
                config,
                punto=modulo.PUNTO_MUSE_BRIDGE_CLASIFICACION,
                regla=regla.value,
                estado=estado,
                contexto={"adapter": "muse", "modelo": model or ""},
            )
            failure = FailureKind(nueva)
        raise MuseBridgeError(f"Muse delegation failed ({failure.value})", failure)

    result = parse_muse_jsonl(completed.stream)
    if model is not None and result["model"] != model:
        raise MuseBridgeError(
            "Muse did not prove the requested model", FailureKind.MODEL_UNAVAILABLE
        )
    result["actor_role"] = actor_role
    return redact_data(result, (prompt,))
