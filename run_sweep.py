import os
import re
import sys
import json
import hashlib
from datetime import datetime
import requests

# ---------------------------------------------------------
# 1. CANDIDATE PROFILE & GATE DEFINITIONS
# ---------------------------------------------------------
AUSTIN_KEYWORDS = ["austin", "78701", "78758", "78735", "78752", "78704", "del valle", "travis county"]

# Tier A: Seniority / Level
SENIORITY_LEVELS = [
    "director", "manager", "lead", "head", "producer", 
    "vp", "vice president", "chief", "principal", "specialist"
]

# Tier B: Core Functional Discipline
EVENT_DOMAINS = [
    "event", "events", "experiential", "experience", 
    "experiences", "production", "festival", "gathering", "entertainment"
]

# Hard Functional Blockers in Title (Instant Rejection)
TITLE_BLOCKERS = [
    "product", "hr", "human resources", "sales", "account executive",
    "business development", "security", "drayage", "booth", "social media",
    "it operations", "warehouse", "culinary", "restaurant", "catering sales",
    "pharma", "speaker bureau"
]

# Hard Disqualifiers in Body
EXCLUDED_INDUSTRIES = ["crypto", "web3", "gambling", "casino", "sports betting"]
EXCLUDED_DUTIES = [
    "ticket sales", "cold call", "cold outreach", "business development manager",
    "booth sales", "drayage", "exhibit sales", "sponsorship sales", 
    "client acquisition quotas", "exhibitor freight"
]

CREATIVE_TOOLS = ["canva", "figma", "clickup", "asana", "airtable"]

# ---------------------------------------------------------
# 2. BOOLEAN & TEXT EVALUATION LOGIC
# ---------------------------------------------------------
def title_clears_level_and_function_gate(title: str) -> bool:
    """
    Evaluates whether a job title satisfies the Level AND Function requirement
    without triggering functional disqualifiers.
    """
    clean_title = title.lower()

    # Step 1: Immediate ejection if title contains hard blockers
    for blocker in TITLE_BLOCKERS:
        if re.search(r'\b' + re.escape(blocker) + r'\b', clean_title):
            return False

    # Step 2: Check for Seniority Level match
    has_level = any(re.search(r'\b' + re.escape(lvl) + r'\b', clean_title) for lvl in SENIORITY_LEVELS)

    # Step 3: Check for Core Event Domain match
    has_event_domain = any(re.search(r'\b' + re.escape(dom) + r'\b', clean_title) for dom in EVENT_DOMAINS)

    # Step 4: Strict Operations Rule (Operations must be paired with Events/Production)
    if "operation" in clean_title or "operations" in clean_title:
        return has_level and has_event_domain

    return has_level and has_event_domain

def extract_salary_range(text: str):
    matches = re.findall(r'\$(\d{2,3}),?(\d{3})?', text)
    if not matches:
        return None, None
    salaries = []
    for m in matches:
        val_str = m[0] + (m[1] if m[1] else "000")
        try:
            salaries.append(int(val_str))
        except ValueError:
            continue
    if salaries:
        return min(salaries), max(salaries)
    return None, None

def evaluate_job(raw_job: dict) -> dict:
    title = raw_job.get("title", "")
    description = raw_job.get("description", "")
    location = raw_job.get("location", "")
    full_text = f"{title} {description} {location}".lower()

    # 1. Title Gate (Seniority x Function Matrix)
    if not title_clears_level_and_function_gate(title):
        return None

    # 2. Hard Disqualifiers: Repost Check
    if "reposted" in full_text or raw_job.get("is_repost", False):
        return None

    # 3. Hard Disqualifiers: Excluded Industries
    for ind in EXCLUDED_INDUSTRIES:
        if ind in full_text:
            return None

    # 4. Hard Disqualifiers: Sales Quotas & Trade Show Drayage
    for duty in EXCLUDED_DUTIES:
        if duty in full_text:
            return None

    # 5. Location Gate: Austin Metro vs US Remote
    is_remote = "remote" in full_text or "remote" in location.lower() or raw_job.get("is_remote", False)
    is_austin = any(k in location.lower() or k in full_text for k in AUSTIN_KEYWORDS)

    if not is_remote and not is_austin:
        return None  # Rejects non-Austin on-site/hybrid

    # 6. Strict Compensation Floor
    sal_min = raw_job.get("salary_min")
    sal_max = raw_job.get("salary_max")
    
    if sal_min is None and sal_max is None:
        text_min, text_max = extract_salary_range(full_text)
        sal_min, sal_max = text_min, text_max

    if sal_max and sal_max < 75000:
        return None  # Under $75k floor

    # 7. Scoring Engine & Operational Categorization
    match_score = 75
    notes = []

    if any(k in full_text for k in ["operations", "logistics", "production", "run of show", "budget"]):
        match_score += 10
        notes.append("Operational Leadership: High alignment with live production & operational execution.")

    found_tools = [t.title() for t in CREATIVE_TOOLS if t in full_text]
    if found_tools:
        match_score += 10
        notes.append(f"Visual / Workflow Synergies: Mentions {', '.join(found_tools)}.")

    # Workplace Type & Fast-Track Routing
    if is_remote:
        workplace_type = "Remote"
        status = "Parked"
        notes.append("Remote Policy: Auto-parked for team structure audit and travel verification.")
    elif is_austin:
        workplace_type = "Hybrid" if "hybrid" in full_text else "On-site"
        if match_score >= 85:
            status = "Fast-Track"
            notes.append("Austin Anchor: Strong local operational fit within target radius.")
        else:
            status = "Inbox"
    else:
        workplace_type = "On-site"
        status = "Inbox"

    # Format salary string
    if sal_min and sal_max:
        salary_str = f"${int(sal_min):,} - ${int(sal_max):,}"
    elif sal_min:
        salary_str = f"${int(sal_min):,}+"
    else:
        salary_str = "Unlisted (Estimated based on scope)"

    # Stable deterministic ID
    unique_key = raw_job.get("url") or f"{title}_{raw_job.get('company')}"
    job_hash = hashlib.md5(unique_key.encode("utf-8")).hexdigest()[:6]

    return {
        "id": f"job_{job_hash}",
        "title": title.strip(),
        "company": raw_job.get("company", "Verified Employer").strip(),
        "url": raw_job.get("url", ""),
        "description": description.strip(),
        "pipeline_state": {
            "status": status,
            "priority": "High" if status == "Fast-Track" else "Standard",
            "date_posted": raw_job.get("date_posted", datetime.now().strftime("%Y-%m-%d")),
            "date_fed": datetime.now().strftime("%Y-%m-%d"),
            "source": raw_job.get("source", "Adzuna Sweep")
        },
        "details": {
            "workplace_type": workplace_type,
            "location": location.strip() if location else "Austin, TX",
            "salary": salary_str
        },
        "evaluation": {
            "match_score": min(match_score, 98),
            "notes": notes
        }
    }

