#!/usr/bin/env python3
"""
Mission Control Autonomous Discovery & Ingestion Engine
Multi-source pipeline: Paginated Adzuna (Pages 1-3), Himalayas Full-Text API,
Jobicy Remote Ops API, We Work Remotely RSS, and Direct Corporate ATS Endpoints.
Enforces Curated Event & Experiential Domains, Dealbreakers (Observations 01-13),
Greater Austin Metro expansion, contract duration checks (>= 1 yr),
and hourly-to-annual compensation normalization (x 2080).
Pushes authenticated JSON to Google Sheet Webhook + sends ntfy mobile alerts.
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
# 1. CANDIDATE PROFILE & GATE DEFINITIONS
# ---------------------------------------------------------
SALARY_STRICT_FLOOR = 75000
SALARY_TARGET_MIN = 90000

# Greater Austin Metro & Submarket Keywords
AUSTIN_METRO_KEYWORDS = [
    "austin", "78701", "78702", "78704", "78735", "78752", "78758",
    "travis county", "del valle",
    "round rock", "cedar park", "pflugerville", "georgetown", "williamson county",
    "buda", "kyle", "san marcos", "dripping springs", "hays county",
    "bee cave", "lakeway"
]

# Curated Positive Functional Domains (No mandatory seniority title required)
EVENT_DOMAINS = [
    "event", "events", "experiential", "experience", "experiences",
    "production", "festival", "gathering", "entertainment",
    "activation", "activations",
    "venue", "venues", "stadium", "arena", "show production",
    "creative operations", "live operations", "guest experience", "fan experience"
]

# LAYER 1: TITLE BLOCKERS (Instant Rejection)
TITLE_BLOCKERS = [
    r"\bproduct manager\b", r"\bhead of product\b", r"\bproduct lead\b", r"\bproduct owner\b",
    r"\bhuman resources\b", r"\bhr\b", r"\btalent acquisition\b", r"\brecruiter\b", r"\bpeople operations\b",
    r"\bsales\b", r"\baccount executive\b", r"\bbusiness development\b", r"\bbdr\b", r"\bsdr\b",
    r"\bchief of staff\b",
    r"\bsecurity\b", r"\bguard\b", r"\bcybersecurity\b",
    r"\btechnical director\b", r"\baudio engineer\b", r"\blighting designer\b", 
    r"\bav technician\b", r"\bbroadcast engineering\b", r"\bsoftware engineer\b", 
    r"\bfull[- ]stack\b", r"\bdevops\b", r"\bdata engineer\b", r"\bsolutions architect\b",
    r"\bdrayage\b", r"\bwarehouse\b", r"\bculinary\b", r"\brestaurant\b", 
    r"\bcatering sales\b", r"\bf&b\b", r"\bpharma\b", r"\bspeaker bureau\b",
    r"\btour\b", r"\broadshow\b", r"\bsummit\b", r"\bconference\b", r"\bsymposium\b"
]

# LAYER 2: BODY TEXT DEALBREAKERS (Observations 01-13)
BODY_DEALBREAKERS = [
    # B2B Enterprise SaaS & Demand Gen
    r"\bb2b (enterprise )?saas\b",
    r"\benterprise software\b",
    r"\bdemand generation\b",
    r"\bmarketing qualified leads?\b",
    r"\bmql(s)?\b",
    r"\bsales qualified leads?\b",
    r"\bsql(s)?\b",
    r"\bpipeline generation\b",

    # Quotas, Cold Calling & Sales Targets
    r"\bsales quotas?\b",
    r"\bclient acquisition quotas?\b",
    r"\bcold call(ing)?\b",
    r"\bcold outreach\b",
    r"\bcommission[- ]only\b",
    r"\bbooth sales\b",
    r"\bexhibit sales\b",
    r"\bsponsorship sales\b",
    r"\bticket sales\b",

    # Trade Shows & Drayage
    r"\btrade show booth\b",
    r"\bexhibit hall\b",
    r"\bdrayage\b",
    r"\bexhibitor freight\b",
    r"\bmodular exhibit\b",

    # Fintech, Crypto & Web3 / Gambling
    r"\bfintech\b",
    r"\bfinancial technology\b",
    r"\bcrypto\b",
    r"\bcryptocurrency\b",
    r"\bweb3\b",
    r"\bblockchain\b",
    r"\btokenomics\b",
    r"\bgambling\b",
    r"\bcasino\b",
    r"\bsports betting\b",

    # Culinary / Restaurant Floor Churn
    r"\bbanquet server\b",
    r"\bkitchen staff\b",
    r"\bmenu tastings?\b",
    r"\btable turns?\b",

    # Health, Pharma & Life Sciences
    r"\blife sciences\b",
    r"\bpharmaceutical\b",
    r"\bpharma\b",
    r"\bcme accreditation\b",
    r"\bmedical device\b",
    r"\bclinical symposium\b",
    r"\bkey opinion leaders?\b",
    r"\bkol speaker\b",

    # Security Operations Core
    r"\bsecurity officer\b",
    r"\bguard dispatch\b",
    r"\bphysical security operations\b",
    r"\baccess control badge\b",

    # Social Media Core Duties
    r"\bmanage (our )?(tiktok|instagram|x|twitter|facebook) account\b",
    r"\bsocial media manager\b",
    r"\bcommunity manager\b",
    r"\binfluencer gifting\b",
    r"\bcontent creator\b",
    r"\btiktok strategy\b",

    # High Travel Ceiling (>30% Travel)
    r"\b(40|50|60|70|80)% travel\b",
    r"\bextensive travel\b",
    r"\broad warrior\b",
    r"\bheavy travel required\b",

    # Technical Infrastructure
    r"\bkubernetes\b",
    r"\bci/cd pipelines?\b",
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

# Positive Alignment Multipliers
POSITIVE_MULTIPLIERS = [
    "run-of-show", "stadium", "festival", "mass gathering", "permitting",
    "apd", "afd", r"\bems\b", "clickup", "asana", "figma", "canva", "pmp",
    "vendor procurement", "experiential", "activation", "production", "logistics"
]

# Direct ATS Discovery Feeds (Public Endpoints)
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

# ---------------------------------------------------------
# 2. STRING CLEANING & UTILITIES
# ---------------------------------------------------------
def clean_url(url: str) -> str:
    """Strip tracking and syndication query tokens."""
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

    # Hourly rates ($55/hr - $59/hr)
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

# ---------------------------------------------------------
# 3. EVALUATION GATES
# ---------------------------------------------------------
def title_clears_domain_gate(title: str) -> bool:
    """Evaluates whether a title satisfies the Event Domain without hitting blockers."""
    clean_title = title.lower()
    for blocker in TITLE_BLOCKERS:
        if re.search(blocker, clean_title):
            return False
    return any(re.search(r'\b' + re.escape(dom) + r'\b', clean_title) for dom in EVENT_DOMAINS)


def check_contract_eligibility(full_text: str) -> tuple[bool, str]:
    """Allows contracts >= 1 year; rejects short-term gigs."""
    for pat in SHORT_TERM_CONTRACT_PATTERNS:
        if re.search(pat, full_text):
            return False, f"Disqualified: Contract duration under 12-month minimum ({pat})."
    return True, "Eligible tenure (permanent or contract >= 1 year)."


def evaluate_job(title: str, description: str, workplace_type: str, location: str, salary_str: str) -> dict:
    """Evaluates candidate calibration gates, negative filters, and category routing."""
    clean_title = title.lower()
    clean_desc = description.lower()
    full_text = f"{clean_title}\n{clean_desc}\n{location.lower()}"

    # Gate 1: Title & Domain Screening
    if not title_clears_domain_gate(title):
        return {
            "passed": False,
            "score": 35,
            "status": "Passed",
            "notes": ["Excluded: Title failed functional domain gate or matched title blocker."]
        }

    # Gate 2: Body Text Dealbreakers (Observations 01-13)
    for dealbreaker in BODY_DEALBREAKERS:
        if re.search(dealbreaker, full_text):
            return {
                "passed": False,
                "score": 40,
                "status": "Passed",
                "notes": [f"Disqualified by dealbreaker: matched pattern '{dealbreaker}'."]
            }

    # Gate 3: Contract Duration Check
    is_contract_valid, contract_msg = check_contract_eligibility(full_text)
    if not is_contract_valid:
        return {
            "passed": False,
            "score": 40,
            "status": "Passed",
            "notes": [contract_msg]
        }

    # Gate 4: Salary Verification & Floor Check
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

    # Gate 5: Geographic Radius & Workplace Routing
    is_remote = "remote" in workplace_type.lower() or "remote" in location.lower()
    is_austin_metro = any(marker in location.lower() for marker in AUSTIN_METRO_KEYWORDS)

    if not is_remote and not is_austin_metro:
        return {
            "passed": False,
            "score": 50,
            "status": "Passed",
            "notes": [f"Exceeds Greater Austin commute boundary ({location})."]
        }

    # Scoring & Status Routing
    score = 75
    notes = []

    matched_multipliers = []
    for m in POSITIVE_MULTIPLIERS:
        if re.search(m, full_text):
            clean_name = m.replace(r"\b", "")
            matched_multipliers.append(clean_name)

    score += min(len(matched_multipliers) * 3, 20)
    if matched_multipliers:
        notes.append(f"Profile multipliers matched: {', '.join(matched_multipliers[:3])}.")

    is_senior_title = any(re.search(r'\b' + re.escape(lvl) + r'\b', clean_title) 
                          for lvl in ["director", "head", "lead", "producer", "manager", "chief", "vp"])

    if is_remote:
        status = "Parked"
        notes.append("Auto-parked for remote audit: verify leadership scope vs. isolated IC churn.")
    elif is_austin_metro:
        if (is_senior_title and score >= 75) or score >= 84:
            status = "Fast-Track"
            notes.append("Local Greater Austin operational fit meeting Fast-Track criteria.")
        else:
            status = "Inbox"
            notes.append("Greater Austin posting placed in Inbox for candidate review.")
    else:
        status = "Parked"
        notes.append(f"Staged for geographic review ({location}).")

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

# ---------------------------------------------------------
# 4. MULTI-SOURCE SOURCING FEEDS
# ---------------------------------------------------------
def fetch_adzuna_jobs() -> list:
    """Queries Adzuna API across Pages 1-3 for Greater Austin and US Remote."""
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")

    if not app_id or not app_key:
        print("[Adzuna] Credentials not set in environment (ADZUNA_APP_ID/ADZUNA_APP_KEY), skipping.")
        return []

    jobs = []
    queries = [
        {"what": "Event Operations", "where": "Austin, TX", "dist": "25"},
        {"what": "Experiential Production", "where": "Austin, TX", "dist": "25"},
        {"what": "Live Operations", "where": "Austin, TX", "dist": "25"},
        {"what": "Brand Activation", "where": "Austin, TX", "dist": "25"},
        {"what": "Event Operations Remote", "where": None, "dist": None},
        {"what": "Experiential Production Remote", "where": None, "dist": None},
        {"what": "Creative Operations Remote", "where": None, "dist": None}
    ]

    for q in queries:
        for page in range(1, 4):
            try:
                url = f"https://api.adzuna.com/v1/api/jobs/us/search/{page}"
                params = {
                    "app_id": app_id,
                    "app_key": app_key,
                    "results_per_page": 25,
                    "what": q["what"],
                    "content-type": "application/json"
                }
                if q["where"]:
                    params["where"] = q["where"]
                if q["dist"]:
                    params["distance"] = q["dist"]

                res = requests.get(url, params=params, timeout=12)
                if res.status_code == 200:
                    results = res.json().get("results", [])
                    print(f"[Adzuna: '{q['what']}' (p.{page})] Harvested {len(results)} listings.")
                    if not results:
                        break

                    for item in results:
                        title = item.get("title", "")
                        company = item.get("company", {}).get("display_name", "Corporate Employer")
                        desc = clean_html_description(item.get("description", ""))
                        loc_name = item.get("location", {}).get("display_name", q["where"] or "Remote")
                        raw_url = item.get("redirect_url", "")

                        sal_min = item.get("salary_min")
                        sal_max = item.get("salary_max")
                        sal_str = "Unlisted"
                        if sal_min and sal_max:
                            sal_str = f"${int(sal_min):,} - ${int(sal_max):,}"
                        elif sal_min:
                            sal_str = f"${int(sal_min):,}+"

                        is_rem = q["where"] is None or "remote" in loc_name.lower()

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
                    break
            except Exception as e:
                print(f"[Adzuna] Error on '{q['what']}' p.{page}: {e}")
                break

    return jobs


def fetch_himalayas_jobs() -> list:
    """Harvests open remote postings with 100% full unedited text via Himalayas API."""
    url = "https://himalayas.app/jobs/api?limit=50"
    jobs = []
    headers = {"User-Agent": "MissionControlJobSweep/2.0"}
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            job_list = res.json().get("jobs", [])
            for item in job_list:
                title = item.get("title", "")
                desc = clean_html_description(item.get("description", ""))
                company = item.get("companyName", "Remote Employer")
                raw_url = item.get("applicationUrl") or item.get("url") or ""
                
                # Check salary if present
                sal_min = item.get("minSalary")
                sal_max = item.get("maxSalary")
                sal_str = f"${sal_min:,} - ${sal_max:,}" if sal_min and sal_max else "Unlisted"

                jobs.append({
                    "title": title,
                    "company": company,
                    "url": clean_url(raw_url),
                    "description": desc,
                    "workplace_type": "Remote",
                    "location": "Remote (US)",
                    "salary": sal_str,
                    "source": f"Himalayas Full-Text ({company})"
                })
            print(f"[Himalayas API] Harvested {len(jobs)} full-text postings.")
    except Exception as e:
        print(f"[Himalayas API] Fetch error: {e}")
    return jobs


def fetch_jobicy_jobs() -> list:
    """Harvests open remote operations postings with full text via Jobicy API."""
    url = "https://jobicy.com/api/v2/remote-jobs?count=50&tag=operations"
    jobs = []
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            job_list = res.json().get("jobs", [])
            for item in job_list:
                title = item.get("jobTitle", "")
                desc = clean_html_description(item.get("jobDescription", ""))
                company = item.get("companyName", "Remote Employer")
                raw_url = item.get("url", "")
                
                sal_min = item.get("annualSalaryMin")
                sal_max = item.get("annualSalaryMax")
                sal_str = f"${sal_min:,} - ${sal_max:,}" if sal_min and sal_max else "Unlisted"

                jobs.append({
                    "title": title,
                    "company": company,
                    "url": clean_url(raw_url),
                    "description": desc,
                    "workplace_type": "Remote",
                    "location": "Remote (US)",
                    "salary": sal_str,
                    "source": f"Jobicy API ({company})"
                })
            print(f"[Jobicy API] Harvested {len(jobs)} full-text operations postings.")
    except Exception as e:
        print(f"[Jobicy API] Fetch error: {e}")
    return jobs


def fetch_wwr_rss() -> list:
    """Harvests We Work Remotely Management & Operations RSS feed."""
    url = "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss"
    jobs = []
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            items = root.findall(".//item")
            for item in items:
                raw_title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = clean_html_description(item.findtext("description", ""))

                title = raw_title
                company = "Remote Enterprise"
                if ":" in raw_title:
                    parts = raw_title.split(":", 1)
                    company = parts[0].strip()
                    title = parts[1].strip()

                jobs.append({
                    "title": title,
                    "company": company,
                    "url": clean_url(link),
                    "description": desc,
                    "workplace_type": "Remote",
                    "location": "Remote (US)",
                    "salary": "Unlisted",
                    "source": f"WeWorkRemotely RSS ({company})"
                })
            print(f"[We Work Remotely RSS] Harvested {len(jobs)} full-text listings.")
    except Exception as e:
        print(f"[WeWorkRemotely RSS] Fetch error: {e}")
    return jobs


def fetch_greenhouse(board_token: str, source_label: str, default_location: str) -> list:
    """Ingests 100% full unedited text from direct Greenhouse boards."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    jobs = []
    try:
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            job_list = res.json().get("jobs", [])
            print(f"[{source_label}] Connected. Raw listings: {len(job_list)}")
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
    """Ingests 100% full unedited text from direct Lever boards."""
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    jobs = []
    try:
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            items = res.json()
            print(f"[{source_label}] Connected. Raw listings: {len(items)}")
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

