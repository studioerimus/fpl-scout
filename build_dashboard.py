#!/usr/bin/env python3
"""
Scout dashboard build pipeline (v5).
Inputs (same folder): fpl-bootstrap-static.json, fpl-fixtures.json,
optional lineups.json (Rotowire), odds.json (Oddschecker), eo.json (LiveFPL).
Folds predicted lineups / injuries / odds into ratings + rolling xP, runs an ILP
optimiser for the best legal XV per horizon (1-5), and injects into scout_template.html.
"""
import json, os, re, unicodedata, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
def load(name, default=None):
    p = os.path.join(HERE, name)
    if not os.path.exists(p): return default
    return json.load(open(p))

d  = load('fpl-bootstrap-static.json')
fx = load('fpl-fixtures.json')
LU = load('lineups.json', {}) or {}
OD = load('odds.json', {}) or {}
EO = load('eo.json', {}) or {}

teams = {t['id']: {'short': t['short_name'], 'name': t['name']} for t in d['teams']}
POS = {1:'GK',2:'DEF',3:'MID',4:'FWD'}

MT = load('myteam.json', None)
# multi-team: myteams.json is a list of entries (id/name/picks/bank). Falls back to the single team.
ENTRIES = load('myteams.json', None)
if not ENTRIES and MT and MT.get('picks'):
    ENTRIES = [MT]
ENTRIES = [e for e in (ENTRIES or []) if e.get('picks')]
if ENTRIES:
    MT = ENTRIES[0]
if MT and MT.get('picks'):
    _pk = MT['picks']
    SQUAD    = [p[0] for p in _pk]
    STARTING = [p[0] for p in _pk if p[1] <= 11]
    CAPTAIN  = next((p[0] for p in _pk if p[2]), SQUAD[9])
    VICE     = next((p[0] for p in _pk if p[3]), SQUAD[0])
    BANK     = MT.get('bank', 0)/10.0
else:
    SQUAD    = [496,8,201,469,208,40,368,426,525,411,346,109,113,499,197]
    STARTING = [496,8,201,469,208,40,368,426,525,411,346]
    CAPTAIN, VICE, BANK = 411, 496, 0.0

HORIZON = 6                          # planning window: next 6 gameweeks
nxt = [e['id'] for e in d['events'] if e.get('is_next')]
start_gw = nxt[0] if nxt else next((e['id'] for e in d['events'] if not e.get('finished')), 1)
upcoming = [e['id'] for e in d['events'] if e['id'] >= start_gw]        # full remaining season for the ticker

from collections import defaultdict
tf = defaultdict(dict)
for f in fx:
    ev = f['event']
    if ev in upcoming:
        tf[f['team_h']][ev] = (teams[f['team_a']]['short'], 1, f['team_h_difficulty'])
        tf[f['team_a']][ev] = (teams[f['team_h']]['short'], 0, f['team_a_difficulty'])
def team_runs(tid, n=None):
    n = n or len(upcoming)
    return [{'gw':ev,'opp':tf[tid][ev][0],'h':tf[tid][ev][1],'d':tf[tid][ev][2]} for ev in upcoming[:n] if ev in tf[tid]]
def fdr_avg(tid, n=5):
    ds=[tf[tid][ev][2] for ev in upcoming[:n] if ev in tf[tid]]
    return round(sum(ds)/len(ds),2) if ds else None

def num(x):
    try: return float(x)
    except: return 0.0
def per90(t,m): return round(t/(m/90),2) if m and m>=450 else 0.0

# ---- name matching helpers ----
def norm(s): return ''.join(c for c in unicodedata.normalize('NFKD', s or '') if not unicodedata.combining(c)).lower()
def toks(s): return {t for t in re.split(r"[\s.\-']+", norm(s)) if len(t) >= 3}

FDRM = {1:1.25,2:1.12,3:1.00,4:0.86,5:0.72,0:1.00}

# ---- bookmaker projection: fit Poisson goal means from 1X2, derive team xG + clean-sheet ----
import math
def _pois(k,l): return math.exp(-l)*l**k/math.factorial(k)
def _probs(lh,la,mx=8):
    ph=pd=pa=0.0
    for i in range(mx):
        pi=_pois(i,lh)
        for j in range(mx):
            pr=pi*_pois(j,la)
            if i>j: ph+=pr
            elif i==j: pd+=pr
            else: pa+=pr
    return ph,pd,pa
