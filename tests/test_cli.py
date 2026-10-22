import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from applied_opt.cli import main


class CLITests(unittest.TestCase):
    def test_review_json_and_sarif_are_real_findings(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'input.py'
            source.write_text('async def f(x: str):\n    a = await fetch(x)\n    b = await classify(x)\n    return a, b\n')
            report=root/'review.json'
            self.assertEqual(main(['review',str(source),'--format','json','--output',str(report),'--fail-on','none']),0)
            findings=json.loads(report.read_text())['review']['findings']
            self.assertTrue(any(f['rule_id']=='AO001' for f in findings))
            self.assertEqual(main(['review',str(source),'--format','sarif','--output',str(report),'--fail-on','none']),0)
            self.assertEqual(json.loads(report.read_text())['version'],'2.1.0')

    def test_optimize_is_diff_only_until_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'input.py'
            original='async def f(x: str):\n    a = await fetch(x)\n    b = await classify(x)\n    return a, b\n'
            source.write_text(original)
            args=['optimize',str(source),'--safe-call','fetch','--safe-call','classify']
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args),0)
                self.assertEqual(source.read_text(),original)
                self.assertEqual(main([*args,'--apply']),0)
            self.assertIn('gather_strict',source.read_text())
            self.assertEqual(Path(str(source)+'.applied-opt.bak').read_text(),original)

    def test_compare_fails_on_missing_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            a=Path(folder)/'a.json';b=Path(folder)/'b.json'
            a.write_text(json.dumps({'runs':[{'case_id':'x','latency_ms':20,'checks':{'schedule':True}}]}))
            b.write_text(json.dumps({'runs':[{'case_id':'x','latency_ms':1,'checks':{'schedule':False}}]}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['compare',str(a),str(b)]),1)

    def test_changed_lines_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def git(*args):
                subprocess.run(['git',*args],cwd=root,check=True,capture_output=True)
            git('init','-q')
            source=root/'input.py'
            source.write_text('async def f(x: str):\n    a = await fetch(x)\n    b = await classify(x)\n    return a, b\n')
            git('add','input.py')
            git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','baseline')
            report=root/'review.json'
            self.assertEqual(main(['review',str(root),'--base','HEAD','--format','json','--output',str(report)]),0)
            self.assertEqual(json.loads(report.read_text())['review']['findings'],[])
            source.write_text(source.read_text().replace('b = await classify(x)','b = await reclassify(x)'))
            self.assertEqual(main(['review',str(root),'--base','HEAD','--format','json','--output',str(report)]),0)
            self.assertTrue(json.loads(report.read_text())['review']['findings'])
