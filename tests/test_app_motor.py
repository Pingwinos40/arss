# test_app_motor.py
# Flask routes for the motor and environment against a fake Mega: cache-only
# /status and /sensor, input validation, HTTP error codes, max RPM mirror,
# and template/static folder resolution.
# Run from the repo root: python -m unittest discover -s tests -v

import time
import unittest

from test_mega_link_fake import FakeBench, MakeLink, WaitFor
import app as AppModule

# Never let a test open a real port: on the Pi the module-level link would find
# /dev/ttyACM0 on the first request and reset the Mega through DTR. Replace it
# with a link whose fake bench is unplugged.
_InertBench = FakeBench()
_InertBench.Plugged = False
AppModule.Mega = MakeLink(_InertBench)


class AppMotorTestBase(unittest.TestCase):
    BenchKwargs = {}
    LinkKwargs = {"Poll": False}

    def setUp(self):
        self.Bench = FakeBench(**self.BenchKwargs)
        self.Link = MakeLink(self.Bench, **self.LinkKwargs)
        self.OriginalMega = AppModule.Mega
        AppModule.Mega = self.Link
        self.Link.Start()
        self.Client = AppModule.app.test_client()

    def tearDown(self):
        self.Link.Close()
        AppModule.Mega = self.OriginalMega

    def MakeReady(self):
        self.assertTrue(WaitFor(lambda: self.Link.IsConnected() and self.Link._ReadyEvt.is_set()))
        self.Link.Request("STATUS")
        self.Link.Request("SENSOR?")
        self.assertTrue(self.Link.IsReady())


class PageTests(unittest.TestCase):
    def test_index_renders_from_repo_html_folder(self):
        client = AppModule.app.test_client()
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"HALO Dashboard", r.data)

    def test_daq_page_renders(self):
        r = AppModule.app.test_client().get("/daq")
        self.assertEqual(r.status_code, 200)

    def test_static_folder_resolves(self):
        r = AppModule.app.test_client().get("/static/vendor/uplot/README.md")
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_debug_off(self):
        self.assertFalse(AppModule.app.debug)


class CacheRouteTests(AppMotorTestBase):
    def test_status_served_from_cache_without_port_io(self):
        self.MakeReady()
        fake = self.Bench.Current
        before = len(fake.WrittenLines())
        for _ in range(20):
            r = self.Client.get("/status")
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
            self.assertEqual(body["link"], "LIVE")
            self.assertIn("running", body)
            self.assertIsNotNone(body["age_s"])
            self.assertIsNotNone(body["updated_utc"])
        r = self.Client.get("/sensor")
        self.assertEqual(r.get_json()["temp_c"], 23.4)
        self.assertEqual(len(fake.WrittenLines()), before)

    def test_status_reports_stale_and_down(self):
        self.Link.StaleAfterS = 0.2
        self.MakeReady()
        time.sleep(0.3)
        self.assertEqual(self.Client.get("/status").get_json()["link"], "STALE")
        self.assertEqual(self.Client.get("/sensor").get_json()["link"], "STALE")
        self.Bench.Unplug()
        self.assertTrue(WaitFor(lambda: self.Client.get("/status").get_json()["link"] == "DOWN"))
        self.assertEqual(self.Client.get("/sensor").get_json()["link"], "DOWN")


class ValidationRouteTests(AppMotorTestBase):
    def test_bad_inputs_return_400_and_write_nothing(self):
        self.MakeReady()
        fake = self.Bench.Current
        before = list(fake.WrittenLines())
        bad = [
            "/setDirection?value=CW%0ASETRPM%209999",
            "/setDirection?value=CW%0D%0ASTART",
            "/setDirection?value=LEFT",
            "/setDirection",
            "/setRpm?rpm=nan",
            "/setRpm?rpm=inf",
            "/setRpm?rpm=-5",
            "/setRpm?rpm=abc",
            "/setRpm?rpm=1e9",
            "/setFreq?hz=nan",
            "/setFreq?hz=-inf",
            "/setFreq?hz=0.1",
            "/setFreq?hz=60000",
            "/setSteps?value=-1",
            "/setSteps?value=1.5",
            "/setSteps?value=3000000000",
            "/setMaxRpm?value=0",
            "/setMaxRpm?value=1e3",
            "/setRamp?up=nan&down=0",
            "/setRamp?up=-1&down=0",
            "/setRamp?up=1&down=infinity",
        ]
        for url in bad:
            r = self.Client.get(url)
            self.assertEqual(r.status_code, 400, url)
            self.assertIn("error", r.get_json(), url)
        self.assertEqual(fake.WrittenLines(), before)

    def test_direction_is_normalized(self):
        self.MakeReady()
        r = self.Client.get("/setDirection?value=ccw")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["dir"], "0")
        self.assertIn("SETDIR CCW", self.Bench.Current.WrittenLines())
        r = self.Client.get("/setDirection?value=%2B")
        self.assertEqual(r.get_json()["dir"], "1")


