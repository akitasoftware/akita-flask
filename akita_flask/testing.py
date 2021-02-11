# 3-Clause BSD License
# 
# Copyright (c) 2009, Boxed Ice <hello@boxedice.com>
# Copyright (c) 2010-2016, Datadog <info@datadoghq.com>
# Copyright (c) 2020-present, Akita Software <info@akitasoftware.com>
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
#     * Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright notice,
#       this list of conditions and the following disclaimer in the documentation
#       and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the names of its contributors
#       may be used to endorse or promote products derived from this software
#       without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from datetime import datetime, timezone
import time

import akita_har.models as har
import werkzeug.test

from akita_har import HarWriter
from flask.testing import FlaskClient
from typing import Optional, List
from urllib import parse
from flask.testing import EnvironBuilder
from werkzeug.http import parse_cookie
from werkzeug.wrappers import BaseResponse, Request, Response


def wsgi_to_har_entry(start: datetime, request: Request, response: Response) -> har.Entry:
    """
    Converts a WSGI request/response pair to a HAR file entry.
    :param start: The start of the request, which must be timezone-aware.
    :param request: A WSGI Request.
    :param response: A WSGI Response.
    :return: A HAR file entry.
    """
    if start.tzinfo is None:
        raise ValueError('start datetime must be timezone-aware')

    # Build request
    server_protocol = 'HTTP/1.1'
    if 'SERVER_PROTOCOL' in request.environ:
        server_protocol = request.environ['SERVER_PROTOCOL']

    url = parse.urlsplit(request.url)
    query_string = [har.Record(name=k, value=v) for k, v in parse.parse_qs(url.query).items()]

    headers = [har.Record(name=k, value=v) for k, v in request.headers.items()]
    encoded_headers = '\n'.join([f'{k}: {v}' for k, v in request.headers.items()]).encode("utf-8")
    body = request.data.decode("utf-8")
    cookies = parse_cookie(request.environ)

    har_request = har.Request(
        method=request.method,
        url=url.path,
        httpVersion=server_protocol,
        cookies=[har.Record(name=k, value=v) for k in cookies for v in cookies.getlist(k)],
        headers=headers,
        queryString=query_string,
        postData=None if not body else har.PostData(mimeType=request.headers['Content-Type'], text=body),
        headersSize=len(encoded_headers),
        bodySize=len(body),
    )

    # Build response
    content = response.get_data(as_text=True)
    headers = response.get_wsgi_headers(request.environ)
    har_response = har.Response(
        status=response.status_code,
        statusText=response.status,
        httpVersion=server_protocol,

        # TODO(cns): Handle cookies.
        cookies=[],

        headers=[har.Record(name=k, value=v) for k, v in headers.items()],
        content=har.ResponseContent(size=len(content), mimeType=response.content_type, text=content),

        # TODO(cns): Handle redirects.
        redirectURL='',

        headersSize=len(str(headers)),
        bodySize=len(content),
    )

    return har.Entry(
        startedDateTime=start,
        time=(datetime.now(timezone.utc) - start).total_seconds(),
        request=har_request,
        response=har_response,
        cache=har.Cache(),
        timings=har.Timings(send=0, wait=0, receive=0),
    )


class HarClient(FlaskClient):
    def __init__(self, *args, har_file_path=f'akita_trace_{time.time()}.har', **kwargs):
        self.har_writer = HarWriter(har_file_path, 'w')
        self.url_prefix = ""
        super().__init__(*args, **kwargs)

    def open(self, *args, **kwargs):
        start = datetime.now(timezone.utc)
        resp: Response = super().open(*args, **kwargs)
        self.har_writer.write_entry(self._create_har_entry(start, args, kwargs, resp))
        return resp

    def __exit__(self, *args, **kwargs):
        self.har_writer.close()
        super().__exit__(*args, **kwargs)

    def _create_wsgi_request(self, request_args: List, request_info: dict) -> Request:
        # Same logic as super.open, which we use to build the
        # werkzeug Request.
        # https://github.com/pallets/flask/blob/master/src/flask/testing.py#L164
        request = None

        def copy_environ(other):
            return {
                **self.environ_base,
                **other,
                "flask._preserve_context": self.preserve_context,
            }

        if not request_info and len(request_args) == 1:
            arg = request_args[0]

            if isinstance(arg, werkzeug.test.EnvironBuilder):
                builder = copy(arg)
                builder.environ_base = copy_environ(builder.environ_base or {})
                request = builder.get_request()
            elif isinstance(arg, dict):
                request = EnvironBuilder.from_environ(
                    arg, app=self.application, environ_base=copy_environ({})
                ).get_request()
            elif isinstance(arg, Request):
                request = copy(arg)
                request.environ = copy_environ(request.environ)

        if request is None:
            request_info["environ_base"] = copy_environ(request_info.get("environ_base", {}))
            builder = EnvironBuilder(self.application, *request_args, **request_info)

            try:
                request = builder.get_request()
            finally:
                builder.close()

        return request


    def _create_har_entry(self, start: datetime, request_args: List, request_info: dict, response: Response) -> har.Entry:
        wsgi_request = self._create_wsgi_request(request_args, request_info)
        return wsgi_to_har_entry(start, wsgi_request, response)

