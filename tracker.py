#!/usr/bin/env python3
"""
Results tracker: seals what the model predicted BEFORE each deadline, scores it
after the gameweek settles, and turns the record into calibration the model uses.

Sealing before the deadline matters. Once teams are announced the projection would
be judged with information it never had, and it would look better than it is.

Files it owns:
  predictions/gw{N}.json  sealed at the deadline: per-player projection + the squad picked
  results/gw{N}.json      after the gameweek: actual points beside each projection
  accuracy.json           rolling error, bias per position, and the correction factors
"""
import json, os, urllib.request, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
UA   = {'User-Agent': 'Mozilla/5.0 (fpl-scout-cloud)'}
SHRINK = 250          # evidence needed before a correction is applied at full strength
LO, HI = 0.80, 1.25   # corrections are capped; the model should never swing wildly

def path(n): return os.path.join(HERE, n)
def get(url): return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45))
def ensure(d):
    p = path(d)
    if not os.path.isdir(p): os.makedirs(p, exist_ok=True)

def load(n, default=None):
    p = path(n)
    if not os.path.exists(p): return default
    try: return json.load(open(p))
    except Exception: return default


def seal(gw, dash, before_deadline=True):
    """Freeze this gameweek's projections.

    Re-sealing is allowed while the deadline is still ahead, so the record reflects
    the freshest pre-deadline view (late team news included). Once the deadline has
    passed the seal is permanent — otherwise the model would be marked against
    information it never had."""
    ensure('predictions')
    f = path(f'predictions/gw{gw}.json')
    if os.path.exists(f) and not before_deadline:
        print(f'GW{gw} sealed and deadline passed, leaving it alone'); return False
    if os.path.exists(f):
        print(f'GW{gw} re-sealing with the latest pre-deadline view')
    snap = {
        'gw': gw,
        'sealed_at': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        'players': [{'i': p['i'], 'n': p['n'], 'tm': p['tm'], 'pos': p['pos'],
                     'proj': (p.get('xpg') or [0])[0], 'mp': p.get('mp'), 'price': p.get('price')}
                    for p in dash['players'] if (p.get('xpg') or [0])[0] > 0.05],
        'entries': [{'id': e.get('id'), 'squad': e.get('squad'), 'starting': e.get('starting'),
                     'captain': e.get('captain'), 'vice': e.get('vice')} for e in dash.get('entries', [])],
    }
    json.dump(snap, open(f, 'w'))
    print(f'GW{gw} sealed: {len(snap["players"])} player projections locked in')
    return True


def settle(gw):
    """Score a finished gameweek against what we sealed."""
    pred = load(f'predictions/gw{gw}.json')
    if not pred:
        print(f'no sealed prediction for GW{gw}, nothing to score'); return False
    ensure('results')
    if os.path.exists(path(f'results/gw{gw}.json')):
        print(f'GW{gw} already scored'); return False
    try:
        live = get(f'https://fantasy.premierleague.com/api/event/{gw}/live/')
    except Exception as e:
        print(f'could not fetch GW{gw} results: {e}'); return False
    actual = {el['id']: el['stats'] for el in live.get('elements', [])}
    rows = []
    for p in pred['players']:
        st = actual.get(p['i'])
        if not st: continue
        rows.append({'i': p['i'], 'n': p['n'], 'pos': p['pos'], 'price': p.get('price'),
                     'proj': round(p['proj'], 2), 'act': st.get('total_points', 0),
                     'min': st.get('minutes', 0), 'mp': p.get('mp'),
                     'dc': st.get('defensive_contribution')})   # banks real per-match DC counts
    out = {'gw': gw, 'scored_at': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
           'rows': rows}
    # how did the squad we actually fielded do?
    for e in pred.get('entries', []):
        by = {r['i']: r for r in rows}
        xi = [by[i] for i in (e.get('starting') or []) if i in by]
        if not xi: continue
        cap = by.get(e.get('captain'))
        best = max(xi, key=lambda r: r['act']) if xi else None
        out.setdefault('squads', []).append({
            'id': e.get('id'),
            'proj': round(sum(r['proj'] for r in xi) + (cap['proj'] if cap else 0), 1),
            'act': sum(r['act'] for r in xi) + (cap['act'] if cap else 0),
            'captain': cap['n'] if cap else None, 'captain_pts': cap['act'] if cap else None,
            'best_captain': best['n'] if best else None, 'best_captain_pts': best['act'] if best else None,
        })
    json.dump(out, open(path(f'results/gw{gw}.json'), 'w'))
    print(f'GW{gw} scored: {len(rows)} players')
    return True


