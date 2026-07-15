import os, glob
pids = []
for p in glob.glob('/proc/[0-9]*/cmdline'):
    try:
        pid = int(p.split('/')[2])
        with open(p, 'rb') as f:
            cmd = f.read().decode('utf-8', 'replace')
        if 'mimic' in cmd.lower():
            pids.append((pid, cmd[:200]))
    except Exception:
        pass
print(pids)
