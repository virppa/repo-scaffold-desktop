from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.core.config import RepoConfig
from app.core.presets import get_preset

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"
_SHARED_DIR = _TEMPLATES_DIR / "shared"


def generate(config: RepoConfig, output_path: Path) -> list[str]:
    """Render all preset template files and write them to output_path.

    Returns the list of relative file paths that were written.
    """
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
    env = Environment(  # nosec B701 — autoescape not relevant for text file generation
        loader=FileSystemLoader([str(templates_dir), str(_SHARED_DIR)]),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    context = config.model_dump()

    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    for relative_path in files_to_write:
        template = env.get_template(f"{relative_path}.j2")
        content = template.render(**context)
        dest = output_path / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    return files_to_write
