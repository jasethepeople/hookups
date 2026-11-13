from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.score import load_profile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "out" / "myads.md"
LOCANTO_HEADLINES = (
    "Seeking a consensual, casual hookup",
    "Open to mutual chemistry and a relaxed meetup",
    "Looking for a low-pressure adult connection",
)
DOUBLELIST_HEADLINES = (
    "Consensual adult hookup, no pressure",
    "Mutual chemistry and a casual meetup",
    "Looking for a relaxed, casual connection",
)


def _clean(value: object, limit: int = 100) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[<>`*_\[\]{}]", "", text)
    if text.casefold() in {"null", "none", "unknown", "unspecified", "n/a"}:
        return ""
    return text[:limit].strip()


def self_stats(profile: dict[str, Any]) -> list[str]:
    values = profile.get("self_stats")
    if not isinstance(values, dict):
        return []
    stats: list[str] = []
    age = values.get("age")
    if isinstance(age, int) and not isinstance(age, bool) and 18 <= age <= 120:
        stats.append(f"age {age}")
    elif isinstance(age, str) and age.strip().isdigit() and 18 <= int(age.strip()) <= 120:
        stats.append(f"age {int(age.strip())}")
    for key, label in (
        ("body_type", "body type"),
        ("position", "position"),
        ("location", "area"),
        ("host_or_travel", "host/travel"),
    ):
        value = _clean(values.get(key))
        if value:
            stats.append(f"{label}: {value}")
    return stats


def generate_ads(profile: dict[str, Any]) -> dict[str, Any]:
    raw_stats = profile.get("self_stats")
    raw_age = raw_stats.get("age") if isinstance(raw_stats, dict) else None
    try:
        age = int(raw_age) if raw_age is not None and not isinstance(raw_age, bool) else None
    except (TypeError, ValueError):
        age = None
    if age is not None and age < 18:
        raise ValueError("Personal ad drafts are adult-only; profile age must be at least 18")
    stats = self_stats(profile)
    stats_line = f"My stats: {'; '.join(stats)}. " if stats else ""
    locanto_bodies = (
        f"{stats_line}I'm looking for a consensual, casual hookup with another adult. I'd like to chat briefly first and make sure we're on the same page. What are you hoping to find?",
        f"{stats_line}Open to meeting another adult for a casual hookup if the interest is mutual. Share what you're looking for and what timing works; we can decide together whether it's a fit.",
    )
    doublelist_bodies = (
        f"{stats_line}Interested in a consensual casual hookup with someone who wants the same. I'd rather chat briefly first, confirm expectations, and agree on a time that works. What are you hoping to find?",
        f"{stats_line}Open to a casual hookup if the interest is mutual. Tell me what you're looking for and your general availability; we can see whether the fit is right.",
    )
    return {
        "locanto": {"headlines": list(LOCANTO_HEADLINES), "bodies": list(locanto_bodies)},
        "doublelist": {"headlines": list(DOUBLELIST_HEADLINES), "bodies": list(doublelist_bodies)},
        "has_stats": bool(stats),
    }


def render_markdown(profile: dict[str, Any], generated_at: datetime | None = None) -> str:
    ads = generate_ads(profile)
    generated = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    profile_note = (
        "Only the explicitly entered `self_stats` fields below are used; no preferences or match criteria are presented as facts."
        if ads["has_stats"]
        else "No `self_stats` are set in `config/profile.yaml`, so the copy contains no age, body type, position, location, or hosting claims."
    )
    sections = [
        "# Personal ad drafts",
        "",
        f"Generated: {generated}",
        "",
        "These are finished, editable drafts only. Nothing is posted or sent.",
        profile_note,
        "",
        "## Locanto",
        "",
        "Choose the accurate city and personal category when posting. Keep the headline direct, the body concise, and the ad non-commercial. Review the current [Locanto Safety Center](https://www.locanto.com/g/info/safety-center) before uploading photos or posting.",
        "",
        "### Headlines",
    ]
    sections.extend(f"{index}. {headline}" for index, headline in enumerate(ads["locanto"]["headlines"], 1))
    sections.extend(("", "### Body variants", ""))
    sections.extend(f"{index}. {body}" for index, body in enumerate(ads["locanto"]["bodies"], 1))
    sections.extend(
        (
            "",
            "## Doublelist",
            "",
            "Post only in the correct local category. Doublelist prohibits phone numbers, addresses, and social handles in ad text, and prohibits service/escorting offers; keep the post personal and non-commercial. Avoid duplicate or excessive posts. See [Doublelist Community Guidelines](https://doublelist.com/guidelines).",
            "",
            "### Headlines",
        )
    )
    sections.extend(f"{index}. {headline}" for index, headline in enumerate(ads["doublelist"]["headlines"], 1))
    sections.extend(("", "### Body variants", ""))
    sections.extend(f"{index}. {body}" for index, body in enumerate(ads["doublelist"]["bodies"], 1))
    sections.extend(
        (
            "",
            "## Photo checklist",
            "",
            "- [ ] Use a recent photo you own and are comfortable sharing; get consent before including anyone else.",
            "- [ ] Keep images non-explicit and within each platform's current rules; never include anyone under 18.",
            "- [ ] Remove GPS/location metadata and avoid visible addresses, work badges, license plates, or other identifying details.",
            "- [ ] Use a clear, well-lit image without misleading edits, overlays, or contact information.",
            "- [ ] Decide deliberately whether your face is visible; do not rely on a platform's privacy settings as anonymity.",
            "- [ ] Check the final crop and preview before posting.",
            "",
            "Before posting, confirm every profile fact is accurate and that the selected location is genuinely local to you.",
            "",
        )
    )
    return "\n".join(sections)


def write_ads(
    profile_path: str | Path | None = None,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> str:
    profile = load_profile(profile_path)
    text = render_markdown(profile)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_text(text, encoding="utf-8")
    output.chmod(0o600)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate local Locanto and Doublelist ad drafts.")
    parser.add_argument("--profile", default=str(PROJECT_ROOT / "config" / "profile.yaml"))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    write_ads(args.profile, args.output)
    print(Path(args.output).read_text(encoding="utf-8"), end="")
    print(f"\nRendered {Path(args.output).resolve()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
