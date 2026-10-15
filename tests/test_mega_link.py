# test_mega_link.py
# MegaLink against a fake Mega: STOP priority latency, reply routing, late
# replies after a timeout, cache freshness, reconnect, and command validation.
# Run from the repo root: python -m unittest discover -s tests -v

import threading
import time
import unittest

from test_mega_link_fake import FakeBench, MakeLink, WaitFor
import mega_link
from mega_link import (CheckCommand, CommandRejected, LinkDown, LinkNotReady,
                       LinkTimeout, FirmwareError, ParseEnvLine, ParseStatusLine,
                       LINK_LIVE, LINK_STALE, LINK_DOWN)


class LinkTestBase(unittest.TestCase):
    def setUp(self):
        self.Links = []

    def tearDown(self):
        for link in self.Links:
            link.Close()

    def StartLink(self, bench, **kwargs):
        link = MakeLink(bench, **kwargs)
        self.Links.append(link)
        link.Start()
        return link

    def WaitReady(self, link, timeoutS=3.0):
        """Connected, READY seen, and one STATUS cached."""
        def Ready():
            if link.IsReady():
                return True
            if link.IsConnected() and link._ReadyEvt.is_set():
                try:
                    link.Request("STATUS")
                except mega_link.LinkError:
                    pass
            return link.IsReady()
        self.assertTrue(WaitFor(Ready, timeoutS), "link never became ready: %r" % link.Health())


class StopPriorityTests(LinkTestBase):
    def test_stop_on_wire_within_50ms_while_queue_busy(self):
        # Every queued reply is slow, the poller keeps the queue full, and
        # several motion commands wait behind the one in flight.
        bench = FakeBench(Delays={"STATUS": 0.25, "SENSOR?": 0.25, "SETRPM": 0.3})
        link = self.StartLink(bench, PollPeriodS=0.05, CommandTimeoutS=3.0, QueueWaitS=5.0)
        self.WaitReady(link)
        fake = bench.Current

        results = []

        def Worker(rpm):
            try:
                results.append(link.Request("SETRPM %d" % rpm))
            except Exception as exc:
                results.append(exc)

        workers = [threading.Thread(target=Worker, args=(10 + i,)) for i in range(3)]
        for th in workers:
            th.start()
        # Wait until a SETRPM is actually in flight on the wire.
        self.assertTrue(WaitFor(lambda: any(l.startswith("SETRPM") for l in fake.WrittenLines())))
        self.assertGreater(link.Health()["queue_depth"] + link.Health()["outstanding"], 1)

        t0 = time.monotonic()
        stopResult = link.Stop("STOP")
        tWire = fake.WriteTime("STOP")
        self.assertIsNotNone(tWire)
        latencyMs = (tWire - t0) * 1000.0
        self.assertLess(latencyMs, 50.0, "STOP reached the wire after %.1f ms" % latencyMs)
        self.assertEqual(stopResult["response"], ["OK STOP"])
        self.assertLess(stopResult["write_latency_ms"], 50.0)

        for th in workers:
            th.join(10)
        # The in-flight SETRPM got its own reply, never 'OK STOP'.
        self.assertEqual(len(results), 3)
        for res in results:
            self.assertIsInstance(res, list, res)
            self.assertTrue(res[0].startswith("OK SETRPM "), res)

    def test_kill_uses_priority_path_and_refreshes_status(self):
        bench = FakeBench()
        link = self.StartLink(bench, Poll=False)
        self.WaitReady(link)
        link.Request("START")
        link.Request("STATUS")
        self.assertTrue(link.CurrentStatus()["running"])
        result = link.Stop("KILL")
        self.assertEqual(result["response"], ["OK STOP"])
        self.assertTrue(result["status_refreshed"])
        self.assertFalse(link.CurrentStatus()["running"])

    def test_stop_when_down_raises_link_down(self):
        bench = FakeBench()
        bench.Plugged = False
        link = self.StartLink(bench)
        with self.assertRaises(LinkDown):
            link.Stop("STOP")


