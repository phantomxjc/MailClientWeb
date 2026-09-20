"""StardustMail 2.0.4 验证：提醒推送的变量取值。

背景（真机报障）：设置里开了「合并推送」后，只来 1 封新邮件也会走 format_batch，
而 format_batch 把微信模板的「主题」写死成「1 封新邮件」、「发件人」写死成「1 封邮件」，
于是微信里收到的提醒是：

    您有一封新邮件
    主题：1 封新邮件
    发件人：1 封邮件

测试按钮走的是 format_one（真实取值），所以「发送测试」看起来一切正常 ——
两条路径不一致才是真凶。

2.0.4 起：① 只有 1 封时，即使开了合并也走单封详情（拿真实主题/发件人/时间）；
          ② 多封汇总时，「发件人」不再是无信息量的「N 封邮件」，改成去重后的姓名摘要。

跑法：python tests/test_notify_vars.py
"""

import pathlib
import sys
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import notifier  # noqa: E402


def _mail(subject, sender, time_str="2026-09-14 22:12", email_id=7):
    return {
        "subject": subject, "from": sender, "account": "me@qq.com",
        "time": time_str, "email_id": email_id, "folder": "INBOX",
    }


MAIL1 = _mail("这是一封测试邮件", "张三 <zhangsan@qq.com>", email_id=7)
MAIL2 = _mail("月度报表", "李四 <lisi@163.com>", "2026-09-14 22:13", email_id=8)


def _cfg(merge):
    return {
        "enabled": True, "channel": "wechat_mp", "merge": merge,
        "min_interval": 0, "base_url": "http://127.0.0.1:8090",
        "config": {"wechat_mp": {
            "appid": "wx1", "appsecret": "s",
            "openid": "o1", "template_id": "t1",
        }},
    }


class _Capture:
    """桩掉异步派发，把要发的 jobs 抓下来查字段。"""

    def __init__(self):
        self.jobs = []
        self.calls = 0

    def __call__(self, channel, cconf, jobs):
        self.calls += 1
        self.jobs = list(jobs)


class NotifyVarsTest(unittest.TestCase):
    def setUp(self):
        self.cap = _Capture()
        self._ps = [
            mock.patch.object(notifier, "_dispatch_async", self.cap),
            mock.patch.object(notifier.db, "get_meta", lambda key: "0"),
            mock.patch.object(notifier.db, "set_meta", lambda key, val: None),
        ]
        for p in self._ps:
            p.start()

    def tearDown(self):
        for p in self._ps:
            p.stop()

    def _run(self, items, merge):
        with mock.patch.object(notifier, "get_settings",
                               lambda with_secrets=False: _cfg(merge)):
            return notifier.notify_new_mails(list(items))

    def _fields(self, idx=0):
        _title, _body, _link, fields = self.cap.jobs[idx]
        return fields

    # ── 核心回归：1 封 + 开了合并，必须拿到真实主题/发件人 ────────────────
    def test_single_mail_with_merge_keeps_real_values(self):
        n = self._run([MAIL1], merge=True)
        self.assertEqual(n, 1)
        self.assertEqual(len(self.cap.jobs), 1)
        f = self._fields()
        self.assertEqual(f["subject"], "这是一封测试邮件")
        self.assertEqual(f["sender"], "张三 <zhangsan@qq.com>")
        self.assertEqual(f["time"], "2026-09-14 22:12")
        self.assertEqual(f["count"], "1")
        # 不能再出现汇总文案
        self.assertNotIn("封新邮件", f["subject"])
        self.assertNotIn("封邮件", f["sender"])

    def test_single_mail_with_merge_has_direct_link(self):
        self._run([MAIL1], merge=True)
        _t, _b, link, _f = self.cap.jobs[0]
        self.assertTrue(link.endswith("#mail=7"), link)

    def test_single_mail_without_merge_same_as_before(self):
        self._run([MAIL1], merge=False)
        self.assertEqual(self._fields()["subject"], "这是一封测试邮件")

    # ── 多封汇总：语义正确、发件人有信息量 ───────────────────────────────
    def test_batch_subject_reads_sensibly(self):
        self._run([MAIL1, MAIL2], merge=True)
        self.assertEqual(len(self.cap.jobs), 1)
        self.assertEqual(self._fields()["subject"], "共 2 封新邮件")
        self.assertEqual(self._fields()["count"], "2")

    def test_batch_sender_is_name_summary(self):
        self._run([MAIL1, MAIL2], merge=True)
        self.assertEqual(self._fields()["sender"], "张三、李四")

    def test_batch_sender_dedupes_same_person(self):
        items = [_mail(f"通知 {i}", "张三 <zhangsan@qq.com>", email_id=i) for i in range(3)]
        self._run(items, merge=True)
        self.assertEqual(self._fields()["sender"], "张三")

    def test_batch_sender_truncates_with_etc(self):
        names = ["张三", "李四", "王五", "赵六", "钱七"]
        items = [_mail(f"第 {i} 封", f"{nm} <a{i}@qq.com>", email_id=i)
                 for i, nm in enumerate(names)]
        self._run(items, merge=True)
        s = self._fields()["sender"]
        self.assertTrue(s.startswith("张三、李四、王五"), s)
        self.assertIn("等 5 人", s)

    def test_batch_keeps_plain_address_when_no_display_name(self):
        items = [_mail("a", "a1@qq.com", email_id=1), _mail("b", "b2@qq.com", email_id=2)]
        self._run(items, merge=True)
        self.assertEqual(self._fields()["sender"], "a1@qq.com、b2@qq.com")

    def test_no_merge_two_mails_sends_two(self):
        self._run([MAIL1, MAIL2], merge=False)
        self.assertEqual(len(self.cap.jobs), 2)
        self.assertEqual(self._fields(0)["subject"], "这是一封测试邮件")
        self.assertEqual(self._fields(1)["subject"], "月度报表")

    # ── 边界：时间/发件人缺失时的兜底 ────────────────────────────────────
    def test_time_falls_back_to_now(self):
        m = _mail("无时间邮件", "张三 <a@qq.com>", time_str="", email_id=1)
        self._run([m], merge=True)
        t = self._fields()["time"]
        self.assertTrue(t.strip(), "时间为空时应该兜底成通知时间")
        self.assertRegex(t, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_missing_sender_shows_unknown(self):
        m = _mail("匿名邮件", "", email_id=1)
        self._run([m], merge=True)
        self.assertEqual(self._fields()["sender"], "未知发件人")

    def test_missing_subject_shows_placeholder(self):
        m = _mail("", "张三 <a@qq.com>", email_id=1)
        self._run([m], merge=True)
        self.assertEqual(self._fields()["subject"], "(无主题)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
