import re

from .db import Job
from . import settings


def pre_filter(job: Job, profile: dict) -> tuple[bool, str]:
    """Returns (keep, reason). reason is set when the job is dropped.

    Geography terms are sourced from the profile yaml's `geography:` block
    (eu_countries, remote_signals, non_eu_terms); built-in defaults in
    settings.py back-stop minimal profile dicts (e.g. test fixtures).
    """
    title = job.title.lower()
    text = f"{title} {job.description_raw}".lower()

    # Keyword terms checked against title + description
    for term in profile.get("pre_filter_terms", []):
        if term.lower() in text:
            return False, f"dealbreaker: '{term}'"

    # Title-only role-type filters
    for term in profile.get("title_filter_terms", []):
        if term.lower() in title:
            return False, f"role type: '{term}'"

    # Location check
    geo = profile.get("geography", {})
    if geo.get("commutable_terms"):
        return _commute_mode_location_check(job, profile)

    open_to_eu = geo.get("open_to_eu_based", False)
    job_loc = job.location.lower()

    eu_terms = settings.eu_allowlist_terms(profile)
    eu_places = settings.eu_place_terms(profile)
    non_eu = settings.non_eu_terms(profile)

    # Fast-reject clearly non-EU locations (catches Dubai, US, etc.)
    if any(term in job_loc for term in non_eu):
        # Only reprieve: "remote" or "hybrid" AND an actual EU place name is also present
        # (e.g. "Remote - Europe" or "Berlin / Remote"). Use eu_places (not eu_terms)
        # so that "Remote - US" (which has "remote" but no EU place) does not sneak through.
        has_remote = "remote" in job_loc or "hybrid" in job_loc
        has_eu_place = any(term in job_loc for term in eu_places)
        if not (has_remote and has_eu_place):
            return False, f"location outside EU/UK: '{job.location}'"

    # Accept if location matches any EU/EEA/UK term
    if open_to_eu and any(term in job_loc for term in eu_terms):
        return True, ""

    # Fall back to explicit target city / country match
    target_city = geo.get("target_city", "").lower()
    target_country = geo.get("target_country", "").lower()
    other_cities = [c.lower() for c in geo.get("open_to_other_cities", [])]

    if target_city or target_country:
        matches = (
            (target_city and target_city in job_loc)
            or (target_country and target_country in job_loc)
            or any(c in job_loc for c in other_cities)
        )
        if not matches:
            return False, f"location mismatch: '{job.location}'"

    return True, ""


def _commute_mode_location_check(job: Job, profile: dict) -> tuple[bool, str]:
    """Location check for a candidate who lives in one metro and will not relocate.

    Active when `geography.commutable_terms` is set in the profile. Order:
      1. Clearly non-EU location -> drop, unless it also carries a remote
         signal AND an EU/EMEA place ("Remote - Europe / US").
      2. Within commuting range -> keep.
      3. Remote/region-wide signal in the location ("Remote", "EMEA") -> keep.
      4. Vague location ("Netherlands", empty) -> keep; prescreen decides.
      5. Anywhere else (London, Berlin, Rotterdam...) is treated as on-site:
         keep only if the JD mentions remote work (or is too short to judge),
         so prescreen can confirm the remote option is open to NL residents.

    Drops here never reach the DB, so step 5 errs toward passing to prescreen.
    """
    geo = profile.get("geography", {})
    loc = (job.location or "").strip().lower()

    commutable = [t.lower() for t in geo.get("commutable_terms", [])]
    remote_loc = [t.lower() for t in geo.get("remote_location_signals", ["remote", "emea", "europe"])]
    ambiguous = {t.lower() for t in geo.get("ambiguous_locations", ["netherlands", ""])}
    eu_places = settings.eu_place_terms(profile)
    non_eu = settings.non_eu_terms(profile)

    has_remote_loc = any(t in loc for t in remote_loc)

    if any(t in loc for t in non_eu):
        if not (has_remote_loc and any(t in loc for t in eu_places)):
            return False, f"location outside EU/EMEA: '{job.location}'"

    if any(t in loc for t in commutable):
        return True, ""
    if has_remote_loc:
        return True, ""
    if loc in ambiguous:
        return True, ""
    # Country-only locations ("United Kingdom", "Germany") are how LinkedIn
    # often labels remote roles; let prescreen read the JD.
    if loc.strip(" ,") in _country_names(profile):
        return True, ""

    jd = job.description_raw or ""
    if len(jd) < int(geo.get("jd_min_chars_for_remote_check", 300)):
        return True, ""
    patterns = geo.get("jd_remote_patterns") or [r"\bremote\b"]
    jd_low = jd.lower()
    if any(re.search(p, jd_low) for p in patterns):
        return True, ""

    return False, f"location outside commuting range, no remote signal: '{job.location}'"


def _country_names(profile: dict) -> set[str]:
    """Country-level names from geography.eu_countries: the first term of each
    country list, plus every term in `other_eu` (those are all countries)."""
    countries = (profile.get("geography") or {}).get("eu_countries") or {}
    names: set[str] = set()
    for key, terms in countries.items():
        if not isinstance(terms, list) or not terms or key == "broad_signals":
            continue
        if key == "other_eu":
            names.update(t.lower() for t in terms)
        else:
            names.add(terms[0].lower())
    return names
