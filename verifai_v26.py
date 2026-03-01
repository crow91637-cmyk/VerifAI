import os, re, math, time
from urllib.parse import quote_plus, urlparse
import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from flask import Flask, request, render_template_string, jsonify
import feedparser
from datetime import datetime, timedelta
import random
import logging
import concurrent.futures
import google.generativeai as genai
import html
import justext
import urllib3
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from collections import Counter
import string

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

#===== SETUP LOGGING ======
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#===== CONFIG ======
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyBQB2CFhSpXb7hcgFIhXMFBogg9nVcHxnE")

try:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-pro')
    GEMINI_AVAILABLE = True
except Exception:
    GEMINI_AVAILABLE = False
    print("Gemini API not configured. Using simple verification only.")

retry_strategy = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
http = requests.Session()
http.mount("https://", adapter)
http.mount("http://", adapter)

NEWS_SOURCES = {
    "Google News PH": {
        "type": "google",
        "url": "https://news.google.com/rss/search?q={q}+philippines&hl=en-PH&gl=PH&ceid=PH:en",
        "weight": 1.0,
        "threshold": 0.28,   # Google returns query-matched results; lower threshold ok
        "time_weight": 0.8,
        "max_results": 15,
        "timeout": 8,
        "category": "Aggregator"
    },
    "Philstar": {
        "type": "rss",
        "url": "https://www.philstar.com/rss/headlines",
        "weight": 1.0,
        "threshold": 0.30,   # Recalibrated: new scorer gives 0.5-0.9 for true matches
        "time_weight": 0.8,
        "domain": "philstar.com",
        "max_results": 15,
        "timeout": 6,
        "category": "National"
    },
    "Inquirer": {
        "type": "rss",
        "url": "https://newsinfo.inquirer.net/feed",
        "weight": 1.0,
        "threshold": 0.30,
        "time_weight": 0.75,
        "domain": "inquirer.net",
        "max_results": 15,
        "timeout": 6,
        "category": "National"
    },
    "Rappler": {
        "type": "rss",
        "url": "https://www.rappler.com/feed/",
        "weight": 1.0,
        "threshold": 0.30,
        "time_weight": 0.75,
        "domain": "rappler.com",
        "max_results": 12,
        "timeout": 5,
        "category": "National"
    },
    "ABS-CBN News": {
        "type": "rss",
        "url": "https://news.abs-cbn.com/rss/news",
        "weight": 1.0,
        "threshold": 0.30,
        "time_weight": 0.75,
        "domain": "abs-cbn.com",
        "max_results": 12,
        "timeout": 6,
        "category": "Broadcast"
    },
    "GMA News": {
        "type": "rss",
        "url": "https://data.gmanetwork.com/gno/rss/news/feed.xml",
        "weight": 1.0,
        "threshold": 0.30,
        "time_weight": 0.75,
        "domain": "gmanetwork.com",
        "max_results": 12,
        "timeout": 6,
        "category": "Broadcast"
    },
    "BusinessWorld": {
        "type": "rss",
        "url": "https://www.bworldonline.com/feed/",
        "weight": 1.0,
        "threshold": 0.28,
        "time_weight": 0.7,
        "domain": "bworldonline.com",
        "max_results": 10,
        "timeout": 6,
        "category": "Business"
    },
    "Philippine Daily Inquirer Business": {
        "type": "rss",
        "url": "https://business.inquirer.net/feed",
        "weight": 1.0,
        "threshold": 0.28,
        "time_weight": 0.7,
        "domain": "inquirer.net",
        "max_results": 10,
        "timeout": 6,
        "category": "Business"
    },
    "The Manila Times": {
        "type": "rss",
        "url": "https://www.manilatimes.net/feed",
        "weight": 1.0,
        "threshold": 0.30,
        "time_weight": 0.7,
        "domain": "manilatimes.net",
        "max_results": 10,
        "timeout": 6,
        "category": "National"
    },
    "SunStar": {
        "type": "rss",
        "url": "https://www.sunstar.com.ph/feed",
        "weight": 1.0,
        "threshold": 0.28,
        "time_weight": 0.7,
        "domain": "sunstar.com.ph",
        "max_results": 10,
        "timeout": 6,
        "category": "Regional"
    },
    "PNA (Philippine News Agency)": {
        "type": "rss",
        "url": "https://www.pna.gov.ph/rss/latest-news",
        "weight": 1.0,
        "threshold": 0.30,
        "time_weight": 0.8,
        "domain": "pna.gov.ph",
        "max_results": 12,
        "timeout": 6,
        "category": "Government/Official"
    },
    "Reuters Asia": {
        "type": "rss",
        "url": "https://feeds.reuters.com/reuters/topNews",
        "weight": 1.0,
        "threshold": 0.26,   # International: lower threshold, fewer PH stories
        "time_weight": 0.85,
        "domain": "reuters.com",
        "max_results": 10,
        "timeout": 7,
        "category": "International"
    },
    "BBC Asia": {
        "type": "rss",
        "url": "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
        "weight": 1.0,
        "threshold": 0.26,
        "time_weight": 0.85,
        "domain": "bbc.com",
        "max_results": 10,
        "timeout": 7,
        "category": "International"
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}

URL_CACHE = {}
CACHE_TIMEOUT = 300

# ===== OPTIMIZED NLP ENGINE =====
#
# ROOT CAUSES FIXED IN V26:
# 1. Jaccard penalizes short queries against long articles (3/9=0.33 for a perfect match)
#    -> Replaced with CONTAINMENT ratio: query terms found in article / total query terms
# 2. check_single_source was re-scoring on title only (discarding summary)
#    -> Now passes full_text through the pipeline end-to-end
# 3. Named entity extraction only found CamelCase (missed lowercase feed titles)
#    -> Now extracts entities from ORIGINAL text before lowercasing
# 4. No stemming: "arrested" != "arrest", "signs" != "sign"
#    -> Added lightweight suffix-stripping stemmer
# 5. F1 harmonic mean replaces raw Jaccard for precision+recall balance
# 6. PH-specific noise words ('philippines','manila','ph') added to stopwords
#    to prevent them from dominating cosine similarity

STOP_WORDS = {
    # English function words
    'the','a','an','and','or','but','in','on','at','to','for','of','with','is','are',
    'was','were','be','been','being','that','this','these','those','from','by','as',
    'it','its','he','she','they','we','you','i','me','him','her','us','them',
    'my','your','his','their','our','what','which','who','have','has','had',
    'do','does','did','will','would','could','should','may','might','must','shall',
    'not','no','nor','so','yet','both','either','neither','each','more','most',
    'other','such','than','too','very','just','over','under','after','before',
    'during','into','through','about','against','between','off','out','up','down',
    'again','further','then','once','here','there','when','where','why','how',
    'all','any','few','also','still','even','though','however','amid','within',
    'without','already','according','per','via','says','said','say','report',
    'reports','reported','sources','new','year','years','day','days','time',
    # PH-specific noise (appears in nearly every PH article = low IDF value)
    'philippines','philippine','manila','ph','ako','mga','ang','ng','sa','si',
    # Generic news boilerplate
    'breaking','watch','read','exclusive','update','latest','top','big',
}


# Known Philippine government/institution abbreviations for expansion
# Helps match "BSP" in article against "Bangko Sentral" in query and vice versa
PH_ABBREV_MAP = {
    'bsp': 'bangko sentral pilipinas',
    'doh': 'department health',
    'doj': 'department justice',
    'dole': 'department labor employment',
    'doe': 'department energy',
    'dot': 'department tourism',
    'dict': 'department information communications technology',
    'dilg': 'department interior local government',
    'dpwh': 'department public works highways',
    'deped': 'department education',
    'ched': 'commission higher education',
    'comelec': 'commission elections',
    'nbi': 'national bureau investigation',
    'pnp': 'philippine national police',
    'afp': 'armed forces philippines',
    'pdea': 'philippine drug enforcement agency',
    'pcgg': 'presidential commission good government',
    'ombudsman': 'office ombudsman',
    'icc': 'international criminal court',
    'imf': 'international monetary fund',
    'wfp': 'world food programme',
    'who': 'world health organization',
    'pagasa': 'philippine atmospheric geophysical astronomical services',
    'phivolcs': 'philippine institute volcanology seismology',
    'psei': 'philippine stock exchange index',
    'boi': 'board investments',
    'peza': 'philippine economic zone authority',
    'neda': 'national economic development authority',
    'dbm': 'department budget management',
    'bir': 'bureau internal revenue',
    'boc': 'bureau customs',
    'sss': 'social security system',
    'gsis': 'government service insurance system',
    'philhealth': 'philippine health insurance',
    'pagibig': 'home development mutual fund',
    'sc': 'supreme court',
    'ca': 'court appeals',
    'rdc': 'regional development council',
    'lgu': 'local government unit',
    'ncr': 'national capital region',
}


def expand_abbreviations(text: str) -> str:
    """Expand known PH abbreviations to their full forms for better matching."""
    words = text.split()
    expanded = []
    for w in words:
        lower = w.lower().rstrip('s')  # handle plural: BSPs -> bsp
        if lower in PH_ABBREV_MAP:
            expanded.append(w)
            expanded.append(PH_ABBREV_MAP[lower])  # add expansion alongside original
        else:
            expanded.append(w)
    return ' '.join(expanded)


def normalize_text(s: str) -> str:
    """Clean, expand abbreviations, and normalize text."""
    if not s:
        return ""
    s = re.sub(r'&\w+;', ' ', s)
    s = re.sub(r'<[^>]+>', '', s)
    # Expand abbreviations BEFORE lowercasing (need original case for some)
    s = expand_abbreviations(s)
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return ' '.join(w for w in s.split() if w not in STOP_WORDS and len(w) > 2)


def stem_light(word: str) -> str:
    """
    Lightweight suffix-stripping stemmer.
    Handles the most common English inflections without over-stemming.
    e.g. 'arrested' -> 'arrest', 'signing' -> 'sign', 'profits' -> 'profit'
    """
    suffixes = [
        ('ations', 4), ('tion', 4), ('tions', 4),
        ('ments', 4), ('ment', 4),
        ('ings', 3), ('ing', 3),
        ('ness', 4), ('less', 4),
        ('ers', 3), ('er', 3),
        ('est', 3), ('ies', 3),
        ('ied', 3), ('ied', 3),
        ('ly', 3), ('ed', 3),
        ('s', 3),   # plural/3rd person: only strip if root >= 3 chars
    ]
    for suffix, min_root in suffixes:
        if word.endswith(suffix) and len(word) - len(suffix) >= min_root:
            root = word[:-len(suffix)]
            # 'ies' -> 'y' e.g. 'companies' -> 'company'
            if suffix == 'ies':
                return root + 'y'
            return root
    return word


def get_tokens(text: str, stem: bool = True) -> list:
    """Normalize text and return stemmed tokens."""
    norm = normalize_text(text)
    words = norm.split()
    if stem:
        return [stem_light(w) for w in words]
    return words


def extract_named_entities(original_text: str) -> set:
    """
    Extract named entities from ORIGINAL (non-lowercased) text.
    Catches capitalized proper nouns, numbers, percentages, and abbreviations.
    Must be called on the original string before any lowercasing.
    """
    if not original_text:
        return set()
    # Capitalized words (proper nouns): 'Marcos', 'Duterte', 'Ayala'
    caps = set(re.findall(r'\b[A-Z][a-zA-Z]{2,}\b', original_text))
    # Multi-word proper nouns: 'Eastern Samar', 'Senate Bill'
    phrases = set(re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)+\b', original_text))
    # Numbers with context: percentages, amounts, years
    nums = set(re.findall(r'\b\d[\d,.]*(B|M|K|%|billion|million|thousand|percent)?\b', original_text, re.I))
    # All-caps abbreviations: 'ICC', 'PDEA', 'AFP', 'DOH'
    abbrevs = set(re.findall(r'\b[A-Z]{2,6}\b', original_text))
    # Remove common false positives
    noise = {'The', 'A', 'An', 'It', 'He', 'She', 'They', 'We', 'In', 'On',
             'At', 'By', 'To', 'Of', 'As', 'Is', 'Are', 'Was', 'For', 'But',
             'And', 'Or', 'If', 'So', 'My', 'No', 'UN', 'US', 'PH'}
    return (caps | phrases | nums | abbrevs) - noise


def extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace('www.', '')
    except:
        return ""


def is_same_domain(url1: str, url2: str) -> bool:
    d1, d2 = extract_domain(url1), extract_domain(url2)
    return bool(d1 and d2 and d1 == d2)


def advanced_sim(query: str, article: str) -> float:
    """
    Optimized multi-signal similarity engine (V26).

    Signals:
    1. Query Containment (30%): fraction of query terms found in article.
       Best signal for short-query vs long-article matching.
    2. F1 Containment (20%): harmonic mean of query→article and article→query
       containment. Balances recall and precision.
    3. Named Entity Score (28%): fraction of query entities found in article.
       Critical: same people/places/numbers = same story.
    4. Bigram Containment (12%): phrase-level matching.
       'Eastern Samar' matters more than {Eastern} + {Samar} separately.
    5. Sequence Match (10%): character-level similarity on normalized tokens.
       Catches near-verbatim headline rephrasing.
    """
    if not query or not article:
        return 0.0

    q_tokens = get_tokens(query)
    a_tokens = get_tokens(article)
    q_set = set(q_tokens)
    a_set = set(a_tokens)

    if not q_set or not a_set:
        return 0.0

    # --- Signal 1: Query Containment ---
    # How many of the query's unique terms appear in the article?
    # This is RECALL from the query's perspective and handles short queries well.
    containment_q = len(q_set & a_set) / len(q_set)

    # --- Signal 2: F1 Containment ---
    # Harmonic mean of query containment and reverse (article→query) containment.
    # Prevents a 2-word query from falsely matching an unrelated long article.
    containment_a = len(q_set & a_set) / max(len(a_set), 1)
    if containment_q + containment_a > 0:
        f1_contain = 2 * containment_q * containment_a / (containment_q + containment_a)
    else:
        f1_contain = 0.0

    # --- Signal 3: Named Entity Overlap ---
    # Extract entities from ORIGINAL strings (before lowercasing) to catch CamelCase.
    ents_q = extract_named_entities(query)
    ents_a = extract_named_entities(article)
    if ents_q:
        ent_score = len(ents_q & ents_a) / len(ents_q)  # Query-entity containment
    else:
        ent_score = containment_q  # Fall back to token containment

    # Boost: if query has entities and ALL of them match, this is very likely the same story
    if ents_q and len(ents_q & ents_a) == len(ents_q) and len(ents_q) >= 2:
        ent_score = min(1.0, ent_score * 1.15)

    # --- Signal 4: Bigram Containment ---
    q_bigrams = set(zip(q_tokens[:-1], q_tokens[1:])) if len(q_tokens) > 1 else set()
    a_bigrams = set(zip(a_tokens[:-1], a_tokens[1:])) if len(a_tokens) > 1 else set()
    if q_bigrams:
        bg_score = len(q_bigrams & a_bigrams) / len(q_bigrams)
    else:
        bg_score = containment_q

    # --- Signal 5: Sequence Match ---
    seq_score = SequenceMatcher(None, ' '.join(q_tokens), ' '.join(a_tokens)).ratio()

    # --- Weighted Combination ---
    final = (
        containment_q * 0.30 +
        f1_contain    * 0.20 +
        ent_score     * 0.28 +
        bg_score      * 0.12 +
        seq_score     * 0.10
    )

    return min(1.0, final)


# ===== CONTENT EXTRACTION =====

def extract_article_text(url: str) -> str:
    try:
        if url in URL_CACHE and time.time() - URL_CACHE[url].get('timestamp', 0) < CACHE_TIMEOUT:
            return URL_CACHE[url].get('content', '')
        response = http.get(url, timeout=8, headers=HEADERS, verify=False)
        paragraphs = justext.justext(response.content, justext.get_stoplist("English"))
        article_text = " ".join(p.text for p in paragraphs if not p.is_boilerplate)
        URL_CACHE[url] = {'content': article_text.strip(), 'timestamp': time.time()}
        return article_text.strip()
    except Exception as e:
        logger.error(f"Error extracting text from {url}: {e}")
        return ""


def text_from_url(url: str) -> str:
    try:
        if url in URL_CACHE and time.time() - URL_CACHE[url].get('timestamp', 0) < CACHE_TIMEOUT:
            return URL_CACHE[url].get('title', url)
        response = http.get(url, timeout=8, headers=HEADERS, verify=False)
        soup = BeautifulSoup(response.content, 'html.parser')
        title = None
        for selector in ['h1', 'meta[property="og:title"]', 'meta[name="twitter:title"]', 'title']:
            element = soup.select_one(selector)
            if element:
                title = element.get('content', '') if selector.startswith('meta') else element.get_text()
                if title and len(title.strip()) > 10:
                    break
        if not title or len(title.strip()) < 10:
            title = url
        URL_CACHE[url] = {'title': title.strip(), 'timestamp': time.time()}
        return title.strip()
    except Exception as e:
        logger.error(f"Error getting title from {url}: {e}")
        return url


# ===== SOURCE SEARCHING =====

def search_google_news(query, max_results=15):
    try:
        q = quote_plus(query)
        url = NEWS_SOURCES["Google News PH"]["url"].format(q=q)
        feed = feedparser.parse(url)
        return [(e.get('title', ''), e.get('link', ''), e.get('published', ''))
                for e in feed.entries[:max_results] if e.get('title') and e.get('link')]
    except Exception as e:
        logger.error(f"Error searching Google News: {e}")
        return []


def search_rss_feed(source_name, query, max_results=12):
    """
    Fetch RSS feed, score each entry against query using full text (title + summary),
    and return top results WITH their pre-computed scores and full_text.
    Returns list of (title, link, published, score, full_text).
    """
    try:
        source = NEWS_SOURCES[source_name]
        feed = feedparser.parse(source["url"])
        results = []
        for entry in feed.entries[:max_results * 3]:  # scan wider pool
            title = entry.get('title', '')
            link = entry.get('link', '')
            published = entry.get('published', '')
            if not title or not link:
                continue
            # Use summary/description to enrich matching context
            summary = entry.get('summary', '') or entry.get('description', '') or ''
            # Strip HTML from summary
            summary = re.sub(r'<[^>]+>', ' ', summary)
            full_text = f"{title} {summary}"
            score = advanced_sim(query, full_text)
            score = min(1.0, score * source.get("weight", 1.0))
            results.append((title, link, published, score, full_text))
        results.sort(key=lambda x: x[3], reverse=True)
        return results[:max_results]  # (title, link, published, score, full_text)
    except Exception as e:
        logger.error(f"Error searching {source_name} RSS: {e}")
        return []


def check_single_source(name, source_info, query_text, original_url):
    """
    Check a single source for query matches.
    Key fix: uses pre-computed scores from search_rss_feed (which scored on full_text)
    instead of re-scoring on title only (which was throwing away summary content).
    """
    best_title, best_url, best_score, best_published = None, None, 0.0, None
    best_content = ""
    try:
        max_results = source_info.get("max_results", 10)
        if source_info["type"] == "google":
            raw_candidates = search_google_news(query_text, max_results)
            # Google returns (title, link, published) - score on title only (no summary available)
            candidates = []
            for title, href, published in raw_candidates:
                score = advanced_sim(query_text, title)
                score = min(1.0, score * source_info.get("weight", 1.0))
                candidates.append((title, href, published, score, title))
        else:
            # RSS returns (title, link, published, score, full_text) - use pre-computed scores
            candidates = search_rss_feed(name, query_text, max_results)

        for candidate in candidates:
            title, href, published = candidate[0], candidate[1], candidate[2]
            score = candidate[3]  # use pre-computed score (full_text already included)
            full_text = candidate[4] if len(candidate) > 4 else title

            # Boost score if the article is from the same domain as the original URL
            if original_url and is_same_domain(original_url, href):
                score = min(1.0, score + 0.20)

            if score > best_score:
                best_title, best_url, best_score = title, href, score
                best_published, best_content = published, full_text

        threshold = source_info.get("threshold", 0.45)
        matched = best_score >= threshold

        if matched:
            note = f"Matched: {best_title[:60]}... (score: {round(best_score, 2)})"
        else:
            note = f"No close match (best score: {round(best_score, 2)})" if best_title else "No results found"

        search_url = source_info["url"] if source_info["type"] == "rss" else \
            NEWS_SOURCES["Google News PH"]["url"].format(q=quote_plus(query_text))

        return {
            "source": name,
            "category": source_info.get("category", "News"),
            "search_url": search_url,
            "matched": matched,
            "note": note,
            "match_url": best_url,
            "match_title": best_title,
            "score": round(best_score, 2) if best_score else 0,
            "published": best_published
        }
    except Exception as e:
        logger.error(f"Error checking source {name}: {e}")
        return {
            "source": name,
            "category": source_info.get("category", "News"),
            "search_url": source_info["url"],
            "matched": False,
            "note": "Error: " + str(e)[:50],
            "match_url": None,
            "match_title": None,
            "score": 0,
            "published": None
        }


def check_sources(query_text: str, original_url=None):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(check_single_source, name, info, query_text, original_url): name
            for name, info in NEWS_SOURCES.items()
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result(timeout=10)
                results.append(result)
            except concurrent.futures.TimeoutError:
                name = futures[future]
                results.append({
                    "source": name,
                    "category": NEWS_SOURCES[name].get("category", "News"),
                    "search_url": "",
                    "matched": False,
                    "note": "Timeout",
                    "match_url": None,
                    "match_title": None,
                    "score": 0,
                    "published": None
                })
    return results


# ===== AI ANALYSIS =====

def ai_overall_analysis(query_text, verification_results):
    """
    Generate a detailed overall analysis explaining WHY the news was flagged
    as credible or not credible, using Gemini AI.
    """
    if not GEMINI_AVAILABLE:
        return generate_fallback_analysis(query_text, verification_results)

    try:
        matches = [c for c in verification_results if c["matched"]]
        non_matches = [c for c in verification_results if not c["matched"]]
        match_count = len(matches)
        total = len(verification_results)

        matched_sources = ", ".join([m["source"] for m in matches]) if matches else "None"
        top_scores = sorted(verification_results, key=lambda x: x["score"], reverse=True)[:3]
        top_score_info = "; ".join([f"{s['source']} ({s['score']})" for s in top_scores])

        verdict = simple_verdict(query_text, verification_results)

        prompt = f"""
You are a professional fact-checker and media analyst for the Philippines.

A user submitted this news claim for verification:
"{query_text}"

VERIFICATION RESULTS:
- Total sources checked: {total}
- Sources that MATCHED: {match_count} ({matched_sources})
- Sources that did NOT match: {total - match_count}
- Overall Verdict: {verdict}
- Top scoring sources: {top_score_info}

Write a detailed, objective analysis (4-6 sentences) that:
1. Clearly states WHY this news is considered {verdict}
2. Explains which sources confirmed or denied it and what that means
3. Points out any red flags or credibility indicators
4. Gives actionable advice on what the reader should do next
5. Mentions if this could be satire, misinformation, or a legitimate story

Be specific, reference the sources by name where relevant, and use plain language for Filipino readers.
Do NOT use bullet points — write in flowing paragraph form.
"""
        response = gemini_model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini API error in overall analysis: {e}")
        return generate_fallback_analysis(query_text, verification_results)


def generate_fallback_analysis(query_text, verification_results):
    """Rule-based analysis when Gemini is unavailable."""
    matches = [c for c in verification_results if c["matched"]]
    match_count = len(matches)
    total = len(verification_results)
    verdict = simple_verdict(query_text, verification_results)

    matched_names = [m["source"] for m in matches]
    has_official = any("PNA" in m or "Government" in m for m in matched_names)
    has_intl = any(m in ["Reuters Asia", "BBC Asia"] for m in matched_names)
    has_broadcast = any(m in ["ABS-CBN News", "GMA News"] for m in matched_names)

    if match_count == 0:
        return (f"This news claim could not be verified across any of the {total} sources checked, "
                f"which includes major Philippine outlets like Rappler, Inquirer, Philstar, ABS-CBN, and GMA News, "
                f"as well as international sources like Reuters and BBC. "
                f"The absence of any corroborating coverage is a significant red flag — legitimate major news stories "
                f"are typically reported by multiple outlets. "
                f"This could indicate the story is fabricated, highly localized with no wider coverage, "
                f"or may be circulating only on social media without journalistic verification. "
                f"We strongly recommend searching directly on trusted news websites before sharing this content.")
    elif match_count == 1:
        source = matched_names[0]
        return (f"This news was found on only one source out of {total} checked: {source}. "
                f"Single-source stories require extra scrutiny — while the story may be legitimate, "
                f"the lack of corroboration from other major outlets means it has not been independently verified. "
                f"This could be an exclusive report, a very recent story that hasn't been picked up yet, "
                f"or content that other outlets have not found credible enough to publish. "
                f"Read the full article at the source and check for proper attribution, named sources, and publication date "
                f"before drawing conclusions or sharing.")
    elif match_count >= 2:
        sources_str = " and ".join(matched_names[:3])
        credibility_notes = []
        if has_official:
            credibility_notes.append("including an official government source (PNA)")
        if has_intl:
            credibility_notes.append("with international corroboration from Reuters or BBC")
        if has_broadcast:
            credibility_notes.append("confirmed by major broadcast networks")
        extra = (", " + "; ".join(credibility_notes)) if credibility_notes else ""
        return (f"This news story was corroborated by {match_count} out of {total} sources checked{extra}. "
                f"Coverage was found on {sources_str}. "
                f"Multiple independent outlets reporting the same story is a strong indicator of credibility, "
                f"as it suggests the story has cleared editorial standards across different newsrooms. "
                f"While this does not guarantee 100% accuracy of every detail, "
                f"the level of corroboration suggests this is a real, reported news event. "
                f"Always read the full articles and check the original sources for the most complete and accurate information.")


def generate_fake_news_tips(query_text, verification_results):
    matches = [c for c in verification_results if c["matched"]]
    match_count = len(matches)

    tips = []
    if match_count == 0:
        tips.append("⚠️ Zero corroboration — be very cautious before sharing this story")
        tips.append("🔍 Try searching the exact headline on Google News directly")
        tips.append("📵 Check if this is circulating only on Facebook or Messenger groups")
    elif match_count == 1:
        tips.append("🔍 Single source — look for additional coverage before sharing")
        tips.append("📅 Check the article's publication date — it may be an old story resurfacing")
        tips.append("🖼️ Verify any images or videos attached to this story using reverse image search")
    else:
        tips.append("✅ Multiple sources confirm this story — generally safe to trust")
        tips.append("📖 Still read the full article for complete context and nuance")
        tips.append("🔗 Check linked sources and named experts for additional credibility")

    return tips[:3]


def simple_verdict(query_text: str, checks: list, original_url=None) -> str:
    """
    Verdict based on weighted match count considering source authority.
    Government/Official and International sources carry more weight.
    """
    matches = [c for c in checks if c["matched"]]
    match_count = len(matches)

    if match_count == 0:
        return "NOT VERIFIED"

    avg_score = sum(c["score"] for c in matches) / match_count

    CAT_WEIGHT = {
        "Government/Official": 1.5,
        "International": 1.4,
        "Broadcast": 1.2,
        "National": 1.1,
        "Aggregator": 1.0,
        "Business": 0.9,
        "Regional": 0.8,
    }
    weighted_matches = sum(CAT_WEIGHT.get(c.get("category", "National"), 1.0) for c in matches)
    has_official = any(c.get("category") == "Government/Official" for c in matches)
    has_intl = any(c.get("category") == "International" for c in matches)

    if weighted_matches >= 3.5 or (match_count >= 3 and (has_official or has_intl)):
        return "HIGHLY CREDIBLE"
    elif weighted_matches >= 2.2 or match_count >= 3:
        return "HIGHLY CREDIBLE"
    elif weighted_matches >= 1.5 or match_count == 2:
        return "CREDIBLE"
    elif match_count == 1 and avg_score >= 0.50:
        return "PARTIALLY VERIFIED"
    elif match_count == 1:
        return "WEAKLY VERIFIED"
    else:
        return "NOT VERIFIED"


# ====== FLASK APP ======
app = Flask(__name__)

HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>VerifAI — Philippine Fact Checker</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #04080f;
  --surface: #080f1a;
  --surface2: #0d1825;
  --accent: #00d4ff;
  --accent2: #0066ff;
  --green: #00e676;
  --red: #ff1744;
  --yellow: #ffea00;
  --orange: #ff6d00;
  --text: #e8f4fd;
  --muted: #5a7a94;
  --border: rgba(0,212,255,0.12);
  --glow: 0 0 20px rgba(0,212,255,0.15);
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'DM Sans', sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  overflow-x: hidden;
}

