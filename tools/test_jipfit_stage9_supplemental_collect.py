#!/usr/bin/env python3
"""Stage9 smoke tests. No network or database required."""
import csv
import tempfile
import unittest
from pathlib import Path

import jipfit_stage9_supplemental_collect as s

class Stage9Tests(unittest.TestCase):
    def make_targets(self, folder):
        path=Path(folder)/"targets.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w=csv.DictWriter(f,fieldnames=["원본JSON파일명","우선순위","연결 단지ID"])
            w.writeheader()
            w.writerow({"원본JSON파일명":"11110_10100_0_0056_0045.json","우선순위":"P1","연결 단지ID":"test-1"})
            w.writerow({"원본JSON파일명":"11110_10100_0_0057_0000.json","우선순위":"P2","연결 단지ID":"test-2"})
        return path

    def test_target_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            p=self.make_targets(temp)
            p1, bad=s.read_targets(p,scope="p1")
            allrows, bad2=s.read_targets(p,scope="all")
            self.assertEqual(len(p1),1)
            self.assertEqual(len(allrows),2)
            self.assertEqual((bad,bad2),([],[]))
            self.assertEqual(p1[0].filename,"11110_10100_0_0056_0045.json")

    def test_xml_response(self):
        xml=b"<response><header><resultCode>00</resultCode></header><body><items><item><bldNm>Test</bldNm></item></items><totalCount>1</totalCount><pageNo>1</pageNo></body></response>"
        total,items,page=s.decode_xml_page(xml)
        self.assertEqual((total,page),(1,1))
        self.assertEqual(items[0]["bldNm"],"Test")

    def test_xml_zero(self):
        xml=b"<response><header><resultCode>00</resultCode></header><body><items></items><totalCount>0</totalCount></body></response>"
        n,rows,_=s.decode_xml_page(xml)
        self.assertEqual((n,rows),(0,[]))

    def test_quota_is_stopped(self):
        xml=b"<response><header><resultCode>22</resultCode><resultMsg>Daily limit</resultMsg></header></response>"
        with self.assertRaises(s.QuotaStop):
            s.decode_xml_page(xml)

    def test_daily_counter_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/"budget.json"
            b=s.Budget(p,2)
            b.account_before_request()
            b.account_before_request()
            self.assertEqual(s.Budget(p,2).count,2)
            with self.assertRaises(s.QuotaStop):
                b.account_before_request()

if __name__=="__main__":unittest.main()
