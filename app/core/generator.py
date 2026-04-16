from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.core.config import RepoConfig
from app.core.presets import get_preset
from app.core.user_prefs import UserPreferences

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"
_SHARED_DIR = _TEMPLATES_DIR / "shared"


def generate(
    config: RepoConfig,
    output_path: Path,
    prefs: UserPreferences | None = None,
) -> list[str]:
    """Render all preset template files and write them to output_path.

    Returns the list of relative file paths that were written.
    """
    if prefs is None:
        prefs = UserPreferences()

    preset = get_preset(config.preset)

    # Build file list: required files + enabled optional files, deduplicated
    files_to_write: list[str] = list(
        dict.fromkeys(
            list(preset.required_files)
            + [
                f
                for toggle_key, optional_files in preset.optional_files.items()
                if getattr(config, toggle_key, False)
                for f in optional_files
            ]
        )
    )

    templates_dir = _TEMPLATES_DIR / config.preset
    env = Environment(  # nosec B701  # nosemgrep: python.flask.security.xss.audit.direct-use-of-jinja2.direct-use-of-jinja2
        loader=FileSystemLoader([str(templates_dir), str(_SHARED_DIR)]),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,  # text file generation, not HTML
    )

    context = {**config.model_dump(), **prefs.model_dump()}

    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    for relative_path in files_to_write:
        template = env.get_template(f"{relative_path}.j2")
        content = template.render(**context)  # nosec  # nosemgrep: python.flask.security.xss.audit.direct-use-of-jinja2.direct-use-of-jinja2
        dest = output_path / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    return files_to_write