/* Animated background grid */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background-image:
    linear-gradient(rgba(0,212,255,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,212,255,0.03) 1px, transparent 1px);
  background-size: 50px 50px;
  pointer-events: none;
  z-index: 0;
}

body::after {
  content: '';
  position: fixed;
  top: -20%;
  left: -10%;
  width: 60%;
  height: 60%;
  background: radial-gradient(ellipse, rgba(0,102,255,0.06) 0%, transparent 70%);
  pointer-events: none;
  z-index: 0;
}

.wrap {
  position: relative;
  z-index: 1;
  max-width: 1100px;
  margin: 0 auto;
  padding: 20px 20px 60px;
}

/* Header */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 28px 0 32px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 36px;
}

.logo {
  display: flex;
  align-items: center;
  gap: 14px;
}

.logo-icon {
  width: 46px;
  height: 46px;
  background: linear-gradient(135deg, var(--accent2), var(--accent));
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  box-shadow: 0 0 24px rgba(0,212,255,0.3);
}

.logo-text h1 {
  font-family: 'Syne', sans-serif;
  font-size: 26px;
  font-weight: 800;
  background: linear-gradient(90deg, var(--accent), #fff 60%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  line-height: 1;
}

.logo-text p {
  font-size: 12px;
  color: var(--muted);
  margin-top: 3px;
  letter-spacing: 0.5px;
}

.badge {
  background: linear-gradient(90deg, rgba(0,212,255,0.15), rgba(0,102,255,0.15));
  border: 1px solid rgba(0,212,255,0.25);
  color: var(--accent);
  padding: 5px 12px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

/* Input card */
.input-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 32px;
  box-shadow: var(--glow);
  margin-bottom: 28px;
}

.input-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 20px;
}

