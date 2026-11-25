"""DuckDuckGo HTML Search Parser and Web Content Extractor
Parses search result sets, detects automated bot challenges, and converts HTML web pages
into clean, structured text representations.
"""

import re
import logging
from urllib.parse import urlparse, parse_qs, unquote
from typing import List, Tuple, Optional
from bs4 import BeautifulSoup

from runtime.search.models import SearchResult

logger = logging.getLogger("presence.search.parser")

CHALLENGE_SIGNATURES = [
    "anomaly-modal",
    "challenge-form",
    "cf-turnstile",
    "recaptcha",
    "hcaptcha",
    "please solve this challenge",
    "automated requests",
    "bots use duckduckgo",
    "unusual traffic from your computer",
    "security verification",
    "access denied",
    "distil_identification",
]

EMPTY_RESULT_SIGNATURES = [
    "no results found",
    "no results for",
    "not found",
    "class=\"no-results\"",
    "zero_click_results",
]


class DuckDuckGoParser:
    """Parses DuckDuckGo HTML results and detects bot challenge barriers."""

    @staticmethod
    def detect_challenge(html_text: str, status_code: int = 200) -> Tuple[bool, str]:
        """Detects if response is a bot challenge or access block."""
        if status_code in (403, 429):
            return True, f"HTTP {status_code} Access Block"

        lower = html_text.lower()
        for sig in CHALLENGE_SIGNATURES:
            if sig in lower:
                return True, f"Challenge signature matched: '{sig}'"

        return False, ""

    @staticmethod
    def detect_empty_results(html_text: str) -> bool:
        """Detects if search returned zero results."""
        lower = html_text.lower()
        for sig in EMPTY_RESULT_SIGNATURES:
            if sig in lower:
                return True
        return False

    @staticmethod
    def parse_results(html_text: str, max_results: int = 5) -> List[SearchResult]:
        """Parses HTML into normalized SearchResult objects."""
        results: List[SearchResult] = []
        soup = BeautifulSoup(html_text, "html.parser")

        # DuckDuckGo HTML search results are enclosed in .result or .web-result divs
        result_divs = soup.find_all("div", class_=re.compile(r"result|web-result"))

        for div in result_divs:
            # Skip promotional banners or ads
            if "result--ad" in div.get("class", []):
                continue

            # Extract title and link
            title_tag = div.find("a", class_=re.compile(r"result__a|result__title"))
            if not title_tag:
                continue

            title = title_tag.get_text(strip=True)
            raw_url = title_tag.get("href", "")

            # Unquote DuckDuckGo redirect wrapper (e.g. //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com...)
            clean_url = raw_url
            if "duckduckgo.com/l/?" in raw_url or "uddg=" in raw_url:
                parsed = urlparse(raw_url)
                qs = parse_qs(parsed.query)
                if "uddg" in qs and qs["uddg"]:
                    clean_url = unquote(qs["uddg"][0])

            # Extract snippet
            snippet_tag = div.find(class_=re.compile(r"result__snippet|snippet"))
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

            if title and clean_url and clean_url.startswith(("http://", "https://")):
                results.append(SearchResult(
                    title=title,
                    url=clean_url,
                    snippet=snippet
                ))

            if len(results) >= max_results:
                break

        return results


class HtmlContentExtractor:
    """Extracts clean human-readable text from HTML pages."""

    @staticmethod
    def extract_text(html_text: str, max_chars: int = 10000) -> str:
        """Strips noisy boilerplate and returns clean readable document text."""
        if not html_text:
            return ""

        soup = BeautifulSoup(html_text, "html.parser")

        # Strip noisy elements
        for element in soup(["script", "style", "nav", "header", "footer", "aside", "svg", "noscript", "form", "iframe"]):
            element.decompose()

        # Extract title if present
        title_tag = soup.find("title")
        title_text = title_tag.get_text(strip=True) if title_tag else ""

        # Format headings and paragraphs
        lines = []
        if title_text:
            lines.append(f"# {title_text}\n")

        for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
            txt = tag.get_text(" ", strip=True)
            if not txt:
                continue
            if tag.name == "h1":
                lines.append(f"\n# {txt}\n")
            elif tag.name == "h2":
                lines.append(f"\n## {txt}\n")
            elif tag.name == "h3":
                lines.append(f"\n### {txt}\n")
            elif tag.name == "li":
                lines.append(f"- {txt}")
            else:
                lines.append(f"{txt}\n")

        full_text = "\n".join(lines).strip()
        # Fallback to general text if no semantic tags found
        if not full_text:
            full_text = soup.get_text("\n", strip=True)

        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + f"\n\n[... Truncated at {max_chars:,} characters ...]"

        return full_text
