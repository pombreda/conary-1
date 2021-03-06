#
# Copyright (c) SAS Institute Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""
NOTE: DO NOT USE unless you need an implementation of sha256 which has an
implementation error when the length of the string being hashed is 55 % 64.
See http://pycrypto.cvs.sourceforge.net/viewvc/pycrypto/crypto/src/SHA256.c?r1=1.3&r2=1.4
for fix to this bug.  In this case, we want to keep the bw compatible bug for
creating older signatures.

On top of that, this module is provided only for backwards compatibility with
existing code (rAPA specifically) that not only relied on the broken digest but
also the particular interface by which Conary provided it.
"""

from conary.lib.ext.sha256_nonstandard import digest as _digest


class SHA256_nonstandard(object):
    name = 'sha256_nonstandard'
    digest_size = digestsize = 32
    block_size = 64

    def __init__(self, msg=''):
        self.msg = msg

    def update(self, msg):
        self.msg += msg

    def digest(self):
        return _digest(self.msg)

    def hexdigest(self):
        return _digest(self.msg).encode('hex')

    def copy(self):
        return type(self)(self.msg)


new = SHA256_nonstandard
blocksize = new.block_size
digest_size = new.digest_size
