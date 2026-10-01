#!/usr/bin/env python3
"""Public event collector. No API key, LLM, browser or website login required."""
from __future__ import annotations
import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil.parser import parse as parse_date
from dateutil.rrule import rrulestr
from lzstring import LZString
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Taipei')
UA = 'TaiwanScienceAgenda/2.0 (public academic event index)'
VERSION = '2.1.1'
AS = 'https://www.math.sinica.edu.tw/f59addca-1da6-47fd-9bb8-18d087da6088'
SOURCES = [
    dict(id='ncts', name='國家理論科學中心', short_name='NCTS 數學組', url='https://ncts.ntu.edu.tw/',
         seeds=['https://ncts.ntu.edu.tw/events.php', *[f'https://ncts.ntu.edu.tw/events_list.php?kind={k}' for k in (1,2,3)], 'https://ncts.ntu.edu.tw/events_list.php?kind=3&bgid=2']),
    dict(id='as_math', name='中央研究院數學研究所', short_name='中研院數學所', url=AS,
         seeds=[AS+'/pages/28',AS+'/pages/25',AS+'/pages/28?online=1',AS+'/pages/28?online=97']),
    dict(id='ntu_phys', name='國立臺灣大學物理學系', short_name='臺大物理系', url='https://www.phys.ntu.edu.tw/Default.html', seeds=['https://www.phys.ntu.edu.tw/Talks.html']),
    dict(id='ntu_math', name='國立臺灣大學數學系', short_name='臺大數學系', url='https://www.math.ntu.edu.tw/', seeds=['https://www.math.ntu.edu.tw/','https://www.math.ntu.edu.tw/research/seminar','https://www.math.ntu.edu.tw/research/conference']),
    dict(id='astro', name='國立臺灣大學天文物理研究所', short_name='臺大天文所', url='https://phys.ntu.edu.tw/astro/', seeds=['https://phys.ntu.edu.tw/astro/talks.html']),
    dict(id='asiaa', name='中央研究院天文及天文物理研究所', short_name='中研院天文所', url='https://www.asiaa.sinica.edu.tw/', seeds=['https://www.asiaa.sinica.edu.tw/','https://www.asiaa.sinica.edu.tw/activity/colloquium.php','https://www.asiaa.sinica.edu.tw/activity/lunchtalk.php']),
    dict(id='lecospa', name='臺大梁次震宇宙學與粒子天文物理學研究中心', short_name='LeCosPA', url='https://www.lecospa.ntu.edu.tw/', seeds=['https://www.lecospa.ntu.edu.tw/talks','https://www.lecospa.ntu.edu.tw/events','https://www.lecospa.ntu.edu.tw/calendar']),
    dict(id='iams', name='中央研究院原子與分子科學研究所', short_name='中研院原分所', url='https://www.iams.sinica.edu.tw/', seeds=['https://www.iams.sinica.edu.tw/events']),
]
MONTH = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?'
NUMDATE = r'(?<!\d)(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?!\d)'
ENGDATE = rf'({MONTH})\s+(\d{{1,2}})(?:\s*[-~–]\s*(?:({MONTH})\s+)?(\d{{1,2}}))?,?\s+(\d{{4}})'

def text(node):
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else ''

def dates(raw):
    """Parse dates only from an adapter's explicit event-date field."""
    if re.search(r'Every\s+\w+day',raw,re.I):
        bounds=re.search(rf'from\s+({MONTH}\s+\d{{1,2}})\s+to\s+({MONTH}\s+\d{{1,2}}),?\s+(\d{{4}})',raw,re.I)
        if bounds:return parse_date(bounds[1]+', '+bounds[3]).date(),parse_date(bounds[2]+', '+bounds[3]).date()
    found = [date(*map(int, m.groups())) for m in re.finditer(NUMDATE, raw)]
    if found:
        return found[0], found[-1]
    for m in re.finditer(ENGDATE, raw, re.I):
        month, day, end_month, end_day, year = m.groups()
        start = parse_date(f'{month} {day}, {year}').date()
        end = parse_date(f'{end_month or month} {end_day or day}, {year}').date()
        found.extend([start, end])
    if not found:
        raise ValueError('no explicit event date')
    return found[0], found[-1]

