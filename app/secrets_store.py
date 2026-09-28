"""Secret storage.

Secrets (SMTP password, session secret key) must never end up in the Git
repository and, where reasonably avoidable, not in plain database exports
either.

Storage order for reads:

1. process environment variables (highest priority, container friendly)
2. the local secrets file (``instance/secrets.env`` by default, chmod 0600)

Writes always go to the local secrets file, because that is the only place an
administrator can reasonably edit by hand. Environment variables always win,
so setting ``SMTP_PASSWORD=...`` overrides whatever the UI stored.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

ENV_MAPPING = {
    "SECRET_KEY": "SECRET_KEY",
    "SMTP_PASSWORD": "SMTP_PASSWORD",
    "DB_ENCRYPTION_KEY": "DB_ENCRYPTION_KEY",
}

REDIRECT = "env:"

# Values that are obviously placeholders from .env.example
_PLACEHOLDERS = {
    "",
    "changeme",
    "change-me",
    "secret",
    "password",
    "********",
    "example",
    "placeholder",
    "todo",
}


def is_placeholder(value: str | None) -> bool:
    """True if *value* is empty, a known placeholder, or a stray comment.

    The comment case matters: systemd reads ``.env`` through
    ``EnvironmentFile=`` and does *not* strip a trailing ``# comment``, so
    ``SMTP_PASSWORD=x # Geheim`` reaches the process as the literal value
    ``x # Geheim``. Because the environment wins over both the secrets file
    and the web form, such a value would lock the administrator out of ever
    changing the password again. Treating it as unset keeps the web form in
 charge.
    """
    if value is None:
        return True
    stripped = value.strip()
    if not stripped or stripped.startswith("#"):
        return True
    return stripped.lower() in _PLACEHOLDERS


class SecretsStoreError(RuntimeError):
    pass


class SecretsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._cache: dict[str, str] | None = None

    # -- internal ----------------------------------------------------------
    def _load_file(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        data: dict[str, str] = {}
        if self.path.exists():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    data[key.strip()] = value.strip().strip('"').strip("'")
            except OSError:
                pass
        self._cache = data
        return data

    def _persist(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# PayPal Verzehrabrechnungssystem - lokale Geheimnisse",
            "# Diese Datei wird NICHT ins Git-Repository committet.",
            "# Dateirechte: 0600",
            "",
        ]
        for key in sorted(data):
            lines.append(f"{key}={data[key]}")
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self._cache = data

    # -- public API --------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        env_key = ENV_MAPPING.get(key, key)
        from_env = os.environ.get(env_key)
        if not is_placeholder(from_env):
            return from_env
        value = self._load_file().get(key)
        if is_placeholder(value):
            return default
        return value

    def set(self, key: str, value: str | None) -> None:
        data = dict(self._load_file())
        if value in (None, ""):
            data.pop(key, None)
        else:
            data[key] = value
        self._persist(data)

    def has(self, key: str) -> bool:
        return bool(self.get(key))

    def ensure_secret_key(self) -> str:
        existing = self.get("SECRET_KEY")
        if existing:
            return existing
        generated = secrets.token_urlsafe(48)
        self.set("SECRET_KEY", generated)
        return generated

    def is_overridden_by_env(self, key: str) -> bool:
        env_key = ENV_MAPPING.get(key, key)
        value = os.environ.get(env_key)
        return not is_placeholder(value)

    def writable(self) -> bool:
        from_env = os.environ.get(ENV_MAPPING.get("SMTP_PASSWORD", "SMTP_PASSWORD"))
        if not is_placeholder(from_env):
            return True
        if self.path.exists():
            return os.access(self.path, os.W_OK)
        parent = self.path.parent
        return parent.exists() and os.access(parent, os.W_OK)

    def mask(self, value: str | None) -> str:
        if not value:
            return ""
        return "\u2022" * min(12, max(4, len(value)))

    def generate(self, nbytes: int = 32) -> str:
        return secrets.token_urlsafe(nbytes)


_store: SecretsStore | None = None


def init_store(path: Path) -> SecretsStore:
    global _store
    _store = SecretsStore(path)
    return _store


def get_store() -> SecretsStore:
    global _store
    if _store is None:
        from .config import INSTANCE_DIR

        _store = SecretsStore(INSTANCE_DIR / "secrets.env")
    return _store
