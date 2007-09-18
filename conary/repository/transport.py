#
# Copyright (c) 2004-2007 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.rpath.com/permanent/licenses/CPL-1.0.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#

""" XMLRPC transport class that uses urllib to allow for proxies
    Unfortunately, urllib needs some touching up to allow 
    XMLRPC commands to be sent, hence the XMLOpener class """

import base64
import httplib
import itertools
import select
import socket
import time
import xmlrpclib
import urllib
import zlib
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

from conary.lib import util

class InfoURL(urllib.addinfourl):
    def __init__(self, fp, headers, url, protocolVersion):
        urllib.addinfourl.__init__(self, fp, headers, url)
        self.protocolVersion = protocolVersion

class DecompressFileObj:
    "implements a wrapper file object that decompress()s data on the fly"
    def __init__(self, fp):
        self.fp = fp
        self.dco = zlib.decompressobj()
        self.readsize = 1024
        self.available = ''

    def _read(self, size=-1):
        # get at least @size uncompressed data ready in the available
        # buffer.  Returns False is there is no more to read at the moment
        bufs = [self.available]
        more = True
        while size == -1 or len(self.available) < size:
            # read some compressed data
            buf = self.fp.read(self.readsize)
            if not buf:
                more = False
                break
            decomp = self.dco.decompress(buf)
            bufs.append(decomp)
        self.available = ''.join(bufs)
        return more

    def read(self, size=-1):
        self._read(size)
        if size == -1:
            # return it all
            ret = self.available
            self.available = ''
        else:
            # return what's asked for
            ret = self.available[:size]
            self.available = self.available[size:]
        return ret

    def readline(self, size=-1):
        bufs = []
        haveline = False
        while True:
            havemore = self._read(1024)

            bufs.append(self.available)
            haveline = '\n' in self.available
            self.available = ''

            haveenough = size != -1 and sum(len(x) for x in bufs) > size
            if (not havemore) or haveenough or haveline:
                line = ''.join(bufs)
                if haveline:
                    i = line.index('\n') + 1
                    if size != -1:
                        i = min(i, size)
                    ret = line[:i]
                    self.available = line[i:]
                    return ret
                if size != -1 and len(line) > size:
                    # return just what was asked
                    ret = line[size:]
                    self.available = line[:size]
                    return ret
                # otherwise return it all
                return line

    def close(self):
        self.fp.close()
        self.available = ''

    def fileno(self):
        return self.fp.fileno()