def event(source_id, title, raw_date, url, speaker='', location='', kind='學術活動'):
    if not title.strip() or urlparse(url).scheme not in ('http','https'):
        raise ValueError('missing title or unsafe URL')
    start, end = dates(raw_date)
    if end < start:
        raise ValueError('event ends before it starts')
    times = re.findall(r'(?<!\d)([0-2]?\d:[0-5]\d)(?!\d)', raw_date)
    times = [t.zfill(5) for t in times if int(t.split(':')[0]) < 24]
    status = 'cancelled' if re.search(r'cancelled|canceled|取消', title, re.I) else 'postponed' if re.search(r'postponed|延期', title, re.I) else 'scheduled'
    return dict(source_id=source_id,title=title.strip(),start_date=start.isoformat(),end_date=end.isoformat(),start_time=times[0] if times else None,end_time=times[1] if len(times)>1 else None,speaker=speaker,location=location,url=url,kind=kind,status=status,raw_date=raw_date,recurring=False)

def expand_recurrence(e, lower, upper):
    raw=e['raw_date']
    match=re.search(r'Every\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)',raw,re.I)
    if not match:
        return [e]
    # A series date range is never displayed as a continuous all-day event.
    bounds=re.search(rf'(?:from\s+)?({MONTH}\s+\d{{1,2}})\s+to\s+({MONTH}\s+\d{{1,2}}),?\s+(\d{{4}})',raw,re.I)
    if not bounds:
        raise ValueError('recurrence without an explicit supported date range')
    start=parse_date(bounds[1]+', '+bounds[3]).date()
    end=parse_date(bounds[2]+', '+bounds[3]).date()
    if end < start: raise ValueError('ambiguous recurring year boundary')
    weekday=['monday','tuesday','wednesday','thursday','friday','saturday','sunday'].index(match[1].lower())
    d=max(start,lower); result=[]
    while d <= min(end,upper):
        if d.weekday()==weekday:
            item=dict(e,start_date=d.isoformat(),end_date=d.isoformat(),recurring=True)
            result.append(item)
        d+=timedelta(days=1)
    return result

