# -*- coding: utf-8; -*-
#
# Licensed to CRATE Technology GmbH ("Crate") under one or more contributor
# license agreements.  See the NOTICE file distributed with this work for
# additional information regarding copyright ownership.  Crate licenses
# this file to you under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.  You may
# obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.
#
# However, if you have executed another commercial license agreement
# with Crate these terms will supersede the license and you may use the
# software solely pursuant to the terms of the relevant commercial agreement.

import json
import time
import sys
import os
from .compat import queue
import random
import traceback
from unittest import TestCase
from mock import patch, MagicMock, ANY
from threading import Thread, Event
from multiprocessing import Process
import datetime as dt
import urllib3.exceptions

from .http import Client, RoundRobin
from .exceptions import ConnectionError, ProgrammingError
from .compat import xrange, BaseHTTPServer, to_bytes


REQUEST = 'crate.client.http.Server.request'


def fake_request(response=None):
    def request(*args, **kwargs):
        if isinstance(response, list):
            resp = response.pop(0)
            response.append(resp)
            return resp
        elif response:
            return response
        else:
            return MagicMock(spec=urllib3.response.HTTPResponse)
    return request


def fake_response(status, reason=None, content_type='application/json'):
    m = MagicMock(spec=urllib3.response.HTTPResponse)
    m.status = status
    m.reason = reason or ''
    m.headers = {'content-type': content_type}
    return m


def fake_redirect(location):
    m = fake_response(307)
    m.get_redirect_location.return_value = location
    return m


def bad_bulk_response():
    r = fake_response(400, 'Bad Request')
    r.data = json.dumps({
        "results": [
            {"rowcount": 1},
            {"error_message": "an error occured"},
            {"error_message": "another error"},
            {"error_message": ""},
            {"error_message": None}
        ]}).encode()
    return r


def fail_sometimes(*args, **kwargs):
    if random.randint(1, 100) % 10 == 0:
        raise urllib3.exceptions.MaxRetryError(None, '/_sql', '')
    return fake_response(200)


class HttpClientTest(TestCase):

    def test_no_connection_exception(self):
        client = Client()
        self.assertRaises(ConnectionError, client.sql, 'select 1')

    @patch(REQUEST)
    def test_http_error_is_re_raised(self, request):
        request.side_effect = Exception

        client = Client()
        self.assertRaises(ProgrammingError, client.sql, 'select 1')

    @patch(REQUEST)
    def test_programming_error_contains_http_error_response_content(self, request):
        request.side_effect = Exception("this shouldn't be raised")

        client = Client()
        try:
            client.sql('select 1')
        except ProgrammingError as e:
            self.assertEquals("this shouldn't be raised", e.message)
        else:
            self.assertTrue(False)

    @patch(REQUEST, fake_request([fake_response(200),
                                  fake_response(503, 'Service Unavailable')]))
    def test_server_error_50x(self):
        client = Client(servers="localhost:4200 localhost:4201")
        client.sql('select 1')
        self.assertEqual(2, len(client.active_servers))
        client.sql('select 2')
        self.assertEqual(1, len(client.active_servers))
        try:
            client.sql('select 3')
        except ConnectionError as e:
            self.assertEqual("No more Servers available, " +
                             "exception from last server: Service Unavailable",
                             e.message)
        self.assertEqual([], list(client.active_servers))

    def test_connect(self):
        client = Client(servers="localhost:4200 localhost:4201")
        self.assertEqual(client.active_servers,
                         ["http://localhost:4200", "http://localhost:4201"])

        client = Client(servers="localhost:4200")
        self.assertEqual(client.active_servers, ["http://localhost:4200"])

        client = Client(servers=["localhost:4200"])
        self.assertEqual(client.active_servers, ["http://localhost:4200"])

        client = Client(servers=["localhost:4200", "127.0.0.1:4201"])
        self.assertEqual(client.active_servers,
                         ["http://localhost:4200", "http://127.0.0.1:4201"])

    @patch('crate.client.http.ServerPool._execute_redirect', autospec=True)
    @patch(REQUEST, fake_request(fake_redirect('http://localhost:4201')))
    def test_redirect_handling(self, redirect):
        redirect.return_value = fake_response(200)
        client = Client(servers='localhost:4200')
        client.blob_get('blobs', 'fake_digest')
        redirect.assert_called_with(
            ANY, ANY, 'GET', '/_blobs/blobs/fake_digest', None, True, None)

    @patch(REQUEST)
    def test_server_infos(self, request):
        request.side_effect = urllib3.exceptions.MaxRetryError(
            None, '/', "this shouldn't be raised")
        client = Client(servers="localhost:4200 localhost:4201")
        self.assertRaises(
            ConnectionError, client.server_infos, 'http://localhost:4200')

    @patch(REQUEST, fake_request(fake_response(503)))
    def test_server_infos_503(self):
        client = Client(servers="localhost:4200")
        self.assertRaises(
            ConnectionError, client.server_infos, 'http://localhost:4200')

    @patch(REQUEST, fake_request(
        fake_response(401, 'Unauthorized', 'text/html')))
    def test_server_infos_401(self):
        client = Client(servers="localhost:4200")
        try:
            client.server_infos('http://localhost:4200')
        except ProgrammingError as e:
            self.assertEqual("401 Client Error: Unauthorized", e.message)
        else:
            self.assertTrue(False, msg="Exception should have been raised")

    @patch(REQUEST, fake_request(bad_bulk_response()))
    def test_bad_bulk_400(self):
        client = Client(servers="localhost:4200")
        try:
            client.sql("Insert into users (name) values(?)",
                       bulk_parameters=[["douglas"], ["monthy"]])
        except ProgrammingError as e:
            self.assertEqual("an error occured\nanother error", e.message)
        else:
            self.assertTrue(False, msg="Exception should have been raised")

    @patch(REQUEST, autospec=True)
    def test_datetime_is_converted_to_ts(self, request):
        client = Client(servers="localhost:4200")
        request.return_value = fake_response(200)

        datetime = dt.datetime(2015, 2, 28, 7, 31, 40)
        client.sql('insert into users (dt) values (?)', (datetime,))
        request.assert_called_with(
            ANY,
            'POST',
            '/_sql',
            json.dumps({
                "args": [1425108700000],
                "stmt": "insert into users (dt) values (?)"
            }),
            False,
            None
        )

    @patch(REQUEST, autospec=True)
    def test_date_is_converted_to_ts(self, request):
        client = Client(servers="localhost:4200")
        request.return_value = fake_response(200)

        day = dt.date(2016, 4, 21)
        client.sql('insert into users (dt) values (?)', (day,))
        self.assertEqual(1, request.call_count)
        request.assert_called_with(
            ANY,
            'POST',
            '/_sql',
            json.dumps({
                "args": [1461196800000],
                "stmt": "insert into users (dt) values (?)"
            }),
            False,
            None
        )


