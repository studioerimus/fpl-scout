#!/usr/bin/env python3
"""
Cloud refresh runner for GitHub Actions (runs daily, laptop-independent).
Fetches the full FPL API (no truncation on a GitHub runner), refreshes the team,
rebuilds the dashboard via build_dashboard.py, and writes a 'what changed' note.

Config: set ENTRY to your FPL team id (the number in your /entry/XXXXXX/ URL).
"""
import json, os, sys, subprocess, urllib.request, datetime

HERE  = os.path.dirname(os.path.abspath(__file__))
ENTRY = 184589                      # <-- Jack's FPL team id
UA    = {'User-Agent': 'Mozilla/5.0 (fpl-scout-cloud)'}

def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45))

# fields the build needs (keeps the committed file small)
K = ['id','web_name','first_name','second_name','team','element_type','now_cost','minutes','total_points','starts',
     'selected_by_percent','ep_next','form','points_per_game','goals_scored','assists',
     'expected_goals','expected_assists','expected_goal_involvements','expected_goals_conceded',
     'expected_goals_conceded_per_90','defensive_contribution','defensive_contribution_per_90',
     'clean_sheets','clean_sheets_per_90','saves_per_90','ict_index','threat','creativity',
     'transfers_in_event','transfers_out_event','cost_change_event','cost_change_start',
     'penalties_order','corners_and_indirect_freekicks_order','direct_freekicks_order',
     'status','chance_of_playing_next_round','news']

def path(n): return os.path.join(HERE, n)

# ---- 1. core FPL data ----
d = get('https://fantasy.premierleague.com/api/bootstrap-static/')
slim = {'events': d['events'], 'teams': d['teams'],
        'elements': [{k: e.get(k) for k in K} for e in d['elements']]}
json.dump(slim, open(path('fpl-bootstrap-static.json'), 'w'))
json.dump(get('https://fantasy.premierleague.com/api/fixtures/'), open(path('fpl-fixtures.json'), 'w'))
print('fetched bootstrap', len(slim['elements']), 'players + fixtures')

# ---- 2. team name + (post-GW1) live picks, all public, no login ----
try:
    entry = get(f'https://fantasy.premierleague.com/api/entry/{ENTRY}/')
    mt = json.load(open(path('myteam.json'))) if os.path.exists(path('myteam.json')) else {'entry': ENTRY}
    mt['name'] = entry.get('name') or mt.get('name')
    cur = entry.get('current_event')
    if cur:  # season under way -> picks are public
        picks = get(f'https://fantasy.premierleague.com/api/entry/{ENTRY}/event/{cur}/picks/')
        mt['picks'] = [[p['element'], p['position'], 1 if p['is_captain'] else 0, 1 if p['is_vice_captain'] else 0]
                       for p in picks['picks']]
        mt['bank'] = picks.get('entry_history', {}).get('bank', mt.get('bank', 0))
    json.dump(mt, open(path('myteam.json'), 'w'))
    print('team synced:', mt.get('name'), '(picks live)' if cur else '(pre-season: kept saved picks)')
except Exception as ex:
    print('team sync warning (kept previous):', ex)

# ---- 3. rebuild (keep previous data for the diff) ----
if os.path.exists(path('dash_data.json')):
    os.replace(path('dash_data.json'), path('dash_data_prev.json'))
os.environ['BUILD_STAMP'] = datetime.datetime.utcnow().strftime('%d %b %H:%M UTC')
subprocess.run([sys.executable, path('build_dashboard.py')], check=True)
# serve at the Pages root too
import shutil; shutil.copy(path('fpl_dashboard.html'), path('index.html'))

# ---- 4. what-changed note (squad-first, ruthless) ----
def load(n): return json.load(open(path(n))) if os.path.exists(path(n)) else None
new, prev = load('dash_data.json'), load('dash_data_prev.json')
lines = []
if new and prev:
    npl = {p['i']: p for p in new['players']}
    ppl = {p['i']: p for p in prev['players']}
    squad = set(new['squad'])
    for i in new['squad']:
        n_, p_ = npl.get(i), ppl.get(i)
        if not n_ or not p_: continue
        if n_['inj'] in ('OUT','SUS','INJ') and p_['inj'] not in ('OUT','SUS','INJ'):
            lines.append(f"⚠️ **{n_['n']}** now {n_['inj']} — was fine yesterday.")
        if n_['pstart'] is False and p_['pstart'] is True:
            lines.append(f"⚠️ **{n_['n']}** dropped out of the predicted XI.")
        if p_['pstart'] is False and n_['pstart'] is True:
            lines.append(f"✅ **{n_['n']}** back in the predicted XI.")
        if round(n_['vr']-p_['vr'],1) <= -1.0:
            lines.append(f"🔻 **{n_['n']}** rating {p_['vr']} → {n_['vr']}.")
    # price moves across the pool
    rises = [npl[i] for i in npl if i in ppl and round(npl[i]['price']-ppl[i]['price'],1) >= 0.1]
    falls = [npl[i] for i in npl if i in ppl and round(npl[i]['price']-ppl[i]['price'],1) <= -0.1]
    for p in sorted(rises, key=lambda x:-x['price'])[:5]:
        tag = ' (yours)' if p['i'] in squad else ''
        lines.append(f"📈 {p['n']} rose to £{p['price']:.1f}m{tag}.")
    for p in falls:
        if p['i'] in squad: lines.append(f"📉 {p['n']} (yours) fell to £{p['price']:.1f}m.")
note = ("# FPL Scout — daily refresh\n\n" + (f"GW{new['meta']['start_gw']} · rebuilt {new['meta']['built']}\n\n" if new else "")
        + ("\n".join('- '+l for l in lines) if lines else "- Nothing decision-relevant changed since yesterday."))
open(path('what_changed.md'), 'w').write(note)
print(note)
