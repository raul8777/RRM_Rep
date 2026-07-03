import os, re, json, sys
from collections import defaultdict

ROOT = '/app'
sys.path.insert(0, ROOT)
from config_helpers import _PARAM_DEFAULTS

# Canonical defaults from config_helpers.py (single source of truth)
CANON = dict(_PARAM_DEFAULTS)
CRITICAL_PARAMS = set(CANON.keys())

import ast

def parse_default(val):
    if val is None:
        return None
    val = val.strip()
    try:
        if val.lower() == 'true':
            return True
        if val.lower() == 'false':
            return False
        if val.startswith('"') and val.endswith('"'):
            return val[1:-1]
        if val.startswith("'") and val.endswith("'"):
            return val[1:-1]
        if val.startswith('[') or val.startswith('{'):
            return ast.literal_eval(val)
        if '.' in val:
            return float(val)
        return int(val)
    except Exception:
        return val

def find_getattr_defaults(root):
    results = []
    for path, dirs, files in os.walk(root):
        for f in files:
            if not f.endswith('.py'):
                continue
            if f.startswith('tmp_'):
                continue
            fp = os.path.join(path, f)
            try:
                with open(fp) as fh:
                    src = fh.read()
            except Exception:
                continue
            for m in re.finditer(r'getattr\(([^,]+),\s*[\'"]([^\'"]+)[\'"](?:,\s*([^)]+))?\)', src):
                obj, attr, default = m.group(1), m.group(2), m.group(3)
                line = src.count('\n', 0, m.start()) + 1
                parsed = parse_default(default.strip() if default else None)
                severity = 'info'
                if attr in CRITICAL_PARAMS:
                    expected = CANON.get(attr)
                    if parsed is not None and parsed != expected:
                        severity = 'critical'
                results.append((fp.replace(root + '/', ''), attr, default.strip() if default else '<no default>', line, severity))
    return results

def find_hardcodes(root):
    results = []
    magic = re.compile(r'\b(\d+(?:\.\d+)?)\b')
    for path, dirs, files in os.walk(root):
        for f in files:
            if not f.endswith('.py'):
                continue
            fp = os.path.join(path, f)
            try:
                with open(fp) as fh:
                    src = fh.read()
            except Exception:
                continue
            for m in re.finditer(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+)$', src, re.M):
                name, val = m.group(1), m.group(2).strip()
                if name in ('FAPI_BASE', 'MAX_SHORTS', 'LEVERAGE_MAX', 'FUNDING_LIMIT', '_PRICE_CACHE_TTL'):
                    continue
                results.append((fp.replace(root + '/', ''), name, val, src.count('\n', 0, m.start())+1))
    return results

getattr_res = find_getattr_defaults(ROOT)
by_file = defaultdict(list)
for r in getattr_res:
    by_file[r[0]].append(r)

with open('/app/audit_getattr.txt', 'w') as out:
    out.write('=== getattr(settings, ...) audit ===\n')
    out.write(f'Total: {len(getattr_res)} occurrences\n')
    critical = [r for r in getattr_res if r[4] == 'critical']
    out.write(f'Critical mismatches: {len(critical)}\n\n')
    for r in critical:
        out.write(f'CRITICAL {r[0]}:{r[3]}  {r[1]:40} default={r[2]:20} expected={CANON.get(r[1])}\n')
    out.write('\n')
    for f in sorted(by_file):
        out.write(f'\n=== {f} ===\n')
        for r in by_file[f]:
            out.write(f'  {r[4]:8} {r[1]:40} default={r[2]:20} line={r[3]}\n')

print('audit_getattr.txt written')
