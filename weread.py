#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
WeRead -> Readwise sync (highlights + notes)

Env:
  WEREAD_COOKIE    (required) full cookie string from weread.qq.com / i.weread.qq.com
  READWISE_TOKEN   (required) Readwise access token

Optional:
  WEREAD_USER_VID  (optional) if not set, will try to parse from cookie (wr_vid)
  ONLY_RECENT_DAYS (optional) int, if set will only sync highlights/notes newer than N days
  DRY_RUN          (optional) "1" to print payload count without posting to Readwise

This script:
- Pulls bookshelf
- For each book:
  - Fetches bookmarklist (highlights; includes comment fields for many highlights)
  - Fetches review/list (note-only "thoughts"/reviews)
- Converts to Readwise highlight payload with stable external_id
"""

from __future__ import annotations

import os
import re
import json
import time
import hashlib
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


READWISE_HIGHLIGHTS_API = "https://readwise.io/api/v2/highlights/"

WEREAD_BASE_I = "https://i.weread.qq.com"
WEREAD_BASE_WEB = "https://weread.qq.com"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class Book:
    book_id: str
    title: str
    author: str
    cover: Optional[str] = None


@dataclass
class RWHighlight:
    text: str
    title: str
    author: str
    source_url: str
    highlighted_at: str  # ISO 8601
    note: Optional[str]
    location: Optional[str]
    location_type: str
    external_id: str
    external_source: str = "weread"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _unix_to_iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_text(s: str) -> str:
    if s is None:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _parse_cookie_value(cookie: str, key: str) -> Optional[str]:
    # very tolerant cookie parser
    m = re.search(rf"(?:^|;\s*){re.escape(key)}=([^;]+)", cookie)
    return m.group(1) if m else None


class WeReadClient:
    def __init__(self, cookie: str):
        self.cookie = cookie
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Cookie": cookie,
                "Origin": WEREAD_BASE_WEB,
                "Referer": WEREAD_BASE_WEB + "/",
            }
        )

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = self.s.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def bookshelf(self, user_vid: str) -> List[Book]:
        """
        Uses: https://i.weread.qq.com/shelf/friendCommon?userVid=...
        Returns a merged list of books.
        """
        data = self._get(f"{WEREAD_BASE_I}/shelf/friendCommon", params={"userVid": user_vid})
        books_raw = []
        for k in ("finishReadBooks", "recentBooks", "allBooks"):
            if isinstance(data.get(k), list):
                books_raw.extend(data[k])

        seen = set()
        out: List[Book] = []
        for b in books_raw:
            book_id = str(b.get("bookId", ""))
            # filter non-book items (e.g., public accounts)
            if not book_id.isdigit():
                continue
            if book_id in seen:
                continue
            seen.add(book_id)

            out.append(
                Book(
                    book_id=book_id,
                    title=str(b.get("title") or ""),
                    author=str(b.get("author") or ""),
                    cover=b.get("cover"),
                )
            )
        return out

    def bookmarklist(self, book_id: str) -> Dict[str, Any]:
        """
        Uses: https://i.weread.qq.com/book/bookmarklist?bookId=...
        Includes highlights, and often includes the comment/note attached to highlights.
        """
        return self._get(f"{WEREAD_BASE_I}/book/bookmarklist", params={"bookId": book_id})

    def my_reviews(self, book_id: str) -> Dict[str, Any]:
        """
        WeRead "thoughts"/reviews (note-only items).
        Uses commonly documented endpoint:
        https://i.weread.qq.com/review/list?bookId=...&listType=11&mine=1&synckey=0&listMode=0
        """
        return self._get(
            f"{WEREAD_BASE_I}/review/list",
            params={
                "bookId": book_id,
                "listType": 11,
                "mine": 1,
                "synckey": 0,
                "listMode": 0,
            },
        )


class ReadwiseClient:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Token {token}",
                "Content-Type": "application/json",
                "User-Agent": UA,
            }
        )

    def post_highlights(self, highlights: List[RWHighlight]) -> Dict[str, Any]:
        payload = {
            "highlights": [
                {
                    "text": h.text,
                    "title": h.title,
                    "author": h.author,
                    "source_url": h.source_url,
                    "highlighted_at": h.highlighted_at,
                    "note": h.note,
                    "location": h.location,
                    "location_type": h.location_type,
                    "external_id": h.external_id,
                    "external_source": h.external_source,
                }
                for h in highlights
            ]
        }
        r = self.s.post(READWISE_HIGHLIGHTS_API, data=json.dumps(payload), timeout=60)
        r.raise_for_status()
        return r.json()


def _weread_book_url(book_id: str) -> str:
    # A reasonable canonical book URL for Readwise "source_url"
    return f"{WEREAD_BASE_WEB}/web/reader/{book_id}"


def _should_include(ts_unix: int, only_recent_days: Optional[int]) -> bool:
    if not only_recent_days:
        return True
    cutoff = int(time.time()) - only_recent_days * 86400
    return ts_unix >= cutoff


def _extract_highlights_from_bookmarklist(
    book: Book,
    data: Dict[str, Any],
    only_recent_days: Optional[int],
) -> List[RWHighlight]:
    chapters = {}
    for c in data.get("chapters", []) or []:
        uid = str(c.get("chapterUid", ""))
        title = str(c.get("title") or "")
        if uid and title:
            chapters[uid] = title

    updated = data.get("updated", []) or []
    out: List[RWHighlight] = []

    for item in updated:
        # highlight text candidates (WeRead varies by client/version)
        highlight_text = (
            item.get("markText")
            or item.get("abstract")
            or item.get("content")
            or item.get("text")
            or ""
        )
        highlight_text = _clean_text(str(highlight_text))

        if not highlight_text:
            continue

        # comment/note attached to highlight
        comment = (
            item.get("review")
            or item.get("reviewContent")
            or item.get("note")
            or item.get("comment")
            or ""
        )
        comment = _clean_text(str(comment))

        # extra context
        chapter_uid = str(item.get("chapterUid") or "")
        chapter_title = chapters.get(chapter_uid)
        if chapter_title:
            if comment:
                comment = f"{comment}\n\n— Chapter: {chapter_title}"
            else:
                # If no comment, still store chapter in note (helps navigation in Readwise)
                comment = f"— Chapter: {chapter_title}"

        # location (range like "123-129")
        location = item.get("range") or item.get("location") or None
        if location is not None:
            location = str(location)

        # timestamp
        ts = int(item.get("createTime") or item.get("updated") or 0)
        if ts <= 0:
            # fall back to now if missing
            ts = int(time.time())

        if not _should_include(ts, only_recent_days):
            continue

        # stable external_id
        # prefer explicit bookmarkId; else hash a few stable fields
        bookmark_id = item.get("bookmarkId") or item.get("id") or ""
        if bookmark_id:
            external_id = f"weread:{book.book_id}:bm:{bookmark_id}"
        else:
            key = f"{book.book_id}|{location or ''}|{highlight_text}"
            external_id = f"weread:{book.book_id}:h:{_sha1(key)}"

        out.append(
            RWHighlight(
                text=highlight_text,
                title=book.title,
                author=book.author,
                source_url=_weread_book_url(book.book_id),
                highlighted_at=_unix_to_iso(ts),
                note=comment or None,
                location=location,
                location_type="weread",
                external_id=external_id,
            )
        )

    return out


def _extract_note_only_reviews(
    book: Book,
    data: Dict[str, Any],
    only_recent_days: Optional[int],
) -> List[RWHighlight]:
    """
    Export WeRead 'thoughts' (review/list mine=1 listType=11) as standalone highlights,
    so they don't get lost. This also helps with the "note export is broken" issue in some exporters.
    """
    reviews = []
    # structure varies; try a few
    for k in ("reviews", "updated", "data", "items"):
        v = data.get(k)
        if isinstance(v, list):
            reviews = v
            break

    out: List[RWHighlight] = []
    for r in reviews:
        content = r.get("content") or r.get("review") or r.get("text") or ""
        content = _clean_text(str(content))
        if not content:
            continue

        ts = int(r.get("createTime") or r.get("ctime") or 0)
        if ts <= 0:
            ts = int(time.time())

        if not _should_include(ts, only_recent_days):
            continue

        review_id = r.get("reviewId") or r.get("id") or ""
        if review_id:
            external_id = f"weread:{book.book_id}:rv:{review_id}"
        else:
            key = f"{book.book_id}|review|{content}"
            external_id = f"weread:{book.book_id}:rvh:{_sha1(key)}"

        # Put the thought itself as highlight text (searchable in Readwise)
        # and also keep a small label in note.
        note = "WeRead note (review/thought)."

        out.append(
            RWHighlight(
                text=content,
                title=book.title,
                author=book.author,
                source_url=_weread_book_url(book.book_id),
                highlighted_at=_unix_to_iso(ts),
                note=note,
                location=None,
                location_type="weread",
                external_id=external_id,
            )
        )
    return out


def main() -> None:
    weread_cookie = os.environ.get("WEREAD_COOKIE", "").strip()
    readwise_token = os.environ.get("READWISE_TOKEN", "").strip()

    if not weread_cookie:
        raise SystemExit("Missing env WEREAD_COOKIE")
    if not readwise_token:
        raise SystemExit("Missing env READWISE_TOKEN")

    user_vid = os.environ.get("WEREAD_USER_VID", "").strip()
    if not user_vid:
        user_vid = _parse_cookie_value(weread_cookie, "wr_vid") or ""

    if not user_vid:
        raise SystemExit(
            "Could not determine userVid. Set env WEREAD_USER_VID explicitly, "
            "or ensure cookie contains wr_vid."
        )

    only_recent_days = os.environ.get("ONLY_RECENT_DAYS", "").strip()
    only_recent_days_int: Optional[int] = int(only_recent_days) if only_recent_days else None

    dry_run = os.environ.get("DRY_RUN", "").strip() == "1"

    wr = WeReadClient(weread_cookie)
    rw = ReadwiseClient(readwise_token)

    books = wr.bookshelf(user_vid)
    if not books:
        print("No books found on bookshelf.")
        return

    all_highlights: List[RWHighlight] = []
    for i, book in enumerate(books, 1):
        try:
            bm = wr.bookmarklist(book.book_id)
            hs = _extract_highlights_from_bookmarklist(book, bm, only_recent_days_int)
            all_highlights.extend(hs)

            rv = wr.my_reviews(book.book_id)
            ns = _extract_note_only_reviews(book, rv, only_recent_days_int)
            all_highlights.extend(ns)

            print(f"[{i}/{len(books)}] {book.title} -> highlights={len(hs)} notes={len(ns)}")
        except Exception as e:
            print(f"[{i}/{len(books)}] {book.title} -> ERROR: {e}")

    # If huge, chunk to avoid request size issues
    CHUNK = 200
    print(f"Total to sync: {len(all_highlights)}")
    if dry_run:
        print("DRY_RUN=1, skipping Readwise post.")
        return

    sent = 0
    for start in range(0, len(all_highlights), CHUNK):
        chunk = all_highlights[start : start + CHUNK]
        if not chunk:
            continue
        resp = rw.post_highlights(chunk)
        sent += len(chunk)
        print(f"Posted {sent}/{len(all_highlights)}. Readwise response keys={list(resp.keys())}")

    print("Done.")


if __name__ == "__main__":
    main()