# ---------------------------------------------------------
# 3. MARKET SWEEPER (Adzuna Austin + US Remote Queries)
# ---------------------------------------------------------
def fetch_adzuna_jobs() -> list:
    """
    Sweeps Adzuna across both Austin Metro (25-mile radius) and US Remote
    to capture high-conviction events, operations, and experience roles.
    """
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    
    if not app_id or not app_key:
        print("Notice: Adzuna credentials not found. Skipping aggregator sweep.")
        return []

    discovered = []
    
    # Dual query setup: Local Austin Metro + Nationwide Remote
    queries = [
        {"what": "Event Operations Producer", "where": "Austin, TX", "dist": "25"},
        {"what": "Experiential Production Manager", "where": "Austin, TX", "dist": "25"},
        {"what": "Director of Events", "where": "Austin, TX", "dist": "25"},
        {"what": "Director of Events Remote", "where": None, "dist": None},
        {"what": "Experiential Producer Remote", "where": None, "dist": None}
    ]

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

        try:
            res = requests.get(url, params=params, timeout=15)
            if res.status_code == 200:
                data = res.json()
                for item in data.get("results", []):
                    discovered.append({
                        "title": item.get("title", ""),
                        "company": item.get("company", {}).get("display_name", "Verified Employer"),
                        "url": item.get("redirect_url", ""),
                        "description": item.get("description", ""),
                        "location": item.get("location", {}).get("display_name", q["where"]),
                        "salary_min": item.get("salary_min"),
                        "salary_max": item.get("salary_max"),
                        "date_posted": (item.get("created", "")[:10]) or datetime.now().strftime("%Y-%m-%d"),
                        "source": "Adzuna Sweep",
                        "is_remote": q["is_remote"]
                    })
            else:
                print(f"Notice: Adzuna returned status {res.status_code} for query '{q['where']}'")
        except Exception as e:
            print(f"Notice: Error querying Adzuna: {e}")

    return discovered

# ---------------------------------------------------------
# 4. RUNTIME PIPELINE DISCOVERY, SYNC & NOTIFICATION
# ---------------------------------------------------------
def main():
    print("Starting Mission Control Autonomous Discovery Sweep...")
    
    raw_listings = fetch_adzuna_jobs()
    print(f"Harvested {len(raw_listings)} raw listings from broad sweep.")

    curated_jobs = []
    for raw in raw_listings:
        evaluated = evaluate_job(raw)
        if evaluated:
            curated_jobs.append(evaluated)

    print(f"Filtered down to {len(curated_jobs)} matching role(s).")

    # 1. Push to Google Sheet Webhook
    sheet_url = os.environ.get("GSHEET_WEBAPP_URL")
    secret_token = os.environ.get("GSHEET_TOKEN", "mc_secure_token_78701")
    appended_count = 0

    if sheet_url and curated_jobs:
        try:
            payload = {
                "token": secret_token,
                "jobs": curated_jobs
            }
            res = requests.post(sheet_url, json=payload, timeout=25)
            if res.status_code == 200:
                resp_json = res.json()
                appended_count = resp_json.get("appended", len(curated_jobs))
                print(f"Successfully pushed {appended_count} new postings to Google Sheet.")
            else:
                print(f"Sheet push returned status {res.status_code}: {res.text}")
        except Exception as e:
            print(f"Error posting to Google Sheet Webhook: {e}")
    else:
        print("No new jobs to append or GSHEET_WEBAPP_URL not configured.")

    # 2. Push Notification via ntfy
    ntfy_topic = os.environ.get("NTFY_TOPIC")
    if ntfy_topic:
        if appended_count > 0:
            msg = f"🎯 Mission Control: The latest push curated {appended_count} new posting(s)."
            tags = "briefcase,tada"
            priority = "high"
        else:
            msg = "🎯 Mission Control: Sweep completed. Feeds are up to date."
            tags = "white_check_mark"
            priority = "low"

        try:
            requests.post(
                f"https://ntfy.sh/{ntfy_topic}",
                data=msg.encode("utf-8"),
                headers={
                    "Title": "Mission Control Update",
                    "Priority": priority,
                    "Tags": tags
                },
                timeout=10
            )
            print("Push alert sent successfully to mobile device.")
        except Exception as e:
            print(f"Failed to deliver mobile notification: {e}")

if __name__ == "__main__":
    main()
