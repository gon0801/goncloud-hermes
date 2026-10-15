"""Model version lines and the public catalogs, shared by both autoupdate runners.

Standard library only and no Hermes imports: the claw runner on the Mac Mini is
this file plus ``claw_autoupdate.py`` and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import signal
import subprocess
from typing import Callable, Mapping, Sequence
import urllib.request

#: Model family -> the models.dev provider that carries that vendor's own
#: catalog. A family is a pattern matched at the start of the id, the same ones
#: the subscription router binds (subscriptions/catalog.py); a test keeps the
#: two lists equal.
FAMILIES = {
    "muse-": "meta",
    "claude-": "anthropic",
    "gpt-": "openai",
    "grok-": "xai",
    "glm-": "zai-coding-plan",
    r"k\d": "kimi-code-plan-global",
    "kimi-": "kimi-code-plan-global",
    "deepseek-": "deepseek",
}
GO = "opencode-go"
MODELS_DEV_URL = "https://models.dev/api.json"
GO_MODELS_URL = "https://opencode.ai/zen/go/v1/models"

_NUMERIC_RUN = re.compile(r"\d+(?:[.-]\d+)*")
_UNSTABLE = frozenset({"preview", "exp", "alpha", "beta", "free", "latest"})


@dataclass(frozen=True)
class Versioned:
    line: str  # the id with its version run replaced by "{}", e.g. "gpt-{}-sol"
    version: tuple[int, ...]


@dataclass(frozen=True)
class Promotion:
    old_ref: str
    new_ref: str
    places: tuple[str, ...]


def family_of(model):
    return next((family for family in FAMILIES if re.match(family, model)), None)


def parse(model_id: str) -> Versioned | None:
    """The line and version of an id, or None when it has no single clean version.

    A dated snapshot (any component of four or more digits), two separate
    numeric runs, or an unstable marker all mean "not a release to follow".
    """
    if _UNSTABLE & set(re.split(r"[^a-z]+", model_id.lower())):
        return None
    runs = list(_NUMERIC_RUN.finditer(model_id))
    if len(runs) != 1:
        return None
    run = runs[0]
    parts = re.split(r"[.-]", run.group())
    if any(len(part) >= 4 for part in parts):
        return None
    return Versioned(
        model_id[: run.start()] + "{}" + model_id[run.end() :],
        tuple(int(part) for part in parts),
    )


def _padded(version, length):
    return version + (0,) * (length - len(version))


def _above(left, right):
    length = max(len(left), len(right))
    return _padded(left, length) > _padded(right, length)


def newer_in_line(current: str, catalog: Mapping[str, Mapping]) -> str | None:
    """The highest non-deprecated id of ``current``'s line above it, if any."""
    mine = parse(current)
    if mine is None:
        return None
    best_version, best_id = mine.version, None
    for model_id, facts in sorted(catalog.items()):
        other = parse(model_id)
        if (
            other is not None
            and other.line == mine.line
            and (facts or {}).get("status") != "deprecated"
            and _above(other.version, best_version)
        ):
            best_version, best_id = other.version, model_id
    return best_id


def version_text(model_id: str) -> str | None:
    """The version as written in the id ("4.7", "5-5"), for renaming display names."""
    versioned = parse(model_id)
    if versioned is None:
        return None
    prefix, suffix = versioned.line.split("{}")
    return model_id[len(prefix) : len(model_id) - len(suffix)]


def catalog_view(models_dev, go_ids):
    """Reduce the public catalogs to the sources and facts the watchers compare."""
    view = {}
    if models_dev is not None:
        for provider in sorted(set(FAMILIES.values())):
            models = (models_dev.get(provider) or {}).get("models") or {}
            view[provider] = {
                model: {
                    key: value
                    for key, value in (
                        ("release_date", facts.get("release_date")),
                        ("context", (facts.get("limit") or {}).get("context")),
                        ("status", facts.get("status")),
                    )
                    if value
                }
                for model, facts in models.items()
            }
    if go_ids is not None:
        view[GO] = {model: {} for model in go_ids}
    return view


def get_json(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "goncloud-model-watch"}
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))


def attempt(fetch):
    try:
        return fetch()
    except Exception:  # noqa: BLE001 - any failure means "not read this run"
        return None


def read_catalogs():
    """``(models_dev, go_ids)``; each is None when it could not be read."""
    models_dev = attempt(lambda: get_json(MODELS_DEV_URL))
    go = attempt(lambda: [row["id"] for row in get_json(GO_MODELS_URL)["data"]])
    return models_dev, go


#: Notice key prefix for a pair that was switched and then reverted. Its notice
#: is kept run after run, so it is said once, and the pair is not tried again
#: until the owner deletes that key from the runner's state file.
HELD = "revertido:"
ON_HOLD = "No lo vuelvo a intentar hasta que lo revises."


def change_of(promotion):
    return f"{promotion.old_ref} -> {promotion.new_ref}"


def split_held(promotions, last):
    """``(promotions to try, notices kept for the pairs on hold)``."""
    trying, kept = [], {}
    for promotion in promotions:
        key = HELD + change_of(promotion)
        if key in last:
            kept[key] = last[key]
        else:
            trying.append(promotion)
    return trying, kept


def held_notices(last):
    """The pairs on hold, kept even by a run that failed before planning."""
    return {key: text for key, text in last.items() if key.startswith(HELD)}


def fresh_notices(current: Mapping[str, str], last: Mapping[str, str]) -> list[str]:
    """The notices that are new or worded differently since the last run.

    The caller stores ``current`` as the next ``last``, so a failure that
    repeats word for word (a model waiting on quota) is said once.
    """
    return [text for key, text in current.items() if last.get(key) != text]


@dataclass(frozen=True)
class Ran:
    code: int | None  # None: it timed out or could not start
    out: str
    err: str

    @property
    def ok(self):
        return self.code == 0

    def why(self, limit=200):
        """One short line saying why it failed, for a notice."""
        if self.code is None:
            return self.err[:limit]
        lines = [line.strip() for line in (self.err + "\n" + self.out).splitlines()]
        last = next((line for line in reversed(lines) if line), "")
        return f"salida {self.code}" + (f": {last[:limit]}" if last else "")


Run = Callable[[Sequence[str], float], Ran]


#: After a timeout or an interrupt, how long the killed tree gets to exit.
REAP_TIMEOUT = 10


def run(argv: Sequence[str], timeout: float) -> Ran:
    """Run ``argv``; on timeout kill its whole process tree, never wait unbounded.

    ``subprocess.run(timeout=...)`` kills only the direct child, and on Windows
    then reads its pipes with no bound: a grandchild holding them (hermes.cmd
    -> python -> git) blocks forever.
    """
    try:
        child = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
        )
    except OSError as error:
        return Ran(None, "", f"no arranco: {type(error).__name__}: {error}")
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(child)
        try:
            child.communicate(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        return Ran(None, "", f"sin respuesta tras {int(timeout)} s")
    except BaseException:
        _kill_tree(child)
        try:
            child.wait(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        raise
    return Ran(child.returncode, out, err)


def _kill_tree(child: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(child.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=REAP_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        child.kill()
        return
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except OSError:
        pass