def _fit(pH,pD,pA):
    best=None
    for a in range(4,71):
        lh=a/20.0
        for b in range(4,71):
            la=b/20.0
            mh,md,ma=_probs(lh,la)
            e=(mh-pH)**2+(md-pD)**2+(ma-pA)**2
            if best is None or e<best[0]: best=(e,lh,la)
    return best[1],best[2]
GW1ODDS={}
for m in OD.get('matches',[]):
    lh,la=_fit(m['pH'],m['pD'],m['pA'])
    GW1ODDS[m['h']]={'xgF':round(lh,2),'xgA':round(la,2),'cs':round(math.exp(-la),3),'opp':m['a'],'home':1}
    GW1ODDS[m['a']]={'xgF':round(la,2),'xgA':round(lh,2),'cs':round(math.exp(-lh),3),'opp':m['h'],'home':0}
def gw1_mult(pt, short):
    o=GW1ODDS.get(short)
    if not o: return None
    if pt in (3,4): return max(0.65, min(1.5, o['xgF']/1.35))   # attackers scale on team expected goals
    return max(0.65, min(1.5, 0.60 + o['cs']*1.3))             # GK/DEF scale on clean-sheet odds

# ---- build players ----
players = []
for e in d['elements']:
    tid=e['team']; short=teams[tid]['short']; price=e['now_cost']/10; mins=e['minutes']; tp=e['total_points']
    xg=num(e['expected_goals']); xa=num(e['expected_assists']); xgi=num(e['expected_goal_involvements']); gs=e.get('goals_scored',0)
    ptoks = toks(e.get('web_name','')) | toks(e.get('second_name','')) | toks(e.get('first_name',''))
    # predicted start
    pstart=None
    if short in LU.get('start',{}):
        pstart=False
        for R in LU['start'][short]:
            if toks(R) & ptoks: pstart=True; break
    # injury (team-scoped) then FPL status
    injtag=''
    for sname,tag in LU.get('inj',{}).get(short,{}).items():
        if toks(sname) & ptoks: injtag=tag; break
    if not injtag and e['status']!='a':
        injtag={'i':'INJ','s':'SUS','u':'OUT','n':'OUT','d':'DBT'}.get(e['status'],'')
    # graded minutes probability (item 2): predicted XI + last-season start rate + availability
    sr=min(1.0, e['starts']/34.0)
    if injtag in ('OUT','SUS','INJ'): mp=0.0
    elif pstart is True: mp=0.90 if injtag in ('QUES','DBT') else 0.97
    elif pstart is False: mp=0.30+0.20*sr
    else: mp=(0.35+0.45*sr) if injtag in ('QUES','DBT') else (0.55+0.42*sr)
    mp=max(0.0,min(1.0,mp))
    # per-90 underlying (FPL Fran's ranking stats)
    goals=gs; assists=e.get('assists',0)
    xg90=per90(xg,mins); xa90=per90(xa,mins)
    npxg90=round(xg90*0.82,2) if e['penalties_order']==1 else xg90
    ga90=per90(goals+assists,mins)
    cbrit90=num(e.get('defensive_contribution_per_90')) or per90(num(e.get('defensive_contribution',0)),mins)
    p={'i':e['id'],'n':e['web_name'],'tm':short,'tid':tid,'pos':POS[e['element_type']],'pt':e['element_type'],
       'price':price,'own':num(e['selected_by_percent']),'ep':num(e['ep_next']),'form':num(e['form']),
       'ppg':num(e['points_per_game']),'pts':tp,'ppm':round(tp/price,1) if price else 0,'min':mins,'starts':e['starts'],
       'xgi':round(xgi,1),'xg':round(xg,1),'xa':round(xa,1),'xgc':round(num(e['expected_goals_conceded']),1),
       'ict':num(e['ict_index']),'thr':num(e['threat']),'crea':num(e['creativity']),
       'xgi90':per90(xgi,mins),'xg90':xg90,'npxg90':npxg90,'xa90':xa90,'ga90':ga90,'cbrit90':cbrit90,
       'xgc90':num(e.get('expected_goals_conceded_per_90')) or per90(num(e['expected_goals_conceded']),mins),
       'dc90':num(e.get('defensive_contribution_per_90')) or per90(num(e.get('defensive_contribution',0)),mins),
       'cs90':num(e.get('clean_sheets_per_90')),'sv90':num(e.get('saves_per_90')),
       'gs':gs,'xgd':round(gs-xg,1),
       'net':e.get('transfers_in_event',0)-e.get('transfers_out_event',0),
       'cc':e.get('cost_change_event',0)/10,'ccs':e.get('cost_change_start',0)/10,
       'pen':e['penalties_order'],'ck':e['corners_and_indirect_freekicks_order'],'fk':e['direct_freekicks_order'],
       'stat':e['status'],'cop':e['chance_of_playing_next_round'],'news':(e.get('news') or '').strip(),
       'fdr':fdr_avg(tid),'pstart':pstart,'inj':injtag,'sp':round(mp,2),'mp':round(mp,2)}
    # component talent (points per start, neutral fixture) — blended with FPL ep after loop
    et=e['element_type']
    Gp={1:6,2:6,3:5,4:4}[et]; CSp={1:4,2:4,3:1,4:0}[et]; thr={1:99,2:10,3:12,4:12}[et]
    p_dc=1.0/(1.0+math.exp(-(cbrit90-thr)*0.7)) if cbrit90>0 else 0.0
    talent=2.0 + npxg90*Gp + xa90*3.0 + 0.28*CSp + p_dc*2.0
    if et in (1,2): talent-=0.35
    if et==1: talent+=num(e.get('saves_per_90'))/3.0
    p['_talent']=talent
    players.append(p)

