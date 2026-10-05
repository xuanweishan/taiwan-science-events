"""Shared title rules for collection, historical data and Pages deployments."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

INTERNAL_MEETING = re.compile(
    # Match a meeting label, not words occurring inside an academic talk title.
    r'(?:(?:(?:research|weekly|regular)\s+)?'
    r'(?:group|lab(?:oratory)?|team|internal|staff)\s+meetings?'
    r'|(?:[\w]{1,20}\s*)?(?:研究群會議|研究組會議|實驗室會議|組內會議|團隊會議|內部會議|組會))'
    r'(?:\s*(?:\([^()]*\)|\[[^\[\]]*\]))?',
    re.IGNORECASE,
)


def is_internal_event(title):
    normalized = unicodedata.normalize('NFKC', str(title))
    normalized = re.sub(r'[-_‐‑–—]+', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return bool(INTERNAL_MEETING.fullmatch(normalized))


def exclude_internal_events(data):
    return dict(data, events=[e for e in data.get('events', [])
                             if not is_internal_event(e.get('title', ''))])


def filter_file(path):
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    filtered = exclude_internal_events(data)
    removed = len(data.get('events', [])) - len(filtered['events'])
    if removed:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            handle.write(json.dumps(filtered, ensure_ascii=False, indent=2) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, path)
    print(f'Excluded {removed} internal team meetings from {path}')
    return removed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    filter_file(parser.parse_args().path)
