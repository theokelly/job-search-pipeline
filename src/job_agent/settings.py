"""
Single source of truth for runtime config: model names, truncation limits, paths,
batch behavior. Reads from the user's profile yaml with sensible defaults so
older / minimal profile dicts (e.g. test fixtures) still work.

Pure functions over a `profile: dict` — no globals — to keep things testable.
"""
from typing import Any


# ---------------------------------------------------------------------------
# Defaults (used when a key is missing from the user's profile yaml)
# ---------------------------------------------------------------------------

_DEFAULT_MODELS: dict[str, Any] = {
    "prescreen": "claude-haiku-4-5-20251001",
    "score": "claude-sonnet-4-6",
    "resume_recommendation": "claude-haiku-4-5-20251001",
    "cover_letter_by_tier": {
        "Strong Fit": "claude-opus-4-7",
        "Worth Reviewing": "claude-sonnet-4-6",
    },
    "cover_letter_default": "claude-sonnet-4-6",
    "application_answers": "claude-haiku-4-5-20251001",
    "truthfulness_check": "claude-haiku-4-5-20251001",
}

_DEFAULT_TRUNCATION: dict[str, int] = {
    "prescreen_jd_chars": 1000,
    "score_jd_chars": 4500,
    "cover_letter_jd_chars": 4000,
    "resume_rec_jd_chars": 3000,
    "cover_letter_starls_chars": 4000,
    "cover_letter_meta_quotes_chars": 1000,
    "cover_letter_resume_chars": 3000,
    "cover_letter_examples_chars": 5000,
    "resume_rec_resume_chars": 4000,
    "answers_jd_chars": 3000,
    "answers_resume_chars": 3000,
    "truthfulness_resume_chars": 4000,
}

_DEFAULT_PIPELINE: dict[str, Any] = {
    "db_path": "data/jobs.sqlite",
    "output_dir": "outputs",
    "expire_after_days": 14,
    "auto_sweep_on_run": True,
    "prune_closed_on_run": True,
}

_DEFAULT_BATCHES: dict[str, Any] = {
    "enabled_by_default": True,
    "poll_timeout_seconds": 600,
    "poll_interval_seconds": 10,
}


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

def _section(profile: dict, key: str, defaults: dict) -> dict:
    section = profile.get(key) or {}
    return {**defaults, **section}


def model_for(profile: dict, task: str, tier: str | None = None) -> str:
    """Return the model name for a given task.

    task: one of 'prescreen', 'score', 'resume_recommendation', 'cover_letter',
    'application_answers', 'truthfulness_check'.
    For 'cover_letter', `tier` selects via models.cover_letter_by_tier with
    fallback to models.cover_letter_default.
    """
    models = _section(profile, "models", _DEFAULT_MODELS)
    if task == "cover_letter":
        by_tier = models.get("cover_letter_by_tier") or _DEFAULT_MODELS["cover_letter_by_tier"]
        if tier and tier in by_tier:
            return by_tier[tier]
        return models.get("cover_letter_default") or _DEFAULT_MODELS["cover_letter_default"]
    return models.get(task) or _DEFAULT_MODELS[task]


def truncation(profile: dict, key: str) -> int:
    trunc = _section(profile, "truncation", _DEFAULT_TRUNCATION)
    return int(trunc.get(key, _DEFAULT_TRUNCATION.get(key, 4000)))


def pipeline_config(profile: dict) -> dict:
    return _section(profile, "pipeline", _DEFAULT_PIPELINE)


def batches_config(profile: dict) -> dict:
    return _section(profile, "batches", _DEFAULT_BATCHES)


def prescreen_config(profile: dict) -> dict:
    """Returns {persona, geography_rules, reject_signals}. Defaults are empty
    strings / lists so prescreen falls back gracefully if not configured."""
    block = profile.get("prescreen") or {}
    return {
        "persona": block.get("persona", "").strip(),
        "geography_rules": block.get("geography_rules", "").strip(),
        "reject_signals": block.get("reject_signals") or [],
    }


