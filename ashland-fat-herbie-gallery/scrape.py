#!/usr/bin/env python3
"""Archive numbered public Shopify product listings from Ashland Leather."""
import json, re, sys
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = 'https://ashlandleather.com'
DEST = Path(__file__).with_name('data.json')
LIMIT = 260

def session():
    s = requests.Session()
    s.headers['User-Agent'] = 'Mozilla/5.0 (compatible; PersonalVisualArchive/1.0)'
    s.mount('https://', HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1.3, status_forcelist=[429,500,502,503,504], respect_retry_after_header=True)))
    return s

def image_url(v):
    if isinstance(v, dict): v = v.get('src') or v.get('url')
    if not isinstance(v, str): return None
    if v.startswith('//'): v = 'https:' + v
    elif v.startswith('/'): v = urljoin(BASE,v)
    return v if v.startswith('https://') else None

def parse_product(n, obj):
    handle = f'one-off-fat-herbie-{n}'
    if obj.get('handle') != handle: return None
    variants = obj.get('variants') or []
    variant = variants[0] if variants else {}
    name = (variant.get('public_title') or variant.get('title') or '').strip()
    if name.lower() in ('default title','default'): name = ''
    if not name: name = obj.get('title','').replace('One-Off Fat Herbie','').strip(' -|') or f'One-off #{n}'
    pics = []
    for value in obj.get('images') or [obj.get('featured_image')]:
        url = image_url(value)
        if url and url not in pics: pics.append(url)
    price = variant.get('price')
    try: price = round((float(price) if isinstance(price,str) and '.' in price else float(price)/100),2) if price is not None else None
    except (ValueError,TypeError): price = None
    return {'number':n,'name':name,'title':obj.get('title','One-Off Fat Herbie'),
        'url':f'{BASE}/products/{handle}','images':pics,'price':price,
        'description':BeautifulSoup(obj.get('description') or '', 'html.parser').get_text(' ', strip=True)[:1300],
        'available':any(v.get('available',False) for v in variants)}

def from_html(n, html):
    handle = f'one-off-fat-herbie-{n}'
    soup = BeautifulSoup(html,'html.parser')
    canonical = soup.find('link',rel='canonical')
    if canonical and handle not in canonical.get('href',''): return None
    heading = soup.title.get_text(' ',strip=True) if soup.title else ''
    if 'Fat Herbie' not in heading: return None
    pics = []
    for meta in soup.select('meta[property="og:image"],meta[property="og:image:secure_url"],.product__media img'):
        url = image_url(meta.get('content') or meta.get('src'))
        if url and url not in pics: pics.append(url)
    color = ''
    text = soup.get_text(' ',strip=True)
    m = re.search(r'Color/Finish\\s+(.{6,100}?)\\s+(?:Variant|Product variants|Quantity|Sold out)',text)
    if m: color = m.group(1).strip()
    return {'number':n,'name':color or heading.split('|')[0],'title':'One-Off Fat Herbie',
        'url':f'{BASE}/products/{handle}','images':pics,'price':None,
        'description':'','available':False}

def collect(n):
    s = session()
    handle = f'one-off-fat-herbie-{n}'
    try:
        r = s.get(f'{BASE}/products/{handle}.js',timeout=20)
        if r.ok:
            obj = parse_product(n,r.json())
            if obj: return n,obj,None
        if r.status_code in (404,410): return n,None,'missing'
        r = s.get(f'{BASE}/products/{handle}',timeout=20)
        if r.ok:
            obj = from_html(n,r.text)
            if obj: return n,obj,None
        return n,None,f'HTTP {r.status_code}'
    except Exception as e:
        return n,None,type(e).__name__+': '+str(e)[:90]
    finally: s.close()

def collect_catalogs():
    """Collect still-public older one-offs through Shopify collection feeds."""
    out = {}
    s = session()
    try:
        for coll in ('fat-herbie','limited-releases-sold-out'):
            for page in range(1,9):
                url = f'{BASE}/collections/{coll}/products.json?limit=250&page={page}'
                try:
                    response = s.get(url,timeout=25)
                    if not response.ok:
                        print(f'Collection {coll} page {page}: HTTP {response.status_code}',flush=True)
                        break
                    rows = response.json().get('products',[])
                    if not rows: break
                    for item in rows:
                        m = re.fullmatch(r'one-off-fat-herbie-(\\d+)',item.get('handle',''))
                        if m:
                            number = int(m.group(1))
                            parsed = parse_product(number,item)
                            if parsed and parsed['images']: out[number] = parsed
                    if len(rows)<250: break
                except Exception as exc:
                    print(f'Collection {coll} page {page}: {type(exc).__name__}: {exc}',flush=True)
                    break
    finally: s.close()
    print(f'Catalog discovery: {len(out)} numbered one-offs',flush=True)
    return out

def main():
    old = {}
    if DEST.exists():
        try: old = {p['number']:p for p in json.loads(DEST.read_text()).get('products',[])}
        except Exception: pass
    products,missing,errors={},[],{}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(collect,n) for n in range(180,LIMIT+1)]
        for future in as_completed(futures):
            n,p,error=future.result()
            if p: products[n]=p
            elif error=='missing':
                missing.append(n)
                if n in old: products[n]=old[n]
            else:
                errors[str(n)]=error
                if n in old: products[n]=old[n]
            if (len(products)+len(missing)+len(errors)) % 30 == 0:
                print(f'Checked {len(products)+len(missing)+len(errors)}/{LIMIT}, archived {len(products)}',flush=True)
    for n,p in collect_catalogs().items():
        if n not in products or len(p['images']) > len(products[n]['images']):
            products[n] = p
    if not products:
        print('No products returned; refusing to overwrite good archive.',file=sys.stderr)
        sys.exit(1)
    payload={'source':BASE,'max_checked':LIMIT,
        'collected_at':datetime.now(timezone.utc).isoformat(),
        'found':len(products),'missing':sorted(missing),'errors':errors,
        'products':[products[n] for n in sorted(products,reverse=True)]}
    DEST.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+'\n')
    print(f'Archived {len(products)} products and {sum(len(p["images"]) for p in products.values())} photo references',flush=True)

if __name__=='__main__':main()