@media(max-width:700px){ .input-grid { grid-template-columns: 1fr; } }

.field label {
  display: block;
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 8px;
}

.field input, .field textarea {
  width: 100%;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 12px;
  color: var(--text);
  padding: 14px 16px;
  font-family: 'DM Sans', sans-serif;
  font-size: 14px;
  transition: border-color 0.2s, box-shadow 0.2s;
  outline: none;
}

.field input:focus, .field textarea:focus {
  border-color: rgba(0,212,255,0.4);
  box-shadow: 0 0 0 3px rgba(0,212,255,0.08);
}

.field textarea { min-height: 100px; resize: vertical; }

.action-row {
  display: flex;
  align-items: center;
  gap: 14px;
}

.btn-verify {
  background: linear-gradient(90deg, var(--accent2), var(--accent));
  color: #04080f;
  border: none;
  padding: 13px 28px;
  border-radius: 12px;
  font-family: 'Syne', sans-serif;
  font-weight: 700;
  font-size: 15px;
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s;
  letter-spacing: 0.3px;
}

.btn-verify:hover { transform: translateY(-1px); box-shadow: 0 8px 24px rgba(0,102,255,0.35); }
.btn-verify:active { transform: translateY(0); }
.btn-verify:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

.status-pill {
  background: var(--surface2);
  border: 1px solid var(--border);
  color: var(--muted);
  padding: 8px 16px;
  border-radius: 999px;
  font-size: 12px;
}