# ---------------------------------------------------------------------------
# Geography helpers
# ---------------------------------------------------------------------------
# These return flat lower-cased sets. The pre-filter uses them as substring
# match targets against job.location.

# Built-in fallbacks — used when profile.geography.eu_countries is absent
# (e.g. minimal test fixtures). Keep these in sync with the yaml for safety.
_FALLBACK_EU_TERMS = {
    "united kingdom", "england", "scotland", "wales", "northern ireland",
    "london", "manchester", "edinburgh", "birmingham", "bristol",
    "netherlands", "nederland", "amsterdam", "rotterdam", "eindhoven", "utrecht",
    "germany", "deutschland", "berlin", "munich", "münchen", "hamburg",
    "frankfurt", "cologne", "köln", "düsseldorf", "stuttgart",
    "france", "paris", "lyon", "bordeaux", "toulouse",
    "spain", "españa", "madrid", "barcelona", "bilbao",
    "italy", "italia", "milan", "milano", "rome", "roma",
    "belgium", "belgique", "brussels", "bruxelles", "antwerp",
    "sweden", "sverige", "stockholm", "gothenburg", "göteborg",
    "denmark", "danmark", "copenhagen", "københavn",
    "norway", "norge", "oslo",
    "finland", "suomi", "helsinki",
    "ireland", "éire", "dublin",
    "austria", "österreich", "vienna", "wien",
    "switzerland", "schweiz", "zurich", "zürich", "geneva", "genève", "bern",
    "portugal", "lisbon", "lisboa", "porto",
    "poland", "polska", "warsaw", "warszawa", "kraków", "cracow",
    "czech", "czechia", "prague", "praha",
    "hungary", "magyarország", "budapest",
    "romania", "românia", "bucharest",
    "greece", "ελλάδα", "athens",
    "luxembourg", "liechtenstein", "iceland", "malta", "cyprus",
    "slovakia", "slovenia", "croatia", "estonia", "latvia", "lithuania",
    "bulgaria", "serbia", "monaco",
    "europe", "emea",
}

_FALLBACK_REMOTE_SIGNALS = {"remote", "hybrid", "eu"}

_FALLBACK_NON_EU = {
    "united states", "usa", " us,", " us ", "- us", "remote us",
    "canada", "mexico", "brazil", "latam",
    "australia", "new zealand", "singapore", "japan", "china", "korea",
    "india", "pakistan", "indonesia", "philippines", "thailand",
    "california", "new york", "texas", "illinois", "florida", "georgia",
    "virginia", "washington", "oregon", "massachusetts", "ohio",
    "dubai", "abu dhabi", "united arab emirates", "uae", "saudi arabia",
    "qatar", "bahrain", "kuwait", "oman", "egypt", "nigeria", "kenya",
    "south africa", "israel", "turkey", "türkiye", "istanbul",
}


def eu_place_terms(profile: dict) -> set[str]:
    geo = profile.get("geography") or {}
    countries = geo.get("eu_countries")
    if not countries:
        return set(_FALLBACK_EU_TERMS)
    out: set[str] = set()
    for country_terms in countries.values():
        if isinstance(country_terms, list):
            out.update(t.lower() for t in country_terms)
    return out


def remote_signals(profile: dict) -> set[str]:
    geo = profile.get("geography") or {}
    signals = geo.get("remote_signals")
    if not signals:
        return set(_FALLBACK_REMOTE_SIGNALS)
    return {s.lower() for s in signals}


def non_eu_terms(profile: dict) -> set[str]:
    geo = profile.get("geography") or {}
    terms = geo.get("non_eu_terms")
    if not terms:
        return set(_FALLBACK_NON_EU)
    return {t.lower() for t in terms}


def eu_allowlist_terms(profile: dict) -> set[str]:
    """EU places + remote signals — the combined set used as the main allow-check."""
    return eu_place_terms(profile) | remote_signals(profile)
