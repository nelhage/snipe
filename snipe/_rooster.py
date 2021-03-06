# -*- encoding: utf-8 -*-
# Copyright © 2014 Karl Ramm
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following
# disclaimer in the documentation and/or other materials provided
# with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
# TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR
# TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF
# THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
#
# This was written while looking at
# https://github.com/roost-im/roost-python/blob/master/lib/roost.py
# which is by David Benjamin and Copyright © 2013 MIT.  I don't think
# there's enough of it in this for this to count as a derivative work,
# but credit where credit is due.
'''
snipe._rooster
--------------

A layer of glue between the roost backend and the upstream code.
'''

import asyncio
import json
import urllib.parse
import base64
import logging
import functools
import concurrent.futures

import aiohttp
import aiohttp.websocket

from . import _websocket
from ._roost_python import krb5
from ._roost_python import gss


class RoosterException(Exception):
    pass


class Rooster:
    def __init__(self, url, service):
        self.token = None
        self.expires = None
        self.url = url
        if self.url[-1] == '/':
            # strip _1_ trailing /
            self.url = self.url[:-1]
        self.service = service + '@' + urllib.parse.urlparse(url).hostname
        self.principal = None
        self.ctx = None
        self.ccache = None
        self.tailid = 0
        self.log = logging.getLogger('Rooster.%x' % (id(self),))
        self.executor = concurrent.futures.ProcessPoolExecutor(1)

    def run_in_exile(self, *args):
        loop = asyncio.get_event_loop()
        try:
            return (yield from loop.run_in_executor(
                self.executor, trampoline, *args))
        except ExileException as e:
            self.log.error(e.error)
            raise

    @asyncio.coroutine
    def credentials(self):
        return (yield from self.run_in_exile(get_zephyr_creds))

    @asyncio.coroutine
    def auth(self, create_user=False):
        loop = asyncio.get_event_loop()
        self.principal, token = yield from self.run_in_exile(
            get_auth_token, self.service)

        result = yield from self.http(
            '/v1/auth',
            {
                'principal': self.principal,
                'token': token,
                'createUser': create_user,
                },
            )

        self.token = result['authToken']
        self.expires = result['expires']

    def ensure_auth(self):
        if self.token is None:
            yield from self.auth()

    @asyncio.coroutine
    def get_info(self):
        yield from self.ensure_auth()
        return (yield from self.http('/v1/info'))

    @asyncio.coroutine
    def send(self, message):
        yield from self.ensure_auth()
        return (yield from self.http(
            '/v1/zwrite', {
                'message': message,
                'credentials': (yield from self.credentials()),
                },
            ))

    @asyncio.coroutine
    def ping(self):
        yield from self.ensure_auth()
        return (yield from self.http('/v1/ping'))

    @asyncio.coroutine
    def subscriptions(self):
        yield from self.ensure_auth()
        return (yield from self.http('/v1/subscriptions'))

    @asyncio.coroutine
    def subscribe(self, subs):
        yield from self.ensure_auth()

        return (yield from self.http(
            '/v1/subscribe', {
                'subscriptions': [
                    {
                        'class': class_,
                        'instance': instance,
                        'recipient': recipient if recipient != '*' else '',
                        } for (class_, instance, recipient) in subs],
                'credentials': (yield from self.credentials()),
                },
            ))

    @asyncio.coroutine
    def unsubscribe(self, subs):
        yield from self.ensure_auth()

        # Why does /v1/subscribe take a list while /v1/unsubscribe
        # take a single triplet?  Who can say?  According to the
        # dictionary, "literally" means "figuratively", which means
        # nothing means anything anything means nothing.  Fix it in
        # the client library.
        for (class_, instance, recipient) in subs:
            result = (yield from self.http(
                '/v1/unsubscribe', {
                    'subscription': {
                        'class': class_,
                        'instance': instance,
                        'recipient': recipient if recipient != '*' else '',
                        },
                    'credentials': (yield from self.credentials()),
                    },
                ))
        return result

    @asyncio.coroutine
    def check_zephyrcreds(self):
        yield from self.ensure_auth()
        return (yield from self.http('/v1/zephyrcreds'))

    @asyncio.coroutine
    def renew_zephyrcreds(self):
        yield from self.ensure_auth()
        return (yield from self.http(
            '/v1/zephyrcreds', {
                'credentials': self.zephyr_creds(),
                }
            ))

    @asyncio.coroutine
    def bytime(self, t):
        yield from self.ensure_auth()
        return (yield from self.http(
            '/v1/bytime',
            {
                't': t,
                },
            ))

    @asyncio.coroutine
    def messages(self, offset, limit, reverse=True, inclusive=False):
        yield from self.ensure_auth()

        if not offset:
            offset = ''

        return (yield from self.http(
            '/v1/messages',
            params = {
                'reverse': 1 if reverse else 0,
                'inclusive': 1 if inclusive else 0,
                'offset': offset,
                'count': limit,
                },
            ))

    @asyncio.coroutine
    def _pinger(self, writer, pingfrequency):
        while True:
            yield from asyncio.sleep(pingfrequency)
            self.log.debug('ping')
            writer.send(json.dumps({
                'type': 'ping',
                }))

    @asyncio.coroutine
    def newmessages(self, coro, pingfrequency=30):
        # will coincidentally ensure_auth
        ms = yield from self.messages(None, 1, reverse=1, inclusive=0)
        if ms['messages']:
            startid = ms['messages'][0]['id']
        else:
            startid = None

        self.log.debug('startid=%s', startid)

        pingtask = None

        reader, writer, response = (
            yield from _websocket.websocket(self.url + '/v1/socket/websocket'))

        try:
            writer.send(json.dumps({
                'type': 'auth',
                'token': self.token,
                }))

            state = 'start'
            msgcount = 1
            tailid = self.tailid
            self.tailid += 1

            while True:
                msg = yield from reader.read()

                if msg.tp == aiohttp.websocket.MSG_PING:
                    writer.pong()
                    # Eventually, 99% of Internet traffic will be
                    # keepalive packets of some sort
                elif msg.tp == aiohttp.websocket.MSG_CLOSE:
                    break
                elif msg.tp == aiohttp.websocket.MSG_TEXT:
                    m = json.loads(msg.data)
                    assert 'type' in m
                    if state == 'start':
                        assert m['type'] == 'ready'
                        self.log.debug('authed, starting tail %d', tailid)
                        writer.send(json.dumps({
                            'type': 'new-tail',
                            'id': tailid,
                            'start': startid,
                            'inclusive': False,
                            }))
                        writer.send(json.dumps({
                            'type': 'extend-tail',
                            'id': tailid,
                            'count': msgcount,
                            }))
                        state = 'go'
                        pingtask = asyncio.Task(
                            self._pinger(writer, pingfrequency))
                    elif state == 'go':
                        if m['type'] == 'pong':
                            self.log.debug('pong')
                        elif m['type'] == 'messages':
                            msgcount += 1
                            writer.send(json.dumps({
                                'type': 'extend-tail',
                                'id': tailid,
                                'count': msgcount,
                                }))
                            ## if m['id'] != tailid:
                            ##     continue
                            for msg in m['messages']:
                                yield from coro(msg)
                        else:
                            self.log.debug('unknown message type: %s', repr(m))

        except aiohttp.EofStream:
            pass

        finally:
            if pingtask is not None:
                pingtask.cancel()
            response.close()

    @asyncio.coroutine
    def http(self, url, data=None, params=None):
        method = 'GET' if data is None else 'POST'

        if data is not None:
            data = json.dumps(data)

        headers = {}
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
        if self.token is not None:
            headers['Authorization'] = 'Bearer ' + self.token

        response = yield from aiohttp.request(
            method,
            self.url + url,
            data = data,
            params = params,
            headers = headers,
            )

        result = []
        while True:
            try:
                x = (yield from response.content.read())
                if x == b'':
                    break
                result.append(x)
            except aiohttp.EofStream:
                break

        response.close()

        result = b''.join(result)
        result = result.decode('utf-8')
        try:
            result = json.loads(result)
        except ValueError as e:
            if result: # then it's probably an error message
                raise RoosterException(result) from e
            raise

        return result


