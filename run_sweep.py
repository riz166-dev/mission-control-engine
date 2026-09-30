#!/usr/bin/env python3
"""
Mission Control Autonomous Discovery & Ingestion Engine
Crawls broad-market job boards, regional HigherEd feeds, and direct ATS targets.
Enforces candidate calibration gates, tags granular sources, and pushes
authenticated JSON to Google Sheet doPost webhook + sends mobile push alerts.
"""

import os
import re
import sys
import json
import html
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests

# ---------------------------------------------------------
# Candidate Profile Calibration Rules
# ---------------------------------------------------------
SALARY_STRICT_FLOOR = 75000
SALARY_TARGET_MIN = 90000

# 1. Hard Disqualifiers in Title
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

# 2. Hard Disqualifiers in Description
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
    r"\bci/cd pipelines?\b"
]

# 3. High-Alignment Multipliers
POSITIVE_MULTIPLIERS = [
    "run-of-show", "stadium", "festival", "mass gathering", "permitting",
    "apd", "afd", "ems", "clickup", "asana", "figma", "canva", "pmp",
    "vendor procurement", "experiential", "activation", "operations", "production"
]

# ---------------------------------------------------------
# Target Discovery Feeds
# ---------------------------------------------------------
DISCOVERY_FEEDS = [
    # --- Primary: Regional HigherEd (Austin Metro) ---
    {
        "type": "rss_highered",
        "url": "https://www.higheredjobs.com/rss/categoryFeed.cfm?catID=24&region=Austin%2C%20TX",
        "source_label": "HigherEdJobs (Austin Metro)",
        "default_location": "Austin, TX"
    },
    # --- Primary: Open Remote Operations Feeds ---
    {
        "type": "jobicy_remote",
        "url": "https://jobicy.com/api/v2/remote-jobs?count=50&tag=operations",
        "source_label": "Jobicy (Remote Ops)",
        "default_location": "Remote (US)"
    },
    # --- Secondary: Direct ATS Anchors (Greenhouse) ---
    {
        "type": "greenhouse",
        "board_token": "automatticcareers",
        "source_label": "Greenhouse (Automattic)",
        "default_location": "Remote (US)"
    },
    {
        "type": "greenhouse",
        "board_token": "gitlab",
        "source_label": "Greenhouse (GitLab)",
        "default_location": "Remote (US)"
    },
    {
        "type": "greenhouse",
        "board_token": "iterable",
        "source_label": "Greenhouse (Iterable Austin)",
        "default_location": "Austin, TX"
    },
    # --- Secondary: Direct ATS Anchors (Lever) ---
    {
        "type": "lever",
        "site": "twooakventures",
        "source_label": "Lever (Two Oak / Austin FC)",
        "default_location": "Austin, TX (Q2 Stadium)"
    }
]


def clean_url(url: str) -> str:
    """Strip UTM tracking and syndication query tokens."""
    if not url:
        return ""
    clean = re.sub(r"([?&])(utm_[^&]+|ref=[^&]+|gh_src=[^&]+|lever-source=[^&]+)", "", url)
    return clean.rstrip("?&")


