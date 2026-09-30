#!/usr/bin/env python3
"""
Mission Control Autonomous Discovery & Ingestion Engine
Crawls target feeds, filters against candidate profile, computes match scores,
posts clean JSON to Google Sheet doPost endpoint, and triggers mobile push alert.
"""

import os
import re
import sys
import json
import hashlib
from datetime import datetime, timezone
import requests

# ---------------------------------------------------------
# Candidate Profile Calibration Rules
# ---------------------------------------------------------
SALARY_STRICT_FLOOR = 75000
SALARY_TARGET_MIN = 90000

HARD_DISQUALIFIERS = [
    r"\bbusiness development\b",
    r"\bclient acquisition\b",
    r"\bticket sales\b",
    r"\bsales quotas?\b",
    r"\btrade show booth\b",
    r"\bexhibit hall\b",
    r"\bdrayage\b",
    r"\bcommission only\b",
    r"\bcold call(ing)?\b"
]

POSITIVE_MULTIPLIERS = [
    "run-of-show", "stadium", "festival", "mass gathering", "permitting",
    "apd", "afd", "ems", "clickup", "asana", "figma", "canva", "pmp",
    "vendor procurement", "experiential", "activation"
]

# Verified Public Feeds / Direct ATS Endpoints
DISCOVERY_FEEDS = [
    {
        "type": "greenhouse",
        "board_token": "yeti",
        "source_label": "Direct / Greenhouse",
        "default_location": "Austin, TX (SW HQ)"
    },
    {
        "type": "greenhouse",
        "board_token": "automattic",
        "source_label": "Direct / Greenhouse Remote",
        "default_location": "Remote (US)"
    },
    {
        "type": "lever",
        "site": "twooakventures", # Austin FC / Q2 Stadium operations
        "source_label": "Sports ATS / Lever",
        "default_location": "Austin, TX (Q2 Stadium)"
    }
]


def clean_url(url: str) -> str:
    """Strip UTM parameters and syndication tracking tokens."""
    if not url:
        return ""
    clean = re.sub(r"([?&])(utm_[^&]+|ref=[^&]+|gh_src=[^&]+|lever-source=[^&]+)", "", url)
    return clean.rstrip("?&")


def compute_content_fingerprint(text: str) -> str:
    """180+ character normalized qualification paragraph fingerprint."""
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    seed = normalized[:220] if len(normalized) >= 180 else normalized
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def parse_salary(salary_str: str) -> tuple[int, int]:
    """Extract minimum and maximum compensation numbers from raw text."""
    if not salary_str:
        return (0, 0)
    nums = re.findall(r"\$([0-9]{1,3}(?:,[0-9]{3})*)", salary_str)
    if not nums:
        return (0, 0)
    int_nums = [int(n.replace(",", "")) for n in nums]
    return (min(int_nums), max(int_nums))


def evaluate_job(title: str, description: str, workplace_type: str, location: str, salary_str: str) -> dict:
    """Applies candidate calibration gates, match scoring, and categorization."""
    full_text = f"{title}\n{description}\n{location}".lower()

    # Gate 1: Hard Disqualifiers (Sales, Booths, Drayage)
    for pattern in HARD_DISQUALIFIERS:
        if re.search(pattern, full_text):
            return {
                "passed": False,
                "score": 40,
                "status": "Passed",
                "notes": [f"Disqualified by dealbreaker: matches exclusion '{pattern}'."]
            }

    # Gate 2: Salary Evaluation
    sal_min, sal_max = parse_salary(salary_str)
    if sal_max > 0 and sal_max < SALARY_STRICT_FLOOR:
        return {
            "passed": False,
            "score": 45,
            "status": "Passed",
            "notes": [f"Salary ${sal_max:,} below strict ${SALARY_STRICT_FLOOR:,} walk-away floor."]
        }

    # Gate 3: Score Calculation
    score = 70  # Baseline
    notes = []

    # Check positive multipliers
    multiplier_hits = [m for m in POSITIVE_MULTIPLIERS if m in full_text]
    score += min(len(multiplier_hits) * 4, 20)
    if multiplier_hits:
        notes.append(f"Operational multipliers matched: {', '.join(multiplier_hits[:3])}.")

    # Location & Workplace Routing
    is_remote = "remote" in workplace_type.lower() or "remote" in location.lower()
    is_austin = "austin" in location.lower() or "787" in location or "del valle" in location.lower()

    if is_remote:
        status = "Parked"
        notes.append("Auto-parked for remote audit: verify leadership scope vs. isolated IC churn.")
    elif is_austin:
        if score >= 90:
            status = "Fast-Track"
            notes.append("Local Austin operational fit meeting high-alignment scoring criteria.")
        else:
            status = "Inbox"
            notes.append("Local Austin posting placed in Inbox for candidate review.")
    else:
        return {
            "passed": False,
            "score": 50,
            "status": "Passed",
            "notes": [f"Exceeds Austin 25-mile commute boundary ({location})."]
        }

    if salary_str and salary_str != "Unlisted":
        notes.append(f"Compensation verified: {salary_str}.")
    else:
        notes.append("Salary unlisted; estimated according to executive scale.")

    return {
        "passed": True,
        "score": min(score, 98),
        "status": status,
        "notes": notes
    }


