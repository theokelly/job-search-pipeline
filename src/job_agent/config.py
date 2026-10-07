from pathlib import Path
import yaml
from dotenv import load_dotenv


load_dotenv("data/secrets.env", override=False)
load_dotenv(".env", override=False)


def load_profile(profile_path: str) -> dict:
    path = Path(profile_path)
    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {profile_path}")
    with path.open() as f:
        return yaml.safe_load(f)


def load_text(file_path: str) -> str:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    return path.read_text()


# Canonical list of profile files needed by the cover-letter + resume-rec prompts.
# Single source of truth: both CLI generation flow and Streamlit "generate now"
# button load via load_profile_files() to stay in sync.
# Resume versions are globbed from profile/resume_versions/ at load time so a
# newly added version (analytics.md, strategy.md) is picked up automatically.
GENERATION_PROFILE_FILES: tuple[str, ...] = (
    "profile/guidelines/cover_letter_principles.md",
    "profile/voice/cover_letter_examples.md",
    "profile/narrative_guidance.md",
    "profile/accomplishments/starls.md",
    "profile/accomplishments/meta_performance.md",
    "profile/voice/elevator_pitch.md",
)


def load_profile_files(rel_paths: tuple[str, ...] | list[str] = GENERATION_PROFILE_FILES) -> dict[str, str]:
    """Load profile files into a dict keyed by their path under profile/.

    Missing files map to empty strings so generation can degrade gracefully if a
    resume version or accomplishments file is not yet populated.
    """
    out: dict[str, str] = {}
    for rel_path in rel_paths:
        key = rel_path.replace("profile/", "")
        try:
            out[key] = load_text(rel_path)
        except FileNotFoundError:
            out[key] = ""
    # All resume versions (README excluded — it's usage docs, not resume content)
    for path in sorted(Path("profile/resume_versions").glob("*.md")):
        if path.stem == "README":
            continue
        out[f"resume_versions/{path.name}"] = path.read_text()
    return out