class ReplyRoutingTests(LinkTestBase):
    def test_late_reply_after_timeout_does_not_complete_next_command(self):
        # First SETRPM answers after 0.4 s, beyond its 0.15 s timeout. Its late
        # 'OK SETRPM 10.00' must not complete the next SETRPM (G-33).
        bench = FakeBench(Delays={"SETRPM": [0.4, 0.0]})
        link = self.StartLink(bench, Poll=False, CommandTimeoutS=0.15)
        self.WaitReady(link)
        with self.assertRaises(LinkTimeout):
            link.Request("SETRPM 10")
        reply = link.Request("SETRPM 20", TimeoutS=2.0)
        self.assertEqual(reply, ["OK SETRPM 20.00 Hz=66.667"])
        self.assertGreaterEqual(link.Health()["late_replies"], 1)

    def test_lost_reply_does_not_shift_later_replies(self):
        # The firmware never answers the first STATUS. The PING barrier after
        # the timeout clears that orphan, so the next STATUS gets its own reply.
        bench = FakeBench(DropTokens={"STATUS": 1})
        link = self.StartLink(bench, Poll=False, CommandTimeoutS=0.2)
        self.assertTrue(WaitFor(lambda: link.IsConnected() and link._ReadyEvt.is_set()))
        with self.assertRaises(LinkTimeout):
            link.Request("STATUS")
        self.assertTrue(WaitFor(lambda: link.Health()["outstanding"] == 0, 2.0), link.Health())
        reply = link.Request("STATUS")
        self.assertIsNotNone(ParseStatusLine(reply[0]))

    def test_err_reply_raises_firmware_error(self):
        bench = FakeBench(ErrTokens={"START"})
        link = self.StartLink(bench, Poll=False)
        self.WaitReady(link)
        with self.assertRaises(FirmwareError):
            link.Request("START")
        # The link stays usable.
        self.assertEqual(link.Request("PING"), ["PONG"])

    def test_cancelled_queued_command_is_never_sent(self):
        # A caller that gave up waiting for its turn must not have its command
        # written later (G-33, first half).
        bench = FakeBench(Delays={"SETRPM": 0.6})
        link = self.StartLink(bench, Poll=False, CommandTimeoutS=1.0, QueueWaitS=0.0)
        self.WaitReady(link)
        th = threading.Thread(target=lambda: link.Request("SETRPM 10"))
        th.start()
        self.assertTrue(WaitFor(lambda: "SETRPM 10" in bench.Current.WrittenLines()))
        with self.assertRaises(LinkTimeout):
            link.Request("SETDIR CW", TimeoutS=0.1)
        th.join(5)
        time.sleep(0.3)
        self.assertNotIn("SETDIR CW", bench.Current.WrittenLines())


class CacheAndStateTests(LinkTestBase):
    def test_poll_fills_cache_and_link_goes_live(self):
        bench = FakeBench()
        link = self.StartLink(bench, PollPeriodS=0.05)
        self.assertTrue(WaitFor(lambda: link.StatusSnapshot()["link"] == LINK_LIVE))
        self.assertTrue(WaitFor(lambda: link.SensorSnapshot()["data"] is not None))
        snap = link.StatusSnapshot()
        self.assertEqual(snap["data"]["max_rpm"], 1200)
        self.assertIsNotNone(snap["updated_utc"])
        self.assertLess(snap["age_s"], 2.0)
        sensor = link.SensorSnapshot()
        self.assertEqual(sensor["data"], {"temp_c": 23.4, "humidity": 45.6})

    def test_stale_then_down(self):
        bench = FakeBench()
        link = self.StartLink(bench, Poll=False, StaleAfterS=0.2)
        self.WaitReady(link)
        self.assertEqual(link.StatusSnapshot()["link"], LINK_LIVE)
        time.sleep(0.3)
        self.assertEqual(link.StatusSnapshot()["link"], LINK_STALE)
        bench.Unplug()
        self.assertTrue(WaitFor(lambda: link.StatusSnapshot()["link"] == LINK_DOWN))

    def test_snapshot_reads_do_no_port_io(self):
        bench = FakeBench()
        link = self.StartLink(bench, Poll=False)
        self.WaitReady(link)
        before = len(bench.Current.WrittenLines())
        for _ in range(50):
            link.StatusSnapshot()
            link.SensorSnapshot()
            link.CurrentStatus()
        self.assertEqual(len(bench.Current.WrittenLines()), before)

    def test_firmware_reset_invalidates_state(self):
        bench = FakeBench()
        link = self.StartLink(bench, Poll=False)
        self.WaitReady(link)
        bench.Current.EmitReady()
        self.assertTrue(WaitFor(lambda: not link.IsReady()))
        self.assertIsNone(link.CurrentStatus())
        with self.assertRaises(LinkNotReady):
            link.Request("START")
        self.assertEqual(link.Health()["firmware_resets"], 1)