class AcademicTLSAdapter(HTTPAdapter):
    """Compatibility for the observed TWCA chains missing a key identifier.

    Keep CA, signature, expiry and hostname verification. Only relax strict
    X.509 extension checks for these exact hosts, including robots.txt. Pool
    selection runs again on redirects, so other destinations keep defaults.
    """
    COMPAT_HOSTS = frozenset({
        'www.math.sinica.edu.tw', 'www.phys.ntu.edu.tw',
        'phys.ntu.edu.tw', 'www.iams.sinica.edu.tw',
    })

    def __init__(self, *args, **kwargs):
        self.compat_context = create_urllib3_context()
        self.compat_context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        super().__init__(*args, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        parsed = urlparse(request.url)
        if (parsed.scheme == 'https' and parsed.hostname in self.COMPAT_HOSTS
                and parsed.port in (None, 443) and verify is not False):
            pool_kwargs['ssl_context'] = self.compat_context
        return host_params, pool_kwargs


class Client:
    def __init__(self, delay=1.0):
        self.session=requests.Session()
        self.session.headers['User-Agent']=os.environ.get('CRAWLER_USER_AGENT',UA)
        self.session.mount('https://',AcademicTLSAdapter(max_retries=Retry(total=2,backoff_factor=1,status_forcelist=[429,500,502,503,504],allowed_methods=['GET'],respect_retry_after_header=True)))
        self.delay=delay;self.last={};self.robots={};self.cache={};self.requests=0
        self.http_log=[]

    def _request(self,url):
        host=urlparse(url).netloc
        time.sleep(max(0,self.delay-(time.monotonic()-self.last.get(host,0))))
        self.last[host]=time.monotonic();self.requests+=1
        entry=dict(url=url);self.http_log.append(entry)
        started=time.monotonic()
        try:
            r=self.session.get(url,timeout=(10,30))
            entry.update(http_status=r.status_code,final_url=r.url,bytes=len(r.content))
        except requests.RequestException as ex:
            entry.update(error_type=type(ex).__name__,error=str(ex)[:300])
            raise
        finally:
            entry['elapsed_seconds']=round(time.monotonic()-started,2)
        # Redirects are accepted only among public official pages; no credentials are sent.
        r.raise_for_status()
        if len(r.content)>8_000_000: raise ValueError('response exceeds 8 MB')
        r.encoding='utf-8' if 'charset=utf-8' in r.headers.get('Content-Type','').lower() or b'utf-8' in r.content[:3000].lower() else r.apparent_encoding
        return r

    def response(self,url):
        origin=f'{urlparse(url).scheme}://{urlparse(url).netloc}'
        if origin not in self.robots:
            rp=RobotFileParser()
            try: rp.parse(self._request(origin+'/robots.txt').text.splitlines())
            except requests.HTTPError as ex:
                if ex.response.status_code==404: rp.parse([])
                else: raise RuntimeError(f'robots.txt HTTP {ex.response.status_code}: {origin}/robots.txt') from ex
            self.robots[origin]=rp
            self.delay=max(self.delay,rp.crawl_delay(UA) or rp.crawl_delay('*') or 0)
        if not self.robots[origin].can_fetch(UA,url):
            hint='; ASIAA public HTML mode requires --asiaa-access public-html' if urlparse(url).hostname=='www.asiaa.sinica.edu.tw' else ''
            raise PermissionError('robots.txt disallows this URL'+hint)
        return self._request(url)

    def get(self,url):
        if url in self.cache:return self.cache[url]
        r=self.response(url)
        if 'html' not in r.headers.get('Content-Type','').lower():raise ValueError('expected HTML')
        s=BeautifulSoup(r.text,'html.parser')
        if len(text(s))<60:raise ValueError('empty or JavaScript-only page')
        self.cache[url]=s
        return s

class AsiaaPublicClient(Client):
    """Explicit, bounded public-HTML mode, separate from robots enforcement.

    Records the site's robots directive without treating HTTP 200 as permission.
    Never sends a Googlebot/browser identity, login cookies, or follows off-site
    redirects. A 401/403/429 or a challenge is a failure, not a fallback trigger.
    """
    PATHS={'/','/robots.txt','/activity/index.php','/activity/colloquium.php','/activity/lunchtalk.php'}

    def __init__(self):
        super().__init__(delay=2.0)
        self.session.headers['User-Agent']=UA
        self.session.mount('https://',HTTPAdapter(max_retries=Retry(
            total=2,backoff_factor=2,status_forcelist=[500,502,503,504],
            allowed_methods=['GET'],respect_retry_after_header=False)))
        self.audit=[]
        self.robots_text=None
        self.robots_parser=None
        self.robots_error=None

    @classmethod
    def allowed_url(cls,url):
        u=urlparse(url)
        return (u.scheme=='https' and u.netloc=='www.asiaa.sinica.edu.tw'
                and u.path in cls.PATHS and not u.fragment
                and (not u.query or (u.path in {'/activity/colloquium.php','/activity/lunchtalk.php'}
                                    and re.fullmatch(r'i=\d{4}',u.query) is not None)))

    def _request(self,url):
        for _ in range(4):
            if not self.allowed_url(url):raise PermissionError('URL outside ASIAA public activity allowlist')
            host=urlparse(url).netloc
            time.sleep(max(0,self.delay-(time.monotonic()-self.last.get(host,0))))
            self.last[host]=time.monotonic();self.requests+=1
            # No session cookies are needed for these public pages.
            self.session.cookies.clear()
            r=self.session.get(url,timeout=(10,25),allow_redirects=False)
            if r.status_code in (301,302,303,307,308):
                target=urljoin(url,r.headers.get('Location',''))
                if target==url or not self.allowed_url(target):raise PermissionError('Unapproved ASIAA redirect')
                url=target;continue
            r.raise_for_status()
            if len(r.content)>8_000_000:raise ValueError('response exceeds 8 MB')
            r.encoding='utf-8'
            return r
        raise ValueError('too many ASIAA redirects')

    def response(self,url):
        if not self.allowed_url(url):raise PermissionError('URL outside ASIAA public activity allowlist')
        if self.robots_text is None and self.robots_error is None:
            try:
                r=self._request('https://www.asiaa.sinica.edu.tw/robots.txt')
                self.robots_text=r.text
                self.robots_parser=RobotFileParser()
                self.robots_parser.parse(r.text.splitlines())
            except Exception as ex:
                self.robots_error=type(ex).__name__+': '+str(ex)[:120]
                if isinstance(ex,requests.HTTPError) and ex.response is not None and ex.response.status_code in (401,403,429):raise
        allowed=self.robots_parser.can_fetch(UA,url) if self.robots_parser else None
        entry=dict(url=url,mode='public-html',robots_allowed=allowed,robots_error=self.robots_error)
        self.audit.append(entry)
        try:
            r=self._request(url)
            entry.update(http_status=r.status_code,bytes=len(r.content),final_url=r.url)
            if re.search(r'cf-chl-|g-recaptcha|h-captcha|verify you are human|just a moment',r.text,re.I):
                raise PermissionError('ASIAA returned a challenge page')
            return r
        except Exception as ex:
            entry['error']=type(ex).__name__+': '+str(ex)[:160]
            if isinstance(ex,requests.HTTPError) and ex.response is not None:entry['http_status']=ex.response.status_code
            raise

class Collector:
    def __init__(self,source,today,max_pages=35,asiaa_access='robots'):
        self.source=source;self.today=today;self.lower=today-timedelta(days=45);self.upper=today+timedelta(days=45)
        self.client=AsiaaPublicClient() if source['id']=='asiaa' and asiaa_access=='public-html' else Client()
        self.events=[];self.issues=[];self.pages=[];self.max_pages=max_pages
        self.queue=list(source['seeds']);self.seen=set();self.recognized=0
        if source['id']=='asiaa':
            self.queue=['https://www.asiaa.sinica.edu.tw/',
                        'https://www.asiaa.sinica.edu.tw/activity/index.php',
                        'https://www.asiaa.sinica.edu.tw/activity/colloquium.php',
                        'https://www.asiaa.sinica.edu.tw/activity/lunchtalk.php']
            # The retained window can cross New Year. Year-specific archives
            # are used only when actually required by that window.
            for year in {self.lower.year,self.upper.year}-{today.year}:
                for page in ['colloquium.php','lunchtalk.php']:
                    self.queue.append(f'https://www.asiaa.sinica.edu.tw/activity/{page}?i={year}')

    def issue(self,url,reason):
        self.issues.append(dict(url=url,reason=reason))

    def add(self,title,raw,url,speaker='',location='',kind='學術活動'):
        self.recognized+=1
        try:
            e=event(self.source['id'],title,raw,url,speaker,location,kind)
            if e['end_date']<self.lower.isoformat() or e['start_date']>self.upper.isoformat():return
            for row in expand_recurrence(e,self.lower,self.upper):
                if row['end_date']>=self.lower.isoformat() and row['start_date']<=self.upper.isoformat():self.events.append(row)
        except (ValueError,OverflowError) as ex:
            self.issue(url,f'無法確認活動日期：{raw[:140]} ({ex})')

    def enqueue(self,url):
        url=url.replace('http://www.math.ntu.edu.tw/','https://www.math.ntu.edu.tw/')
        if urlparse(url).hostname!=urlparse(self.source['url']).hostname:return
        if url not in self.seen and url not in self.queue:self.queue.append(url)

    def paginate(self,s,url,stop_old=False):
        # For a descending list, once its last dated record precedes the retained
        # interval, older pages cannot contribute to that interval.
        if stop_old:
            stamps=[]
            for x in s.select('article .time'):
                try:stamps.append(dates(text(x))[1])
                except ValueError:pass
            if stamps and min(stamps)<self.lower:return
        for a in s.select('a[rel~=next], .pagination a, .pager-next a, .w-pagination-next'):
            if text(a) in ['>','Next','Next ›','下一頁','›','→'] or 'next' in ' '.join(a.get('rel',[])) or 'w-pagination-next' in a.get('class',[]):self.enqueue(urljoin(url,a.get('href','')))

    def parse_ncts(self,s,url):
        tables=s.select('table.events_Title-Line0')
        for card in tables:
            a=card.select_one('.Title a')
            if not a:continue
            spans=card.select('span.Text1')
            raw=next((text(x) for x in card.select('font') if text(x)), '')
            leaf=[text(x) for x in spans[1:] if not x.find('a') and text(x)]
            venue=next((x for x in leaf if re.search(r'Room|Building|Online|Webex|HyHyve|室|樓|會議|TBA',x,re.I)), '')
            speaker=next((x for x in leaf if x!=venue),'')
            self.add(text(a),raw,urljoin(url,a['href']),speaker,venue)
        for a in s.select('a[href]'):
            if 'Previous' in text(a):self.enqueue(urljoin(url,a['href']))
        self.paginate(s,url)
        return bool(tables)

    def parse_as_math(self,s,url):
        cards=s.select('article.thumb-custom')
        for card in cards:
            a=card.select_one('.article-title a')
            if a:self.add(text(a),text(card.select_one('.time')),urljoin(url,a['href']),text(card.select_one('.thumb-info-caption-text > div')),text(card.select_one('.location')))
        self.paginate(s,url,stop_old=True)
        return bool(cards)

    def parse_phys(self,s,url):
        rows=[row for row in s.select('table.zTable tr') if row.find('td')]
        for row in rows:
            cells=row.select('td');a=row.select_one('td a[href]')
            if a and len(cells)>=4:self.add(re.sub(r'^【[^】]+】','',text(a)),text(cells[2]),urljoin(url,a['href']),text(cells[1]),text(cells[3]),'演講')
        self.paginate(s,url)
        return bool(rows)

    def parse_ntu_math(self,s,url):
        for row in s.select('div.views-row'):
            a=row.select_one('a[href*="/node/"]')
            if a and re.search(NUMDATE,text(row)):
                raw=re.split(re.escape(text(a)),text(row))[0]
                self.add(text(a),raw,urljoin(url,a['href']))
                try:
                    start,end=dates(raw)
                    if end>=self.lower and start<=self.upper:self.enqueue(urljoin(url,a['href']))
                except ValueError:pass
        detail=s.select_one('article.node-speech')
        if detail:
            title=detail.select_one('[property="dc:title"]')
            when=detail.select_one('.field-name-code-collect-time-hm')
            place=detail.select_one('.field-name-field-speech-place .field-items')
            speaker=next((re.sub(r'^演講者[：:]\s*','',text(p)) for p in detail.select('p') if re.match(r'演講者[：:]',text(p))), '')
            if title and when:self.add(title.get('content',''),text(when),url,speaker,text(place))
        for row in s.select('table.views-table tr'):
            a=row.select_one('a[href]');d=row.select_one('.views-field-field-series-date')
            if a and d and re.search(NUMDATE,text(d)):
                start,end=dates(text(d))
                if end>=self.lower and start<=self.upper:
                    if (end-start).days>14:self.enqueue(urljoin(url,a['href']))
                    else:self.add(text(a),text(d),urljoin(url,a['href']))
        if '/research/' in url:
            # Follow current series only; never treat a year-long series as a talk.
            for field in s.select('.field-collection-view'):
                title=field.select_one('h4')
                if title and '近日' in text(title):
                    for a in field.select('a[href*="/speech_series/"]'):self.enqueue(urljoin(url,a['href']))
        self.paginate(s,url)
        return bool(s.select('.view,.node'))

    def parse_asiaa(self,s,url):
        recognized=0
        for d in s.select('.date2show'):
            card=d.find_next_sibling('dd') if d.name=='dt' else d.parent
            a=card.select_one('a[href]') if card else None
            if a:
                recognized+=1
                self.add(text(a),text(d),urljoin(url,a['href']),kind='研討會')
        rows=s.select('div.row4talk')
        for row in rows:
            cols=row.find_all('div',recursive=False)
            if len(cols)<3 or not re.search(NUMDATE,text(cols[0])):continue
            recognized+=1
            title=cols[2].select_one('.modal-title')
            if title:title=text(title)
            else:
                clean=copy.copy(cols[2])
                for n in clean.select('button,.modal,strong'):n.decompose()
                title=text(clean)
            raw=text(cols[0]);time_match=re.search(r'\d{1,2}:\d{2}(?:\s*[~–-]\s*\d{1,2}:\d{2})?\s*(.*)$',raw)
            location=time_match[1].strip(' []') if time_match else ''
            kind='午餐演講' if 'lunchtalk.php' in url else '專題演講'
            label=text(cols[2].find('strong')).strip('* ')
            if label in ('Seminar','Colloquium','Tech Lunch Talk','Lunch Talk'):kind=label
            self.add(title or '講題待公告',raw,url,text(cols[1]),location,kind)
        return recognized>0

    def parse_lecospa(self,s,url):
        if url.rstrip('/').endswith('/calendar'):
            iframe=s.select_one('iframe[src*="embed.styledcalendar.com"]')
            if not iframe:return False
            calendar_id=urlparse(iframe['src']).fragment
            if not re.fullmatch(r'[A-Za-z0-9]+',calendar_id):raise ValueError('unexpected calendar identifier')
            endpoint='https://embed.styledcalendar.com/api/get-styled-calendar-events-data/?styledCalendarId='+calendar_id
            payload=self.client.response(endpoint).json()
            self.parse_styled_calendar(payload,url)
            self.pages.append(dict(url=endpoint,status='fetched'))
            return True
        cards=s.select('.w-dyn-item')
        for card in cards:
            a=card.select_one('a.list-link');titles=card.select('.medium-text');d=card.select('.date-comma')
            if not a or not titles:continue
            self.add(text(titles[-1]),' - '.join(text(x) for x in d),urljoin(url,a['href']),text(titles[0]) if len(titles)>1 else '')
        self.paginate(s,url)
        return bool(cards)

    def parse_styled_calendar(self,payload,url):
        """Public data endpoint used by the institution's embedded calendar.

        lzstring 1.0.4 expects integer UTF-16 values in decompressFromUTF16;
        this sequence adapter keeps its public API usable under Python 3.
        """
        class UTF16Values:
            def __init__(self,s):self.s=s
            def __len__(self):return len(self.s)
            def __getitem__(self,i):return ord(self.s[i])
        for group in payload['compressedEventsAndIds']:
            records=json.loads(LZString.decompressFromUTF16(UTF16Values(group['compressedEvents'])))
            for item in records:
                try:
                    tz=ZoneInfo(item.get('timeZone','Asia/Taipei'))
                    start=datetime.fromisoformat(item['start'].replace('Z','+00:00'))
                    end=datetime.fromisoformat(item.get('end',item['start']).replace('Z','+00:00'))
                    if start.tzinfo is None:start=start.replace(tzinfo=tz)
                    if end.tzinfo is None:end=end.replace(tzinfo=tz)
                    duration=end-start
                    if duration<timedelta(0):raise ValueError('negative event duration')
                    occurrences=[start]
                    if item.get('recurrence'):
                        rules=rrulestr('\n'.join(item['recurrence']),dtstart=start,forceset=True)
                        for excluded in item.get('exdate',[]):
                            ex=datetime.fromisoformat(excluded.replace('Z','+00:00'))
                            rules.exdate(ex.replace(tzinfo=tz) if ex.tzinfo is None else ex)
                        lo=datetime.combine(self.lower,datetime.min.time(),TZ)-duration
                        hi=datetime.combine(self.upper+timedelta(days=1),datetime.min.time(),TZ)
                        occurrences=rules.between(lo,hi,inc=True)
                    props=item.get('extendedProps',{})
                    for occurrence in occurrences:
                        first=occurrence.astimezone(TZ);last=(occurrence+duration).astimezone(TZ)
                        if last.date()<self.lower or first.date()>self.upper:continue
                        all_day=bool(item.get('allDay'))
                        # Calendar ends are exclusive; an event ending at midnight
                        # must not be shown as happening on the following date.
                        last_date=(last-timedelta(microseconds=1)).date() if last>first else first.date()
                        raw=first.date().isoformat()+' ~ '+last_date.isoformat()
                        e=event('lecospa',item['title'],raw,url,location=props.get('location',''),kind='行事曆活動')
                        e.update(start_time=None if all_day else first.strftime('%H:%M'),end_time=None if all_day else last.strftime('%H:%M'),recurring=bool(item.get('recurrence')),calendar_event_id=item['id'])
                        self.events.append(e);self.recognized+=1
                except (ValueError,KeyError,TypeError) as ex:self.issue(url,'行事曆事件解析失敗：'+str(ex)[:140])

    def parse_iams(self,s,url):
        rows=s.select('tbody tr')
        for row in rows:
            cells=row.select('td')
            if len(cells)==4 and re.search(NUMDATE,text(cells[0])):
                a=cells[3].find('a',href=True)
                self.add(text(cells[3]),text(cells[0]),urljoin(url,a['href']) if a else url,text(cells[2]),text(cells[1]),'演講')
        for a in s.select('main a.group'):
            h=a.find('h3');p=a.find('p')
            if h and p:self.add(text(h),text(p),urljoin(url,a['href']),kind='研討會')
        return bool(rows)

    def run(self):
        parser=getattr(self,'parse_'+('phys' if self.source['id'] in ('ntu_phys','astro') else self.source['id']))
        successes=0
        while self.queue and len(self.seen)<self.max_pages:
            url=self.queue.pop(0)
            if url in self.seen:continue
            self.seen.add(url)
            print(f"fetch {self.source['id']}: {url}",flush=True)
            try:
                soup=self.client.get(url)
                if not parser(soup,url):
                    self.issue(url,'未找到預期活動結構，需檢查網站改版或空清單。')
                else:successes+=1
                self.pages.append(dict(url=url,status='fetched'))
            except Exception as ex:
                self.issue(url,f'{type(ex).__name__}: {str(ex)[:180]}')
                self.pages.append(dict(url=url,status='failed'))
                if isinstance(self.client,AsiaaPublicClient) and (
                    isinstance(ex,PermissionError) or
                    (isinstance(ex,requests.HTTPError) and ex.response is not None and ex.response.status_code in (401,403,429))):
                    self.issue(url,'已停止本來源的後續請求；未嘗試替換身分、代理或驗證碼繞過。')
                    self.queue.clear()
                    break
        if self.queue:self.issue(self.source['url'],'已達每來源頁數上限，尚有分頁未檢查。')
        # These are actual coverage gaps observed during implementation.
        sid=self.source['id']
        if sid=='iams':self.issue(self.source['url']+'events','頁面主要提供未來活動；最近七天需依歷次成功擷取累積。')
        if sid=='ntu_phys':self.issue(self.source['url'],'目前已驗證學術演講清單；學術活動及系所活動公告尚待個別日期規則驗證。')
        status='ok' if not self.issues else 'partial' if successes else 'error'
        result=dict(source=self.source,events=self.events,status=status,issues=self.issues,pages=self.pages,recognized=self.recognized,requests=self.client.requests,http_log=self.client.http_log)
        if isinstance(self.client,AsiaaPublicClient):
            result.update(access_mode='public-html',access_audit=self.client.audit,
                          robots_text=self.client.robots_text,robots_note='公開 HTML 模式；robots.txt 指示仍另外記錄，HTTP 200 不代表機構授權。')
        return result

def stable_key(e):
    # Preserve separate source attribution while deduplicating duplicate listings.
    title=re.sub(r'【?\s*(?:cancelled|canceled)\s*】?','',e['title'],flags=re.I)
    title=re.sub(r'\s+','',title).casefold()
    return (e['source_id'],e['start_date'],e.get('start_time'),title)

def merge(previous,results,now,today):
    output=[];states=[];lower=(today-timedelta(days=45)).isoformat();upper=(today+timedelta(days=45)).isoformat()
    old_states={s['id']:s for s in previous.get('sources',[])}
    for result in results:
        source=result['source'];sid=source['id'];fresh={stable_key(e):dict(e,stale=False,seen_at=now) for e in result['events']}
        # For partial runs, retain missing records with explicit stale markers.
        # For successful runs, retain past events that naturally disappeared from
        # upcoming-only lists; remove absent future events instead of resurrecting them.
        for old in previous.get('events',[]):
            if old['source_id']!=sid:continue
            key=stable_key(old)
            if key not in fresh and (result['status']!='ok' or old['end_date']<today.isoformat()):
                fresh[key]=dict(old,stale=True)
        for e in fresh.values():
            if e['end_date']>=lower and e['start_date']<=upper:
                e['id']=hashlib.sha256(json.dumps(stable_key(e),ensure_ascii=False).encode()).hexdigest()[:20];output.append(e)
        state={k:source[k] for k in ['id','name','short_name','url']}
        good=any(p['status']=='fetched' for p in result['pages']) and result['status']!='error'
        state.update(status=result['status'],last_attempt=now,last_success=now if good else old_states.get(sid,{}).get('last_success'),pages=len(result['pages']),message='；'.join(i['reason'] for i in result['issues'][:2]) if result['issues'] else f"已檢查 {len(result['pages'])} 個來源頁面。")
        if result.get('access_mode'):
            state['access_mode']=result['access_mode']
            state['message']+=' 公開 HTML 模式；robots.txt 指示另見執行報告。'
        states.append(state)
    selected={r['source']['id'] for r in results}
    for source in SOURCES:
        if source['id'] in selected:continue
        state=old_states.get(source['id'])
        if state is None:
            state={k:source[k] for k in ['id','name','short_name','url']}
            state.update(status='pending',last_attempt=None,last_success=None,pages=0,message='本次未選取此來源。')
        states.append(state)
    for e in previous.get('events',[]):
        if e['source_id'] not in selected and e['end_date']>=lower and e['start_date']<=upper:output.append(e)
    order={s['id']:i for i,s in enumerate(SOURCES)}
    states.sort(key=lambda s:order[s['id']])
    return dict(schema_version=1,timezone='Asia/Taipei',generated_at=now,window=dict(retained_from=lower,retained_through=upper),sources=states,events=sorted(output,key=lambda e:(e['start_date'],e.get('start_time') or '99',e['source_id'])))

def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    payload=json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    # Stage and replace on the same filesystem; readers see a complete JSON file.
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False) as f:
        tmp=f.name;f.write(payload);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def refresh_preview(output,data):
    """Rebuild the offline view from the SAME JSON just written, when assets exist."""
    dist=output.parent.parent
    if output.parent.name!='data' or not all((dist/name).is_file() for name in ('index.html','style.css','app.js')):
        return None
    html=(dist/'index.html').read_text(encoding='utf-8')
    css=(dist/'style.css').read_text(encoding='utf-8')
    js=(dist/'app.js').read_text(encoding='utf-8')
    boot='window.INITIAL_EVENTS='+json.dumps(data,ensure_ascii=False).replace('<','\\u003c')+';\n'
    document=BeautifulSoup(html,'html.parser')
    style_link=document.find('link',href=lambda value: value in ('style.css','./style.css'))
    script_link=document.find('script',src=lambda value: value in ('app.js','./app.js'))
    if style_link is None or script_link is None:
        raise ValueError('index.html must reference style.css and app.js')
    style=document.new_tag('style');style.string=css
    style_link.replace_with(style)
    script_link.decompose()
    html=str(document)
    html=html.replace('</body>','<script>'+boot+js.replace('</script','<\\/script')+'</script></body>')
    target=dist.parent/'preview.html'
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=target.parent,delete=False) as f:
        tmp=f.name;f.write(html);f.flush();os.fsync(f.fileno())
    os.replace(tmp,target)
    return target.resolve()

