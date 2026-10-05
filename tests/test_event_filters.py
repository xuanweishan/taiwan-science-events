from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from lzstring import LZString
from event_filters import exclude_internal_events, filter_file, is_internal_event
from update_events import Collector, SOURCES, event, merge


class InternalEventTests(unittest.TestCase):
    def test_internal_labels_and_public_titles(self):
        for title in ('Group Meeting(Pisin)', 'GROUP MEETING (Nam)',
                      'Research group-meeting', 'Lab Meeting', 'Laboratory Meeting',
                      'Team_Meeting', 'Internal Meeting', 'Staff Meeting',
                      'Ｇｒｏｕｐ　Ｍｅｅｔｉｎｇ', '物理組會', '實驗室會議'):
            with self.subTest(title=title):self.assertTrue(is_internal_event(title))
        for title in ('Group theory seminar', 'Working group workshop',
                      'Annual Meeting on Astrophysics', 'Journal Club',
                      'Public lecture', '研究群公開演講', '團隊合作研討會',
                      'Linear and Bilinear Bochner-Riesz Means on Métivier Groups',
                      'On Linear and Bilinear Bochner-Riesz Means on Métivier Groups',
                      'A study of communication in group meetings',
                      '公開演講：組會中的科學溝通'):
            with self.subTest(title=title):self.assertFalse(is_internal_event(title))

    def test_bochner_riesz_talk_survives_collection_and_old_snapshot_filter(self):
        title='Linear and Bilinear Bochner-Riesz Means on Métivier Groups'
        url='https://ncts.ntu.edu.tw/events_1_detail.php?nid=3198'
        collector=Collector(SOURCES[0],date(2026,10,5))
        collector.add(title,'14:30 - 16:30, October 6, 2026 (Tuesday)',url,
                      location='Room 509+Online Meeting')
        self.assertEqual(len(collector.events),1)
        data=exclude_internal_events({'events':collector.events})
        self.assertEqual(data['events'][0]['url'],url)
        self.assertEqual(data['events'][0]['start_date'],'2026-10-06')
        self.assertEqual(data['events'][0]['start_time'],'14:30')

    def test_regular_collector_skips_internal_before_date_parsing(self):
        collector=Collector(SOURCES[0],date(2026,10,5))
        collector.add('Group Meeting', 'unannounced', 'https://ncts.ntu.edu.tw/test')
        collector.add('Public lecture', '2026/10/06', 'https://ncts.ntu.edu.tw/test')
        self.assertEqual([e['title'] for e in collector.events],['Public lecture'])
        self.assertFalse(collector.issues)

    def test_calendar_skips_internal_before_recurrence_expansion(self):
        collector=Collector(next(s for s in SOURCES if s['id']=='lecospa'),date(2026,10,5))
        records=[dict(title='Group Meeting',recurrence=['invalid recurrence']),
                 dict(id='public',title='Public lecture',start='2026-10-06T10:00:00+08:00',end='2026-10-06T11:00:00+08:00')]
        payload={'compressedEventsAndIds':[{'compressedEvents':LZString.compressToUTF16(json.dumps(records))}]}
        collector.parse_styled_calendar(payload,'https://www.lecospa.ntu.edu.tw/calendar')
        self.assertEqual([e['title'] for e in collector.events],['Public lecture'])
        self.assertFalse(collector.issues)

    def test_merge_cleans_fresh_failed_and_unselected_source_records(self):
        old=[event(sid,title,'2026/10/06','https://example.com/event')
             for sid,title in [('ncts','Group Meeting'),('as_math','Lab Meeting'),
                               ('lecospa','Team Meeting'),('as_math','Public lecture')]]
        results=[dict(source=SOURCES[0],status='partial',events=old[:1],pages=[dict(status='fetched')],issues=[]),
                 dict(source=SOURCES[1],status='error',events=[],pages=[dict(status='failed')],issues=[])]
        data=merge({'events':old},results,'2026-10-05T10:00:00+08:00',date(2026,10,5))
        self.assertEqual([e['title'] for e in data['events']],['Public lecture'])
        self.assertTrue(data['events'][0]['stale'])

    def test_snapshot_cleanup_preserves_metadata_and_is_idempotent(self):
        data={'generated_at':'original timestamp','sources':[{'id':'ncts','status':'error'}],
              'events':[{'title':'Group Meeting'},{'title':'Public lecture'}]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'events.json'
            path.write_text(json.dumps(data),encoding='utf-8')
            self.assertEqual(filter_file(path),1)
            filtered=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(filtered,exclude_internal_events(data))
            self.assertEqual(filtered['generated_at'],data['generated_at'])
            self.assertEqual(filtered['sources'],data['sources'])
            self.assertEqual(filter_file(path),0)


if __name__ == '__main__':unittest.main()
