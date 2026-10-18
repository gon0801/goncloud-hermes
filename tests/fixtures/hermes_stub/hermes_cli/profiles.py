"""The three profile helpers configure_team.py uses, against HERMES_PROFILES_DIR."""

import os
from pathlib import Path


def get_profile_dir(role):
    return Path(os.environ["HERMES_PROFILES_DIR"]) / role


def create_profile(role, no_alias=False, description=None):
    directory = get_profile_dir(role)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def seed_profile_skills(profile, quiet=False):
    skill = Path(profile) / "skills" / "saludo"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: saludo\n---\n", encoding="utf-8")
    return True