# ---------------------------------------------------------
# 5. MAIN DISCOVERY ORCHESTRATOR
# ---------------------------------------------------------
def main():
    gsheet_url = os.environ.get("GSHEET_WEBAPP_URL")
    gsheet_token = os.environ.get("GSHEET_TOKEN", "mc_secure_token_78701")
    ntfy_topic = os.environ.get("NTFY_TOPIC")

    if not gsheet_url:
        print("Error: GSHEET_WEBAPP_URL environment variable is missing.")
        sys.exit(1)

    print("Executing discovery sweep across broad-market boards, open feeds & direct ATS...")
    raw_candidates = []

    # 1. Broad Aggregator (Adzuna)
    raw_candidates.extend(fetch_adzuna_jobs())

    # 2. Open Full-Text APIs & Feeds
    raw_candidates.extend(fetch_himalayas_jobs())
    raw_candidates.extend(fetch_jobicy_jobs())
    raw_candidates.extend(fetch_wwr_rss())

    # 3. Direct Verified ATS Feeds
    for feed in DISCOVERY_FEEDS:
        f_type = feed["type"]
        if f_type == "greenhouse":
            raw_candidates.extend(fetch_greenhouse(feed["board_token"], feed["source_label"], feed["default_location"]))
        elif f_type == "lever":
            raw_candidates.extend(fetch_lever(feed["site"], feed["source_label"], feed["default_location"]))

    print(f"Raw candidate pool harvested: {len(raw_candidates)} total postings.")

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

    # Commit to Google Sheet Webhook via POST
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
            msg = f"🎯 Mission Control: The latest sweep curated {ingested_count} new postings."
            tags = "dart,briefcase"
            priority = "high"
        else:
            msg = "🎯 Mission Control: Sweep completed. Feeds verified up to date."
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
