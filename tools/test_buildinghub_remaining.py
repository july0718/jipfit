import csv
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import jipfit_buildinghub_remaining as hub

OK_XML = b'''<response><header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header><body><items><item><bldNm>A</bldNm></item></items><numOfRows>100</numOfRows><pageNo>1</pageNo><totalCount>1</totalCount></body></response>'''
EMPTY_XML = b'''<response><header><resultCode>00</resultCode></header><body><items/><pageNo>1</pageNo><totalCount>0</totalCount></body></response>'''
QUOTA_XML = b'''<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</errMsg><returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>'''


class FakeResponse:
    def __init__(self, body):
        self.body = body
    def read(self):
        return self.body
    def __enter__(self):
        return self
    def __exit__(self, *unused):
        return False


class AuditTests(unittest.TestCase):
    def test_parcel_key_and_csv_aliases(self):
        p = hub.parse_parcel({'sigunguCd': '11710', 'bjdongCd': '10700', 'platGbCd': '0', 'bun': '171', 'ji': '0'})
        self.assertEqual(p.filename, '11710_10700_0_0171_0000.json')
        q = hub.parse_parcel({'법정동코드': '1171010700', '지번주소': '서울특별시 송파구 가락동 산12-3'})
        self.assertEqual(q.filename, '11710_10700_1_0012_0003.json')
        with self.assertRaises(ValueError):
            hub.parse_parcel({'sigunguCd': '11710', 'bjdongCd': '10700', 'bun': 'BL-3-1'})

    def test_quota_error_22_and_23(self):
        for code in ('22', '23'):
            data = QUOTA_XML.replace(b'22', code.encode())
            with self.assertRaises(hub.QuotaStop):
                hub.decode_xml_page(data)
        with self.assertRaises(hub.QuotaStop):
            hub.decode_xml_page(b'<response><header><resultCode>23</resultCode></header></response>')

    def test_ok_pages(self):
        self.assertEqual(hub.decode_xml_page(OK_XML)[0], 1)
        self.assertEqual(hub.decode_xml_page(EMPTY_XML)[0], 0)
        self.assertEqual(len(hub.decode_xml_page(EMPTY_XML)[1]), 0)

    def test_skip_existing_and_resume_with_3_services(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            inp = root / 'jipfit_full_targets.csv'
            out = root / 'results'
            out.mkdir()
            inp.write_text('sigunguCd,bjdongCd,platGbCd,bun,ji,address\n11710,10700,0,171,0,A\n11710,10700,0,172,0,B\n', encoding='utf-8')
            old = out / '11710_10700_0_0171_0000.json'
            old.write_text('{"legacy":true}', encoding='utf-8')
            calls = []
            def fake_urlopen(req, timeout):
                calls.append(req.full_url)
                return FakeResponse(OK_XML)
            with mock.patch.dict(os.environ, {'DATA_GO_KR_API_KEY': 'dummy-decoding-key'}):
                with mock.patch.object(hub.urllib.request, 'urlopen', fake_urlopen):
                    exit_code = hub.main(['--targets', str(inp), '--results', str(out), '--expected-total', '2', '--max-records', '0', '--daily-request-cap', '10', '--pause', '0'])
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(calls), 3)
            self.assertEqual(json.loads(old.read_text())['legacy'], True)
            new = out / '11710_10700_0_0172_0000.json'
            self.assertTrue(new.exists())
            self.assertEqual(json.loads(new.read_text())['building_registry']['totalCount'], 1)
            summary = json.loads((out / '.state' / 'last-run-summary.json').read_text())
            self.assertEqual(summary['completed_now'], 2)
            self.assertEqual(summary['remaining_valid'], 0)
            self.assertEqual(summary['api_requests_today'], 3)

    def test_quota_stop_leaves_no_partial_result(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            inp, out = root / 'targets.csv', root / 'results'
            inp.write_text('sigunguCd,bjdongCd,platGbCd,bun,ji\n11710,10700,0,173,0\n', encoding='utf-8')
            counter = [0]
            def fake_urlopen(req, timeout):
                counter[0] += 1
                if counter[0] == 2:
                    return FakeResponse(QUOTA_XML)
                return FakeResponse(OK_XML)
            with mock.patch.dict(os.environ, {'DATA_GO_KR_API_KEY': 'dummy-decoding-key'}):
                with mock.patch.object(hub.urllib.request, 'urlopen', fake_urlopen):
                    code = hub.main(['--targets', str(inp), '--results', str(out), '--expected-total', '1', '--max-records', '0', '--daily-request-cap', '10', '--pause', '0'])
            self.assertEqual(code, 3)
            self.assertEqual(counter[0], 2)
            self.assertFalse((out / '11710_10700_0_0173_0000.json').exists())
            summary = json.loads((out / '.state' / 'last-run-summary.json').read_text())
            self.assertEqual(summary['remaining_valid'], 1)
            self.assertIn('QuotaStop', summary['stop_reason'])
            self.assertFalse((out / '.state' / 'audit.lock').exists())

    def test_locally_enforced_daily_limit(self):
        with tempfile.TemporaryDirectory() as root:
            b = hub.Budget(Path(root) / 'request-budget.json', 1)
            b.account_before_request()
            with self.assertRaises(hub.QuotaStop):
                b.account_before_request()
            self.assertEqual(json.loads((Path(root) / 'request-budget.json').read_text())['requests'], 1)


if __name__ == '__main__':
    unittest.main()