/* Loader */
.loader-card {
  display: none;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 28px 32px;
  margin-bottom: 28px;
}

.loader-inner {
  display: flex;
  align-items: center;
  gap: 18px;
}

.spin-ring {
  width: 38px;
  height: 38px;
  border-radius: 50%;
  border: 3px solid rgba(0,212,255,0.15);
  border-top-color: var(--accent);
  animation: spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes spin { to { transform: rotate(360deg); } }

.loader-text strong { font-family: 'Syne', sans-serif; font-size: 16px; font-weight: 700; }
.loader-text p { color: var(--muted); font-size: 13px; margin-top: 3px; }

.scan-bar {
  margin-top: 16px;
  height: 3px;
  background: var(--surface2);
  border-radius: 999px;
  overflow: hidden;
}

.scan-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent2), var(--accent));
  border-radius: 999px;
  animation: scan 2s ease-in-out infinite;
  transform-origin: left;
}

@keyframes scan {
  0% { width: 0%; opacity: 1; }
  60% { width: 85%; opacity: 1; }
  100% { width: 100%; opacity: 0; }
}

/* Results */
#resultSection { display: none; }

.verdict-banner {
  border-radius: 20px;
  padding: 28px 32px;
  margin-bottom: 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
}

.verdict-banner.highly-credible {
  background: linear-gradient(135deg, rgba(0,230,118,0.1), rgba(0,212,255,0.08));
  border: 1px solid rgba(0,230,118,0.25);
}
.verdict-banner.credible {
  background: linear-gradient(135deg, rgba(0,230,118,0.07), rgba(0,102,255,0.07));
  border: 1px solid rgba(0,230,118,0.18);
}
.verdict-banner.partially {
  background: linear-gradient(135deg, rgba(255,234,0,0.07), rgba(255,109,0,0.07));
  border: 1px solid rgba(255,234,0,0.2);
}
.verdict-banner.weak {
  background: linear-gradient(135deg, rgba(255,109,0,0.07), rgba(255,23,68,0.07));
  border: 1px solid rgba(255,109,0,0.2);
}
.verdict-banner.unverified {
  background: linear-gradient(135deg, rgba(255,23,68,0.1), rgba(80,0,20,0.1));
  border: 1px solid rgba(255,23,68,0.25);
}