# ---- projection: blend component model with FPL ep ("stand on shoulders"), roll over fixtures ----
_est=[p for p in players if p['min']>=1000]
_mt=(sum(p['_talent'] for p in _est)/len(_est)) if _est else 1.0
_me=(sum(p['ep'] for p in _est)/len(_est)) if _est else 1.0
_scale=(_me/_mt) if _mt else 1.0
ACC=load('accuracy.json',{}) or {}
CALIB=ACC.get('calibration',{}) or {}      # learned from sealed predictions vs actual results
for p in players:
    blended=0.5*(p['_talent']*_scale)+0.5*p['ep']
    blended*=CALIB.get(p['pos'],1.0)        # correction earned from the record, not guessed
    p['pg']=round(blended,2)
    runs=team_runs(p['tid'],HORIZON); om=gw1_mult(p['pt'],p['tm']); mp=p['mp']
    xph=[]; xpg=[]; cum=0.0
    for k,r in enumerate(runs):
        m=(om if (om is not None and k==0) else FDRM.get(r['d'],1.0))
        wk=mp*blended*m
        cum+=wk; xph.append(round(cum,2)); xpg.append(round(wk,2))
    while len(xph)<HORIZON: xph.append(xph[-1] if xph else 0.0)
    while len(xpg)<HORIZON: xpg.append(0.0)          # blank gameweek = no fixture, no points
    p['xph']=xph; p['xpg']=xpg; del p['_talent']

# ---- consistency layer: how points arrive, not just how many ----
# DEFCON pays 2 pts per match for 10+ CBIT (DEF) or 12+ CBIRT (MID/FWD), capped at 2.
# The reward is per-match and binary, so an average misleads: what matters is how OFTEN
# a player clears it. Measured rates replace modelled ones as the season banks matches.
DC_THR = {1: None, 2: 10, 3: 12, 4: 12}
DC_BIAS = 0.97      # our Poisson estimate ran ~2 pts high against 50 measured rates
DC_EST_CAP = 0.75   # no measured rate in a full season of real data exceeded 71%, so an
                    # estimate above this is a data artefact, not a player. Mainly catches
                    # position reclassification: FPL counts ball recoveries for midfielders
                    # but not defenders, so a MID->DEF switch carries an inflated per-90.

def _pois_at_least(k, lam):
    if lam <= 0: return 0.0
    term, acc = math.exp(-lam), 0.0
    for i in range(k):
        acc += term
        term *= lam / (i + 1)
    return max(0.0, min(1.0, 1.0 - acc))