class LinkErrorRouteTests(AppMotorTestBase):
    def test_503_when_link_down(self):
        self.Bench.Unplug()
        self.assertTrue(WaitFor(lambda: not self.Link.IsConnected()))
        for url in ("/start", "/stop", "/kill", "/ping", "/setRpm?rpm=10", "/setSteps?value=5"):
            r = self.Client.get(url)
            self.assertEqual(r.status_code, 503, url)
            self.assertEqual(r.get_json()["link"], "DOWN")

    def test_503_before_first_status(self):
        self.assertTrue(WaitFor(lambda: self.Link.IsConnected() and self.Link._ReadyEvt.is_set()))
        r = self.Client.get("/start")
        self.assertEqual(r.status_code, 503)
        r = self.Client.get("/setRpm?rpm=10")
        self.assertEqual(r.status_code, 503)


class TimeoutRouteTests(AppMotorTestBase):
    BenchKwargs = {"Delays": {"START": 0.5}}
    LinkKwargs = {"Poll": False, "CommandTimeoutS": 0.15}

    def test_504_on_command_timeout(self):
        self.MakeReady()
        r = self.Client.get("/start")
        self.assertEqual(r.status_code, 504)


class MotorRouteTests(AppMotorTestBase):
    BenchKwargs = {"MaxRpm": 500}

    def test_max_rpm_mirror_comes_from_status(self):
        self.MakeReady()
        r = self.Client.get("/setRpm?rpm=600")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["max_rpm"], 500)
        self.assertEqual(self.Client.get("/status").get_json()["max_rpm_limit"], 500)
        r = self.Client.get("/setRpm?rpm=600&override=1&start=1")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["start"], ["OK START"])

    def test_set_max_rpm_updates_mirror(self):
        self.MakeReady()
        r = self.Client.get("/setMaxRpm?value=800")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["max_rpm"], 800)
        self.assertEqual(self.Client.get("/status").get_json()["max_rpm_limit"], 800)
        self.assertEqual(self.Client.get("/setRpm?rpm=700").status_code, 200)

    def test_set_freq_checks_limit_with_firmware_ppr(self):
        self.MakeReady()
        # 2000 Hz at 200 pulses/rev is 600 RPM, above the 500 RPM limit.
        self.assertEqual(self.Client.get("/setFreq?hz=2000").status_code, 409)
        r = self.Client.get("/setFreq?hz=1000&start=0")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["target_hz"], "1000.000")
        self.assertEqual(r.get_json()["start"], [])

    def test_stop_route(self):
        self.MakeReady()
        self.Client.get("/start")
        r = self.Client.get("/stop")
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual(body["response"], ["OK STOP"])
        self.assertLess(body["write_latency_ms"], 50.0)
        self.assertFalse(self.Link.CurrentStatus()["running"])

    def test_set_ramp_reports_firmware_values(self):
        self.MakeReady()
        r = self.Client.get("/setRamp?up=12.5&down=3")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["ramp_up"], "12.5")
        self.assertEqual(r.get_json()["ramp_down"], "3.0")
        self.assertIn("SETRAMP 12.5 3", self.Bench.Current.WrittenLines())

    def test_set_steps(self):
        self.MakeReady()
        r = self.Client.get("/setSteps?value=400")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["response"], ["OK SETSTEPS 400"])


class CameraLazyTests(unittest.TestCase):
    def test_camera_routes_503_without_backend(self):
        original = AppModule.PickCameraBackend
        AppModule._CamBackend = None

        def NoBackend():
            raise RuntimeError("No camera backend found (install rpicam-apps or ffmpeg).")

        AppModule.PickCameraBackend = NoBackend
        try:
            client = AppModule.app.test_client()
            for url in ("/video.mjpg?ch=0", "/snapshot.jpg?ch=0"):
                r = client.get(url)
                self.assertEqual(r.status_code, 503, url)
                self.assertEqual(r.get_json()["error"], "camera unavailable")
        finally:
            AppModule.PickCameraBackend = original
            AppModule._CamBackend = None


if __name__ == "__main__":
    unittest.main()