.verdict-left { flex: 1; min-width: 220px; }
.verdict-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 6px;
}
.verdict-text {
  font-family: 'Syne', sans-serif;
  font-size: 30px;
  font-weight: 800;
  line-height: 1;
}
.verdict-sub { font-size: 14px; color: var(--muted); margin-top: 8px; }

.verdict-meter { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
.meter-track {
  width: 180px;
  height: 8px;
  background: rgba(255,255,255,0.06);
  border-radius: 999px;
  overflow: hidden;
}
.meter-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.8s ease;
}
.meter-label { font-size: 12px; color: var(--muted); }

/* Two-column layout */
.results-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 24px;
}

@media(max-width:800px){ .results-grid { grid-template-columns: 1fr; } }

.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 18px;
  padding: 24px;
}

.panel-title {
  font-family: 'Syne', sans-serif;
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  color: var(--accent);
  margin-bottom: 18px;
  display: flex;
  align-items: center;
  gap: 8px;
}

/* Source list */
.source-item {
  background: var(--surface2);
  border: 1px solid rgba(255,255,255,0.04);
  border-radius: 12px;
  padding: 12px 14px;
  margin-bottom: 8px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  transition: border-color 0.2s;
}

.source-item:hover { border-color: var(--border); }

.source-item.matched { border-left: 3px solid var(--green); }
.source-item.unmatched { border-left: 3px solid rgba(255,255,255,0.06); }

