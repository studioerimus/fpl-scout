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
# teams.json = ["184589", 999999, ...]  (first is the default shown). Falls back to ENTRY.
TEAM_IDS = []
if os.path.exists(path('teams.json')):
    try:
        TEAM_IDS = [int(str(x).strip()) for x in json.load(open(path('teams.json'))) if str(x).strip()]
    except Exception as ex:
        print('teams.json unreadable, using default entry:', ex)
if not TEAM_IDS:
    TEAM_IDS = [ENTRY]

prev_all = {}
if os.path.exists(path('myteams.json')):
    try: prev_all = {e.get('entry'): e for e in json.load(open(path('myteams.json')))}
    except Exception: prev_all = {}
if os.path.exists(path('myteam.json')):
    try:
        _m = json.load(open(path('myteam.json')))
        prev_all.setdefault(_m.get('entry'), _m)
    except Exception: pass

all_teams = []
for tid_ in TEAM_IDS:
    mt = dict(prev_all.get(tid_) or {'entry': tid_})
    mt['entry'] = tid_
    try:
        entry = get(f'https://fantasy.premierleague.com/api/entry/{tid_}/')
        mt['name'] = entry.get('name') or mt.get('name')
        cur = entry.get('current_event')
        if cur:  # season under way -> picks are public
            picks = get(f'https://fantasy.premierleague.com/api/entry/{tid_}/event/{cur}/picks/')
            mt['picks'] = [[p['element'], p['position'], 1 if p['is_captain'] else 0, 1 if p['is_vice_captain'] else 0]
                           for p in picks['picks']]
            mt['bank'] = picks.get('entry_history', {}).get('bank', mt.get('bank', 0))
        print('team synced:', tid_, mt.get('name'), '(picks live)' if cur else '(pre-season: kept saved picks)')
    except Exception as ex:
        print(f'team {tid_} sync warning (kept previous):', ex)
    if mt.get('picks'): all_teams.append(mt)

if all_teams:
    json.dump(all_teams, open(path('myteams.json'), 'w'))
    json.dump(all_teams[0], open(path('myteam.json'), 'w'))   # primary, back-compat
print('teams tracked:', [t.get('entry') for t in all_teams])

# ---- 2b. purchase prices, so selling prices can be exact ----
# FPL gives back only half of any profit, rounded down, and the public API never
# exposes what you paid. We refresh several times a day, so the first time a player
# appears in a squad we record that day's price as the buy price and track it from there.
try:
    PRICE_NOW = {e['id']: e['now_cost'] for e in d['elements']}
    purch = json.load(open(path('purchases.json'))) if os.path.exists(path('purchases.json')) else {}
    for t in all_teams:
        key = str(t.get('entry'))
        held = {str(p[0]) for p in t.get('picks', [])}
        book = purch.get(key, {})
        for pid in held:
            if pid not in book and int(pid) in PRICE_NOW:
                book[pid] = PRICE_NOW[int(pid)]          # first sighting = what you paid
        for pid in list(book):                            # sold players drop out of the book
            if pid not in held: del book[pid]
        purch[key] = book
    json.dump(purch, open(path('purchases.json'), 'w'))
    print('purchase prices tracked for', len(purch), 'team(s)')
except Exception as ex:
    print('purchase-price warning:', ex)

# ---- 2c. live feeds: predicted lineups (Rotowire) + odds (the-odds-api, Oddschecker fallback) ----
NAMEMAP = {'arsenal':'ARS','aston villa':'AVL','bournemouth':'BOU','brentford':'BRE','brighton':'BHA',
           'chelsea':'CHE','coventry':'COV','crystal palace':'CRY','everton':'EVE','fulham':'FUL',
           'hull':'HUL','ipswich':'IPS','leeds':'LEE','liverpool':'LIV','manchester city':'MCI',
           'manchester united':'MUN','newcastle':'NEW','nottingham':'NFO','tottenham':'TOT','sunderland':'SUN'}
def to_short(name):
    n = (name or '').lower()
    for k, v in NAMEMAP.items():
        if k in n: return v
    return None