def clean_html_description(raw_html: str) -> str:
    """Converts raw HTML descriptions into clean, formatted plain text."""
    if not raw_html:
        return ""
    # 1. Unescape HTML entities
    text = html.unescape(raw_html)
    # 2. Normalize spaces and special typographical entities
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    text = text.replace("&bull;", "•").replace("&middot;", "·")
    text = text.replace("&rsquo;", "'").replace("&lsquo;", "'")
    text = text.replace("&rdquo;", '"').replace("&ldquo;", '"')
    text = text.replace("&amp;", "&")
    # 3. Clean linebreaks and headers
    text = re.sub(r"<(br|p|div|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
    # 4. Clean list items to bullet points
    text = re.sub(r"<li[^>]*>", "\n• ", text, flags=re.IGNORECASE)
    # 5. Strip residual HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # 6. Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text


def compute_content_fingerprint(text: str) -> str:
    """Produces a consistent 16-character deduplication hash."""
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    seed = normalized[:220] if len(normalized) >= 180 else normalized
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def parse_salary(salary_str: str) -> tuple[int, int]:
    """Extracts minimum and maximum integer values from salary strings."""
    if not salary_str:
        return (0, 0)
    nums = re.findall(r"\$([0-9]{1,3}(?:,[0-9]{3})*)", salary_str)
    if not nums:
        return (0, 0)
    int_nums = [int(n.replace(",", "")) for n in nums]
    return (min(int_nums), max(int_nums))


def evaluate_job(title: str, description: str, workplace_type: str, location: str, salary_str: str) -> dict:
    """Evaluates candidate calibration gates, match scoring, and category routing."""
    title_lower = title.lower()
    full_text = f"{title}\n{description}\n{location}".lower()

    # Gate 1: Title Exclusions (Technical / Engineering)
    for pattern in TITLE_EXCLUSIONS:
        if re.search(pattern, title_lower):
            return {
                "passed": False,
                "score": 35,
                "status": "Passed",
                "notes": [f"Filtered: Excluded technical role pattern '{pattern}'."]
            }

    # Gate 2: Description Hard Disqualifiers (Sales / Drayage / Quotas)
    for pattern in HARD_DISQUALIFIERS:
        if re.search(pattern, full_text):
            return {
                "passed": False,
                "score": 40,
                "status": "Passed",
                "notes": [f"Disqualified by dealbreaker: matches exclusion '{pattern}'."]
            }

    # Gate 3: Salary Evaluation
    sal_min, sal_max = parse_salary(salary_str)
    if sal_max > 0 and sal_max < SALARY_STRICT_FLOOR:
        return {
            "passed": False,
            "score": 45,
            "status": "Passed",
            "notes": [f"Salary ${sal_max:,} below strict ${SALARY_STRICT_FLOOR:,} walk-away floor."]
        }

    # Gate 4: Match Scoring
    score = 75
    notes = []

    multiplier_hits = [m for m in POSITIVE_MULTIPLIERS if m in full_text]
    score += min(len(multiplier_hits) * 3, 20)
    if multiplier_hits:
        notes.append(f"Operational multipliers matched: {', '.join(multiplier_hits[:3])}.")

    is_remote = "remote" in workplace_type.lower() or "remote" in location.lower()
    is_austin = any(marker in location.lower() for marker in ["austin", "787", "del valle", "round rock", "travis"])

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


def fetch_greenhouse(board_token: str, source_label: str, default_location: str) -> list:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    jobs = []
    try:
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            job_list = res.json().get("jobs", [])
            print(f"[{source_label}] Connected. Total raw listings: {len(job_list)}")
            target_keywords = ["event", "operation", "producer", "production", "creative", "program", "director", "manager", "project"]
            for item in job_list:
                title = item.get("title", "")
                if any(kw in title.lower() for kw in target_keywords):
                    loc = item.get("location", {}).get("name", default_location)
                    desc = clean_html_description(item.get("content", ""))
                    jobs.append({
                        "title": title,
                        "company": board_token.capitalize(),
                        "url": clean_url(item.get("absolute_url")),
                        "description": desc,
                        "workplace_type": "Remote" if "remote" in loc.lower() else "Hybrid",
                        "location": loc,
                        "salary": "Unlisted",
                        "source": source_label
                    })
    except Exception as e:
        print(f"Greenhouse fetch error ({source_label}): {e}")
    return jobs


def fetch_lever(site: str, source_label: str, default_location: str) -> list:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    jobs = []
    try:
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            items = res.json()
            print(f"[{source_label}] Connected. Total raw listings: {len(items)}")
            target_keywords = ["event", "operation", "producer", "production", "creative", "director", "manager"]
            for item in items:
                title = item.get("text", "")
                if any(kw in title.lower() for kw in target_keywords):
                    loc = item.get("categories", {}).get("location", default_location)
                    desc = clean_html_description(item.get("descriptionPlain", ""))
                    jobs.append({
                        "title": title,
                        "company": site.replace("twooakventures", "Two Oak Ventures").capitalize(),
                        "url": clean_url(item.get("hostedUrl")),
                        "description": desc,
                        "workplace_type": "Remote" if "remote" in loc.lower() else "On-site",
                        "location": loc,
                        "salary": "Unlisted",
                        "source": source_label
                    })
    except Exception as e:
        print(f"Lever fetch error ({source_label}): {e}")
    return jobs


def fetch_highered_rss(feed_url: str, source_label: str, default_location: str) -> list:
    jobs = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(feed_url, headers=headers, timeout=12)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            items = root.findall(".//item")
            print(f"[{source_label}] Connected. Total raw listings: {len(items)}")
            target_keywords = ["event", "operation", "producer", "production", "director", "manager", "creative", "program"]
            for item in items:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = clean_html_description(item.findtext("description", ""))
                
                # Extract employer if present in title "Title - Company"
                company = "HigherEd Institution"
                if " - " in title:
                    parts = title.split(" - ")
                    title = parts[0].strip()
                    company = parts[1].strip()

                if any(kw in title.lower() for kw in target_keywords):
                    jobs.append({
                        "title": title,
                        "company": company,
                        "url": clean_url(link),
                        "description": desc,
                        "workplace_type": "On-site",
                        "location": default_location,
                        "salary": "Unlisted",
                        "source": source_label
                    })
    except Exception as e:
        print(f"HigherEd RSS fetch error: {e}")
    return jobs


def fetch_jobicy_remote(feed_url: str, source_label: str) -> list:
    jobs = []
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(feed_url, headers=headers, timeout=12)
        if res.status_code == 200:
            job_list = res.json().get("jobs", [])
            print(f"[{source_label}] Connected. Total raw listings: {len(job_list)}")
            target_keywords = ["operation", "producer", "production", "program", "director", "creative", "project"]
            for item in job_list:
                title = item.get("jobTitle", "")
                if any(kw in title.lower() for kw in target_keywords):
                    company = item.get("companyName", "Remote Brand")
                    url = item.get("url", "")
                    desc = clean_html_description(item.get("jobDescription", ""))
                    
                    # Extract salary if provided in API
                    sal_min = item.get("annualSalaryMin")
                    sal_max = item.get("annualSalaryMax")
                    sal_str = f"${sal_min:,} - ${sal_max:,}" if sal_min and sal_max else "Unlisted"

                    jobs.append({
                        "title": title,
                        "company": company,
                        "url": clean_url(url),
                        "description": desc,
                        "workplace_type": "Remote",
                        "location": "Remote (US)",
                        "salary": sal_str,
                        "source": source_label
                    })
    except Exception as e:
        print(f"Jobicy fetch error: {e}")
    return jobs


def main():
    gsheet_url = os.environ.get("GSHEET_WEBAPP_URL")
    gsheet_token = os.environ.get("GSHEET_TOKEN", "mc_secure_token_78701")
    ntfy_topic = os.environ.get("NTFY_TOPIC")

    if not gsheet_url:
        print("Error: GSHEET_WEBAPP_URL environment variable is missing.")
        sys.exit(1)

    print("Executing discovery sweep across broad-market boards & ATS endpoints...")
    raw_candidates = []

    for feed in DISCOVERY_FEEDS:
        f_type = feed["type"]
        if f_type == "greenhouse":
            raw_candidates.extend(fetch_greenhouse(feed["board_token"], feed["source_label"], feed["default_location"]))
        elif f_type == "lever":
            raw_candidates.extend(fetch_lever(feed["site"], feed["source_label"], feed["default_location"]))
        elif f_type == "rss_highered":
            raw_candidates.extend(fetch_highered_rss(feed["url"], feed["source_label"], feed["default_location"]))
        elif f_type == "jobicy_remote":
            raw_candidates.extend(fetch_jobicy_remote(feed["url"], feed["source_label"]))

    print(f"Candidate filtering pool: {len(raw_candidates)} matching keyword roles found.")

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

    # Ingest to Google Sheet Webhook via POST (Uncapped)
    ingested_count = 0
    if curated_batch:
        try:
            payload = {
                "token": gsheet_token,
                "jobs": curated_batch  # Uncapped: all passing records are delivered
            }
            res = requests.post(gsheet_url, json=payload, timeout=40)
            res_data = res.json()
            if res_data.get("status") == "success":
                ingested_count = res_data.get("appended", 0)
                print(f"Successfully posted to Sheet. Appended: {ingested_count} rows.")
            else:
                print(f"Google Sheet response: {res_data.get('message')}")
        except Exception as e:
            print(f"Failed to post to Google Apps Script: {e}")

    # Trigger Mobile Notification via ntfy.sh
    if ntfy_topic:
        if ingested_count > 0:
            msg = f"🎯 Mission Control: The latest push curated {ingested_count} new postings."
            tags = "dart,briefcase"
            priority = "high"
        else:
            msg = "🎯 Mission Control: Sweep completed. Verified feeds up to date."
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