class URLOpener(urllib.FancyURLopener):
    '''Replacement class for urllib.FancyURLopener'''
    contentType = 'application/x-www-form-urlencoded'

    localhosts = set(['localhost', 'localhost.localdomain', '127.0.0.1',
        socket.gethostname()])

    def __init__(self, *args, **kw):
        self.compress = False
        self.abortCheck = None
        self.usedProxy = False
        self.proxyHost = None
        self.proxyProtocol = None
        # FIXME: this should go away in a future release.
        # forceProxy is used to ensure that if the proxy returns some
        # bogus address like "localhost" from a URL fetch, we can
        # be sure to use the proxy the next time we speak to the proxy
        # too.
        self.forceProxy = kw.pop('forceProxy', False)
        urllib.FancyURLopener.__init__(self, *args, **kw)

    def setCompress(self, compress):
        self.compress = compress

    def setAbortCheck(self, check):
        self.abortCheck = check

    def open_https(self, url, data=None):
        return self.open_http(url, data=data, ssl=True)

    def _splitport(self, hostport, defaultPort):
        host, port = urllib.splitport(hostport)
        if port is None:
            port = defaultPort
        return (host, int(port))

    def proxy_ssl(self, proxy, endpoint, proxyAuth):
        host, port = self._splitport(proxy, 3128)
        endpointHost, endpointPort = self._splitport(endpoint,
            httplib.HTTPS_PORT)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((host, port))
        except socket.error, e:
            self._processSocketError(e)
            raise

        sock.sendall("CONNECT %s:%s HTTP/1.0\r\n" %
                                         (endpointHost, endpointPort))
        sock.sendall("User-Agent: %s\r\n" % Transport.user_agent)
        if proxyAuth:
            sock.sendall("Proxy-Authorization: Basic %s\r\n" % proxyAuth)
        sock.sendall('\r\n')

        # Have HTTPResponse parse the status line for us
        resp = httplib.HTTPResponse(sock, strict=True)
        resp.begin()

        if resp.status != 200:
            # Fake a socket error, use a code that make it obvious it hasn't
            # been generated by the socket library
            raise socket.error(-71,
                               "Error talking to HTTP proxy %s:%s: %s (%s)" %
                               (host, port, resp.status, resp.reason))

        # We can safely close the response, it duped the original socket
        resp.close()

        # Wrap the socket in an SSL socket
        sslSock = socket.ssl(sock, None, None)
        h = httplib.HTTPConnection("%s:%s" % (endpointHost, endpointPort))
        # Force HTTP/1.0 (this is the default for the old-style HTTP;
        # new-style HTTPConnection defaults to 1.1)
        h._http_vsn = 10
        h._http_vsn_str = 'HTTP/1.0'
        # This is a bit unclean
        h.sock = httplib.FakeSocket(sock, sslSock)
        return h

    def proxyBypass(self, proxy, host):
        if self.forceProxy:
            return False
        # Split the port and username/pass from proxy
        proxyHost = urllib.splituser(urllib.splitport(proxy)[0])[1]

        destHost = urllib.splitport(host)[0]

        # don't proxy localhost unless the proxy is running on
        # localhost as well
        if destHost in self.localhosts and proxyHost not in self.localhosts:
            return True
        return False

    def createConnection(self, url, ssl=False, withProxy=False):
        # Return an HTTP or HTTPS class suitable for use by open_http
        self.usedProxy = False
        if ssl:
            protocol='https'
        else:
            protocol='http'

        if withProxy:
            # XXX this is duplicating work done in urllib.URLoperner.open
            proxy = self.proxies.get(protocol, None)
            if proxy:
                urltype, proxyhost = urllib.splittype(proxy)
                host, selector = urllib.splithost(proxyhost)
                url = (host, protocol + ':' + url)

        useConaryProxy = False
        user_passwd = None
        proxyUserPasswd = None
        if isinstance(url, str):
            host, selector = urllib.splithost(url)
            if host:
                user_passwd, host = urllib.splituser(host)
                host = urllib.unquote(host)
            realhost = host
            # SPX: use the full URL here, not just the selector or name
            # based virtual hosts don't work
            selector = '%s:%s' %(protocol, url)
        else:
            # Request should go through a proxy
            # Check to see if it's a conary proxy
            proxy = self.proxies[protocol]
            proxyUrlType, proxyhost = urllib.splittype(proxy)
            useConaryProxy = proxyUrlType in ('conary', 'conarys')

            self.proxyProtocol = proxyUrlType

            host, selector = url
            proxyUserPasswd, host = urllib.splituser(host)
            urltype, rest = urllib.splittype(selector)
            url = rest
            user_passwd = None
            if urltype.lower() not in ['http', 'https']:
                realhost = None
            else:
                realhost, rest = urllib.splithost(rest)
                if realhost:
                    user_passwd, realhost = urllib.splituser(realhost)
                if user_passwd:
                    selector = "%s://%s%s" % (urltype, realhost, rest)
                if self.proxyBypass(host, realhost):
                    host = realhost
                else:
                    self.usedProxy = True
                    # To make it visible for users of this object 
                    # that we're going through a proxy
                    self.proxyHost = host
                    if useConaryProxy:
                        # override ssl setting to talk the right protocol to the
                        # proxy - the proxy will take the real url and communicate
                        # either ssl or not as appropriate

                        # Other proxies will not support proxying ssl over !ssl
                        # or vice versa.
                        ssl = (proxyUrlType == 'conarys')

        if not host: raise IOError, ('http error', 'no host given')
        if user_passwd:
            auth = base64.b64encode(user_passwd)
        else:
            auth = None
        if proxyUserPasswd:
            proxyAuth = base64.b64encode(proxyUserPasswd)
        else:
            proxyAuth = None

        headers = []

        if ssl:
            if host != realhost and not useConaryProxy:
                h = self.proxy_ssl(host, realhost, proxyAuth)
            else:
                h = httplib.HTTPSConnection(host)
        else:
            h = httplib.HTTPConnection(host)
            if host != realhost and not useConaryProxy and proxyAuth:
                headers.append(("Proxy-Authorization",
                                "Basic " + proxyAuth))
        # Force HTTP/1.0 (this is the default for the old-style HTTP;
        # new-style HTTPConnection defaults to 1.1)
        h._http_vsn = 10
        h._http_vsn_str = 'HTTP/1.0'

        if realhost:
            headers.append(('Host', realhost))
        else:
            headers.append(('Host', host))
        if auth:
            headers.append(('Authorization', 'Basic %s' % auth))
        return h, url, selector, headers

    def open_http(self, url, data=None, ssl=False):
        """override this WHOLE FUNCTION to change
	   one magic string -- the content type --
	   which is hardcoded in (this version also supports https)"""
        # Splitting some of the functionality so we can reuse this code with
        # PUT requests too
        h, urlstr, selector, headers = self.createConnection(url, ssl=ssl)
        if data is not None:
            h.putrequest('POST', selector)
            if self.compress:
                h.putheader('Content-encoding', 'deflate')
                data = zlib.compress(data, 9)
            h.putheader('Content-type', self.contentType)
            h.putheader('Content-length', '%d' % len(data))
            h.putheader('Accept-encoding', 'deflate')
        else:
            h.putrequest('GET', selector)
        for args in itertools.chain(headers, self.addheaders):
            h.putheader(*args)
        try:
            h.endheaders()
        except socket.error, e:
            self._processSocketError(e)
            raise

        if data is not None:
            h.send(data)
        # wait for a response
        self._wait(h)
        response = h.getresponse()
        errcode, errmsg = response.status, response.reason
        headers = response.msg
        fp = response.fp
        if errcode == 200:
            encoding = headers.get('Content-encoding', None)
            if encoding == 'deflate':
                # disable until performace is better
                #fp = DecompressFileObj(fp)
                fp = util.decompressStream(fp)
                fp.seek(0)

            protocolVersion = "HTTP/%.1f" % (response.version / 10.0)
            return InfoURL(fp, headers, selector, protocolVersion)
        else:
            return self.http_error(selector, fp, errcode, errmsg, headers, data)

    def _processSocketError(self, error):
        if not self.proxyHost:
            return
        # Add the name of the real proxy
        if self.proxyProtocol.startswith('http'):
            pt = 'HTTP'
        else:
            pt = 'Conary'
        error.args = (error[0], "%s (via %s proxy %s)" % 
            (error[1], pt, self.proxyHost))

    def _wait(self, h):
        # wait for data if abortCheck is set
        if self.abortCheck:
            check = self.abortCheck
        else:
            check = lambda: False

        pollObj = select.poll()
        pollObj.register(h.sock.fileno(), select.POLLIN)

        while True:
            if check():
                raise AbortError
            # wait 5 seconds for a response
            l = pollObj.poll(5000)

            if not l:
                # still no response from the server.  send a space to
                # keep the connection alive - in case the server is
                # behind a load balancer/firewall with short
                # connection timeouts.
                h.send(' ')
            else:
                # ready to read response
                break

    def http_error_default(self, url, fp, errcode, errmsg, headers, data=None):
        raise TransportError("Unable to open %s: %s" % (url, errmsg))

