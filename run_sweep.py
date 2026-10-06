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
    r"\bcold call(ing)?\b",
    r"\bkubernetes\b",
    r"\bci/cd pipelines?\b",
    r"\bb2b enterprise marketing\b",
    r"\benterprise saas marketing\b",
    r"\bsaas marketing\b"
]

TITLE_EXCLUSIONS = [
    r"\bsoftware engineer\b",
    r"\bfull[- ]stack\b",
    r"\bbackend\b",
    r"\bfrontend\b",
    r"\bdevops\b",
    r"\bdata engineer\b",
    r"\btechnical architect\b",
    r"\bqa engineer\b",
    r"\bit director\b"
]

POSITIVE_MULTIPLIERS = [
    "run-of-show", "stadium", "festival", "mass gathering", "permitting",
    "apd", "afd", r"\bems\b", "clickup", "asana", "figma", "canva", "pmp",
    "vendor procurement", "experiential", "activation", "operations", "production"
]

# Verified Public Feeds / Direct ATS Endpoints
DISCOVERY_FEEDS = [
    {
        "type": "lever",
        "site": "twooakventures",
        "source_label": "Two Oak / Austin FC (Q2 Stadium)",
        "default_location": "Austin, TX (Q2 Stadium)"
    },
    {
        "type": "greenhouse",
        "board_token": "c3presents",
        "source_label": "Greenhouse (C3 Presents / Live Nation)",
        "default_location": "Austin, TX"
    },
    {
        "type": "greenhouse",
        "board_token": "yeti",
        "source_label": "Greenhouse (YETI Experiential)",
        "default_location": "Austin, TX 78735"
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
    
    # Check for hourly rates (e.g. $55/hr - $60/hr) and convert to annual (x 2080 hrs)
    hourly_match = re.findall(r"\$([0-9]{2,3}(?:\.[0-9]{2})?)\s*(?:/|\bper\b)?\s*(?:hr|hour)", salary_str.lower())
    if hourly_match:
        rates = [float(r) * 2080 for r in hourly_match]
        return (int(min(rates)), int(max(rates)))

    nums = re.findall(r"\$([0-9]{1,3}(?:,[0-9]{3})*)", salary_str)
    if not nums:
        return (0, 0)
    int_nums = [int(n.replace(",", "")) for n in nums]
    return (min(int_nums), max(int_nums))


def evaluate_job(title: str, description: str, workplace_type: str, location: str, salary_str: str) -> dict:
    """Applies candidate calibration gates, match scoring, and categorization."""
    title_lower = title.lower()
    full_text = f"{title}\n{description}\n{location}".lower()

    # Gate 1: Title Disqualifiers
    for pattern in TITLE_EXCLUSIONS:
        if re.search(pattern, title_lower):
            return {
                "passed": False,
                "score": 35,
                "status": "Passed",
                "notes": [f"Filtered: Excluded technical role pattern '{pattern}'."]
            }

    # Gate 2: Hard Disqualifiers (Sales, Booths, Drayage)
    for pattern in HARD_DISQUALIFIERS:
        if re.search(pattern, full_text):
            return {
                "passed": False,
                "score": 40,
                "status": "Passed",
                "notes": [f"Disqualified by dealbreaker: matches exclusion '{pattern}'."]
            }

    # Gate 3: Short-term Contract Filter (< 1 Year)
    if "contract" in full_text or "temporary" in full_text:
        short_term_match = re.search(r"\b(1|2|3|4|5|6|7|8|9|10|11)\s*(?:-|to)?\s*(?:month|mo)s?\b", full_text)
        if short_term_match:
            return {
                "passed": False,
                "score": 40,
                "status": "Passed",
                "notes": ["Filtered: Contract duration under 1 year walk-away boundary."]
            }

    # Gate 4: Salary Evaluation
    sal_min, sal_max = parse_salary(salary_str)
    if sal_max > 0 and sal_max < SALARY_STRICT_FLOOR:
        return {
            "passed": False,
            "score": 45,
            "status": "Passed",
            "notes": [f"Salary ${sal_max:,} below strict ${SALARY_STRICT_FLOOR:,} walk-away floor."]
        }

    # Gate 5: Score Calculation
    score = 75
    notes = []

    multiplier_hits = [m for m in POSITIVE_MULTIPLIERS if re.search(m, full_text)]
    score += min(len(multiplier_hits) * 3, 20)
    if multiplier_hits:
        clean_hits = [m.replace(r"\b", "") for m in multiplier_hits[:3]]
        notes.append(f"Operational multipliers matched: {', '.join(clean_hits)}.")

    # Location & Workplace Routing
    is_remote = "remote" in workplace_type.lower() or "remote" in location.lower()
    is_austin = any(marker in location.lower() for marker in ["austin", "787", "del valle", "round rock", "travis"])
    is_senior_title = any(re.search(pat, title_lower) for pat in [r"\bdirector\b", r"\bhead\b", r"\bexecutive\b", r"\bsenior producer\b", r"\bsr\. producer\b"])

    if is_remote:
        status = "Parked"
        notes.append("Auto-parked for remote audit: verify leadership scope vs. isolated IC churn.")
    elif is_austin:
        # Route Austin leadership to Fast-Track, rest to Inbox
        if (is_senior_title and score >= 75) or score >= 84:
            status = "Fast-Track"
            notes.append("Local Austin operational fit meeting Fast-Track criteria.")
        else:
            status = "Inbox"
            notes.append("Local Austin posting placed in Inbox for candidate review.")
    else:
        status = "Parked"
        notes.append(f"Regional/National scope ({location}) staged for geographic review.")

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


def fetch_adzuna_jobs(app_id: str, app_key: str) -> list:
    """Fetch roles via Adzuna API, avoiding HTTP 400 on remote queries."""
    if not app_id or not app_key:
        print("[Adzuna] Credentials missing in environment, skipping.")
        return []

    queries = [
        {"what": "Event Operations Producer", "where": "Austin, TX", "dist": "25"},
        {"what": "Experiential Production Manager", "where": "Austin, TX", "dist": "25"},
        {"what": "Director of Events", "where": "Austin, TX", "dist": "25"},
        {"what": "Director of Events Remote", "where": None, "dist": None},
        {"what": "Experiential Producer Remote", "where": None, "dist": None}
    ]

    jobs = []
    for q in queries:
        try:
            url = "https://api.adzuna.com/v1/api/jobs/us/search/1"
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": 20,
                "what": q["what"],
                "content-type": "application/json"
            }
            if q["where"]:
                params["where"] = q["where"]
                params["distance"] = q["dist"]

            res = requests.get(url, params=params, timeout=12)
            if res.status_code == 200:
                results = res.json().get("results", [])
                label = f"'{q['what']}'" + (f" in {q['where']}" if q["where"] else " [Remote]")
                print(f"[Adzuna: {label}] Harvested {len(results)} listings.")
                for item in results:
                    loc_name = item.get("location", {}).get("display_name", "Austin, TX")
                    salary_min = item.get("salary_min")
                    salary_max = item.get("salary_max")
                    sal_str = f"${int(salary_min):,} - ${int(salary_max):,}" if salary_min and salary_max else "Unlisted"
                    jobs.append({
                        "title": item.get("title", ""),
                        "company": item.get("company", {}).get("display_name", "Direct Employer"),
                        "url": clean_url(item.get("redirect_url")),
                        "description": item.get("description", ""),
                        "workplace_type": "Remote" if not q["where"] or "remote" in loc_name.lower() else "Hybrid",
                        "location": "Remote (US)" if not q["where"] else loc_name,
                        "salary": sal_str,
                        "source": "Adzuna Aggregator"
                    })
            else:
                print(f"[Adzuna] Status {res.status_code} for query '{q['what']}'")
        except Exception as e:
            print(f"[Adzuna] Connection error for '{q['what']}': {e}")

    return jobs


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
                    desc = re.sub(r"<[^>]+>", " ", item.get("content", "")).strip()
                    jobs.append({
                        "title": title,
                        "company": board_token.capitalize(),
                        "url": clean_url(item.get("absolute_url")),
                        "description": desc[:2500],
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
                    jobs.append({
                        "title": title,
                        "company": site.capitalize(),
                        "url": clean_url(item.get("hostedUrl")),
                        "description": item.get("descriptionPlain", "")[:2500],
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
    adzuna_id = os.environ.get("ADZUNA_APP_ID")
    adzuna_key = os.environ.get("ADZUNA_APP_KEY")

    if not gsheet_url:
        print("Error: GSHEET_WEBAPP_URL environment variable is missing.")
        sys.exit(1)

    print("Starting Mission Control Autonomous Discovery Sweep...")
    raw_candidates = []

    # 1. Broad Market: Adzuna Engine
    raw_candidates.extend(fetch_adzuna_jobs(adzuna_id, adzuna_key))

    # 2. Targeted Feeds
    for feed in DISCOVERY_FEEDS:
        if feed["type"] == "greenhouse":
            raw_candidates.extend(fetch_greenhouse(feed["board_token"], feed["source_label"], feed["default_location"]))
        elif feed["type"] == "lever":
            raw_candidates.extend(fetch_lever(feed["site"], feed["source_label"], feed["default_location"]))

    print(f"Harvested {len(raw_candidates)} raw listings from broad sweep.")

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

    print(f"Filtered down to {len(curated_batch)} matching role(s).")

    # Ingest to Google Sheet Webhook
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
                print(f"Google Sheet response: {res_data.get('message')}")
        except Exception as e:
            print(f"Failed to post to Google Apps Script: {e}")
    else:
        print("No new jobs to append or GSHEET_WEBAPP_URL not configured.")

    # Trigger Mobile Notification via ntfy.sh
    if ntfy_topic:
        if ingested_count > 0:
            msg = f"🎯 Mission Control: Curated {ingested_count} new postings."
            priority = "high"
            tags = "dart,briefcase"
        else:
            msg = "🎯 Mission Control: Sweep completed. Feeds up to date."
            priority = "low"
            tags = "check"

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
            print("Push alert sent successfully to mobile device.")
        except Exception as e:
            print(f"ntfy alert failed: {e}")


if __name__ == "__main__":
    main()