# ---- what the season has actually shown us so far ----
REAL = {}          # player -> counters built from sealed, scored gameweeks
OPP_DC = {}        # opponent team -> DEFCON conceded, split by CB and by midfielder
_res = os.path.join(HERE, 'results')
if os.path.isdir(_res):
    for _f in sorted(os.listdir(_res)):
        try: _r = json.load(open(os.path.join(_res, _f)))
        except Exception: continue
        for _w in _r.get('rows', []):
            if (_w.get('min') or 0) < 60: continue          # judge starters only
            pt = {'GK':1,'DEF':2,'MID':3,'FWD':4}.get(_w.get('pos'))
            a = REAL.setdefault(_w['i'], {'starts':0,'dc_hit':0,'dc_seen':0,'four':0,
                                          'cs':0,'cs_dc':0,'one_ret':0,'one_ret_bon':0})
            a['starts'] += 1
            if (_w.get('act') or 0) >= 4: a['four'] += 1
            thr = DC_THR.get(pt)
            if thr and _w.get('dc') is not None:
                a['dc_seen'] += 1
                hit = _w['dc'] >= thr
                if hit: a['dc_hit'] += 1
                if _w.get('cs'):                             # clean sheet AND defcon?
                    a['cs'] += 1
                    if hit: a['cs_dc'] += 1
                o = _w.get('opp')
                if o and pt in (2, 3):
                    k = 'cb' if pt == 2 else 'mid'
                    t = OPP_DC.setdefault(o, {'cb':[0,0], 'mid':[0,0]})
                    t[k][1] += 1
                    if hit: t[k][0] += 1
            if (_w.get('ret') or 0) == 1:                    # bonus off a single return
                a['one_ret'] += 1
                if (_w.get('bon') or 0) > 0: a['one_ret_bon'] += 1

opp_dc = {}
for tid, v in OPP_DC.items():
    sh = teams.get(tid, {}).get('short')
    if not sh: continue
    opp_dc[sh] = {k: (round(v[k][0]/v[k][1], 2) if v[k][1] >= 6 else None) for k in ('cb','mid')}

for p_ in players:
    thr = DC_THR.get(p_['pt'])
    r = REAL.get(p_['i']) or {}
    st = r.get('starts', 0)
    p_['dcThr'] = thr
    if not thr:
        p_['dcHit'] = None; p_['dcN'] = st; p_['dcReal'] = False
    elif r.get('dc_seen', 0) >= 4:
        p_['dcHit'] = round(r['dc_hit']/r['dc_seen'], 2); p_['dcN'] = r['dc_seen']; p_['dcReal'] = True
    elif p_['min'] >= 450:
        # FPL publishes a per-90 rate at any sample size, so a player with a handful of
        # minutes reads absurdly high. Only estimate off a real body of minutes.
        p_['dcHit'] = round(min(DC_EST_CAP, _pois_at_least(thr, p_['dc90']) * DC_BIAS), 2)
        p_['dcN'] = st; p_['dcReal'] = False
    else:
        p_['dcHit'] = None; p_['dcN'] = st; p_['dcReal'] = False

    # floor: how often a start turns into a genuinely useful score
    p_['floor4'] = round(r['four']/st, 2) if st >= 4 else None
    p_['floorN'] = st
    # do his clean sheets and his DEFCON arrive together, or instead of each other?
    p_['csDc'] = round(r['cs_dc']/r['cs'], 2) if r.get('cs', 0) >= 3 else None
    # does a single return still earn bonus?
    p_['bon1'] = round(r['one_ret_bon']/r['one_ret'], 2) if r.get('one_ret', 0) >= 3 else None

# ---- Value Rating (balanced, within position, nailedness-aware) ----
import bisect
def pct_fn(vals):
    s=sorted(vals); nn=len(s)
    def f(v):
        if v is None: return 0.5
        lo=bisect.bisect_left(s,v); hi=bisect.bisect_right(s,v)
        return ((lo+hi)/2)/(nn-1) if nn>1 else 0.5
    return f
W={'GK':dict(ep=.25,val=.25,nail=.20,threat=.00,cs=.20,fix=.10),
   'DEF':dict(ep=.22,val=.25,nail=.18,threat=.10,cs=.15,fix=.10),
   'MID':dict(ep=.30,val=.20,nail=.15,threat=.25,cs=.00,fix=.10),
   'FWD':dict(ep=.30,val=.18,nail=.15,threat=.27,cs=.00,fix=.10)}
