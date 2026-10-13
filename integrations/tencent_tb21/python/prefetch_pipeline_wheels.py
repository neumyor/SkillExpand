"""Prefetch exact pipeline torch wheels using bounded HTTP range transfers."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import html
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin
from urllib.request import Request,urlopen

INDEX='https://mirrors.tencent.com/pypi/simple/'
PLATFORMS=[f'manylinux_2_{n}_x86_64' for n in range(28,16,-1)]+['manylinux2014_x86_64','manylinux2010_x86_64','manylinux1_x86_64','linux_x86_64']
BLOCK=8*1024*1024


def choose(name,version):
    from packaging.tags import cpython_tags,compatible_tags
    from packaging.utils import parse_wheel_filename
    tags=list(cpython_tags((3,13),abis=['cp313'],platforms=PLATFORMS))+list(compatible_tags((3,13),interpreter='cp313',platforms=PLATFORMS))
    order={tag:i for i,tag in enumerate(tags)}
    index=INDEX+name+'/'
    data=urlopen(index,timeout=30).read().decode()
    candidates=[]
    for link in re.findall(r'href="([^"]+)"',data):
        url=urljoin(index,html.unescape(link).split('#')[0]);filename=url.rsplit('/',1)[-1]
        if not filename.endswith('.whl'):continue
        try:n,v,_,tags=parse_wheel_filename(filename)
        except Exception:continue
        compatible=tags & order.keys()
        if str(v)==version and compatible:candidates.append((min(order[t] for t in compatible),url))
    if not candidates:raise RuntimeError('No compatible wheel: '+name+'=='+version)
    return min(candidates)[1]


def download(url,folder):
    name=url.rsplit('/',1)[-1];target=folder/name
    response=urlopen(Request(url,headers={'Range':'bytes=0-0'}),timeout=30)
    cr=response.headers.get('Content-Range','')
    if not cr.startswith('bytes 0-0/'):raise RuntimeError('Server did not honor range request')
    size=int(cr.split('/')[-1]);response.close()
    if target.exists() and target.stat().st_size==size:return dict(file=name,bytes=size,url=url,reused=True)
    temporary=folder/(name+'.partial')
    with temporary.open('wb') as f:f.truncate(size)
    def part(start):
        end=min(start+BLOCK,size)-1
        for attempt in range(3):
            try:
                with urlopen(Request(url,headers={'Range':f'bytes={start}-{end}'}),timeout=60) as r:
                    if r.headers.get('Content-Range')!=f'bytes {start}-{end}/{size}':raise RuntimeError('Unexpected range response')
                    payload=r.read(BLOCK+1)
                if len(payload)!=end-start+1:raise RuntimeError('Incomplete range response')
                with temporary.open('r+b') as f:f.seek(start);f.write(payload)
                return
            except Exception:
                if attempt==2:raise
                time.sleep(attempt+1)
    with ThreadPoolExecutor(max_workers=8) as executor:list(executor.map(part,range(0,size,BLOCK)))
    temporary.replace(target)
    return dict(file=name,bytes=size,url=url,reused=False)


def main():
    from packaging.requirements import Requirement
    p=argparse.ArgumentParser();p.add_argument('--destination',type=Path,required=True);args=p.parse_args()
    args.destination.mkdir(parents=True,exist_ok=True)
    start=time.monotonic();torch=choose('torch','2.7.0')
    metadata=json.loads(urlopen('https://pypi.org/pypi/torch/2.7.0/json',timeout=30).read())
    dependencies=[('torch','2.7.0',torch)]
    for value in metadata['info']['requires_dist']:
        r=Requirement(value)
        if not (r.name.startswith('nvidia-') or r.name=='triton'):continue
        if r.marker and not r.marker.evaluate({'sys_platform':'linux','platform_machine':'x86_64','python_version':'3.13'}):continue
        pins=[x.version for x in r.specifier if x.operator=='==']
        if len(pins)!=1:raise RuntimeError('Expected pinned torch dependency')
        dependencies.append((r.name,pins[0],choose(r.name,pins[0])))
    rows=[]
    for name,version,url in dependencies:
        before=time.monotonic();row=download(url,args.destination);row.update(package=name,version=version,seconds=time.monotonic()-before);rows.append(row)
        print(json.dumps({k:row[k] for k in ('package','version','bytes','seconds')}),flush=True)
    (args.destination/'prefetch.json').write_text(json.dumps({'seconds':time.monotonic()-start,'wheels':rows},indent=2))

if __name__=='__main__':main()
