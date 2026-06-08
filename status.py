#!/usr/bin/env python3
import json, time, uuid, hashlib, hmac, base64, requests
from pathlib import Path
from urllib.parse import urlencode

env = {}
for line in Path("C:/Users/mknig/blofin-auto-trader/.env").read_text().splitlines():
    l = line.strip()
    if l and not l.startswith('#') and '=' in l:
        k, _, v = l.partition('='); env[k.strip()] = v.strip()

KEY=env['BLOFIN_API_KEY']; SECRET=env['BLOFIN_API_SECRET']; PASS=env['BLOFIN_API_PASSPHRASE']

def sign(m,p,b=''):
    ts=str(int(time.time()*1000));n=str(uuid.uuid4())
    ph=f'{p}{m.upper()}{ts}{n}{b}'
    h=hmac.new(SECRET.encode(),ph.encode(),hashlib.sha256).hexdigest()
    return base64.b64encode(h.encode()).decode(),ts,n

s=requests.Session()
def call(m,p,params=None,body=None):
    time.sleep(0.5)
    q=urlencode({k:str(v) for k,v in params.items()}) if params else ''
    sp=p+('?'+q if q else '')
    bs=json.dumps(body,separators=(',',':')) if body else ''
    sig,ts,n=sign(m,sp,bs)
    h={'ACCESS-KEY':KEY,'ACCESS-SIGN':sig,'ACCESS-TIMESTAMP':ts,'ACCESS-NONCE':n,'ACCESS-PASSPHRASE':PASS,'Content-Type':'application/json','User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64)','Accept':'application/json','Origin':'https://blofin.com','Referer':'https://blofin.com/'}
    u='https://openapi.blofin.com'+sp
    try:
        r=(s.get(u,headers=h,timeout=12) if m=='GET' else s.post(u,headers=h,data=bs or None,timeout=12))
        if r.status_code==200 and r.text: return r.json()
    except: pass
    return None

j=call('GET','/api/v1/account/balance',{'accountType':'futures'})
eq=float(j['data']['totalEquity']) if j and j.get('data') else 0
det=j.get('data',{}).get('details',[]) if j and j.get('data') else []
av=float(det[0].get('available',0)) if det else eq
print(f'[{time.strftime("%H:%M:%S")}] EQ: ${eq:.2f} FREE: ${av:.2f}')

j2=call('GET','/api/v1/account/positions',{'accountType':'futures'})
opens=[p for p in (j2.get('data') or []) if float(p.get('positions',0) or 0)!=0] if j2 else []
for p in opens:
    avg=float(p.get('averagePrice',0)); mark=float(p.get('markPrice',0)); upl=float(p.get('upl',0))
    roe=(mark/avg-1)*100 if avg>0 else 0
    print(f"  {p['instId']:20s} {p.get('positionSide'):>5s} sz={str(p.get('positions')):>4} avg={avg:.6f} mark={mark:.6f} roe={roe:+.2f}% upl={upl:+.4f}")