def xgc90v(p): return (p['xgc']/(p['min']/90)) if p['min']>=450 else None
for pos in ['GK','DEF','MID','FWD']:
    grp=[p for p in players if p['pos']==pos]
    fEp=pct_fn([p['pg'] for p in grp]); fVal=pct_fn([p['ppm'] for p in grp])
    fNail=pct_fn([p['min'] for p in grp]); fThr=pct_fn([p['xgi'] for p in grp])
    fCs=pct_fn([-xgc90v(p) for p in grp if xgc90v(p) is not None])
    fFix=pct_fn([-(p['fdr']) for p in grp if p['fdr'] is not None])
    w=W[pos]; raws=[]
    for p in grp:
        cs=fCs(-xgc90v(p)) if xgc90v(p) is not None else 0.5
        fxs=fFix(-p['fdr']) if p['fdr'] is not None else 0.5
        raw=(w['ep']*fEp(p['pg'])+w['val']*fVal(p['ppm'])+w['nail']*fNail(p['min'])
             +w['threat']*fThr(p['xgi'])+w['cs']*cs+w['fix']*fxs)
        raw*=(0.35+0.65*p['sp'])           # nailedness from predicted lineups / injuries
        p['_raw']=raw; raws.append(raw)
    lo,hi=min(raws),max(raws)
    for p in grp:
        r=(p['_raw']-lo)/(hi-lo) if hi>lo else 0.5
        p['vr']=round(1+9*r,1)
        p['tier']=('A' if p['vr']>=8.5 else 'B' if p['vr']>=7 else 'C' if p['vr']>=5.5 else 'D' if p['vr']>=4 else 'E')
        del p['_raw']

# ---- EO (real if provided, else estimate) ----
att=[p for p in players if p['pt'] in (3,4)]; fCap=pct_fn([p['ep'] for p in att])
eo_real=EO.get('eo',{})
for p in players:
    if p['n'] in eo_real or str(p['i']) in eo_real:
        p['eo']=round(num(eo_real.get(p['n'], eo_real.get(str(p['i']),0))),1); p['eoLive']=True
    else:
        capfrac=(fCap(p['ep'])**2) if p['pt'] in (3,4) else 0.03
        p['eo']=round(p['own']+p['own']*capfrac,1); p['eoLive']=False

# ---- ILP optimiser: best legal XV per horizon ----
import pulp
byid={p['i']:p for p in players}
pool=[p for p in players if p['min']>=250 or p['pstart'] or p['i'] in SQUAD]
def optimise(h):
    xp={p['i']:p['xph'][h-1] for p in pool}
    prob=pulp.LpProblem('sq',pulp.LpMaximize)
    x={p['i']:pulp.LpVariable(f'x{p["i"]}',cat='Binary') for p in pool}
    y={p['i']:pulp.LpVariable(f'y{p["i"]}',cat='Binary') for p in pool}
    c={p['i']:pulp.LpVariable(f'c{p["i"]}',cat='Binary') for p in pool}
    prob += pulp.lpSum(xp[p['i']]*(y[p['i']]+c[p['i']]) for p in pool) - 0.03*pulp.lpSum(p['price']*(x[p['i']]-y[p['i']]) for p in pool)
    prob += pulp.lpSum(x.values())==15
    prob += pulp.lpSum(p['price']*x[p['i']] for p in pool)<=100.0
    for pt,cnt in [(1,2),(2,5),(3,5),(4,3)]:
        prob += pulp.lpSum(x[p['i']] for p in pool if p['pt']==pt)==cnt
    for tid in set(p['tid'] for p in pool):
        prob += pulp.lpSum(x[p['i']] for p in pool if p['tid']==tid)<=3
    for p in pool:
        prob += y[p['i']]<=x[p['i']]; prob += c[p['i']]<=y[p['i']]
    prob += pulp.lpSum(y.values())==11
    prob += pulp.lpSum(y[p['i']] for p in pool if p['pt']==1)==1
    prob += pulp.lpSum(y[p['i']] for p in pool if p['pt']==2)>=3
    prob += pulp.lpSum(y[p['i']] for p in pool if p['pt']==2)<=5
    prob += pulp.lpSum(y[p['i']] for p in pool if p['pt']==3)>=2
    prob += pulp.lpSum(y[p['i']] for p in pool if p['pt']==3)<=5
    prob += pulp.lpSum(y[p['i']] for p in pool if p['pt']==4)>=1
    prob += pulp.lpSum(y[p['i']] for p in pool if p['pt']==4)<=3
    prob += pulp.lpSum(c.values())==1
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    squad=[p['i'] for p in pool if x[p['i']].value()>0.5]
    xi=[p['i'] for p in pool if y[p['i']].value()>0.5]
    cap=[p['i'] for p in pool if c[p['i']].value()>0.5]
    nd=sum(1 for i in xi if byid[i]['pt']==2); nm=sum(1 for i in xi if byid[i]['pt']==3); nf=sum(1 for i in xi if byid[i]['pt']==4)
    return {'squad':squad,'xi':xi,'cap':cap[0] if cap else None,'form':f'{nd}-{nm}-{nf}',
            'cost':round(sum(byid[i]['price'] for i in squad),1),
            'xp':round(sum(byid[i]['xph'][h-1] for i in xi)+ (byid[cap[0]]['xph'][h-1] if cap else 0),1)}
