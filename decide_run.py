#!/usr/bin/env python3
"""
Decides what this hourly run should do, so we only do heavy work when it matters.

  full   - normal rebuild (the daily cadence, a push, or a manual run)
  freeze - a deadline is imminent: rebuild AND seal this gameweek's predictions
  settle - a gameweek has finished and we haven't scored it yet
  skip   - nothing to do; exit cheaply

Writes "mode=<x>" to $GITHUB_OUTPUT (and prints it).
"""
import json, os, sys, urllib.request, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
UA   = {'User-Agent': 'Mozilla/5.0 (fpl-scout-cloud)'}
FULL_HOURS = {7, 9, 12, 15, 18, 21}      # the daily cadence, UTC
FREEZE_MINS = 100                         # seal predictions within this long of a deadline

def path(n): return os.path.join(HERE, n)

def emit(mode, why):
    print(f'mode={mode}  ({why})')
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a') as f: f.write(f'mode={mode}\n')
    return 0

def main():
    ev = os.environ.get('GITHUB_EVENT_NAME', '')
    if ev in ('push', 'workflow_dispatch'):
        return emit('full', f'triggered by {ev}')

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

    # 3. otherwise keep the normal daily cadence
    if now.hour in FULL_HOURS:
        return emit('full', f'{now.hour:02d}:00 UTC scheduled rebuild')
    return emit('skip', 'nothing due this hour')

if __name__ == '__main__':
    sys.exit(main())
