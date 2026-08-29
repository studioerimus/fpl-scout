#!/usr/bin/env python3
"""
Decides what this hourly run should do, so we only do heavy work when it matters.

  full   - normal rebuild (the daily cadence, a push, or a manual run)
  freeze - a deadline is imminent: rebuild AND seal this gameweek's predictions
  settle - a gameweek has finished and we haven't scored it yet
  skip   - nothing to do; exit cheaply

Writes "mode=<x>" to $GITHUB_OUTPUT (and prints it).
"""
import json, os, sys, urllib.request, datetime, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
UA   = {'User-Agent': 'Mozilla/5.0 (fpl-scout-cloud)'}
MAX_AGE_H   = 3.5    # rebuild once the committed data is older than this
FREEZE_MINS = 360    # start sealing this long before a deadline, re-sealing as it nears

def path(n): return os.path.join(HERE, n)

def emit(mode, why):
    print(f'mode={mode}  ({why})')
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a') as f: f.write(f'mode={mode}\n')
    return 0

SRC = ['scout_template.html', 'build_dashboard.py', 'cloud_refresh.py', 'tracker.py', 'decide_run.py']

def source_hash():
    """Fingerprint of the code that produces the dashboard."""
    h = hashlib.sha256()
    for f in SRC:
        try:
            with open(path(f), 'rb') as fh: h.update(fh.read())
        except FileNotFoundError:
            pass
    return h.hexdigest()[:16]

def main():
    ev = os.environ.get('GITHUB_EVENT_NAME', '')
    if ev in ('push', 'workflow_dispatch'):
        return emit('full', f'triggered by {ev}')

    # Self-healing: if the code changed since the last build, rebuild regardless of
    # the clock. Without this, a missed push trigger leaves the site silently stale.
    try:
        want = source_hash()
        have = None
        if os.path.exists(path('build_stamp.json')):
            have = (json.load(open(path('build_stamp.json'))) or {}).get('src')
        if want != have:
            return emit('full', f'source changed ({have} -> {want})')
    except Exception as e:
        print('source check skipped:', e)

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        d = json.load(urllib.request.urlopen(
            urllib.request.Request('https://fantasy.premierleague.com/api/bootstrap-static/', headers=UA), timeout=45))
        events = d['events']
    except Exception as e:
        return emit('full', f'could not read events ({e}); rebuilding to be safe')

    # 1. a deadline about to land and this gameweek not yet sealed -> freeze
    for e in events:
        if e.get('finished'): continue
        dl = datetime.datetime.strptime(e['deadline_time'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
        mins = (dl - now).total_seconds() / 60
        if 0 < mins <= FREEZE_MINS:
            # run on every tick inside the window; the tracker keeps the freshest
            # pre-deadline seal, so a failed run still leaves an earlier one in place
            return emit('freeze', f'GW{e["id"]} deadline in {mins:.0f} min')
        break   # only the next unfinished gameweek matters

    # 2. a finished gameweek we haven't scored -> settle
    for e in events:
        if e.get('finished') and e.get('data_checked') and os.path.exists(path(f'predictions/gw{e["id"]}.json')) \
           and not os.path.exists(path(f'results/gw{e["id"]}.json')):
            return emit('settle', f'GW{e["id"]} finished and unscored')

    # 3. otherwise: rebuild whenever the data has gone stale.
    # This used to ask "is it exactly 07:00 now?", which quietly broke: GitHub delays and
    # drops scheduled ticks, so a job meant for 07:00 arriving at 08:05 missed its window
    # and nothing recovered it. Staleness can't be missed — a late tick still sees old data.
    age = None
    try:
        if os.path.exists(path('build_stamp.json')):
            t = (json.load(open(path('build_stamp.json'))) or {}).get('built')
            if t:
                built = datetime.datetime.strptime(t.replace('Z', ''), '%Y-%m-%dT%H:%M:%S') \
                        .replace(tzinfo=datetime.timezone.utc)
                age = (now - built).total_seconds() / 3600
    except Exception as e:
        print('could not read the build age:', e)
    if age is None:
        return emit('full', 'no build on record')
    if age >= MAX_AGE_H:
        return emit('full', f'data is {age:.1f}h old (rebuild past {MAX_AGE_H}h)')
    return emit('skip', f'data is only {age:.1f}h old')

if __name__ == '__main__':
    sys.exit(main())