def recompute():
    """Roll every scored gameweek into headline accuracy + per-position corrections."""
    files = sorted([f for f in os.listdir(path('results'))] if os.path.isdir(path('results')) else [])
    gws, allrows = [], []
    for f in files:
        if not f.startswith('gw'): continue
        r = load('results/' + f)
        if not r: continue
        gws.append(r); allrows.extend(r['rows'])
    if not allrows:
        json.dump({'gws': 0, 'note': 'no scored gameweeks yet'}, open(path('accuracy.json'), 'w')); return

    def stats(rows):
        n = len(rows)
        if not n: return None
        mae = sum(abs(r['act'] - r['proj']) for r in rows) / n
        bias = sum(r['act'] - r['proj'] for r in rows) / n
        sp, sa = sum(r['proj'] for r in rows), sum(r['act'] for r in rows)
        return {'n': n, 'mae': round(mae, 2), 'bias': round(bias, 2),
                'proj_total': round(sp, 1), 'act_total': sa,
                'ratio': round(sa / sp, 3) if sp > 0 else None}

    # A projection is (minutes probability) x (points if he starts). Those are two different
    # models that fail in different ways, so they get measured — and corrected — separately.
    # Calibrating on everyone at once mostly learns noise about players who were never going to feature.
    def started(r): return (r.get('min') or 0) >= 60
    def per_start(r):
        mp = r.get('mp') or 1.0
        return r['proj'] / max(mp, 0.25)          # what we implied he'd score if he played

    bypos, calib = {}, {}
    for pos in ['GK', 'DEF', 'MID', 'FWD']:
        rows = [r for r in allrows if r['pos'] == pos]
        s = stats(rows)
        if not s: continue
        played = [r for r in rows if started(r)]
        s['started'] = len(played)
        if played:
            ps, pa = sum(per_start(r) for r in played), sum(r['act'] for r in played)
            s['ratio_started'] = round(pa / ps, 3) if ps > 0 else None
            s['mae_started'] = round(sum(abs(r['act'] - per_start(r)) for r in played) / len(played), 2)
        bypos[pos] = s
        raw = s.get('ratio_started') or 1.0
        w = len(played) / (len(played) + SHRINK)   # shrink toward "no change" until evidence earns it
        calib[pos] = round(max(LO, min(HI, 1 + (raw - 1) * w)), 3)

    # is the minutes model itself any good? predicted start likelihood vs what actually happened
    mins_model = []
    for lo, hi in [(0, .3), (.3, .6), (.6, .85), (.85, 1.01)]:
        grp = [r for r in allrows if r.get('mp') is not None and lo <= r['mp'] < hi]
        if not grp: continue
        mins_model.append({'band': f'{int(lo*100)}-{int(hi*100)}%', 'n': len(grp),
                           'predicted': round(sum(r['mp'] for r in grp) / len(grp), 2),
                           'actual': round(sum(1 for r in grp if started(r)) / len(grp), 2)})

    per_gw = [{'gw': g['gw'], **(stats(g['rows']) or {}),
               'squads': g.get('squads', [])} for g in gws]
    # captain decisions across the season
    caps = [s for g in gws for s in g.get('squads', [])]
    cap_summary = None
    if caps:
        hit = sum(1 for c in caps if c.get('captain') == c.get('best_captain'))
        lost = sum((c.get('best_captain_pts') or 0) - (c.get('captain_pts') or 0) for c in caps)
        cap_summary = {'picked_best': hit, 'of': len(caps), 'points_left_behind': lost}

    json.dump({'gws': len(gws), 'overall': stats(allrows), 'by_pos': bypos,
               'calibration': calib, 'minutes_model': mins_model, 'per_gw': per_gw, 'captain': cap_summary,
               'updated': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()},
              open(path('accuracy.json'), 'w'))
    print('accuracy updated:', len(gws), 'gameweeks, corrections', calib)


def run(events, dash, force_freeze=False):
    """Called by cloud_refresh after a build. Seals / settles as needed."""
    nxt = next((e for e in events if not e.get('finished')), None)
    if nxt:
        dl = datetime.datetime.strptime(nxt['deadline_time'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
        mins = (dl - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60
        if force_freeze or 0 < mins <= 100:
            seal(nxt['id'], dash, before_deadline=mins > 0)
    for e in events:
        if e.get('finished') and e.get('data_checked'):
            if os.path.exists(path(f'predictions/gw{e["id"]}.json')) and not os.path.exists(path(f'results/gw{e["id"]}.json')):
                settle(e['id'])
    recompute()