.source-info { flex: 1; min-width: 0; }
.source-name { font-size: 13px; font-weight: 600; margin-bottom: 2px; }
.source-cat {
  font-size: 10px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  margin-bottom: 4px;
}
.source-note { font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 240px; }
.source-links { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
.source-link {
  font-size: 10px;
  color: var(--accent);
  text-decoration: none;
  background: rgba(0,212,255,0.08);
  padding: 2px 8px;
  border-radius: 999px;
  white-space: nowrap;
}
.source-link:hover { background: rgba(0,212,255,0.15); }

.source-status { flex-shrink: 0; font-size: 18px; }

.score-chip {
  display: inline-flex;
  align-items: center;
  font-size: 10px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 6px;
  margin-left: 6px;
}
.score-chip.high { background: rgba(0,230,118,0.15); color: var(--green); }
.score-chip.mid  { background: rgba(255,234,0,0.15); color: var(--yellow); }
.score-chip.low  { background: rgba(255,23,68,0.12); color: #ff6b87; }

/* Analysis panel */
.analysis-section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 18px;
  padding: 28px 32px;
  margin-bottom: 24px;
}

.analysis-text {
  font-size: 15px;
  line-height: 1.75;
  color: #c8dce8;
}

/* Tips */
.tips-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
@media(max-width:700px){ .tips-grid { grid-template-columns: 1fr; } }

.tip-card {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 16px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--text);
}

/* Query display */
.query-display {
  font-size: 13px;
  color: var(--muted);
  margin-bottom: 20px;
  padding: 10px 16px;
  background: var(--surface2);
  border-radius: 10px;
  border-left: 3px solid var(--accent2);
  font-style: italic;
}

