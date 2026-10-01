import json
from pathlib import Path
import shutil
import ssl
import tempfile
import unittest
from unittest.mock import Mock

import requests
from bs4 import BeautifulSoup

from update_events import AcademicTLSAdapter, Client, ROOT, refresh_preview


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


if __name__ == '__main__':
    unittest.main()