class ReconnectTests(LinkTestBase):
    def test_unplug_fails_pending_then_reconnects(self):
        bench = FakeBench(Delays={"SETRPM": 1.0})
        link = self.StartLink(bench, Poll=True, PollPeriodS=0.05, CommandTimeoutS=3.0)
        self.WaitReady(link)
        first = bench.Current

        errors = []

        def Pending():
            try:
                link.Request("SETRPM 10")
            except Exception as exc:
                errors.append(exc)

        th = threading.Thread(target=Pending)
        th.start()
        self.assertTrue(WaitFor(lambda: "SETRPM 10" in first.WrittenLines()))
        t0 = time.monotonic()
        bench.Unplug()
        th.join(3)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], LinkDown)
        self.assertIn("link down", str(errors[0]))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(WaitFor(lambda: link.StatusSnapshot()["link"] == LINK_DOWN))
        self.assertTrue(first.Closed)
        with self.assertRaises(LinkDown):
            link.Request("START")

        bench.Replug()
        self.assertTrue(WaitFor(lambda: link.StatusSnapshot()["link"] == LINK_LIVE, 5.0),
                        link.Health())
        self.assertIsNot(bench.Current, first)
        self.assertEqual(link.Health()["reconnects"], 1)
        self.assertEqual(link.Request("START"), ["OK START"])

    def test_motion_held_until_status_after_reconnect(self):
        bench = FakeBench()
        link = self.StartLink(bench, Poll=False)
        self.assertTrue(WaitFor(lambda: link.IsConnected() and link._ReadyEvt.is_set()))
        with self.assertRaises(LinkNotReady):
            link.Request("START")
        self.assertNotIn("START", bench.Current.WrittenLines())
        link.Request("STATUS")
        self.assertEqual(link.Request("START"), ["OK START"])

    def test_backoff_grows_and_is_bounded(self):
        bench = FakeBench()
        bench.Plugged = False
        attempts = []
        realFind = bench.Find

        def CountingFind():
            attempts.append(time.monotonic())
            return realFind()

        link = mega_link.MegaLink(PortFinder=CountingFind, Opener=bench.Open,
                                  BackoffMinS=0.02, BackoffMaxS=0.08, BackoffJitterS=0.0)
        self.Links.append(link)
        link.Start()
        time.sleep(0.6)
        self.assertGreaterEqual(len(attempts), 5)
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        self.assertLess(gaps[0], 0.06)
        self.assertGreater(max(gaps), 0.07)
        self.assertLess(max(gaps), 0.2)
        self.assertEqual(link.StatusSnapshot()["link"], LINK_DOWN)


class CommandValidationTests(unittest.TestCase):
    def test_rejects_line_breaks_and_control_characters(self):
        for bad in ("SETDIR CW\nSETRPM 9999", "SETDIR CW\rSTART", "START\n", "PING\x00",
                    "SETRPM 10\t", "SETDIR \u00e9", "", " START", "START ", "STATUS x",
                    "SETRPM", "FLASH 1", "SETRPM " + "9" * 60):
            with self.assertRaises(CommandRejected, msg=repr(bad)):
                CheckCommand(bad)

    def test_accepts_firmware_commands(self):
        for good in ("PING", "STATUS", "SENSOR?", "START", "STOP", "KILL", "SETFREQ 100",
                     "SETRPM 30.5", "SETDIR CW", "SETSTEPS 0", "SETMAXRPM 1500",
                     "SETRAMP 10 5", "SETPPR 400"):
            self.assertEqual(CheckCommand(good), good.split(" ")[0])

    def test_rejected_command_never_reaches_the_port(self):
        bench = FakeBench()
        link = MakeLink(bench, Poll=False)
        link.Start()
        try:
            self.assertTrue(WaitFor(lambda: link.IsConnected() and link._ReadyEvt.is_set()))
            link.Request("STATUS")
            with self.assertRaises(CommandRejected):
                link.Request("SETDIR CW\nSETRPM 9999")
            with self.assertRaises(CommandRejected):
                link.Stop("STOP\nSTART")
            self.assertEqual(bench.Current.WrittenLines(), ["STATUS"])
        finally:
            link.Close()

    def test_parsers(self):
        self.assertEqual(ParseEnvLine("ENV 21.5, 40.2 %RH"), {"temp_c": 21.5, "humidity": 40.2})
        self.assertEqual(ParseEnvLine("ENV nan, nan %RH"), {"temp_c": None, "humidity": None})
        self.assertIsNone(ParseEnvLine("ENV garbage"))
        st = ParseStatusLine('{"running":false,"hz":NaN,"env":{"temp_c":null,"humidity":1.0}}')
        self.assertIsNone(st["hz"])
        self.assertIsNone(ParseStatusLine("{not json}"))


if __name__ == "__main__":
    unittest.main()