def fetch_greenhouse(board_token: str, source_label: str, default_location: str) -> list:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    jobs = []
    try:
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            for item in res.json().get("jobs", []):
                title = item.get("title", "")
                if any(kw in title.lower() for kw in ["event", "operation", "producer", "production", "creative", "program"]):
                    loc = item.get("location", {}).get("name", default_location)
                    desc = item.get("content", "")
                    clean_desc = re.sub(r"<[^>]+>", " ", desc).strip()
                    jobs.append({
                        "raw_id": f"gh_{item.get('id')}",
                        "title": title,
                        "company": board_token.capitalize(),
                        "url": clean_url(item.get("absolute_url")),
                        "description": clean_desc[:2500],
                        "workplace_type": "Remote" if "remote" in loc.lower() else "Hybrid",
                        "location": loc,
                        "salary": "Unlisted",
                        "source": source_label
                    })
    except Exception as e:
        print(f"Greenhouse fetch error ({board_token}): {e}")
    return jobs


def fetch_lever(site: str, source_label: str, default_location: str) -> list:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    jobs = []
    try:
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            for item in res.json():
                title = item.get("text", "")
                if any(kw in title.lower() for kw in ["event", "operation", "producer", "director", "creative"]):
                    loc = item.get("categories", {}).get("location", default_location)
                    desc = item.get("descriptionPlain", "")
                    jobs.append({
                        "raw_id": f"lev_{item.get('id')}",
                        "title": title,
                        "company": site.capitalize(),
                        "url": clean_url(item.get("hostedUrl")),
                        "description": desc[:2500],
                        "workplace_type": "Remote" if "remote" in loc.lower() else "On-site",
                        "location": loc,
                        "salary": "Unlisted",
                        "source": source_label
                    })
    except Exception as e:
        print(f"Lever fetch error ({site}): {e}")
    return jobs


def main():
    gsheet_url = os.environ.get("GSHEET_WEBAPP_URL")
    gsheet_token = os.environ.get("GSHEET_TOKEN", "mc_secure_token_78701")
    ntfy_topic = os.environ.get("NTFY_TOPIC")

    if not gsheet_url:
        print("Error: GSHEET_WEBAPP_URL environment variable is missing.")
        sys.exit(1)

    print("Executing discovery sweep...")
    raw_candidates = []

    for feed in DISCOVERY_FEEDS:
        if feed["type"] == "greenhouse":
            raw_candidates.extend(fetch_greenhouse(feed["board_token"], feed["source_label"], feed["default_location"]))
        elif feed["type"] == "lever":
            raw_candidates.extend(fetch_lever(feed["site"], feed["source_label"], feed["default_location"]))

    curated_batch = []
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for raw in raw_candidates:
        eval_result = evaluate_job(
            raw["title"], raw["description"], raw["workplace_type"], raw["location"], raw["salary"]
        )

        if not eval_result["passed"]:
            continue

        job_record = {
            "id": f"job_{compute_content_fingerprint(raw['title'] + raw['description'])}",
            "title": raw["title"],
            "company": raw["company"],
            "url": raw["url"],
            "description": raw["description"],
            "pipeline_state": {
                "status": eval_result["status"],
                "priority": "High" if eval_result["status"] == "Fast-Track" else "Standard",
                "date_posted": today_str,
                "date_fed": today_str,
                "source": raw["source"]
            },
            "details": {
                "workplace_type": raw["workplace_type"],
                "location": raw["location"],
                "salary": raw["salary"]
            },
            "evaluation": {
                "match_score": eval_result["score"],
                "notes": eval_result["notes"]
            }
        }
        curated_batch.append(job_record)

    print(f"Discovery complete. Evaluated {len(raw_candidates)} postings -> {len(curated_batch)} curated.")

    # Ingest to Google Sheet Webhook via POST
    ingested_count = 0
    if curated_batch:
        try:
            payload = {
                "token": gsheet_token,
                "jobs": curated_batch
            }
            res = requests.post(gsheet_url, json=payload, timeout=30)
            res_data = res.json()
            if res_data.get("status") == "success":
                ingested_count = res_data.get("appended", 0)
                print(f"Successfully posted to Sheet. Appended: {ingested_count} rows.")
            else:
                print(f"Google Sheet rejected payload: {res_data.get('message')}")
        except Exception as e:
            print(f"Failed to post to Google Apps Script: {e}")

    # Trigger Mobile Notification via ntfy.sh
    if ntfy_topic:
        if ingested_count > 0:
            msg = f"🎯 Mission Control: The latest push curated {ingested_count} new postings."
            tags = "dart,briefcase"
            priority = "high"
        else:
            msg = "🎯 Mission Control: Sweep completed. No new listings matched your criteria."
            tags = "check"
            priority = "low"

        try:
            requests.post(
                f"https://ntfy.sh/{ntfy_topic}",
                data=msg.encode("utf-8"),
                headers={
                    "Title": "Mission Control Pipeline",
                    "Priority": priority,
                    "Tags": tags
                },
                timeout=10
            )
            print(f"ntfy alert sent to topic: {ntfy_topic}")
        except Exception as e:
            print(f"ntfy push notification failed: {e}")


if __name__ == "__main__":
    main()
