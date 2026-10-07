from pathlib import Path
import sys
import yaml
from dotenv import load_dotenv


load_dotenv("data/secrets.env", override=False)
load_dotenv(".env", override=False)


class ProfileError(ValueError):
    """Raised when a profile yaml is missing, unparseable, or fails validation."""


def load_profile(profile_path: str) -> dict:
    """Load and validate the profile yaml. Bad edits fail here with a clear
    message instead of silently mis-shaping the pipeline."""
    path = Path(profile_path)
    if not path.exists():
        raise ProfileError(f"Profile not found: {profile_path}")
    with path.open() as f:
        data = yaml.safe_load(f)
    return validate_profile(data, profile_path)


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


# ---------------------------------------------------------------------------
# Profile schema validation
# ---------------------------------------------------------------------------
# Hand-edited yaml is a recurring source of silent pipeline misbehavior
# (typo'd key, wrong type). validate_profile() runs on every load_profile()
# call so bad edits fail fast with a clear message. Unknown keys only warn,
# so experimental sections don't break loading.
#
# Spec language:
#   type or tuple-of-types -> value must be an instance (int excludes bool;
#                            NoneType allowed explicitly where yaml nulls occur)
#   [spec]                -> list whose elements must match spec
#   {key: spec, ...}      -> dict; unknown keys warn, known keys recurse
#   bare list / dict      -> must be that container, contents unchecked

_PROFILE_SPEC: dict[str, object] = {
    "user": {
        "name": str, "email": str, "phone": str, "linkedin": str,
        "current_location": str, "current_role": str,
        "current_role_end": str, "employment_status": str,
    },
    "geography": {
        "target_city": str,
        "target_country": str,
        "relocation_status": str,
        "open_to_remote_eu": bool,
        "open_to_eu_based": bool,
        "open_to_remote_emea": bool,
        "open_to_remote_global": bool,
        "open_to_other_cities": list,
        "commutable_terms": [str],
        "ambiguous_locations": [str],
        "remote_location_signals": [str],
        "remote_signals": [str],
        "jd_remote_patterns": [str],
        "jd_min_chars_for_remote_check": int,
        "eu_countries": dict,
        "non_eu_terms": [str],
    },
    "work_authorization": {
        "eu_citizen": bool,
        "eu_citizenship_country": str,
        "requires_sponsorship": bool,
        "notes": str,
    },
    "target_roles": [{
        "priority": int,
        "category": str,
        "description": str,
        "example_titles": [str],
        "rationale": str,
    }],
    "not_targeting": [str],
    "seniority": {
        "current_level": str,
        "ideal_target": str,
        "willing_to_step_down": str,
        "step_down_conditions": str,
    },
    "company_preferences": {
        "size": {
            "preferred": str, "weight": str, "rationale": str,
            "stance": dict,
        },
        "ai_posture": {"preferred": str, "weight": str, "rationale": str},
        "industry": {"open_to": [str], "avoid": list},
        "operating_model": {"pe_backed_relevant": bool, "notes": str},
        "remote_culture": {
            "in_office_ok": bool, "hybrid_preferred": bool,
            "full_remote_ok_if_emea": bool,
        },
    },
    "target_companies": [{
        "name": str,
        "website": (str, type(None)),
        "city": (str, type(None)),
        "country": (str, type(None)),
        "ats_type": (str, type(None)),
        "greenhouse_token": (str, type(None)),
        "lever_slug": (str, type(None)),
        "priority": (str, type(None)),
        "size": (str, type(None)),
        "notes": (str, type(None)),
    }],
    "linkedin_search_queries": [{
        "keywords": str,
        "location": (str, type(None)),
        "company_id": (str, type(None)),
    }],
    "pre_filter_terms": [str],
    "title_filter_terms": [str],
    "dealbreakers": {
        "location": [str], "language": [str],
        "role_type": [str], "authorization": [str],
    },
    "compensation": {
        "target_base_eur": (int, type(None)),
        "target_total_eur": (int, type(None)),
        "notes": str,
    },
    "differentiator": {
        "one_liner": str,
        "three_parts": [str],
    },
    "anchor_story": {
        "primary": str,
        "why": str,
    },
    "supporting_stories": [str],
    "credentials": [str],
    "scoring": {
        "skill_fit_signals": {
            "strong_positive": [str], "positive": [str], "neutral": [str],
            "negative": [str], "blocking": [str],
        },
        "preference_alignment_signals": {
            "strong_positive": [str], "positive": [str], "negative": [str],
        },
        "tier_definitions": dict,
        "honesty_calibration": [str],
    },
    "application_outputs": {
        "output_dir": str,
        "naming_convention": str,
        "contents": [str],
    },
    "pipeline": {
        "db_path": str,
        "output_dir": str,
        "expire_after_days": int,
        "auto_sweep_on_run": bool,
        "prune_closed_on_run": bool,
    },
    "models": {
        "prescreen": str,
        "score": str,
        "resume_recommendation": str,
        "cover_letter_by_tier": dict,
        "cover_letter_default": str,
    },
    "truncation": dict,  # str -> int; values checked separately below
    "batches": {
        "enabled_by_default": bool,
        "poll_timeout_seconds": int,
        "poll_interval_seconds": int,
    },
    "prescreen": {
        "persona": str,
        "geography_rules": str,
        "reject_signals": [str],
    },
}


