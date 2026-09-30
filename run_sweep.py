#!/usr/bin/env python3
"""
Mission Control Autonomous Discovery & Ingestion Engine
Crawls broad-market aggregators (Adzuna), regional HigherEd, and direct ATS targets.
Enforces candidate calibration gates, contract duration filtering (>1 yr requirement),
hourly-to-annual comp conversion, and tags granular sources.
Pushes authenticated JSON to Google Sheet Webhook + sends mobile push alerts.
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
    r"\bit director\b",
    r"\bcloud architect\b",
    r"\bsolutions architect\b"
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
    r"\bci/cd pipelines?\b",
    r"\bb2b enterprise marketing\b",
    r"\benterprise saas marketing\b",
    r"\bsaas marketing\b",
    r"\breposted\b"
]

# Short-term contract disqualifiers (< 1 year)
SHORT_TERM_CONTRACT_PATTERNS = [
    r"\b(1|2|3|4|5|6|7|8|9|10|11)[- ]month (contract|assignment|temp|duration)\b",
    r"\b(1|2|3|4|5|6|7|8|9|10|11) months (contract|assignment|temp|duration)\b",
    r"\bcontract[- ]to[- ]hire for (3|6) months\b",
    r"\bshort[- ]term contract\b",
    r"\btemporary role for (3|6) months\b"
]

# 3. High-Alignment Multipliers (Clean Word Boundaries)
POSITIVE_MULTIPLIERS = [
    "run-of-show", "stadium", "festival", "mass gathering", "permitting",
    "apd", "afd", r"\bems\b", "clickup", "asana", "figma", "canva", "pmp",
    "vendor procurement", "experiential", "activation", "production", "logistics"
]

# ---------------------------------------------------------
# Direct & Regional Discovery Feeds
# ---------------------------------------------------------
DISCOVERY_FEEDS = [
    {
        "type": "rss_highered",
        "url": "https://www.higheredjobs.com/rss/categoryFeed.cfm?catID=24",
        "source_label": "HigherEdJobs (Austin Metro)",
        "default_location": "Austin, TX"
    },
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
    """Strip UTM tracking and syndication query tokens."""
    if not url:
        return ""
    clean = re.sub(r"([?&])(utm_[^&]+|ref=[^&]+|gh_src=[^&]+|lever-source=[^&]+)", "", url)
    return clean.rstrip("?&")


def clean_html_description(raw_html: str) -> str:
    """Converts raw HTML descriptions into clean, formatted plain text."""
    if not raw_html:
        return ""
    text = html.unescape(raw_html)
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    text = text.replace("&bull;", "•").replace("&middot;", "·")
    text = text.replace("&rsquo;", "'").replace("&lsquo;", "'")
    text = text.replace("&rdquo;", '"').replace("&ldquo;", '"')
    text = text.replace("&amp;", "&")
    text = re.sub(r"<(br|p|div|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "\n• ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text


def compute_content_fingerprint(text: str) -> str:
    """Produces a consistent 16-character deduplication hash."""
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    seed = normalized[:220] if len(normalized) >= 180 else normalized
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def parse_salary(salary_str: str) -> tuple[int, int]:
    """Extracts minimum and maximum annual values (converts hourly rates via 2,080 hrs/yr)."""
    if not salary_str:
        return (0, 0)

    # Check for hourly rates (e.g. $55/hr - $59/hr)
    if "/hr" in salary_str.lower() or "hour" in salary_str.lower():
        hourly_matches = re.findall(r"\$([0-9]{2,3}(?:\.[0-9]{2})?)", salary_str)
        if hourly_matches:
            hourly_nums = [float(h) for h in hourly_matches]
            return (int(min(hourly_nums) * 2080), int(max(hourly_nums) * 2080))

    # Standard annual figures ($95,000 - $120,000)
    nums = re.findall(r"\$([0-9]{1,3}(?:,[0-9]{3})*)", salary_str)
    if nums:
        int_nums = [int(n.replace(",", "")) for n in nums]
        return (min(int_nums), max(int_nums))

    return (0, 0)


def scrub_description_for_salary(text: str) -> str:
    """Scans full description text for compensation patterns ($XX/hr or $XXX,XXX/yr)."""
    if not text:
        return "Unlisted"

    # Match hourly: $50/hr - $60/hr
    hourly_pat = r"\$([0-9]{2,3}(?:\.[0-9]{2})?)\s*(?:-|to)\s*\$([0-9]{2,3}(?:\.[0-9]{2})?)\s*(?:/hr|hr|per hour)"
    h_match = re.search(hourly_pat, text, re.IGNORECASE)
    if h_match:
        h_min = float(h_match.group(1))
        h_max = float(h_match.group(2))
        return f"${h_min:.0f}/hr - ${h_max:.0f}/hr (Est. ${int(h_min*2080):,} - ${int(h_max*2080):,}/yr)"

    # Match annual: $120,000 - $150,000
    annual_pat = r"\$([0-9]{2,3}(?:,[0-9]{3})+)(?:\s*(?:-|to)\s*\$([0-9]{2,3}(?:,[0-9]{3})+))?"
    a_match = re.search(annual_pat, text)
    if a_match:
        if a_match.group(2):
            return f"${a_match.group(1)} - ${a_match.group(2)}"
        return f"${a_match.group(1)}"

    return "Unlisted"


def check_contract_eligibility(full_text: str) -> tuple[bool, str]:
    """
    Evaluates contract roles: allows contracts >= 1 year, rejects short-term assignments.
    """
    for pat in SHORT_TERM_CONTRACT_PATTERNS:
        if re.search(pat, full_text):
            return False, f"Disqualified: Contract duration under 12-month minimum ({pat})."
    return True, "Eligible tenure (permanent or contract >= 1 year)."


def check_boolean_essence(title: str, description: str) -> tuple[bool, list]:
    """
    Evaluates role against the 3-Pillar Boolean Essence:
    Pillar 1: Seniority / Leadership Anchor
    Pillar 2: Event / Experiential / Production Domain
    Pillar 3: Operational & Logistics DNA
    """
    title_clean = title.lower()
    desc_clean = description.lower()
    full_text = f"{title_clean}\n{desc_clean}"

    # Gate A: Negative Exclusions
    for pat in HARD_DISQUALIFIERS:
        if re.search(pat, full_text):
            return False, [f"Disqualified by dealbreaker: matches exclusion '{pat}'."]
    for pat in TITLE_EXCLUSIONS:
        if re.search(pat, title_clean):
            return False, [f"Filtered: Excluded technical title pattern '{pat}'."]

    # Check contract tenure
    is_contract_valid, contract_msg = check_contract_eligibility(full_text)
    if not is_contract_valid:
        return False, [contract_msg]

    # Pillar 1: Seniority / Leadership Anchor
    seniority_terms = [
        r"\bdirector\b", r"\bhead\b", r"\blead\b", r"\bmanager\b",
        r"\bproducer\b", r"\bexecutive\b", r"\bproject manager\b", r"\bprogram manager\b",
        r"\bspecialist\b"
    ]
    has_seniority = any(re.search(pat, title_clean) for pat in seniority_terms)

    # Pillar 2: Core Domain Anchor
    domain_terms = [
        r"\bevent(s)?\b", r"\bexperiential\b", r"\bproduction\b",
        r"\bactivation(s)?\b", r"\bfestival\b", r"\blive entertainment\b",
        r"\bguest experience\b", r"\bvenue\b", r"\bcreative operations\b"
    ]
    has_domain_in_title = any(re.search(pat, title_clean) for pat in domain_terms)
    domain_body_hits = sum(1 for pat in domain_terms if re.search(pat, desc_clean))

    if not (has_domain_in_title or domain_body_hits >= 2):
        return False, ["Lacks core event/experiential/production domain foundation."]

    # Pillar 3: Operational DNA (Execution, Logistics, Run-of-show)
    ops_terms = [
        "run-of-show", "logistics", "vendor management", "vendor procurement",
        "permitting", "budget", "cross-functional", "load-in", "site operations",
        "staging", "timeline", "milestones", "fabrication"
    ]
    matched_dna = [term for term in ops_terms if term in desc_clean]

    if not matched_dna and not has_seniority:
        return False, ["Lacks operational/production execution DNA."]

    return True, matched_dna


def evaluate_job(title: str, description: str, workplace_type: str, location: str, salary_str: str) -> dict:
    """Evaluates candidate calibration gates, match scoring, and category routing."""
    full_text = f"{title}\n{description}\n{location}".lower()

    # Step 1: Boolean Essence Gate
    passed_essence, essence_notes = check_boolean_essence(title, description)
    if not passed_essence:
        return {
            "passed": False,
            "score": 40,
            "status": "Passed",
            "notes": essence_notes
        }

    # Step 2: Salary Scrubbing & Conversion
    if salary_str == "Unlisted":
        scrubbed = scrub_description_for_salary(description)
        if scrubbed != "Unlisted":
            salary_str = scrubbed

    sal_min, sal_max = parse_salary(salary_str)
    if sal_max > 0 and sal_max < SALARY_STRICT_FLOOR:
        return {
            "passed": False,
            "score": 45,
            "status": "Passed",
            "notes": [f"Salary ${sal_max:,} below strict ${SALARY_STRICT_FLOOR:,} walk-away floor."]
        }

    # Step 3: Match Scoring
    score = 75
    notes = []

    if essence_notes:
        notes.append(f"Operational DNA matched: {', '.join(essence_notes[:3])}.")

    matched_multipliers = []
    for m in POSITIVE_MULTIPLIERS:
        if re.search(m, full_text):
            clean_name = m.replace(r"\b", "")
            matched_multipliers.append(clean_name)

    score += min(len(matched_multipliers) * 3, 20)
    if matched_multipliers:
        notes.append(f"Profile multipliers matched: {', '.join(matched_multipliers[:3])}.")

    is_remote = "remote" in workplace_type.lower() or "remote" in location.lower()
    is_austin = any(marker in location.lower() for marker in ["austin", "787", "del valle", "round rock", "travis", "q2"])

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
        "salary": salary_str,
        "notes": notes
    }


def fetch_adzuna_jobs() -> list:
    """Queries Adzuna API for Austin Metro and Remote event/experiential operations roles."""
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")

    if not app_id or not app_key:
        print("[Adzuna] Credentials not set in environment (ADZUNA_APP_ID/ADZUNA_APP_KEY), skipping.")
        return []

    jobs = []
    queries = [
        {"what": "Event Operations Producer", "where": "Austin, TX", "dist": "25"},
        {"what": "Experiential Production Manager", "where": "Austin, TX", "dist": "25"},
        {"what": "Director of Events", "where": "Austin, TX", "dist": "25"},
        {"what": "Creative Operations Producer", "where": "Remote", "dist": "0"}
    ]

    for q in queries:
        try:
            url = f"https://api.adzuna.com/v1/api/jobs/us/search/1"
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": 20,
                "what": q["what"],
                "where": q["where"],
                "distance": q["dist"],
                "content-type": "application/json"
            }
            res = requests.get(url, params=params, timeout=12)
            if res.status_code == 200:
                results = res.json().get("results", [])
                print(f"[Adzuna: '{q['what']}' in {q['where']}] Harvested {len(results)} listings.")
                for item in results:
                    title = item.get("title", "")
                    company = item.get("company", {}).get("display_name", "Corporate Employer")
                    desc = clean_html_description(item.get("description", ""))
                    loc_name = item.get("location", {}).get("display_name", q["where"])
                    raw_url = item.get("redirect_url", "")

                    sal_min = item.get("salary_min")
                    sal_max = item.get("salary_max")
                    sal_str = "Unlisted"
                    if sal_min and sal_max:
                        sal_str = f"${int(sal_min):,} - ${int(sal_max):,}"
                    elif sal_min:
                        sal_str = f"${int(sal_min):,}+"

                    is_rem = "remote" in q["where"].lower() or "remote" in loc_name.lower()

                    jobs.append({
                        "title": title,
                        "company": company,
                        "url": clean_url(raw_url),
                        "description": desc,
                        "workplace_type": "Remote" if is_rem else "Hybrid",
                        "location": loc_name,
                        "salary": sal_str,
                        "source": f"Adzuna Aggregator ({company})"
                    })
            else:
                print(f"[Adzuna] HTTP {res.status_code} on query '{q['what']}': {res.text}")
        except Exception as e:
            print(f"[Adzuna] Fetch error: {e}")

    return jobs


def fetch_greenhouse(board_token: str, source_label: str, default_location: str) -> list:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    jobs = []
    try:
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            job_list = res.json().get("jobs", [])
            print(f"[{source_label}] Connected. Total raw listings: {len(job_list)}")
            for item in job_list:
                title = item.get("title", "")
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
            for item in items:
                title = item.get("text", "")
                loc = item.get("categories", {}).get("location", default_location)
                desc = clean_html_description(item.get("descriptionPlain", ""))
                jobs.append({
                    "title": title,
                    "company": site.replace("twooakventures", "Two Oak / Austin FC").capitalize(),
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
            clean_xml = re.sub(r"&(?!(?:amp|lt|gt|quot|apos);)", "&amp;", res.text)
            root = ET.fromstring(clean_xml)
            items = root.findall(".//item")
            print(f"[{source_label}] Connected. Total raw listings: {len(items)}")
            for item in items:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = clean_html_description(item.findtext("description", ""))
                company = "HigherEd Institution"
                if " - " in title:
                    parts = title.split(" - ")
                    title = parts[0].strip()
                    company = parts[1].strip()

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


def main():
    gsheet_url = os.environ.get("GSHEET_WEBAPP_URL")
    gsheet_token = os.environ.get("GSHEET_TOKEN", "mc_secure_token_78701")
    ntfy_topic = os.environ.get("NTFY_TOPIC")

    if not gsheet_url:
        print("Error: GSHEET_WEBAPP_URL environment variable is missing.")
        sys.exit(1)

    print("Executing discovery sweep across broad-market boards & ATS endpoints...")
    raw_candidates = []

    # 1. Broad Aggregator (Adzuna)
    raw_candidates.extend(fetch_adzuna_jobs())

    # 2. Curated Direct ATS & RSS Feeds
    for feed in DISCOVERY_FEEDS:
        f_type = feed["type"]
        if f_type == "greenhouse":
            raw_candidates.extend(fetch_greenhouse(feed["board_token"], feed["source_label"], feed["default_location"]))
        elif f_type == "lever":
            raw_candidates.extend(fetch_lever(feed["site"], feed["source_label"], feed["default_location"]))
        elif f_type == "rss_highered":
            raw_candidates.extend(fetch_highered_rss(feed["url"], feed["source_label"], feed["default_location"]))

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
                "salary": eval_result.get("salary", raw["salary"])
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