optimal={str(h):optimise(h) for h in range(1,HORIZON+1)}

# ---- transfer suggestions from Jack's squad (bank 0) per horizon ----
def suggest(h, topn=4, squad=None):
    SQUAD = squad if squad is not None else globals()['SQUAD']
    bank=round(100-sum(byid[i]['price'] for i in SQUAD),1)
    cur_clubs=defaultdict(int)
    for i in SQUAD: cur_clubs[byid[i]['tid']]+=1
    out=[]
    for i in SQUAD:
        s=byid[i]; best=None
        for r in players:
            if r['i'] in SQUAD or r['pt']!=s['pt']: continue
            if r['price']>s['price']+bank: continue
            if r['tid']!=s['tid'] and cur_clubs[r['tid']]>=3: continue
            delta=r['xph'][h-1]-s['xph'][h-1]
            if best is None or delta>best[1]: best=(r,delta)
        if best and best[1]>0.3: out.append({'out':i,'in':best[0]['i'],'delta':round(best[1],1)})
    out.sort(key=lambda z:-z['delta'])
    return out[:topn]
suggestions={str(h):suggest(h) for h in range(1,HORIZON+1)}

# ---- selling prices (FPL returns only half of any profit, rounded down) ----
PURCH=load('purchases.json',{}) or {}
def sell_price(entry_id, pid, cur):
    """cur is in £m. Falls back to current price when we have no record of the buy."""
    book=PURCH.get(str(entry_id)) or {}
    buy=book.get(str(pid))
    if buy is None: return round(cur,1), False
    cur_t, buy_t = int(round(cur*10)), int(buy)
    if cur_t <= buy_t: return round(cur_t/10,1), True      # a loss is taken in full
    return round((buy_t + (cur_t-buy_t)//2)/10, 1), True   # profit halved, rounded down

# ---- per-entry payloads (multi-team) ----
entries=[]
for E in ENTRIES:
    pk=E['picks']
    esq=[p[0] for p in pk]
    valid=[i for i in esq if i in byid]
    entries.append({
        'id': E.get('entry'), 'name': E.get('name') or f"Team {E.get('entry')}",
        'squad': esq, 'starting':[p[0] for p in pk if p[1]<=11],
        'captain': next((p[0] for p in pk if p[2]), esq[9] if len(esq)>9 else esq[0]),
        'vice': next((p[0] for p in pk if p[3]), esq[0]),
        'bank': E.get('bank',0)/10.0,
        'sell': {str(i): sell_price(E.get('entry'), i, byid[i]['price'])[0] for i in valid},
        'sell_known': any(sell_price(E.get('entry'), i, byid[i]['price'])[1] for i in valid),
        'suggestions': {str(h):suggest(h,squad=valid) for h in range(1,HORIZON+1)} if len(valid)==15 else {},
    })

# ---- team rating tables for the Fixture Lab (editable in-app) ----
gk_cs=defaultdict(int); team_goals=defaultdict(int)
for e in d['elements']:
    if e['element_type']==1: gk_cs[e['team']]=max(gk_cs[e['team']], e.get('clean_sheets',0))
    team_goals[e['team']]+=e.get('goals_scored',0)
PROMOTED={'COV','HUL','IPS','LEE','SUN'}
def atk_scale(g): return 5 if g>=68 else 4 if g>=52 else 3 if g>=42 else 2 if g>=33 else 1
def def_scale(c): return 5 if c>=14 else 4 if c>=11 else 3 if c>=8 else 2 if c>=5 else 1
tmeta={t['id']:t for t in d['teams']}
ratings={'overall':{},'attack':{},'defence':{}}
for tid in teams:
    sh=teams[tid]['short']; oh=tmeta[tid]['strength_overall_home']; oa=tmeta[tid]['strength_overall_away']
    ratings['overall'][sh]={'h':oh,'a':oa}
    if sh in PROMOTED:
        ratings['attack'][sh]={'h':oh,'a':oa}; ratings['defence'][sh]={'h':oh,'a':oa}
    else:
        a=atk_scale(team_goals[tid]); df=def_scale(gk_cs[tid])
        ratings['attack'][sh]={'h':a,'a':a}; ratings['defence'][sh]={'h':df,'a':df}

# ---- mini-league intelligence + transfer velocity ----
LG=load('leagues.json',{}) or {}
VEL=load('velocity.json',{}) or {}
league_out=[]
for L in LG.get('leagues',[]):
    rivals=L.get('rivals',[]) or []
    withpicks=[r for r in rivals if r.get('picks')]
    own={}; capown={}
    for r in withpicks:
        for pid in r['picks']: own[pid]=own.get(pid,0)+1
        if r.get('captain'): capown[r['captain']]=capown.get(r['captain'],0)+1
    n=len(withpicks)
    def pct(c): return round(100.0*c/n,1) if n else None
    # effective ownership inside the league: a captain counts twice
    eo={pid: round(100.0*(own[pid]+capown.get(pid,0))/n,1) for pid in own} if n else {}
    mine=set(SQUAD)
    diffs_for=sorted([{'i':pid,'own':pct(own.get(pid,0)),'eo':eo.get(pid,0.0)} for pid in mine if pid in byid],
                     key=lambda z:(z['own'] if z['own'] is not None else 0))
    theirs=sorted([{'i':pid,'own':pct(c),'eo':eo.get(pid)} for pid,c in own.items() if pid not in mine and pid in byid],
                  key=lambda z:-(z['own'] or 0))[:15]
    league_out.append({'id':L.get('id'),'name':L.get('name'),'size':L.get('size'),
        'rivals':[{k:r.get(k) for k in ('entry','name','player','rank','total','gw','captain','chip')} for r in rivals],
        'covered':n,'eo':eo,'own':{str(k):pct(v) for k,v in own.items()},
        'mine_rare':[z for z in diffs_for if z['own'] is not None][:15],'their_edge':theirs,
        'me':LG.get('me')})

# transfer velocity: net flow since the earliest sample we still hold
vel_out={}
sm=VEL.get('samples',[])
if len(sm)>=2:
    first,last=sm[0],sm[-1]
    for pid,(net,cost) in last.get('n',{}).items():
        f=first.get('n',{}).get(pid)
        if not f: continue
        vel_out[pid]={'net':net,'delta':net-f[0],'from':first['t'],'to':last['t'],
                      'price_moved':round((cost-f[1])/10,1)}

out={'ratings':ratings,'gw1odds':GW1ODDS,'meta':{'start_gw':start_gw,'deadline':d['events'][start_gw-1]['deadline_time'],
             'team_name':(MT.get('name') if MT else None) or "Jack's FPL Team",
             'season_started':any(e.get('finished') for e in d['events']),
             'built':os.environ.get('BUILD_STAMP',''),'built_iso':datetime.datetime.utcnow().replace(microsecond=0).isoformat()+'Z',
             'eo_live':bool(eo_real),'horizon':HORIZON,'gws':upcoming[:HORIZON],
             'lineups':bool(LU.get('start')),'odds':bool(OD.get('matches'))},
     'entries':entries,'accuracy':ACC,'opp_dc':opp_dc,'leagues':league_out,'velocity':vel_out,
     'teams':{teams[tid]['short']:{'name':teams[tid]['name'],'runs':team_runs(tid),'avg5':fdr_avg(tid,5)} for tid in teams},
     'team_summary':sorted([{'short':teams[tid]['short'],'name':teams[tid]['name'],'runs':team_runs(tid),'avg5':fdr_avg(tid,5)} for tid in teams],key=lambda x:(x['avg5'] if x['avg5'] else 9)),
     'players':players,'squad':SQUAD,'starting':STARTING,'captain':CAPTAIN,'vice':VICE,
     'optimal':optimal,'suggestions':suggestions}
json.dump(out,open(os.path.join(HERE,'dash_data.json'),'w'))
tmpl=open(os.path.join(HERE,'scout_template.html')).read()
html=tmpl.replace('__DATA__',json.dumps(out)); assert '__DATA__' not in html
open(os.path.join(HERE,'fpl_dashboard.html'),'w').write(html)
print(f"Built {len(players)} players | optimal GW{start_gw} xp(h3)={optimal['3']['xp']} form {optimal['3']['form']} cost {optimal['3']['cost']} | {len(html)} bytes")
