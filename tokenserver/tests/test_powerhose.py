# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
import os
from pyramid import testing
import time

from webtest import TestApp
from logging.config import fileConfig
from ConfigParser import NoSectionError

from cornice.tests import CatchErrors
from mozsvc.config import load_into_settings
from mozsvc.plugin import load_and_register

from powerhose import get_cluster

from tokenserver.assignment import INodeAssignment
from tokenserver.verifiers import PowerHoseVerifier
from tokenserver.tests.mockworker import MockCryptoWorker
from tokenserver.crypto.pyworker import CryptoWorker
from tokenserver.tests.support import (
    PurePythonRunner,
    get_assertion,
    patched_environ,
    unittest,
)

from browserid.errors import InvalidSignatureError

TOKEN_URI = '/1.0/sync/2.1'
DEFAULT_EMAIL = "alexis@mozilla.com"
DEFAULT_NODE = "https://example.com"


class TestPowerHoseVerifier(unittest.TestCase):

    def test_assertion_verification(self):
        # giving a valid assertion should return True
        with patched_environ():
            worker = MockCryptoWorker()
            verifier = PowerHoseVerifier(runner=PurePythonRunner(worker),
                                         audiences=('*',))
            self.assertTrue(verifier.verify(get_assertion(DEFAULT_EMAIL)))

            # An assertion not signed with the root issuer certificate should
            # fail.

            self.assertRaises(InvalidSignatureError, verifier.verify,
                    get_assertion(DEFAULT_EMAIL, bad_issuer_cert=True))

    def test_loadtest_mode(self):
        with patched_environ():
            worker = CryptoWorker(loadtest_mode=True)
            verifier = PowerHoseVerifier(runner=PurePythonRunner(worker),
                                         audiences=('*',))
            result = verifier.verify(get_assertion('alexis@loadtest.local',
                                                   issuer='loadtest.local'))
            self.assertTrue(result)


class TestPowerService(unittest.TestCase):

    @classmethod
    def get_ini(self):
        return os.path.join(os.path.dirname(__file__), 'test_powerhose.ini')

    @classmethod
    def setUpClass(cls):
        cls.config = testing.setUp()
        settings = {}
        try:
            fileConfig(cls.get_ini())
        except NoSectionError:
            pass
        load_into_settings(cls.get_ini(), settings)
        cls.config.add_settings(settings)
        cls.config.include("tokenserver")
        load_and_register("tokenserver", cls.config)
        cls.backend = cls.config.registry.getUtility(INodeAssignment)
        cls.cluster = get_cluster('tokenserver.tests.mockworker.crypto_worker',
                                  numprocesses=1, background=True, debug=True)
        cls.cluster.start()

    @classmethod
    def tearDownClass(cls):
        cls.cluster.stop()

    def setUp(self):
        wsgiapp = TestPowerService.config.make_wsgi_app()
        wsgiapp = CatchErrors(wsgiapp)
        self.app = TestApp(wsgiapp)

    def _test_valid_app(self):
        assertion = get_assertion(DEFAULT_EMAIL)
        headers = {'Authorization': 'Browser-ID %s' % assertion}
        res = self.app.get(TOKEN_URI, headers=headers)
        self.assertEqual(res.json['api_endpoint'], DEFAULT_NODE + '/1.0/0')

    def test_authentication_failures2(self):
        self.test_authentication_failures()

    def test_authentication_failures(self):
        # sending a request without any authentication header should result in
        # a 401 Unauthorized response.
        self.app.get(TOKEN_URI, status=401)

        # sending a request with a broken authentication header should return a
        # 401 as well
        headers = {'Authorization': 'VELOCIRAPTOR'}
        self.app.get(TOKEN_URI, headers=headers, status=401)

        # the authentication should be browserid
        headers = {'Authorization': 'Basic-Auth alexis:alexis'}
        res = self.app.get(TOKEN_URI, headers=headers, status=401)
        self.assertTrue('WWW-Authenticate' in res.headers)
        self.assertEqual(res.headers['WWW-Authenticate'], 'Browser-ID ')

        # if the headers are good but the given assertion is not valid, a 401
        # should be raised as well.
        wrong_assertion = get_assertion(DEFAULT_EMAIL,
                                        bad_issuer_cert=True)
        headers = {'Authorization': 'Browser-ID %s' % wrong_assertion}
        res = self.app.get(TOKEN_URI, headers=headers, status=401)

        # test the different cases of bad assertions.
        assertion = get_assertion('alexis@loadtest.local',
                                  bad_issuer_cert=True)
        headers = {'Authorization': 'Browser-ID %s' % assertion}
        res = self.app.get(TOKEN_URI, headers=headers, status=401)

        assertion = get_assertion('alexis@mozilla.com',
                                   issuer='loadtest.local')
        res = self.app.get(TOKEN_URI, headers=headers, status=401)

        assertion = get_assertion('alexis@mozilla.com',
                                exp=int(time.time() - 60) * 1000)
        res = self.app.get(TOKEN_URI, headers=headers, status=401)
