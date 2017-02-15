import json
import time
from urllib import urlencode

import mock
import httpretty
from django.conf import settings
from django.test import override_settings
from requests.exceptions import HTTPError, Timeout

from ecommerce.core.models import User
from ecommerce.extensions.payment.models import SDNCheckFailure
from ecommerce.extensions.payment.utils import clean_field_value, middle_truncate, SDNClient
from ecommerce.tests.testcases import TestCase


class UtilsTests(TestCase):
    def test_truncation(self):
        """Verify that the truncation utility behaves as expected."""
        length = 10
        string = 'x' * length

        # Verify that the original string is returned when no truncation is necessary.
        self.assertEqual(string, middle_truncate(string, length))
        self.assertEqual(string, middle_truncate(string, length + 1))

        # Verify that truncation occurs when expected.
        self.assertEqual('xxx...xxx', middle_truncate(string, length - 1))
        self.assertEqual('xx...xx', middle_truncate(string, length - 2))

        self.assertRaises(ValueError, middle_truncate, string, 0)

    def test_clean_field_value(self):
        """ Verify the passed value is cleaned of specific special characters. """
        value = 'Some^text:\'test-value'
        self.assertEqual(clean_field_value(value), 'Sometexttest-value')


class SDNCheckTests(TestCase):
    """ Tests for the SDN check function. """

    def setUp(self):
        super(SDNCheckTests, self).setUp()
        self.name = 'Dr. Evil'
        self.country = 'Evilland'
        self.user = self.create_user(full_name=self.name)
        self.site_configuration = self.site.siteconfiguration
        self.site_configuration.enable_sdn_check = True,
        self.site_configuration.sdn_api_url = 'http://sdn-test.fake/'
        self.site_configuration.sdn_api_key = 'fake-key'
        self.site_configuration.sdn_api_list = 'SDN,TEST'
        self.site_configuration.save()

        self.sdn_validator = SDNClient(
            self.site_configuration.sdn_api_url,
            self.site_configuration.sdn_api_key,
            self.site_configuration.sdn_api_list
        )

    def mock_sdn_response(self, response, status_code=200):
        """ Mock the SDN check API endpoint response. """
        params = urlencode({
            'sources': self.site_configuration.sdn_api_list,
            'api_key': self.site_configuration.sdn_api_key,
            'type': 'individual',
            'name': self.name,
            'countries': self.country
        })
        sdn_check_url = '{api_url}?{params}'.format(
            api_url=self.site_configuration.sdn_api_url,
            params=params
        )

        httpretty.register_uri(
            httpretty.GET,
            sdn_check_url,
            status=status_code,
            body=response,
            content_type='application/json'
        )

    def assert_sdn_check_failure_recorded(self, response):
        """ Assert an SDN check failure is logged and has the correct values. """
        self.assertEqual(SDNCheckFailure.objects.count(), 1)
        sdn_object = SDNCheckFailure.objects.first()
        self.assertEqual(sdn_object.full_name, self.name)
        self.assertEqual(sdn_object.country, self.country)
        self.assertEqual(sdn_object.site, self.site_configuration.site)
        self.assertEqual(sdn_object.sdn_check_response, response)

    @httpretty.activate
    @override_settings(SDN_CHECK_REQUEST_TIMEOUT=0.1)
    def test_sdn_check_timeout(self):
        """Verify SDN check logs an exception if the request times out."""
        def mock_timeout(_request, _uri, headers):
            time.sleep(settings.SDN_CHECK_REQUEST_TIMEOUT + 0.1)
            return (200, headers, {'total': 1})

        self.mock_sdn_response(mock_timeout, status_code=200)
        with self.assertRaises(Timeout):
            with mock.patch('ecommerce.extensions.payment.utils.logger.exception') as mock_logger:
                self.sdn_validator.search(self.name, self.country)
                self.assertTrue(mock_logger.called)

    @httpretty.activate
    def test_sdn_check_connection_error(self):
        """ Verify the check logs an exception in case of a connection error. """
        self.mock_sdn_response(json.dumps({'total': 1}), status_code=400)
        with self.assertRaises(HTTPError):
            with mock.patch('ecommerce.extensions.payment.utils.logger.exception') as mock_logger:
                self.sdn_validator.search(self.name, self.country)
                self.assertTrue(mock_logger.called)

    @httpretty.activate
    def test_sdn_check_match(self):
        """ Verify the SDN check returns the number of matches and records the match. """
        sdn_response = {'total': 1}
        self.mock_sdn_response(json.dumps(sdn_response))
        response = self.sdn_validator.search(self.name, self.country)
        self.assertEqual(response, sdn_response)

    def test_deactivate_user(self):
        """ Verify an SDN failure is logged. """
        response = {'description': 'Bad dude.'}
        self.assertEqual(SDNCheckFailure.objects.count(), 0)
        with mock.patch.object(User, 'deactivate_account') as deactivate_account:
            deactivate_account.return_value = True
            self.sdn_validator.deactivate_user(
                self.user,
                self.site_configuration,
                self.name,
                self.country,
                response)
            self.assert_sdn_check_failure_recorded(response)
