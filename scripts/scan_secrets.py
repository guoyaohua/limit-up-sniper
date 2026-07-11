"""Fail fast on high-confidence secrets before a Git push."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {'.git', 'output', 'reports', 'logs', '__pycache__'}
TEXT_SUFFIXES = {
    '.bat', '.cfg', '.ini', '.json', '.md', '.py', '.toml', '.txt', '.yaml', '.yml'
}
PATTERNS = {
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'GitHub token': re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}'),
    'OpenAI-style key': re.compile(r'\bsk-[A-Za-z0-9_-]{20,}'),
    'AWS access key': re.compile(r'\bAKIA[0-9A-Z]{16}\b'),
    'hard-coded password': re.compile(
        r'(?i)["\']?(?:password|passwd|api[_-]?key|secret)["\']?'
        r'\s*[:=]\s*["\'](?!<|your_|example|change-me)[^"\']{8,}["\']'
    ),
}


def tracked_files():
    result = subprocess.run(
        ['git', 'ls-files'], cwd=ROOT, check=True, capture_output=True, text=True
    )
    for relative in result.stdout.splitlines():
        path = ROOT / relative
        if path.suffix.lower() in TEXT_SUFFIXES and not SKIP_PARTS.intersection(path.parts):
            yield path


def main() -> int:
    findings = []
    for path in tracked_files():
        try:
            content = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(content):
                line = content.count('\n', 0, match.start()) + 1
                findings.append(f'{path.relative_to(ROOT)}:{line}: {label}')

    if findings:
        print('Potential secrets found:', file=sys.stderr)
        print('\n'.join(findings), file=sys.stderr)
        return 1
    print('Secret scan passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