/* Tabs for sources */
.tab-row {
  display: flex;
  gap: 6px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.tab {
  background: var(--surface2);
  border: 1px solid var(--border);
  color: var(--muted);
  padding: 5px 12px;
  border-radius: 8px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
  letter-spacing: 0.3px;
}

.tab.active {
  background: rgba(0,212,255,0.12);
  border-color: rgba(0,212,255,0.3);
  color: var(--accent);
}

/* Disclaimer Modal */
.modal {
  display: flex;
  position: fixed;
  inset: 0;
  z-index: 9999;
  background: rgba(4,8,15,0.9);
  backdrop-filter: blur(8px);
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.modal-box {
  background: var(--surface);
  border: 1px solid rgba(0,212,255,0.2);
  border-radius: 24px;
  padding: 40px;
  max-width: 580px;
  width: 100%;
  box-shadow: 0 0 60px rgba(0,102,255,0.15);
  animation: modalIn 0.3s ease;
}

@keyframes modalIn {
  from { transform: translateY(20px); opacity: 0; }
  to   { transform: translateY(0); opacity: 1; }
}

.modal-icon { font-size: 40px; margin-bottom: 16px; text-align: center; }
.modal-box h2 {
  font-family: 'Syne', sans-serif;
  font-size: 22px;
  font-weight: 800;
  text-align: center;
  margin-bottom: 14px;
}
.modal-box p { font-size: 14px; color: #a0b8c8; line-height: 1.65; margin-bottom: 16px; }
.modal-list { list-style: none; margin-bottom: 20px; }
.modal-list li {
  font-size: 13px;
  color: #a0b8c8;
  padding: 7px 0 7px 24px;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  position: relative;
}
.modal-list li::before { content: '›'; position: absolute; left: 6px; color: var(--accent); font-weight: 700; }

.proceed-btn {
  width: 100%;
  background: linear-gradient(90deg, var(--accent2), var(--accent));
  color: #04080f;
  border: none;
  padding: 14px;
  border-radius: 12px;
  font-family: 'Syne', sans-serif;
  font-weight: 800;
  font-size: 15px;
  cursor: pointer;
  transition: transform 0.15s;
}
.proceed-btn:hover { transform: translateY(-1px); }

.blur-main { filter: blur(4px); pointer-events: none; user-select: none; }

/* Footer */
footer {
  text-align: center;
  color: var(--muted);
  font-size: 12px;
  margin-top: 48px;
  padding-top: 24px;
  border-top: 1px solid var(--border);
}
footer button {
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  text-decoration: underline;
  font-size: 12px;
}
</style>
</head>
<body>

<!-- Disclaimer Modal -->
<div id="disclaimerModal" class="modal">
  <div class="modal-box">
    <div class="modal-icon">🔍</div>
    <h2>Before You Use VerifAI</h2>
    <p>VerifAI is an automated AI-powered fact-checking tool that cross-references news claims against trusted Philippine and international news sources. Please read the following before proceeding:</p>
    <ul class="modal-list">
      <li>Results are generated automatically and may contain inaccuracies</li>
      <li>This tool is not a substitute for professional fact-checkers or journalists</li>
      <li>Always verify critical information through multiple authoritative sources</li>
      <li>AI analysis reflects patterns in available data, not absolute truth</li>
      <li>Use VerifAI as a supplementary tool, not your sole source of judgment</li>
    </ul>
    <button class="proceed-btn" onclick="closeDisclaimer()">I Understand — Let's Verify</button>
  </div>
</div>

<div class="wrap" id="mainContent">
  <!-- Header -->
  <div class="header">
    <div class="logo">
      <div class="logo-icon">🛡</div>
      <div class="logo-text">
        <h1>VerifAI</h1>
        <p>Philippine AI Fact Checker • v2.5</p>
      </div>
    </div>
    <div class="badge">Gemini AI + 13 Sources</div>
  </div>

  <!-- Input -->
  <div class="input-card">
    <div class="input-grid">
      <div class="field">
        <label>News Link (optional)</label>
        <input id="link" type="text" placeholder="https://www.rappler.com/...">
      </div>
      <div class="field">
        <label>Headline or Text</label>
        <textarea id="headline" placeholder="Paste or type the news headline or claim here..."></textarea>
      </div>
    </div>
    <div class="action-row">
      <button class="btn-verify" id="goBtn" onclick="submitForm()">Verify Now</button>
      <div class="status-pill" id="statusPill">Ready to verify</div>
    </div>
  </div>

  <!-- Loading -->
  <div class="loader-card" id="loaderCard">
    <div class="loader-inner">
      <div class="spin-ring"></div>
      <div class="loader-text">
        <strong>Scanning sources...</strong>
        <p id="loaderSub">Checking 13 trusted news outlets simultaneously</p>
      </div>
    </div>
    <div class="scan-bar"><div class="scan-fill"></div></div>
  </div>

  <!-- Results -->
  <div id="resultSection">
    <div class="query-display" id="queryDisplay"></div>

    <!-- Verdict Banner -->
    <div class="verdict-banner" id="verdictBanner">
      <div class="verdict-left">
        <div class="verdict-label">Verification Result</div>
        <div class="verdict-text" id="verdictText"></div>
        <div class="verdict-sub" id="verdictSub"></div>
      </div>
      <div class="verdict-meter">
        <div class="meter-track">
          <div class="meter-fill" id="meterFill" style="width:0%"></div>
        </div>
        <div class="meter-label" id="meterLabel"></div>
      </div>
    </div>

    <!-- Sources + Stats Grid -->
    <div class="results-grid">
      <!-- Sources Panel -->
      <div class="panel">
        <div class="panel-title">📡 Sources Checked</div>
        <div class="tab-row" id="tabRow">
          <div class="tab active" onclick="filterSources('all', this)">All</div>
          <div class="tab" onclick="filterSources('matched', this)">✓ Matched</div>
          <div class="tab" onclick="filterSources('unmatched', this)">✗ Not Found</div>
        </div>
        <div id="sourcesList"></div>
      </div>

      <!-- Stats Panel -->
      <div class="panel">
        <div class="panel-title">📊 Match Overview</div>
        <div id="statsContent"></div>

        <div class="panel-title" style="margin-top:24px">💡 Tips</div>
        <div id="tipsList" class="tips-grid"></div>
      </div>
    </div>

    <!-- Overall Analysis -->
    <div class="analysis-section">
      <div class="panel-title" style="margin-bottom:14px">🤖 AI Analysis — Why This Verdict?</div>
      <div class="analysis-text" id="analysisText"></div>
    </div>
  </div>

  <footer>
    VerifAI v2.5 • Powered by Google Gemini AI • 13 Philippine & International Sources
    <br><br>
    <button onclick="openDisclaimer()">View Disclaimer</button>
  </footer>
</div>

<script>
let allChecks = [];

function openDisclaimer() {
  document.getElementById('disclaimerModal').style.display = 'flex';
  document.getElementById('mainContent').classList.add('blur-main');
}
function closeDisclaimer() {
  document.getElementById('disclaimerModal').style.display = 'none';
  document.getElementById('mainContent').classList.remove('blur-main');
}
window.onload = () => openDisclaimer();

function getVerdictClass(v) {
  if (v === 'HIGHLY CREDIBLE') return 'highly-credible';
  if (v === 'CREDIBLE') return 'credible';
  if (v === 'PARTIALLY VERIFIED') return 'partially';
  if (v === 'WEAKLY VERIFIED') return 'weak';
  return 'unverified';
}

function getVerdictColor(v) {
  if (v === 'HIGHLY CREDIBLE') return '#00e676';
  if (v === 'CREDIBLE') return '#69f0ae';
  if (v === 'PARTIALLY VERIFIED') return '#ffea00';
  if (v === 'WEAKLY VERIFIED') return '#ff6d00';
  return '#ff1744';
}

function getMeterColor(pct) {
  if (pct >= 60) return 'linear-gradient(90deg, #00e676, #00d4ff)';
  if (pct >= 30) return 'linear-gradient(90deg, #ffea00, #ff6d00)';
  return 'linear-gradient(90deg, #ff1744, #ff6d00)';
}

function getScoreClass(score) {
  if (score >= 0.6) return 'high';
  if (score >= 0.4) return 'mid';
  return 'low';
}

function filterSources(type, tabEl) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  tabEl.classList.add('active');
  document.querySelectorAll('.source-item').forEach(el => {
    if (type === 'all') el.style.display = '';
    else if (type === 'matched') el.style.display = el.dataset.matched === 'true' ? '' : 'none';
    else el.style.display = el.dataset.matched === 'false' ? '' : 'none';
  });
}

function buildSources(checks) {
  const list = document.getElementById('sourcesList');
  list.innerHTML = '';
  checks.forEach(c => {
    const div = document.createElement('div');
    div.className = `source-item ${c.matched ? 'matched' : 'unmatched'}`;
    div.dataset.matched = c.matched;
    const sc = c.score || 0;
    const scClass = getScoreClass(sc);
    div.innerHTML = `
      <div class="source-info">
        <div class="source-name">${c.source}<span class="score-chip ${scClass}">${sc.toFixed(2)}</span></div>
        <div class="source-cat">${c.category || ''}</div>
        <div class="source-note">${c.note || ''}</div>
        <div class="source-links">
          ${c.match_url ? `<a class="source-link" href="${c.match_url}" target="_blank">View Match</a>` : ''}
          ${c.search_url ? `<a class="source-link" href="${c.search_url}" target="_blank">Search Feed</a>` : ''}
        </div>
      </div>
      <div class="source-status">${c.matched ? '✅' : '⬜'}</div>
    `;
    list.appendChild(div);
  });
}

function buildStats(data) {
  const pct = data.total ? Math.round((data.matches / data.total) * 100) : 0;
  const color = getMeterColor(pct);
  document.getElementById('statsContent').innerHTML = `
    <div style="margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:13px;color:var(--muted)">Source Coverage</span>
        <span style="font-size:22px;font-family:'Syne',sans-serif;font-weight:800">${pct}%</span>
      </div>
      <div style="height:8px;background:rgba(255,255,255,0.06);border-radius:999px;overflow:hidden">
        <div style="height:100%;width:${pct}%;background:${color};border-radius:999px;transition:width 0.8s ease"></div>
      </div>
      <div style="font-size:12px;color:var(--muted);margin-top:6px">${data.matches} of ${data.total} sources matched</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px">
      <div style="background:var(--surface2);border-radius:12px;padding:14px;text-align:center">
        <div style="font-size:28px;font-family:'Syne',sans-serif;font-weight:800;color:var(--green)">${data.matches}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">Matched</div>
      </div>
      <div style="background:var(--surface2);border-radius:12px;padding:14px;text-align:center">
        <div style="font-size:28px;font-family:'Syne',sans-serif;font-weight:800;color:var(--muted)">${data.total - data.matches}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">Not Found</div>
      </div>
    </div>
  `;
}

function buildTips(tips) {
  const el = document.getElementById('tipsList');
  el.innerHTML = '';
  tips.forEach(tip => {
    const d = document.createElement('div');
    d.className = 'tip-card';
    d.innerHTML = tip;
    el.appendChild(d);
  });
}

async function submitForm() {
  const link = document.getElementById('link').value.trim();
  const headline = document.getElementById('headline').value.trim();
  if (!link && !headline) { alert('Please enter a news link or headline.'); return; }

  // Reset UI
  document.getElementById('resultSection').style.display = 'none';
  document.getElementById('loaderCard').style.display = 'block';
  document.getElementById('goBtn').disabled = true;
  document.getElementById('statusPill').textContent = 'Verifying...';

  const loaderMessages = [
    'Scanning Google News Philippines...',
    'Checking Rappler, Inquirer, Philstar...',
    'Verifying with GMA News & ABS-CBN...',
    'Cross-referencing Reuters & BBC Asia...',
    'Running AI analysis...'
  ];
  let msgIdx = 0;
  const msgInterval = setInterval(() => {
    document.getElementById('loaderSub').textContent = loaderMessages[msgIdx % loaderMessages.length];
    msgIdx++;
  }, 1800);

  try {
    const t0 = performance.now();
    const res = await fetch('/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link, headline })
    });
    const t1 = performance.now();
    clearInterval(msgInterval);
    document.getElementById('loaderCard').style.display = 'none';
    document.getElementById('goBtn').disabled = false;

    if (!res.ok) { alert('Server error: ' + res.statusText); return; }

    const data = await res.json();
    allChecks = data.checks;

    // Status pill
    document.getElementById('statusPill').textContent = `Done in ${data.time}s`;

    // Query
    document.getElementById('queryDisplay').textContent = 'Checked: "' + data.query + '"';

    // Verdict Banner
    const banner = document.getElementById('verdictBanner');
    const vc = getVerdictClass(data.verdict_short);
    banner.className = 'verdict-banner ' + vc;
    document.getElementById('verdictText').style.color = getVerdictColor(data.verdict_short);
    document.getElementById('verdictText').textContent = data.verdict_short;
    document.getElementById('verdictSub').textContent = data.explanation_short;

    const pct = data.total ? Math.round((data.matches / data.total) * 100) : 0;
    const meterFill = document.getElementById('meterFill');
    meterFill.style.background = getMeterColor(pct);
    setTimeout(() => { meterFill.style.width = pct + '%'; }, 100);
    document.getElementById('meterLabel').textContent = pct + '% source coverage';

    // Sources
    buildSources(data.checks);

    // Stats
    buildStats(data);

    // Tips
    buildTips(data.tips);

    // Analysis
    document.getElementById('analysisText').textContent = data.analysis || 'Analysis not available.';

    document.getElementById('resultSection').style.display = 'block';
    document.getElementById('resultSection').scrollIntoView({ behavior: 'smooth', block: 'start' });

  } catch (err) {
    clearInterval(msgInterval);
    document.getElementById('loaderCard').style.display = 'none';
    document.getElementById('goBtn').disabled = false;
    alert('Error: ' + err.message);
  }
}
</script>
</body>
</html>
"""


@app.route("/", methods=['GET'])
def home():
    return render_template_string(HTML)


@app.route("/verify", methods=['POST'])
def verify():
    t0 = time.time()
    body = request.get_json(force=True)
    link = (body.get("link") or "").strip()
    headline = (body.get("headline") or "").strip()

    if link and not headline:
        query = text_from_url(link)
    else:
        query = headline or link

    if not query:
        return jsonify({"error": "No input"}), 400

    # 1. Cross-check sources
    checks = check_sources(query, original_url=link)
    matches = sum(1 for c in checks if c["matched"])
    total = len(checks)

    # 2. Verdict
    verdict_text = simple_verdict(query, checks, original_url=link)
    explanation = f"Found {matches} matching sources out of {total} checked."

    # 3. Overall AI analysis
    analysis = ai_overall_analysis(query, checks)

    # 4. Tips
    tips = generate_fake_news_tips(query, checks)

    return jsonify({
        "query": query,
        "checks": checks,
        "matches": matches,
        "total": total,
        "verdict_short": verdict_text,
        "explanation_short": explanation,
        "analysis": analysis,
        "tips": tips,
        "time": round(time.time() - t0, 2),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