@patch(REQUEST, fail_sometimes)
class ThreadSafeHttpClientTest(TestCase):
    """
    Using a pool of 5 Threads to emit commands to the multiple servers through
    one Client-instance

    check if number of servers in inactive_servers and active_servers always
    equals the number of servers initially given.
    """
    servers = [
        "http://127.0.0.1:44209",
        "http://127.0.0.2:44209",
        "http://127.0.0.3:44209",
    ]
    num_threads = 5
    num_commands = 1000
    thread_timeout = 5.0  # seconds

    def __init__(self, *args, **kwargs):
        self.event = Event()
        self.err_queue = queue.Queue()
        super(ThreadSafeHttpClientTest, self).__init__(*args, **kwargs)

    def setUp(self):
        self.client = Client(self.servers)
        self.client.retry_interval = 0.0001  # faster retry

    def _run(self):
        self.event.wait()  # wait for the others
        for x in xrange(self.num_commands):
            try:
                self.client.sql('select name from sys.cluster')
            except ConnectionError:
                pass
            try:
                with self.client._lock:
                    servers = self.client.active_servers + self.client.inactive_servers
                self.assertEquals(self.servers, sorted(servers))
            except AssertionError:
                self.err_queue.put(sys.exc_info())

    def test_client_threaded(self):
        """
        Testing if lists of servers is handled correctly when client is used
        from multiple threads with some requests failing.

        **ATTENTION:** this test is probabilistic and does not ensure that the
        client is indeed thread-safe in all cases, it can only show that it
        withstands this scenario.
        """
        self.assertEquals(
            self.servers,
            sorted(self.client.active_servers + self.client.inactive_servers))
        threads = []
        for x in xrange(self.num_threads):
            t = Thread(target=self._run, name=str(x))
            t.daemon = True
            threads.append(t)
            t.start()

        self.event.set()
        for t in threads:
            t.join(self.thread_timeout)

        if not self.err_queue.empty():
            self.assertTrue(False, "".join(
                traceback.format_exception(*self.err_queue.get(block=False))))


class ClientAddressRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    """
    http handler for use with BaseHTTPServer

    returns client host and port in crate-conform-responses
    """
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        content_length = self.headers.get("content-length")
        if content_length:
            self.rfile.read(int(content_length))
        response = json.dumps({
            "cols": ["host", "port"],
            "rows": [
                self.client_address[0],
                self.client_address[1]
            ],
            "rowCount": 1,
        })
        self.send_response(200)
        self.send_header("Content-Length", len(response))
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.end_headers()
        self.wfile.write(to_bytes(response, 'UTF-8'))

    do_POST = do_PUT = do_DELETE = do_HEAD = do_GET


class KeepAliveClientTest(TestCase):

    server_address = ("127.0.0.1", 65535)

    def __init__(self, *args, **kwargs):
        super(KeepAliveClientTest, self).__init__(*args, **kwargs)
        self.server_process = Process(target=self._run_server)

    def setUp(self):
        super(KeepAliveClientTest, self).setUp()
        self.client = Client(["%s:%d" % self.server_address])
        self.server_process.start()
        time.sleep(.10)

    def tearDown(self):
        self.server_process.terminate()
        super(KeepAliveClientTest, self).tearDown()

    def _run_server(self):
        self.server = BaseHTTPServer.HTTPServer(self.server_address,
                                                ClientAddressRequestHandler)
        self.server.handle_request()

    def test_client_keepalive(self):
        for x in range(10):
            result = self.client.sql("select * from fake")

            another_result = self.client.sql("select again from fake")
            self.assertEqual(result, another_result)


class ParamsTest(TestCase):

    def test_params(self):
        client = Client(['127.0.0.1:4200'], error_trace=True)
        from six.moves.urllib.parse import urlparse, parse_qs
        parsed = urlparse(client.path)
        params = parse_qs(parsed.query)
        self.assertEquals(params["error_trace"], ["1"])

    def test_no_params(self):
        client = Client(['127.0.0.1:4200'])
        self.assertEqual(client.path, "/_sql")


class RequestsCaBundleTest(TestCase):

    def test_open_client(self):
        os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/certs/ca-certificates.crt"
        try:
            Client('http://127.0.0.1:4200')
        except ProgrammingError:
            self.fail("HTTP not working with REQUESTS_CA_BUNDLE")


class RoundRobinTest(TestCase):

    def test_round_robin(self):
        rr = RoundRobin([1, 2, 3, 4])
        self.assertEqual([1, 2, 3, 4], list(rr))
        self.assertEqual([2, 3, 4, 1], list(rr))
        self.assertEqual([3, 4, 1, 2], list(rr))
