import json
import unittest
from unittest.mock import patch

import pods_local


class FakeResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.data


class SingleInstanceTests(unittest.TestCase):
    @patch.object(pods_local.urllib.request, 'urlopen')
    def test_recognizes_existing_eks_console(self, urlopen):
        urlopen.return_value = FakeResponse({'app': pods_local.APP_ID, 'version': '3.3'})
        self.assertEqual(pods_local.existing_instance_url(8765), 'http://127.0.0.1:8765')

    @patch.object(pods_local.urllib.request, 'urlopen')
    def test_does_not_reuse_an_unknown_local_service(self, urlopen):
        urlopen.return_value = FakeResponse({'app': 'another-service'})
        self.assertEqual(pods_local.existing_instance_url(8765), '')

    @patch.object(pods_local.webbrowser, 'open')
    def test_no_browser_mode_does_not_open_browser(self, open_browser):
        self.assertEqual(pods_local.reopen_existing('http://127.0.0.1:8765', True), 0)
        open_browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