def source_summary(result,previous,today):
    fresh={stable_key(e):e for e in result['events']}
    old={stable_key(e) for e in previous.get('events',[]) if e['source_id']==result['source']['id']}
    def count(lo,hi):
        return sum(e['start_date']<=hi.isoformat() and e['end_date']>=lo.isoformat() for e in fresh.values())
    return dict(fetched_unique=len(fresh),new_events=len(fresh.keys()-old),
                recent_seven_days=count(today-timedelta(days=6),today),
                upcoming_seven_days=count(today,today+timedelta(days=6)))

def main():
    # Windows redirected logs must not fail on non-CP950 characters in titles/URLs.
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='backslashreplace')
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--version',action='version',version=VERSION)
    ap.add_argument('--date',type=date.fromisoformat,help='override Taiwan date for reproducible QA')
    ap.add_argument('--output',type=Path,default=ROOT/'dist/data/events.json')
    ap.add_argument('--report',type=Path,default=ROOT/'reports/latest.json')
    ap.add_argument('--workers',type=int,default=3)
    ap.add_argument('--max-pages',type=int,default=35)
    ap.add_argument('--only',nargs='+',choices=[s['id'] for s in SOURCES],help='update selected institutions while retaining the others')
    ap.add_argument('--asiaa-access',choices=['robots','public-html'],default=os.environ.get('ASIAA_ACCESS_MODE','robots'),
                    help='robots enforces robots.txt (default); public-html explicitly fetches only allowlisted public ASIAA pages and records robots directives')
    args=ap.parse_args();today=args.date or datetime.now(TZ).date();now=datetime.now(TZ).isoformat(timespec='seconds')
    args.output=args.output.resolve();args.report=args.report.resolve()
    if args.output==args.report:ap.error('--output and --report must use different paths')
    if args.asiaa_access not in ('robots','public-html'):ap.error('ASIAA_ACCESS_MODE must be robots or public-html')
    previous=json.loads(args.output.read_text(encoding='utf-8-sig')) if args.output.exists() else {}
    selected=[s for s in SOURCES if not args.only or s['id'] in args.only]
    print(f'Updater {VERSION} | Taiwan date: {today} | ASIAA mode: {args.asiaa_access}',flush=True)
    print('Sources: '+', '.join(s['id'] for s in selected),flush=True)
    print(f'JSON output: {args.output}\nDiagnostic report: {args.report}',flush=True)
    if args.only:print('Only selected sources are refreshed; other sources retain their previous data.',flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(args.workers,4))) as pool:
        results=list(pool.map(lambda s:Collector(s,today,args.max_pages,args.asiaa_access).run(),selected))
    data=merge(previous,results,now,today)
    atomic_json(args.output,data)
    preview=None;preview_error=None
    try:preview=refresh_preview(args.output,data)
    except Exception as ex:preview_error=f'{type(ex).__name__}: {ex}'
    summaries={r['source']['id']:source_summary(r,previous,today) for r in results}
    atomic_json(args.report,dict(generated_at=now,completed_at=datetime.now(TZ).isoformat(timespec='seconds'),
        version=VERSION,python_version=sys.version.split()[0],platform=sys.platform,
        effective_date=today.isoformat(),output=str(args.output),asiaa_access=args.asiaa_access,
        selected_sources=[s['id'] for s in selected],preview=str(preview) if preview else None,preview_error=preview_error,
        sources=[{k:v for k,v in r.items() if k not in ('events','source')}|{'id':r['source']['id'],'events':len(r['events']),**summaries[r['source']['id']]} for r in results]))
    for r in results:
        stats=summaries[r['source']['id']]
        print(f"{r['source']['id']}: {r['status']}, fetched={stats['fetched_unique']}, new={stats['new_events']}, recent7={stats['recent_seven_days']}, upcoming7={stats['upcoming_seven_days']}",flush=True)
        for issue in r['issues']:print('  '+issue['reason']+' | '+issue['url'],flush=True)
    print(f'Updated JSON: {args.output}',flush=True)
    print(f'Updated offline preview: {preview}' if preview else 'Offline preview not updated: '+(preview_error or 'website assets not found next to output; inspect the JSON output path.'),flush=True)
    print(f'Diagnostic report: {args.report}',flush=True)
    # Network / parser failures cause the daily workflow to report failure after
    # publishing honest health information. Known coverage limitations are partial.
    return 2 if preview_error or any(r['status']=='error' or any(p['status']=='failed' for p in r['pages']) for r in results) else 0

if __name__=='__main__':raise SystemExit(main())
