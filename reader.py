#!/usr/bin/env python3
"""終端小說閱讀器：python3 reader.py [novel.txt]"""

from __future__ import annotations

import argparse
import curses
import locale
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")
CHAPTER_RE = re.compile(r"^##\s+(.+?)\s*$")
SCENE_BREAK = "＊　＊　＊"
NO_LINE_START = set(
    "，。、；：？！）］｝〉》」』】…‥·,.;:?!)]}’”"
)
NO_LINE_END = set("（［｛〈《「『【([{“‘")
MIN_WIDTH = 24
MIN_HEIGHT = 6


@dataclass(frozen=True)
class Chapter:
    title: str
    paragraphs: tuple[str, ...]


@dataclass
class Book:
    title: str
    chapters: list[Chapter]


@dataclass(frozen=True)
class VLine:
    text: str
    kind: str
    chapter: int
    para: int
    start: int
    end: int


def char_width(ch: str) -> int:
    if ch == "\t":
        return 4
    if unicodedata.combining(ch):
        return 0
    if ch in "\u200b\u200c\u200d\ufeff":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def clip_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def ellipsize(text: str, width: int) -> str:
    if text_width(text) <= width:
        return text
    if width <= 1:
        return clip_text(text, max(0, width))
    return clip_text(text, width - 1) + "…"