ROTOWIRE_JS = r"""() => {
  const map={'Arsenal':'ARS','Coventry City':'COV','Hull City':'HUL','Manchester United':'MUN','Nottingham Forest':'NFO','Leeds United':'LEE','Ipswich Town':'IPS','Sunderland':'SUN','Everton':'EVE','Crystal Palace':'CRY','Brentford':'BRE','Tottenham Hotspur':'TOT','Manchester City':'MCI','AFC Bournemouth':'BOU','Brighton & Hove Albion':'BHA','Aston Villa':'AVL','Newcastle United':'NEW','Liverpool':'LIV','Fulham':'FUL','Chelsea':'CHE'};
  const SPS=new Set(['GK','DL','DC','DR','DMC','DML','DMR','WBL','WBR','ML','MC','MR','CM','AML','AMC','AMR','FWL','FWR','FWC','FW','ST','LW','RW']);
  const sn=n=>{const p=n.replace(/\./g,'').trim().split(/\s+/);return p[p.length-1];};
  const el=document.querySelector('main')||document.body;
  const lines=el.innerText.split('\n').map(s=>s.trim()).filter(Boolean);
  const teams=[],blocks=[]; let cur=null,mode=null;
  for(const ln of lines){
    if(map[ln]){teams.push(map[ln]);continue;}
    if(/^(Predicted|Confirmed|Unknown) Lineup$/.test(ln)){cur={xi:[],inj:[]};blocks.push(cur);mode='xi';continue;}
    if(ln==='Injuries'){mode='inj';continue;}
    const m=ln.match(/^(\S+)\s+(.+?)(?:\s+(QUES|OUT|SUS|DOUBT))?$/);
    if(!m||!cur)continue;
    if(mode==='xi'&&SPS.has(m[1]))cur.xi.push(sn(m[2]));
    else if(mode==='inj'&&m[3])cur.inj.push([sn(m[2]),m[3]]);
  }
  return {teams,blocks};
}"""

