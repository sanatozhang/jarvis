"""Modulehub configuration: yaml section `modulehub:` (defaults) overridden by env `MODULEHUB_*`."""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, List

from pydantic_settings import BaseSettings

from app.config import PROJECT_ROOT, _load_yaml
from app.modulehub.ports import ModuleHubSettings


class ModulehubSettings(BaseSettings):
    enabled: bool = False                       # workers only start when enabled
    jenkins_job: str = "module-publish"
    poll_interval_seconds: int = 30
    mirror_interval_minutes: int = 10
    shell_repo_android: str = "Plaud-AI/plaud-native-android"
    shell_repo_ios: str = "Plaud-AI/plaud-native-ios"
    versions_path: str = "modules.versions.toml"
    github_token: str = ""                      # falls back to GH_TOKEN / GITHUB_TOKEN env
    notify_emails: List[str] = []
    allow_anonymous: bool = False               # accept requests without a logged-in user (dev only)
    base_url: str = ""                          # jarvis frontend URL for deep links

    model_config = {
        "env_prefix": "MODULEHUB_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache(maxsize=1)
def get_modulehub_settings() -> ModulehubSettings:
    s = ModulehubSettings()
    section: Dict = _load_yaml().get("modulehub", {}) or {}
    for key, value in section.items():
        # env wins over yaml: only fill what the environment did not set
        if key in ModulehubSettings.model_fields and key not in s.model_fields_set:
            setattr(s, key, value)
    return s


def to_port_settings(s: ModulehubSettings) -> ModuleHubSettings:
    return ModuleHubSettings(
        shell_repos={"android": s.shell_repo_android, "ios": s.shell_repo_ios},
        versions_path=s.versions_path, base_url=s.base_url,
    )
