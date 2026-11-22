"""Small helpers for preserving data in pipe-style Markdown tables."""
import re


def pipe_cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith('|'):
        text = text[1:]
    if text.endswith('|'):
        text = text[:-1]
    return [cell.strip() for cell in text.split('|')]


def is_header_separator(cells: list[str], header: list[str]) -> bool:
    return len(cells) == len(header) and all(re.fullmatch(r':?-+:?', cell) for cell in cells)
