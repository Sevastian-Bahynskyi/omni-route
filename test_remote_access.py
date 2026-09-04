from __future__ import annotations

import subprocess
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import remote_access as remote


class RemoteAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        remote._APPROVAL_URL = None

    def test_timeout_preserves_approval_output(self) -> None:
        message = b'https://login.tailscale.com/f/serve?node=test'
        with patch.object(remote, 'tailscale_cli', return_value='/test/tailscale'), patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('serve', 8, output=message)):
            result = remote._run('serve')
        self.assertEqual(result.returncode, 124)
        self.assertEqual(remote._approval_link(result.stdout), message.decode())
        self.assertIsNone(remote._approval_link('https://login.tailscale.com.evil/f/serve?node=test'))

    def test_disabled_services_do_not_advertise_links(self) -> None:
        with patch.object(remote, 'tailscale_cli', return_value='tailscale'), patch.object(remote, '_connection', return_value=(True, 'connected', 'test.ts.net', None)), patch.object(remote, '_serve_state', return_value=(False, False)):
            state = remote.status()
        self.assertIsNone(state['url'])
        self.assertIsNone(state['serverUrl'])

    def test_approval_is_reported_and_retained(self) -> None:
        state={'connected':True,'portConflict':False,'dashboardEnabled':False,'serverEnabled':False,'enabled':False}
        with patch.object(remote, 'status', return_value=state), patch.object(remote, '_run', return_value=subprocess.CompletedProcess([],124,stdout='https://login.tailscale.com/f/serve?node=test')) as run:
            result=remote.enable()
        self.assertFalse(result['ok'])
        self.assertIn('approval', result['error'])
        self.assertIsNotNone(remote._APPROVAL_URL)
        self.assertEqual(run.call_count,1)

    def test_both_services_are_enabled_on_distinct_ports(self) -> None:
        state={'connected':True,'portConflict':False,'dashboardEnabled':False,'serverEnabled':False,'enabled':False}
        middle={**state,'dashboardEnabled':True}
        final={**middle,'serverEnabled':True,'enabled':True}
        with patch.object(remote,'status',side_effect=[state,middle,final,final]), patch.object(remote,'_run',return_value=subprocess.CompletedProcess([],0,stdout='')) as run:
            result=remote.enable()
        self.assertTrue(result['ok'])
        self.assertEqual(run.call_args_list[0].args[-2:],('--https=8443',remote.DASHBOARD_TARGET))
        self.assertEqual(run.call_args_list[1].args[-2:],('--https=8444',remote.SERVER_TARGET))

    def test_different_or_extra_routes_are_not_overwritten(self) -> None:
        result=subprocess.CompletedProcess([],0,stdout='{"Web":{"test.ts.net:8443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8787"},"/other":{"Proxy":"http://127.0.0.1:9999"}}}}}')
        with patch.object(remote,'_run',return_value=result):
            self.assertEqual(remote._serve_state(),(False,True))
        with patch.object(remote,'status',return_value={'connected':True,'portConflict':True,'detail':'conflict'}), patch.object(remote,'_run') as run:
            self.assertFalse(remote.enable()['ok'])
            run.assert_not_called()

    def test_trusted_origin_is_exact_and_removed_when_remote_access_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            remote, 'TRUSTED_ORIGINS_PATH', Path(directory) / 'origins.json'
        ):
            origin = remote.sync_trusted_origin({
                'serverUrl': 'https://device.example.ts.net:8444/'
            })
            self.assertEqual(origin, 'https://device.example.ts.net:8444')
            saved = json.loads(remote.TRUSTED_ORIGINS_PATH.read_text())
            self.assertEqual(saved, {'origins': [origin]})
            self.assertEqual(remote.TRUSTED_ORIGINS_PATH.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(remote.sync_trusted_origin({'serverUrl': None}))
            self.assertEqual(
                json.loads(remote.TRUSTED_ORIGINS_PATH.read_text()), {'origins': []}
            )


if __name__=='__main__':
    unittest.main()
