"""发布与检查工作流只接受通知机器人派发（2026-11-12 用户决定）。"""
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOT = 'cloudlinkpos-ci-notifier[bot]'
# 工作流 → 无前置依赖的首个作业；其余作业都依赖它，首步失败即整批停止。
GUARDED = {
    'build-pos-release.yml': 'plan',
    'full-version-build.yml': 'plan',
    'miniprogram-upload.yml': 'plan',
    'private-quality.yml': 'context',
}


class DispatchGuardTest(unittest.TestCase):
    def test_first_job_rejects_non_bot_actor_first(self):
        for name, job in GUARDED.items():
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'))
                jobs = workflow['jobs']
                roots = [k for k, v in jobs.items() if not v.get('needs')]
                self.assertEqual(roots, [job], '只能有一个根作业，其他作业都要依赖它')
                first = jobs[job]['steps'][0]
                self.assertEqual(first['env']['DISPATCH_ACTOR'], '${{ github.actor }}')
                self.assertIn(f'!= "{BOT}"', first['run'])
                self.assertIn('exit 1', first['run'])

    def test_private_main_only_calls_guarded_build(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/private-main.yml').read_text(encoding='utf-8'))
        for job in workflow['jobs'].values():
            self.assertEqual(job.get('uses'), './.github/workflows/full-version-build.yml')


if __name__ == '__main__':
    unittest.main()