def center_text(text: str, width: int) -> str:
    text = ellipsize(text, width)
    pad = max(0, (width - text_width(text)) // 2)
    return " " * pad + text


def wrap_paragraph(
    text: str, width: int, indent: int = 2
) -> list[tuple[str, int, int]]:
    width = max(1, width)
    if width < 10:
        indent = 0
    result: list[tuple[str, int, int]] = []
    i = 0
    n = len(text)
    first = True

    while i < n:
        prefix = "　" * indent if first else ""
        start = i
        available = width - text_width(prefix)

        used = 0
        end = start
        while end < n:
            w = char_width(text[end])
            if used + w > available:
                break
            used += w
            end += 1
        if end == start:
            end += 1

        if end < n:
            while end > start + 1 and text[end] in NO_LINE_START:
                end -= 1
            while end > start + 1 and text[end - 1] in NO_LINE_END:
                end -= 1

        result.append((prefix + text[start:end], start, end))
        i = end
        first = False

    return result


def parse_book(text: str, fallback_title: str = "") -> Book:
    title = ""
    chapters: list[Chapter] = []
    preamble: list[str] = []
    cur_title: str | None = None
    cur_paras: list[str] = []

    for raw in text.splitlines():
        line = raw.replace("\t", "    ").strip()
        if not line:
            continue

        match = TITLE_RE.match(line)
        if match:
            title = match.group(1)
            continue

        match = CHAPTER_RE.match(line)
        if match:
            if cur_title is not None:
                chapters.append(Chapter(cur_title, tuple(cur_paras)))
            cur_title = match.group(1)
            cur_paras = []
            continue

        if cur_title is None:
            preamble.append(line)
        else:
            cur_paras.append(line)

    if cur_title is not None:
        chapters.append(Chapter(cur_title, tuple(cur_paras)))

    if preamble:
        chapters.insert(0, Chapter("序", tuple(preamble)))

    if not title:
        title = chapters[0].title if chapters else fallback_title

    return Book(title, chapters)


def build_lines(book: Book, width: int) -> tuple[list[VLine], list[int]]:
    lines: list[VLine] = []
    chapter_at: list[int] = []

    if book.title:
        lines.append(VLine("", "blank", -1, -1, 0, 0))
        lines.append(VLine(center_text(book.title, width), "title", -1, -1, 0, len(book.title)))

    for ci, chapter in enumerate(book.chapters):
        lines.append(VLine("", "blank", -1, -1, 0, 0))
        chapter_at.append(len(lines))
        lines.append(
            VLine(ellipsize(chapter.title, width), "chapter", ci, -1, 0, len(chapter.title))
        )
        lines.append(VLine("", "blank", -1, -1, 0, 0))

        for pi, paragraph in enumerate(chapter.paragraphs):
            if paragraph == SCENE_BREAK:
                lines.append(
                    VLine(center_text(SCENE_BREAK, width), "scene", ci, pi, 0, len(paragraph))
                )
                continue
            for text, start, end in wrap_paragraph(paragraph, width):
                lines.append(VLine(text, "body", ci, pi, start, end))

    return lines, chapter_at


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    if not text:
        return
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


class Reader:
    def __init__(self, book: Book) -> None:
        self.book = book
        self.lines: list[VLine] = []
        self.chapter_at: list[int] = []
        self.width: int | None = None
        self.offset = 0
        self.search_term = ""
        self.match_line: int | None = None
        self.message = ""

    def anchor(self) -> tuple[int, int, int] | None:
        if not self.lines:
            return None
        start = min(self.offset, len(self.lines) - 1)
        for line in self.lines[start:]:
            if line.kind == "body":
                return (line.chapter, line.para, line.start)
            if line.kind in ("title", "chapter", "scene"):
                return (line.chapter, line.para, line.start)
        return None

    @staticmethod
    def resolve_anchor(
        lines: list[VLine], anchor: tuple[int, int, int] | None
    ) -> int:
        if not lines or anchor is None:
            return 0
        ci, pi, start = anchor
        fallback = None
        for idx, line in enumerate(lines):
            if line.chapter != ci or line.para != pi or line.kind == "blank":
                continue
            if fallback is None:
                fallback = idx
            if line.start <= start < max(line.end, line.start + 1):
                return idx
        return fallback if fallback is not None else 0

    def layout(self, width: int) -> None:
        width = max(4, width)
        if width == self.width:
            return
        anchor = self.anchor()
        self.lines, self.chapter_at = build_lines(self.book, width)
        self.width = width
        self.offset = self.resolve_anchor(self.lines, anchor)

    def body_height(self, screen_height: int) -> int:
        return max(1, screen_height - 2)

    def max_offset(self, body_h: int) -> int:
        return max(0, len(self.lines) - body_h)

    def clamp(self, body_h: int) -> None:
        self.offset = min(max(0, self.offset),
self.max_offset(body_h))

    def visible_chapter(self, body_h: int) -> int:
        for line in self.lines[self.offset : self.offset + body_h]:
            if line.chapter >= 0:
                return line.chapter
        for line in self.lines[self.offset :]:
            if line.chapter >= 0:
                return line.chapter
        return -1

    def scroll(self, delta: int, body_h: int) -> None:
        self.offset = min(max(0, self.offset + delta), self.max_offset(body_h))

    def reading_chapter(self) -> int:
        current = -1
        for i, start in enumerate(self.chapter_at):
            if start <= self.offset:
                current = i
            else:
                break
        return current

    def goto_chapter(self, step: int) -> None:
        if not self.chapter_at:
            return
        current = self.reading_chapter()
        if current < 0:
            target = 0
        else:
            target = min(max(0, current + step), len(self.chapter_at) - 1)
        self.offset = self.chapter_at[target]

    def search(self, term: str, forward: bool) -> None:
        if not term or not self.lines:
            return
        if self.match_line is None:
            start = self.offset
        else:
            start = self.match_line + (1 if forward else -1)
        step = 1 if forward else -1
        total = len(self.lines)
        for k in range(total):
            idx = (start + step * k) % total
            if term.lower() in self.lines[idx].text.lower():
                self.offset = idx
                self.match_line = idx
                return
        self.message = f"找不到「{term}」"

    def prompt(self, scr, label: str) -> str | None:
        h, w = scr.getmaxyx()
        if h < MIN_HEIGHT or w < MIN_WIDTH:
            return None
        text: list[str] = []
        curses.curs_set(1)
        try:
            while True:
                h, w = scr.getmaxyx()
                if h < MIN_HEIGHT or w < MIN_WIDTH:
                    return None
                row = h - 1
                shown = label + "".join(text)
                safe_addstr(scr, row, 0, " " * max(0, w - 1))
                safe_addstr(scr, row, 0, clip_text(shown, max(0, w - 1)))
                cursor_x = min(max(0, w - 2), text_width(shown))
                try:
                    scr.move(row, cursor_x)
                except curses.error:
                    pass
                scr.refresh()

                ch = scr.get_wch()
                if ch in ("\n", "\r", 10, 13, curses.KEY_ENTER):
                    break
                if ch in ("\x1b", 27):
                    return None
                if ch in ("\x03", 3):
                    raise KeyboardInterrupt
                if ch in ("\x7f", "\b", 8, 127, curses.KEY_BACKSPACE):
                    if text:
                        text.pop()
                    continue
                if ch == curses.KEY_RESIZE:
                    continue
                if isinstance(ch, str) and ch.isprintable():
                    text.append(ch)
        finally:
            curses.curs_set(0)
        return "".join(text).strip()

    def draw(self, scr) -> None:
        h, w = scr.getmaxyx()
        scr.erase()

        if h < MIN_HEIGHT or w < MIN_WIDTH:
            safe_addstr(scr, 0, 0, clip_text("視窗太小，請調整終端大小。", max(0, w - 1)))
            scr.refresh()
            return

        margin = 1
        width = max(4, w - margin * 2)
        self.layout(width)
        body_h = self.body_height(h)
        self.clamp(body_h)

        chapter = self.visible_chapter(body_h)
        chapter_total = len(self.book.chapters)

        header_attr = curses.A_REVERSE
        safe_addstr(scr, 0, 0, " " * (w - 1), header_attr)
        left = f" {self.book.title}"
        if 0 <= chapter < chapter_total:
            right = self.book.chapters[chapter].title
        else:
            right = ""
        if right:
            right_text = f"{right} "
            if text_width(left) + text_width(right_text) + 2 <= w - 1:
                safe_addstr(scr, 0, w - 1 - text_width(right_text), right_text, header_attr)
                safe_addstr(scr, 0, 0, clip_text(left, max(0, w - 3 - text_width(right_text))), header_attr)
            else:
                safe_addstr(scr, 0, 0, clip_text(left, w - 1), header_attr)
        else:
            safe_addstr(scr, 0, 0, clip_text(left, w - 1), header_attr)

        top = 1
        for row in range(body_h):
            idx = self.offset + row
            if idx >= len(self.lines):
                break
            line = self.lines[idx]
            if not line.text:
                continue
            attr = 0
            if line.kind == "title":
                attr = curses.A_BOLD
            elif line.kind == "chapter":
                attr = curses.A_BOLD
            elif line.kind == "scene":
                attr = curses.A_DIM
            self.draw_line(scr, top + row, margin, line.text, width, attr, self.search_term)

        max_off = self.max_offset(body_h)
        percent = 100 if max_off == 0 else min(100, round(self.offset * 100 / max_off))
        if chapter_total:
            pos = f"{chapter + 1 if chapter >= 0 else 1}/{chapter_total}章"
        else:
            pos = ""
        left_footer = self.message or "  ".join(part for part in (f"{percent}%", pos) if part)
        hints = "q 退出  / 搜尋  ? 說明"
        if self.message:
            hints = ""

        footer_attr = curses.A_REVERSE
        safe_addstr(scr, h - 1, 0, " " * (w - 1), footer_attr)
        safe_addstr(scr, h - 1, 0, clip_text(left_footer, w - 1), footer_attr)
        if hints and text_width(left_footer) + text_width(hints) + 2 <= w - 1:
            safe_addstr(scr, h - 1, w - 1 - text_width(hints), hints, footer_attr)

        scr.refresh()

    def draw_line(
        self,
        scr,
        row: int,
        col: int,
        text: str,
        width: int,
        attr: int,
        term: str,
    ) -> None:
        text = clip_text(text, width)
        ranges: list[tuple[int, int]] = []
        if term:
            low = text.lower()
            needle = term.lower()
            start = 0
            while True:
                found = low.find(needle, start)
                if found < 0:
                    break
                ranges.append((found, found + len(needle)))
                start = found + max(1, len(needle))
        if not ranges:
            safe_addstr(scr, row, col, text, attr)
            return

        highlighted: set[int] = set()
        for begin, end in ranges:
            highlighted.update(range(begin, end))

        x = col
        run: list[str] = []
        run_attr = attr
        for i, ch in enumerate(text):
            want = attr | (curses.A_REVERSE if i in highlighted else 0)
            if want != run_attr:
                if run:
                    safe_addstr(scr, row, x, "".join(run), run_attr)
                    x += text_width("".join(run))
                    run = []
                run_attr = want
            run.append(ch)
        if run:
            safe_addstr(scr, row, x, "".join(run), run_attr)

    def help_overlay(self, scr) -> None:
        lines = [
            "瀏覽",
            "  ↑ ↓ / j k        逐行滾動",
            "  PgUp / PgDn      翻頁",
            "  Space / b        下一頁 / 上一頁",
            "  g / G            開頭 / 結尾",
            "  [ / ]            上一章 / 下一章",
            "  c                章節目錄",
            "",
            "搜尋",
            "  /                輸入搜尋詞",
            "  n / N            下一個 / 上一個結果",
            "",
            "按 Esc、Enter 或 q 關閉",
        ]
        self.overlay(scr, "操作說明", lines)

    def chapter_overlay(self, scr) -> None:
        chapters = self.book.chapters
        h, w = scr.getmaxyx()
        if not chapters or h < MIN_HEIGHT or w < MIN_WIDTH:
            return
        current = self.reading_chapter()
        selected = current if current >= 0 else 0
        top_item = 0

        while True:
            h, w = scr.getmaxyx()
            box_w = min(w - 2, max(30, max(text_width(c.title) for c in chapters) + 6))
            max_items = max(1, h - 5)
            box_h = min(h - 2, max_items + 3)
            view_h = box_h - 2
            if selected < top_item:
                top_item = selected
            elif selected >= top_item + view_h:
                top_item = selected - view_h + 1

            top = max(0, (h - box_h) // 2)
            left = max(0, (w - box_w) // 2)
            win = curses.newwin(box_h, box_w, top, left)
            win.keypad(True)
            win.erase()
            win.box()
            safe_addstr(win, 0, 2, " 目錄 ")

            for row in range(view_h):
                item = top_item + row
                if item >= len(chapters):
                    break
                prefix = "  "
                label = clip_text(f"{prefix}{chapters[item].title}", box_w - 4)
                pad = " " * max(0, box_w - 4 - text_width(label))
                if item == selected:
                    win.attron(curses.A_REVERSE)
                    safe_addstr(win, 1 + row, 2, label + pad, curses.A_REVERSE)
                    win.attroff(curses.A_REVERSE)
                else:
                    safe_addstr(win, 1 + row, 2, label + pad)

            win.refresh()
            try:
                ch = win.get_wch()
            except curses.error:
                continue

            if ch == curses.KEY_RESIZE:
                del win
                return
            if ch in ("\n", "\r", 10, 13, curses.KEY_ENTER):
                self.offset = self.chapter_at[selected] if selected < len(self.chapter_at) else 0
                del win
                return
            if ch in ("q", "\x1b", 27, "\x03", 3):
                del win
                return
            if ch in (curses.KEY_UP, "k"):
                selected = max(0, selected - 1)
            elif ch in (curses.KEY_DOWN, "j"):
                selected = min(len(chapters) - 1, selected + 1)
            elif ch == curses.KEY_HOME:
                selected = 0
            elif ch == curses.KEY_END:
                selected = len(chapters) - 1
            elif ch == curses.KEY_PPAGE:
                selected = max(0, selected - view_h)
            elif ch == curses.KEY_NPAGE:
                selected = min(len(chapters) - 1, selected + view_h)
            del win

    def overlay(self, scr, title: str, lines: list[str]) -> None:
        while True:
            h, w = scr.getmaxyx()
            if h < MIN_HEIGHT or w < MIN_WIDTH:
                return
            box_w = min(w - 2, max(30, max(text_width(line) for line in lines) + 6))
            box_h = min(h - 2, len(lines) + 4)
            top = max(0, (h - box_h) // 2)
            left = max(0, (w - box_w) // 2)
            win = curses.newwin(box_h, box_w, top, left)
            win.keypad(True)
            win.erase()
            win.box()
            safe_addstr(win, 0, 2, f" {title} ")
            for i, line in enumerate(lines[: box_h - 3]):
                safe_addstr(win, 2 + i, 2, clip_text(line, box_w - 4))
            win.refresh()
            try:
                ch = win.get_wch()
            except curses.error:
                continue
            del win
            if ch == curses.KEY_RESIZE:
                return
            if ch in ("\x1b", 27, "\n", "\r", 10, 13, "q", "?", curses.KEY_ENTER):
                return

    def run(self, scr) -> None:
        curses.curs_set(0)
        scr.keypad(True)
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        while True:
            self.draw(scr)
            try:
                ch = scr.get_wch()
            except curses.error:
                continue
            if ch in ("\x03", 3):
                return
            self.message = ""

            h, w = scr.getmaxyx()
            if h < MIN_HEIGHT or w < MIN_WIDTH:
                if ch in ("q", "Q", "\x03", 3):
                    return
                continue
            body_h = self.body_height(h)
            page = max(1, body_h - 1)

            if ch in ("q", "Q"):
                return
            if ch in (curses.KEY_UP, "k"):
                self.scroll(-1, body_h)
            elif ch in (curses.KEY_DOWN, "j"):
                self.scroll(1, body_h)
            elif ch == curses.KEY_PPAGE:
                self.scroll(-page, body_h)
            elif ch in (curses.KEY_NPAGE, " "):
                self.scroll(page, body_h)
            elif ch == "b":
                self.scroll(-page, body_h)
            elif ch in (curses.KEY_HOME, "g"):
                self.offset = 0
            elif ch in (curses.KEY_END, "G"):
                self.offset = self.max_offset(body_h)
            elif ch == "[":
                self.goto_chapter(-1)
            elif ch == "]":
                self.goto_chapter(1)
            elif ch == "c":
                self.chapter_overlay(scr)
            elif ch == "?":
                self.help_overlay(scr)
            elif ch == "/":
                term = self.prompt(scr, "搜尋：")
                if term:
                    self.search_term = term
                    self.match_line = None
                    self.search(term, True)
            elif ch == "n":
                if self.search_term:
                    self.search(self.search_term, True)
                else:
                    self.message = "先按 / 輸入搜尋詞"
            elif ch == "N":
                if self.search_term:
                    self.search(self.search_term, False)
                else:
                    self.message = "先按 / 輸入搜尋詞"


def init_locale() -> None:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    name = locale.setlocale(locale.LC_CTYPE)
    if "utf" not in name.lower():
        try:
            locale.setlocale(locale.LC_ALL, "C.UTF-8")
        except locale.Error:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="在終端閱讀小說。")
    parser.add_argument(
        "path",
        nargs="?",
        default=str(Path(__file__).with_name("novel.txt")),
        help="小說文字檔（預設為同目錄的 novel.txt）",
    )
    args = parser.parse_args()

    path = Path(args.path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"無法讀取檔案：{exc}", file=sys.stderr)
        return 1

    book = parse_book(text, fallback_title=path.stem)
    if not book.chapters:
        print("檔案中沒有可閱讀的內容。", file=sys.stderr)
        return 1

    init_locale()
    try:
        curses.wrapper(lambda scr: Reader(book).run(scr))
    except KeyboardInterrupt:
        return 130
    except curses.error as exc:
        print(f"無法啟動終端介面：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
