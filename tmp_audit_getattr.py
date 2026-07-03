import os, re
from collections import defaultdict

def find_getattr_defaults(root):
    results = []
    for path, dirs, files in os.walk(root):
        for f in files:
            if not f.endswith('.py'): continue
            fp = os.path.join(path, f)
            try:
                with open(fp) as fh:
                    src = fh.read()
            except Exception:
                continue
            for m in re.finditer(r'getattr\(([^,]+),\s*[\'\"]([^\'\"]+)[\'\"](?:,\s*([^)]+))?\)', src):
                obj, attr, default = m.group(1), m.group(2), m.group(3)
                results.append((fp.replace(root + '/', ''), attr, default.strip() if default else None, src.count('\n', 0, m.start())+1))
    return results

res = find_getattr_defaults('/app')
by_file = defaultdict(list)
for r in res:
    by_file[r[0]].append(r)
for f in sorted(by_file):
    print(f'\n=== {f} ===')
    for r in by_file[f]:
        d = r[2] if r[2] is not None else '<no default>'
        print(f'  {r[1]:40} default={d:20} line={r[3]}')
