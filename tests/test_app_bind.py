# test_app_bind.py
# Listen address for app.run (#14): loopback by default, ARSS_BIND and
# ARSS_PORT override it, a bad port fails loudly instead of falling back.
# Run from the repo root: python -m unittest discover -s tests -v

import os
import sys
import unittest

SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "RaspberryPiWebserver")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

# Importing app opens nothing: the Mega link threads start on the first request.
import app as AppModule


class ResolveBindAddressTest(unittest.TestCase):
    def test_default_is_loopback_port_5000(self):
        self.assertEqual(AppModule.ResolveBindAddress({}), ("127.0.0.1", 5000))

    def test_blank_values_use_defaults(self):
        Env = {"ARSS_BIND": "  ", "ARSS_PORT": ""}
        self.assertEqual(AppModule.ResolveBindAddress(Env), ("127.0.0.1", 5000))

    def test_arss_bind_overrides_host(self):
        Env = {"ARSS_BIND": "0.0.0.0"}
        self.assertEqual(AppModule.ResolveBindAddress(Env), ("0.0.0.0", 5000))

    def test_arss_port_overrides_port(self):
        Env = {"ARSS_BIND": "0.0.0.0", "ARSS_PORT": "8080"}
        self.assertEqual(AppModule.ResolveBindAddress(Env), ("0.0.0.0", 8080))

    def test_bad_port_raises(self):
        for Text in ("abc", "0", "65536", "-1", "50.5"):
            with self.subTest(port=Text):
                with self.assertRaises(ValueError):
                    AppModule.ResolveBindAddress({"ARSS_PORT": Text})

    def test_reads_process_environment_by_default(self):
        Saved = {Key: os.environ.get(Key) for Key in ("ARSS_BIND", "ARSS_PORT")}
        try:
            os.environ.pop("ARSS_PORT", None)
            os.environ["ARSS_BIND"] = "0.0.0.0"
            self.assertEqual(AppModule.ResolveBindAddress(), ("0.0.0.0", 5000))
            os.environ.pop("ARSS_BIND")
            self.assertEqual(AppModule.ResolveBindAddress(), ("127.0.0.1", 5000))
        finally:
            for Key, Value in Saved.items():
                if Value is None:
                    os.environ.pop(Key, None)
                else:
                    os.environ[Key] = Value


if __name__ == "__main__":
    unittest.main()