class ConaryURLOpener(URLOpener):
    """An opener aware of the conary:// protocol"""
    open_conary = URLOpener.open_http
    open_conarys = URLOpener.open_https

class XMLOpener(URLOpener):
    contentType = 'text/xml'

    def open_http(self, *args, **kwargs):
        fp = URLOpener.open_http(self, *args, **kwargs)
        usedAnonymous = 'X-Conary-UsedAnonymous' in fp.headers
        return usedAnonymous, fp

    def http_error(self, url, fp, errcode, errmsg, headers, data=None):
        raise xmlrpclib.ProtocolError(url, errcode, errmsg, headers)

    open_conary = open_http
    open_conarys = URLOpener.open_https


def getrealhost(host):
    """ Slice off username/passwd and portnum """
    atpoint = host.find('@') + 1
    colpoint = host.rfind(':')
    if colpoint == -1 or colpoint < atpoint:
	return host[atpoint:]
    else:
	return host[atpoint:colpoint]


class Transport(xmlrpclib.Transport):

    # override?
    user_agent =  "xmlrpclib.py/%s (www.pythonware.com modified by rPath, Inc.)" % xmlrpclib.__version__
    # make this a class variable so that across all attempts to transport we'll only
    # spew messages once per host.
    failedHosts = set()

    def __init__(self, https = False, proxies = None, serverName = None,
                 extraHeaders = None):
        self.https = https
        self.compress = False
        self.abortCheck = None
        self.proxies = proxies
        self.serverName = serverName
        self.setExtraHeaders(extraHeaders)
        self.responseHeaders = None
        self.responseProtocol = None
        self.usedProxy = False
        self.entitlement = None

    def setEntitlements(self, entitlementList):
        self.entitlements = entitlementList
        if entitlementList is not None:
            l = []
            for entitlement in entitlementList:
                if entitlement[0] is None:
                    l.append("* %s" % (base64.b64encode(entitlement[1])))
                else:
                    l.append("%s %s" % (entitlement[0],
                                        base64.b64encode(entitlement[1])))
            self.entitlement = " ".join(l)
        else:
            self.entitlement = None

        self.proxyHost = None
        self.proxyProtocol = None

    def getEntitlements(self):
        return self.entitlements

    def setExtraHeaders(self, extraHeaders):
        self.extraHeaders = extraHeaders or {}

    def addExtraHeaders(self, extraHeaders):
        self.extraHeaders.update(extraHeaders)

    def setCompress(self, compress):
        self.compress = compress

    def setAbortCheck(self, abortCheck):
        self.abortCheck = abortCheck

    def _protocol(self):
        if self.https:
            return 'https'
        return 'http'

    def request(self, host, handler, body, verbose=0):
	self.verbose = verbose

        protocol = self._protocol()

        opener = XMLOpener(self.proxies)
        opener.setCompress(self.compress)
        opener.setAbortCheck(self.abortCheck)

	opener.addheaders = []
	host, extra_headers, x509 = self.get_host_info(host)
	if extra_headers:
	    if isinstance(extra_headers, dict):
		extra_headers = extra_headers.items()
	    for key, value in extra_headers:
		opener.addheader(key,value)

        if self.entitlement:
            opener.addheader('X-Conary-Entitlement', self.entitlement)

        if self.serverName:
            opener.addheader('X-Conary-Servername', self.serverName)

        opener.addheader('User-agent', self.user_agent)
        for k, v in self.extraHeaders.items():
            opener.addheader(k, v)

        tries = 0
        url = ''.join([protocol, '://', host, handler])
        while tries < 5:
            try:
                # Make sure we capture some useful information from the
                # opener, even if we failed
                try:
                    usedAnonymous, response = opener.open(url, body)
                finally:
                    self.usedProxy = getattr(opener, 'usedProxy', False)
                    self.proxyHost = getattr(opener, 'proxyHost', None)
                    self.proxyProtocol = getattr(opener, 'proxyProtocol', None)
                break
            except IOError, e:
                tries += 1
                if tries >= 5 or host in self.failedHosts:
                    self.failedHosts.add(host)
                    raise
                if e.args[0] == 'socket error':
                    e = e.args[1]
                if isinstance(e, socket.gaierror):
                    if e.args[0] == socket.EAI_AGAIN:
                        from conary.lib import log
                        log.warning('got "%s" when trying to '
                                    'resolve %s.  Retrying in '
                                    '500 ms.' %(e.args[1], host))
                        time.sleep(.5)
                    else:
                        raise
                else:
                    raise
        if hasattr(response, 'headers'):
            self.responseHeaders = response.headers
            self.responseProtocol = response.protocolVersion
        resp = self.parse_response(response)
        rc = ( [ usedAnonymous ] + resp[0], )
	return rc

    def getparser(self):
        return util.xmlrpcGetParser()

class AbortError(Exception): pass

class TransportError(Exception): pass
