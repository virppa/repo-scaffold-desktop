import json
import platform
from pathlib import Path

from pydantic import BaseModel


class UserPreferences(BaseModel):
    author_name: str = ""
    author_email: str = ""
    github_username: str = ""
    default_output_dir: Path | None = None
    default_preset: str = "python_basic"

    model_config = {"extra": "ignore"}


class PrefsStore:
    _APP_DIR = "repo-scaffold"

    @classmethod
    def get_path(cls) -> Path:
        if platform.system() == "Windows":
            base = Path.home() / "AppData" / "Roaming"
        else:
            base = Path.home() / ".config"
        return base / cls._APP_DIR / "prefs.json"

    @classmethod
    def load(cls) -> UserPreferences:
        path = cls.get_path()
        if not path.exists():
            return UserPreferences()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return UserPreferences.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            return UserPreferences()

    @classmethod
    def save(cls, prefs: UserPreferences) -> None:
        path = cls.get_path()
        cls._assert_not_in_git_repo(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            prefs.model_dump_json(indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _assert_not_in_git_repo(path: Path) -> None:
        for parent in path.parents:
            if (parent / ".git").exists():
                raise RuntimeError(
                    f"Refusing to write prefs inside a git repository: {path}"
                )
