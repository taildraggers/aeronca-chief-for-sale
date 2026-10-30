"""Scraper for Aeronca Chief aircraft listings on barnstormers.com.

Listings come from a Barnstormers advanced search on the ad headline
"Chief". That also turns up plenty that isn't an Aeronca Chief (Chief
Aircraft parts, "chief pilot" jobs, the Piper Cherokee Chief), so each ad's
title must give a Chief model code, or say "Chief" together with "Aeronca",
and must not name another make or Aeronca model. Titles that look like
parts/accessories/services/raffles are dropped too. Surviving titles are
rewritten to a canonical "YEAR Aeronca MODEL" form so every listing follows
the same format.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .common import (
    MIN_AIRCRAFT_PRICE,
    Listing,
    extract_date,
    extract_location,
    extract_price,
    fetch,
    format_aircraft_title,
    price_value,
)

SITE_NAME = "Barnstormers.com"
BASE = "https://www.barnstormers.com"
MAKE = "Aeronca"

# Barnstormers advanced search for ads whose headline contains "Chief".
SEARCH_URLS = [
    f"{BASE}/cat_search.php?headline=Chief&body=&part_num=&mfg=&model="
    "&user__profile__company=&user__last_name=&user__first_name="
    "&user__profile__country=&specialcase__state=&user__profile__city="
    "&user__profile__uzip=&specialcase__phone=&user__email=&my_cats__name="
    "&price__gte=&price__lte=&search_type=advanced&keyword=",
]

MAX_PAGES = 15
LISTING_LINK_RE = re.compile(r"^/classified-(\d+)-(.+)\.html$")
GENERIC_SITE_TITLE_SNIPPET = "barnstormers.com find aircraft"

# Chief model codes: the post-war 11AC Chief, 11BC and 11CC Super Chief, and
# the pre-war 50C/65C Chief and 65CA Super Chief. No trailing \b, since titles
# often run the code into the next word ("11ACChief").
_MODEL_CODE_RE = re.compile(r"\b(11ac|11bc|11cc|65ca|65c|50c)", re.IGNORECASE)
_SUPER_CODES = {"11CC", "65CA"}
_AERONCA_RE = re.compile(r"aeronca", re.IGNORECASE)
_CHIEF_RE = re.compile(r"\bchief\b", re.IGNORECASE)
_SUPER_CHIEF_RE = re.compile(r"super\s*chief", re.IGNORECASE)
# Other makes and Aeronca models. A title naming one of these is some other
# airplane ("Piper Cherokee Chief"), a vendor ("Chief Aircraft"), or a mixed
# ad, and is dropped.
_OTHER_RE = re.compile(
    r"piper|cherokee|cessna|\bcub\b|champ|citabria|sedan|chief\s*aircraft|chief\s*pilot"
    r"|\b(?:7ac|7bcm|7ccm|7dc|7ec|7fc|15ac)",
    re.IGNORECASE,
)


def _extract_model(title: str) -> tuple[str, str] | None:
    if _OTHER_RE.search(title):
        return None
    match = _MODEL_CODE_RE.search(title)
    if match:
        code = match.group(1).upper()
        return MAKE, f"{code} {'Super Chief' if code in _SUPER_CODES else 'Chief'}"
    if _AERONCA_RE.search(title) and _CHIEF_RE.search(title):
        return MAKE, "Super Chief" if _SUPER_CHIEF_RE.search(title) else "Chief"
    return None


def _title_from_url(url: str) -> str:
    """Listing pages share a generic <title>/<h1>, but the URL slug is the ad's own title."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    match = LISTING_LINK_RE.match("/" + slug)
    if not match:
        return unquote(slug)
    return unquote(match.group(2)).replace("-", " ").strip()


def _find_listing_links(html: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = urlparse(urljoin(BASE, a["href"])).path
        if LISTING_LINK_RE.match(href):
            links.add(urljoin(BASE, href))
    return links


def _page_url(search_url: str, page: int) -> str:
    """Build a search results page's URL directly by setting its page=<n>
    query parameter, rather than hunting for "Next" links - Barnstormers'
    pager renders as bare page-number buttons with no "Next" text or
    rel="next" for a link-following heuristic to find.
    """
    if page <= 1:
        return search_url
    parts = urlparse(search_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    query.append(("page", str(page)))
    return urlunparse(parts._replace(query=urlencode(query)))


def _debug_dump_hrefs(html: str, limit: int = 25) -> None:
    soup = BeautifulSoup(html, "lxml")
    hrefs = [a["href"] for a in soup.find_all("a", href=True)]
    interesting = [h for h in hrefs if "classified" in h.lower() or "chief" in h.lower()]
    sample = interesting[:limit] or hrefs[:limit]
    print(f"  [debug] {len(hrefs)} total <a href> on page; sample: {sample}")


def _parse_detail_page(url: str, html: str) -> Listing | None:
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    if title:
        title = re.sub(r"\s*[\|\-]\s*Barnstormers.*$", "", title, flags=re.IGNORECASE).strip()
    if not title or GENERIC_SITE_TITLE_SNIPPET in title.lower():
        title = _title_from_url(url)
    if not title:
        return None

    text = soup.get_text(" ", strip=True)

    formatted_title = format_aircraft_title(title, text, _extract_model)
    if not formatted_title:
        return None
    title = formatted_title

    price = extract_price(text)
    value = price_value(price)
    if value is not None and value < MIN_AIRCRAFT_PRICE:
        return None
    location = extract_location(text)
    date_posted = extract_date(text)

    return Listing(
        title=title,
        price=price,
        location=location,
        date_posted=date_posted,
        site=SITE_NAME,
        url=url,
    )


def scrape() -> list[Listing]:
    print(f"[{SITE_NAME}] starting scrape")
    all_links: set[str] = set()

    for search_url in SEARCH_URLS:
        seen_this_search: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            url = _page_url(search_url, page)
            html = fetch(url)
            if not html:
                break
            links = _find_listing_links(html)
            new_links = links - seen_this_search
            print(f"  [search page {page}]: {len(links)} links ({len(new_links)} new)")
            if page == 1 and not links:
                _debug_dump_hrefs(html)
            seen_this_search |= links
            if not new_links:
                break
        all_links |= seen_this_search

    print(f"[{SITE_NAME}] {len(all_links)} unique listing URLs found")

    listings: list[Listing] = []
    for url in sorted(all_links):
        html = fetch(url)
        if not html:
            continue
        listing = _parse_detail_page(url, html)
        if listing:
            listings.append(listing)

    print(f"[{SITE_NAME}] parsed {len(listings)} listings")
    return listings