def _type_names(spec) -> str:
    types = spec if isinstance(spec, tuple) else (spec,)
    return "/".join(t.__name__ for t in types)


def _value_ok(value, spec) -> bool:
    """Check a scalar against a type or tuple of types (int excludes bool)."""
    types = spec if isinstance(spec, tuple) else (spec,)
    for t in types:
        if t is int:
            if isinstance(value, int) and not isinstance(value, bool):
                return True
        elif isinstance(value, t):
            return True
    return False


def _check_value(path: str, value, spec, errors: list[str], warnings: list[str]) -> None:
    if isinstance(spec, list):
        if not isinstance(value, list):
            errors.append(f"'{path}' must be a list, got {type(value).__name__}")
            return
        for i, item in enumerate(value):
            _check_value(f"{path}[{i}]", item, spec[0], errors, warnings)
    elif isinstance(spec, dict):
        if not isinstance(value, dict):
            errors.append(f"'{path}' must be a mapping, got {type(value).__name__}")
            return
        for k, v in value.items():
            if k not in spec:
                warnings.append(f"unknown '{path}' key '{k}' \u2014 typo?")
            else:
                _check_value(f"{path}.{k}", v, spec[k], errors, warnings)
    elif spec is list or spec is dict:
        if not isinstance(value, spec):
            errors.append(f"'{path}' must be {spec.__name__}, got {type(value).__name__}")
    elif not _value_ok(value, spec):
        errors.append(f"'{path}' must be {_type_names(spec)}, got {type(value).__name__}")


def validate_profile(profile: dict, profile_path: str = "profile") -> dict:
    """Validate a parsed profile mapping. Raises ProfileError on type errors;
    warns (stderr) on unknown keys. Returns the profile unchanged."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(profile, dict):
        raise ProfileError(
            f"{profile_path}: top level must be a mapping, "
            f"got {type(profile).__name__} (empty or malformed yaml?)"
        )

    for key, value in profile.items():
        if key not in _PROFILE_SPEC:
            warnings.append(f"unknown top-level key '{key}' \u2014 typo?")
        else:
            _check_value(key, value, _PROFILE_SPEC[key], errors, warnings)

    # 'keywords' is required: discover.py does query["keywords"] (KeyError if absent)
    for i, q in enumerate(profile.get("linkedin_search_queries") or []):
        if isinstance(q, dict) and "keywords" not in q:
            errors.append(f"'linkedin_search_queries[{i}]' is missing required 'keywords'")

    # truncation values must all be ints (keys are dynamic)
    trunc = profile.get("truncation")
    if isinstance(trunc, dict):
        for k, v in trunc.items():
            if not _value_ok(v, int):
                errors.append(f"'truncation.{k}' must be int, got {type(v).__name__}")

    for w in warnings:
        print(f"[profile] warning: {w}", file=sys.stderr)
    if errors:
        raise ProfileError(
            f"{profile_path}: invalid profile:\n" + "\n".join(f"  - {e}" for e in errors)
        )
    return profile