STATUS = {}
def scrape_feeds():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        STATUS['playwright_import'] = 'FAIL: '+str(e); print('playwright unavailable:', e); return
    STATUS['playwright_import'] = 'ok'
    UA2 = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'
    with sync_playwright() as pw:
        try:
            br = pw.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage','--disable-blink-features=AutomationControlled'])
            STATUS['chromium_launch'] = 'ok ' + pw.chromium.name
        except Exception as e:
            STATUS['chromium_launch'] = 'FAIL: '+str(e)[:200]; print('chromium launch failed:', e); return
        pg = br.new_context(user_agent=UA2, locale='en-GB').new_page()
        # lineups (Rotowire)
        try:
            r = pg.goto('https://www.rotowire.com/soccer/lineups.php', timeout=40000, wait_until='domcontentloaded')
            STATUS['rotowire_http'] = r.status if r else None
            pg.wait_for_timeout(3000)
            data = pg.evaluate(ROTOWIRE_JS)
            STATUS['rotowire_teams_seen'] = len(data.get('teams', []))
            start, inj = {}, {}
            for k, team in enumerate(data['teams']):
                blk = data['blocks'][k] if k < len(data['blocks']) else None
                if not blk: continue
                if blk['xi']: start[team] = blk['xi']
                if blk['inj']: inj[team] = {s: t for s, t in blk['inj']}
            if start:
                json.dump({'start': start, 'unposted': [t for t in set(data['teams']) if t not in start], 'inj': inj},
                          open(path('lineups.json'), 'w'))
                STATUS['lineups'] = f'refreshed {len(start)} teams'; print('lineups refreshed:', len(start))
            else:
                STATUS['lineups'] = 'loaded but 0 parsed, kept seeded'
        except Exception as e:
            STATUS['lineups'] = 'FAIL: '+str(e)[:200]; print('lineups failed:', e)
        # odds — prefer the-odds-api (reliable JSON) if a key is set, else Oddschecker
        got = False
        key = os.environ.get('ODDS_API_KEY')
        STATUS['odds_key_present'] = bool(key)
        if key:
            try:
                ev = get(f'https://api.the-odds-api.com/v4/sports/soccer_epl/odds/?regions=uk&markets=h2h&oddsFormat=decimal&apiKey={key}')
                matches, seen = [], set()
                for e in ev:
                    h, a = to_short(e.get('home_team')), to_short(e.get('away_team'))
                    if not h or not a or h in seen or a in seen: continue
                    prices = {'H': [], 'D': [], 'A': []}
                    for bk in e.get('bookmakers', []):
                        for mk in bk.get('markets', []):
                            if mk.get('key') != 'h2h': continue
                            for o in mk.get('outcomes', []):
                                s = to_short(o.get('name'))
                                if s == h: prices['H'].append(o['price'])
                                elif s == a: prices['A'].append(o['price'])
                                elif 'draw' in o.get('name','').lower(): prices['D'].append(o['price'])
                    if not (prices['H'] and prices['D'] and prices['A']): continue
                    med = lambda L: sorted(L)[len(L)//2]
                    ph, pd, pa = 1/med(prices['H']), 1/med(prices['D']), 1/med(prices['A'])
                    s = ph+pd+pa
                    matches.append({'h': h, 'a': a, 'pH': round(ph/s,3), 'pD': round(pd/s,3), 'pA': round(pa/s,3)})
                    seen.add(h); seen.add(a)
                    if len(matches) >= 10: break
                if matches:
                    json.dump({'matches': matches}, open(path('odds.json'), 'w'))
                    print('odds: refreshed via the-odds-api,', len(matches), 'matches'); got = True
                    STATUS['odds'] = f'the-odds-api {len(matches)} matches'
                else:
                    STATUS['odds'] = 'the-odds-api returned 0 usable matches'
            except Exception as e:
                STATUS['odds'] = 'the-odds-api FAIL: '+str(e)[:150]; print('the-odds-api failed:', e)
        if not got:
            try:
                pg.goto('https://www.oddschecker.com/football/english/premier-league', timeout=45000, wait_until='domcontentloaded')
                pg.wait_for_timeout(3000)
                txt = pg.inner_text('body')
                import re
                toks = [t.strip() for t in txt.split('\n') if t.strip()]
                frac = re.compile(r'^\d+/\d+$')
                matches, i = [], 0
                while i < len(toks)-4:
                    h, a = to_short(toks[i]), to_short(toks[i+1])
                    if h and a and frac.match(toks[i+2]) and frac.match(toks[i+3]) and frac.match(toks[i+4]):
                        def p(fr): x,y = fr.split('/'); return float(y)/(float(x)+float(y))
                        ph, pd, pa = p(toks[i+2]), p(toks[i+3]), p(toks[i+4]); s=ph+pd+pa
                        matches.append({'h':h,'a':a,'pH':round(ph/s,3),'pD':round(pd/s,3),'pA':round(pa/s,3)}); i+=5
                    else: i+=1
                if matches:
                    json.dump({'matches': matches[:10]}, open(path('odds.json'), 'w'))
                    print('odds: refreshed via Oddschecker,', len(matches), 'matches')
                    STATUS['odds'] = f'oddschecker {len(matches)} matches'
                else:
                    STATUS['odds'] = 'oddschecker parsed 0, kept seeded'
                    print('odds: Oddschecker parsed nothing, kept seeded')
            except Exception as e:
                STATUS['odds'] = 'oddschecker FAIL: '+str(e)[:150]; print('odds scrape failed, kept seeded:', e)
        br.close()

try:
    scrape_feeds()
except Exception as e:
    STATUS['fatal'] = str(e)[:200]; print('feeds step error (kept seeded):', e)
# probe: does the FPL API allow direct browser calls (CORS)? decides if live in-page refresh is possible
try:
    _r = urllib.request.urlopen(urllib.request.Request(
        'https://fantasy.premierleague.com/api/bootstrap-static/',
        headers={**UA, 'Origin': 'https://studioerimus.github.io'}), timeout=30)
    STATUS['fpl_cors'] = _r.headers.get('Access-Control-Allow-Origin') or 'absent'
except Exception as e:
    STATUS['fpl_cors'] = 'probe failed: '+str(e)[:80]
STATUS['ts'] = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
json.dump(STATUS, open(path('feeds_status.json'), 'w'), indent=2)
print('feeds_status:', json.dumps(STATUS))

# ---- 2c. mini-leagues: standings always, rival squads once per gameweek ----
# Rivals' picks only become public after a deadline, and they only change weekly,
# so pulling them on every refresh would hammer the API for nothing.
try:
    prev_lg = json.load(open(path('leagues.json'))) if os.path.exists(path('leagues.json')) else {}
    cur_gw = None
    me = all_teams[0].get('entry') if all_teams else ENTRY
    ent = get(f'https://fantasy.premierleague.com/api/entry/{me}/')
    cur_gw = ent.get('current_event')
    classic = [l for l in (ent.get('leagues', {}) or {}).get('classic', []) if not l.get('league_type') == 's']
    classic = [l for l in classic if (l.get('num_entries') or 0) <= 200][:6]      # skip the giant global ones
    need_picks = bool(cur_gw) and prev_lg.get('picks_gw') != cur_gw
    out_lg = {'picks_gw': cur_gw if need_picks else prev_lg.get('picks_gw'), 'me': me, 'leagues': []}
    prev_by_id = {l['id']: l for l in prev_lg.get('leagues', [])}
    seen_picks = {}
    for L in classic:
        lid = L['id']
        try:
            st = get(f'https://fantasy.premierleague.com/api/leagues-classic/{lid}/standings/')
        except Exception as ex:
            print('league', lid, 'standings failed:', ex)
            if lid in prev_by_id: out_lg['leagues'].append(prev_by_id[lid])
            continue
        rivals = []
        for r in (st.get('standings', {}) or {}).get('results', [])[:60]:
            row = {'entry': r['entry'], 'name': r['entry_name'], 'player': r['player_name'],
                   'rank': r['rank'], 'total': r['total'], 'gw': r.get('event_total')}
            if need_picks:
                if r['entry'] in seen_picks:
                    row.update(seen_picks[r['entry']])
                else:
                    try:
                        pk = get(f"https://fantasy.premierleague.com/api/entry/{r['entry']}/event/{cur_gw}/picks/")
                        got = {'picks': [q['element'] for q in pk['picks']],
                               'captain': next((q['element'] for q in pk['picks'] if q['is_captain']), None),
                               'chip': pk.get('active_chip')}
                        seen_picks[r['entry']] = got; row.update(got)
                    except Exception:
                        pass
            else:
                old = next((x for x in (prev_by_id.get(lid, {}).get('rivals') or []) if x['entry'] == r['entry']), None)
                if old and old.get('picks'):
                    row.update({k: old[k] for k in ('picks', 'captain', 'chip') if k in old})
            rivals.append(row)
        out_lg['leagues'].append({'id': lid, 'name': L['name'], 'size': L.get('num_entries'), 'rivals': rivals})
    json.dump(out_lg, open(path('leagues.json'), 'w'))
    print('leagues:', [(l['id'], l['name'], len(l['rivals'])) for l in out_lg['leagues']],
          '| squads pulled' if need_picks else '| standings only (pre-deadline or already current)')
except Exception as ex:
    print('mini-league warning:', ex)

# ---- 2d. transfer velocity: a rolling record so price moves can be sensed ----
try:
    vel = json.load(open(path('velocity.json'))) if os.path.exists(path('velocity.json')) else {'samples': []}
    stamp = datetime.datetime.utcnow().replace(microsecond=0).isoformat()
    watch = set()
    for t in all_teams: watch.update(str(p[0]) for p in t.get('picks', []))
    top = sorted(d['elements'], key=lambda e: -(e.get('transfers_in_event', 0) + e.get('transfers_out_event', 0)))[:60]
    watch.update(str(e['id']) for e in top)
    vel['samples'].append({'t': stamp,
        'n': {str(e['id']): [e.get('transfers_in_event', 0) - e.get('transfers_out_event', 0), e['now_cost']]
              for e in d['elements'] if str(e['id']) in watch}})
    vel['samples'] = vel['samples'][-96:]            # about four days at six samples a day
    json.dump(vel, open(path('velocity.json'), 'w'))
    print('velocity sampled:', len(vel['samples']), 'snapshots on file')
except Exception as ex:
    print('velocity warning:', ex)

# ---- 3. rebuild (keep previous data for the diff) ----
if os.path.exists(path('dash_data.json')):
    os.replace(path('dash_data.json'), path('dash_data_prev.json'))
os.environ['BUILD_STAMP'] = datetime.datetime.utcnow().strftime('%d %b %H:%M UTC')
subprocess.run([sys.executable, path('build_dashboard.py')], check=True)
# serve at the Pages root too
import shutil; shutil.copy(path('fpl_dashboard.html'), path('index.html'))

# ---- 3b. results tracker: seal predictions before the deadline, score finished gameweeks ----
try:
    import tracker
    _dash = json.load(open(path('dash_data.json')))
    tracker.run(d['events'], _dash, force_freeze=os.environ.get('RUN_MODE') == 'freeze')
except Exception as ex:
    print('tracker warning (build unaffected):', ex)

# ---- 3c. record which source produced this build (lets a missed trigger self-heal) ----
try:
    import hashlib
    _h = hashlib.sha256()
    for _f in ['scout_template.html','build_dashboard.py','cloud_refresh.py','tracker.py','decide_run.py']:
        if os.path.exists(path(_f)):
            _h.update(open(path(_f),'rb').read())
    json.dump({'src': _h.hexdigest()[:16],
               'built': datetime.datetime.utcnow().replace(microsecond=0).isoformat()+'Z'},
              open(path('build_stamp.json'),'w'))
    print('build stamp:', _h.hexdigest()[:16])
except Exception as ex:
    print('build stamp warning:', ex)

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