class ExileException(RoosterException):
    def __init__(self, gloss, error):
        self.gloss = gloss
        self.error = error

    def __str__(self):
        return self.gloss

    def __repr__(self):
        return '<%s: %s\n%s\n>' % (
            self.__class__.__name__,
            self.gloss,
            self.error)


def trampoline(f, *args, **kwargs):
    import traceback
    try:
        return f(*args, **kwargs)
    except BaseException as e:
        raise ExileException(str(e), str(traceback.format_exc())) from None


def get_auth_token(service):
    context = krb5.Context()
    ccache = context.cc_default()
    principal = ccache.get_principal()
    princ_str = principal.unparse_name()

    client_name = gss.import_name(princ_str, gss.KRB5_NT_PRINCIPAL_NAME)
    target_name = gss.import_name(service, gss.C_NT_HOSTBASED_SERVICE)
    cred = gss.acquire_cred(client_name, initiate=True)

    gss_ctx = gss.create_initiator(
        target_name, credential=cred, mechanism=gss.KRB5_MECHANISM)
    token = gss_ctx.init_sec_context()
    if not gss_ctx.is_established():
        raise Exception('Should be single-token')

    return princ_str.decode('utf-8'), base64.b64encode(token).decode('ascii')


def get_zephyr_creds():
    #XXX hardcoded ATHENA.MIT.EDU
    context = krb5.Context()
    ccache = context.cc_default()
    principal = ccache.get_principal()
    zephyr = context.build_principal('ATHENA.MIT.EDU', ['zephyr', 'zephyr'])
    creds = ccache.get_credentials(principal, zephyr)
    return creds.to_dict()
