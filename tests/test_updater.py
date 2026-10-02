import json
import io
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
import shutil
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from bs4 import BeautifulSoup

from update_events import (AcademicTLSAdapter, Client, Collector, ROOT, SOURCES,
    RobotsAccessDenied, dates, event, main, refresh_preview, update_exit_code)


class TLSCompatibilityTests(unittest.TestCase):
    def test_only_exact_affected_https_hosts_use_compatibility_context(self):
        adapter = AcademicTLSAdapter()
        for host in adapter.COMPAT_HOSTS:
            request = requests.Request('GET', f'https://{host}/robots.txt').prepare()
            _, options = adapter.build_connection_pool_key_attributes(request, True)
            context = options['ssl_context']
            self.assertIs(context, adapter.compat_context)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertFalse(context.verify_flags & ssl.VERIFY_X509_STRICT)
        for url in ('https://www.math.ntu.edu.tw/',
                    'https://www.math.sinica.edu.tw.attacker.example/',
                    'https://www.math.sinica.edu.tw:444/',
                    'http://www.math.sinica.edu.tw/'):
            request = requests.Request('GET', url).prepare()
            _, options = adapter.build_connection_pool_key_attributes(request, True)
            self.assertIsNot(options.get('ssl_context'), adapter.compat_context)

    def test_custom_ca_bundle_is_preserved(self):
        adapter = AcademicTLSAdapter()
        request = requests.Request('GET', 'https://www.math.sinica.edu.tw/').prepare()
        _, options = adapter.build_connection_pool_key_attributes(request, 'custom-ca.pem')
        self.assertEqual(options['ca_certs'], 'custom-ca.pem')
        self.assertEqual(options['cert_reqs'], 'CERT_REQUIRED')

    def test_robots_disallow_is_still_enforced(self):
        client = Client(delay=0)
        client.session.get = Mock(return_value=Mock(
            status_code=200, url='https://www.math.sinica.edu.tw/robots.txt',
            content=b'User-agent: *\nDisallow: /',
            text='User-agent: *\nDisallow: /', headers={}, apparent_encoding='utf-8'))
        with self.assertRaises(PermissionError):
            client.get('https://www.math.sinica.edu.tw/pages/28')
        self.assertEqual(client.session.get.call_count, 1)


class PreviewTests(unittest.TestCase):
    def test_preview_embeds_current_assets_and_json_without_external_assets(self):
        data = {'sources': [], 'events': [{'title': '</script><script>alert(1)</script>'}]}
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / 'dist'
            (dist / 'data').mkdir(parents=True)
            for name in ('index.html', 'app.js', 'style.css'):
                shutil.copyfile(ROOT / 'dist' / name, dist / name)
            output = dist / 'data' / 'events.json'
            for prefix in ('./', ''):
                html = (ROOT / 'dist' / 'index.html').read_text(encoding='utf-8')
                (dist / 'index.html').write_text(html.replace('./', prefix), encoding='utf-8')
                preview = refresh_preview(output, data)
                soup = BeautifulSoup(preview.read_text(encoding='utf-8'), 'html.parser')
                self.assertFalse(soup.select('script[src], link[rel="stylesheet"]'))
                self.assertEqual(len(soup.select('script')), 1)
                self.assertEqual(soup.style.string, (dist / 'style.css').read_text(encoding='utf-8'))
                script = soup.script.string
                embedded = script.split('window.INITIAL_EVENTS=', 1)[1].split(';\n', 1)[0]
                self.assertEqual(json.loads(embedded), data)
                self.assertIn('data=await loadEvents()', script)


class RunnerFailureTests(unittest.TestCase):
    def result(self, source, status, events=(), pages=None):
        return dict(source=source, status=status, events=list(events),
                    pages=pages if pages is not None else [dict(status='failed' if status=='error' else 'fetched')],
                    issues=[], recognized=len(events), requests=1, http_log=[])

    def test_partial_policy_still_rejects_total_and_preview_failures(self):
        good=self.result(SOURCES[0], 'ok')
        bad=self.result(SOURCES[1], 'error')
        partial=self.result(SOURCES[0], 'partial', pages=[dict(status='fetched'),dict(status='failed')])
        self.assertEqual(update_exit_code([good,bad],None),2)
        self.assertEqual(update_exit_code([good,bad],None,True),0)
        self.assertEqual(update_exit_code([partial,bad],None,True),0)
        self.assertEqual(update_exit_code([bad],None,True),2)
        self.assertEqual(update_exit_code([good,bad],'broken assets',True),2)

    def test_robots_403_is_cached_and_collector_stops_without_fetching_event_pages(self):
        response=requests.Response();response.status_code=403
        denied=requests.HTTPError(response=response)
        client=Client(delay=0)
        client._request=Mock(side_effect=denied)
        for url in SOURCES[1]['seeds']:
            with self.assertRaises(RobotsAccessDenied):client.response(url)
        client._request.assert_called_once_with('https://www.math.sinica.edu.tw/robots.txt')
        collector=Collector(SOURCES[1],date(2026,10,2))
        collector.client=Client(delay=0)
        collector.client._request=Mock(side_effect=denied)
        with redirect_stdout(io.StringIO()):result=collector.run()
        self.assertEqual(result['status'],'error')
        self.assertEqual(len(result['pages']),1)
        self.assertFalse(collector.queue)

    def test_cli_keeps_failed_source_history_and_saves_healthy_updates(self):
        old=event('as_math','Previously fetched talk','2026/10/03','https://www.math.sinica.edu.tw/talk')
        new=event('ncts','New talk','2026/10/04','https://ncts.ntu.edu.tw/talk')
        old_success='2026-10-01T06:15:00+08:00'
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'dist/data/events.json'
            output.parent.mkdir(parents=True)
            for name in ('index.html','app.js','style.css'):
                shutil.copyfile(ROOT/'dist'/name,output.parent.parent/name)
            output.write_text(json.dumps({'events':[old],'sources':[dict(SOURCES[1],last_success=old_success)]}),encoding='utf-8')
            report=Path(directory)/'report.json'
            results=[self.result(SOURCES[0],'ok',[new]),self.result(SOURCES[1],'error')]
            argv=['update_events.py','--date','2026-10-02','--only','ncts','as_math',
                  '--allow-partial','--output',str(output),'--report',str(report)]
            with patch('sys.argv',argv), patch('update_events.Collector.run',side_effect=results), redirect_stdout(io.StringIO()):
                self.assertEqual(main(),0)
            data=json.loads(output.read_text(encoding='utf-8'))
            states={source['id']:source for source in data['sources']}
            retained=next(e for e in data['events'] if e['source_id']=='as_math')
            self.assertTrue(retained['stale'])
            self.assertEqual(states['as_math']['status'],'error')
            self.assertEqual(states['as_math']['last_success'],old_success)
            self.assertFalse(next(e for e in data['events'] if e['source_id']=='ncts')['stale'])
            diagnostic=json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(diagnostic['failed_sources'],['as_math'])
            self.assertTrue(diagnostic['degraded'])
            self.assertEqual(diagnostic['exit_code'],0)
            self.assertTrue(Path(diagnostic['preview']).exists())

    def test_explicit_month_day_ranges_and_invalid_dates(self):
        self.assertEqual(dates('9/30-10/2, 2026'),(date(2026,9,30),date(2026,10,2)))
        self.assertEqual(dates('10/1–7, 2026'),(date(2026,10,1),date(2026,10,7)))
        for raw in ('9/30-10/2','2/30-3/2, 2026','12/30-1/2, 2026'):
            with self.assertRaises(ValueError):dates(raw)


if __name__ == '__main__':
    unittest.main()
