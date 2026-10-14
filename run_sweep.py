#!/usr/bin/env python3
"""
Mission Control Autonomous Discovery & Ingestion Engine
Crawls broad-market aggregators (Adzuna across Pages 1-3) and direct ATS feeds.
Enforces Curated Event Domains, Observations 01-13 Negative Gates,
Greater Austin Metro expansion, contract duration checks (>= 1 yr),
and converts hourly compensation to annual equivalents.
Pushes authenticated JSON to Google Sheet Webhook + sends mobile push alerts.
"""

import os
import re
import sys
import json
import html
import hashlib
from datetime import datetime, timezone
import requests

# ---------------------------------------------------------
# 1. CANDIDATE PROFILE & GATE DEFINITIONS
# ---------------------------------------------------------
SALARY_STRICT_FLOOR = 75000
SALARY_TARGET_MIN = 90000

# Greater Austin Metro & Submarket Keywords
AUSTIN_METRO_KEYWORDS = [
    # Core Austin & Urban Zip Codes
    "austin", "78701", "78702", "78704", "78735", "78752", "78758",
    "travis county", "del valle",
    
    # North Metro
    "round rock", "cedar park", "pflugerville", "georgetown", "williamson county",
    
    # South & Southwest Metro
    "buda", "kyle", "san marcos", "dripping springs", "hays county",
    
    # West Metro
    "bee cave", "lakeway"
]

# Curated Positive Functional Domains (No mandatory seniority title required)
EVENT_DOMAINS = [
    # Core Direct Experiential & Production
    "event", "events", "experiential", "experience", "experiences",
    "production", "festival", "gathering", "entertainment",
    
    # Brand Field Activations (Fixed/Local scope)
    "activation", "activations",
    
    # Facilities & Stadiums
    "venue", "venues", "stadium", "arena", "show production",
    
    # Operational & Guest Delivery
    "creative operations", "live operations", "guest experience", "fan experience"
]

# LAYER 1: TITLE BLOCKERS (Instant Rejection at Title Level)
TITLE_BLOCKERS = [
    # Observation 04: Product Management
    r"\bproduct manager\b", r"\bhead of product\b", r"\bproduct lead\b", r"\bproduct owner\b",
    
    # Observation 07: HR & Internal People Ops
    r"\bhuman resources\b", r"\bhr\b", r"\btalent acquisition\b", r"\brecruiter\b", r"\bpeople operations\b",
    
    # Observation 02 & 03: Sales / Business Development
    r"\bsales\b", r"\baccount executive\b", r"\bbusiness development\b", r"\bbdr\b", r"\bsdr\b",
    
    # Observation 08: Chief of Staff (Excluding events agencies)
    r"\bchief of staff\b",
    
    # Observation 12: Security Operations
    r"\bsecurity\b", r"\bguard\b", r"\bcybersecurity\b",
    
    # Technical AV / Engineering Exclusions
    r"\btechnical director\b", r"\baudio engineer\b", r"\blighting designer\b", 
    r"\bav technician\b", r"\bbroadcast engineering\b", r"\bsoftware engineer\b", 
    r"\bfull[- ]stack\b", r"\bdevops\b", r"\bdata engineer\b", r"\bsolutions architect\b",
    
    # Non-Target Back-Office / Trades / Food
    r"\bdrayage\b", r"\bwarehouse\b", r"\bculinary\b", r"\brestaurant\b", 
    r"\bcatering sales\b", r"\bf&b\b", r"\bpharma\b", r"\bspeaker bureau\b",
    
    # High-Travel / Conventions Excluded
    r"\btour\b", r"\broadshow\b", r"\bsummit\b", r"\bconference\b", r"\bsymposium\b"
]

# LAYER 2: BODY TEXT DEALBREAKERS (Deep Description Scan across Observations 01-13)
BODY_DEALBREAKERS = [
    # Observation 01: B2B Enterprise SaaS & Demand Gen
    r"\bb2b (enterprise )?saas\b",
    r"\benterprise software\b",
    r"\bdemand generation\b",
    r"\bmarketing qualified leads?\b",
    r"\bmql(s)?\b",
    r"\bsales qualified leads?\b",
    r"\bsql(s)?\b",
    r"\bpipeline generation\b",

    # Observation 02: Quotas, Cold Calling & Sales Targets
    r"\bsales quotas?\b",
    r"\bclient acquisition quotas?\b",
    r"\bcold call(ing)?\b",
    r"\bcold outreach\b",
    r"\bcommission[- ]only\b",
    r"\bbooth sales\b",
    r"\bexhibit sales\b",
    r"\bsponsorship sales\b",
    r"\bticket sales\b",

    # Observation 05: Trade Shows & Drayage
    r"\btrade show booth\b",
    r"\bexhibit hall\b",
    r"\bdrayage\b",
    r"\bexhibitor freight\b",
    r"\bmodular exhibit\b",

    # Fintech
    r"\bfintech\b",
    r"\bfinancial technology\b",

    # Observation 10: Crypto & Web3
    r"\bcrypto\b",
    r"\bcryptocurrency\b",
    r"\bweb3\b",
    r"\bblockchain\b",
    r"\btokenomics\b",
    r"\bgambling\b",
    r"\bcasino\b",
    r"\bsports betting\b",

    # Observation 09: Culinary / Restaurant Floor Churn
    r"\bbanquet server\b",
    r"\bkitchen staff\b",
    r"\bmenu tastings?\b",
    r"\btable turns?\b",

    # Observation 11: Health, Pharma & Life Sciences
    r"\blife sciences\b",
    r"\bpharmaceutical\b",
    r"\bpharma\b",
    r"\bcme accreditation\b",
    r"\bmedical device\b",
    r"\bclinical symposium\b",
    r"\bkey opinion leaders?\b",
    r"\bkol speaker\b",

    # Observation 12: Security Operations Core
    r"\bsecurity officer\b",
    r"\bguard dispatch\b",
    r"\bphysical security operations\b",
    r"\baccess control badge\b",

    # Observation 13: Social Media Core Duties (Company names preserved)
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
# 2. STRING CLEANING & PARSING UTILITIES
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
        return f"${h_min:.0f}/hr -${h_max:.0f}/hr (Est. ${int(h_min*2080):,} -${int(h_max*2080):,}/yr)"

    # Match annual: $120,000 - $150,000
    annual_pat = r"\$([0-9]{2,3}(?:,[0-9]{3})+)(?:\s*(?:-|to)\s*\$([0-9]{2,3}(?:,[0-9]{3})+))?"
    a_match = re.search(annual_pat, text)
    if a_match:
        if a_match.group(2):
            return f"${a_match.group(1)} -${a_match
