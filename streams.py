#
# Copyright (c) 2004 Specifix, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any waranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#
"""
Defines the datastreams stored in a changeset
"""

import copy
import struct
import versions

from deps import filedeps, deps

_STREAM_CONTENTS    = 1
_STREAM_DEVICE	    = 2
_STREAM_FLAGS	    = 3
_STREAM_FLAVOR	    = 4
_STREAM_INODE	    = 5
_STREAM_PROVIDES    = 6
_STREAM_REQUIRES    = 7
_STREAM_TAGS	    = 8
_STREAM_TARGET	    = 9

_STREAM_TROVE_CHANGE_SET = 100

class InfoStream(object):

    __slots__ = ()

    def copy(self):
        return self.__class__(self.freeze())
    
    def freeze(self):
	raise NotImplementedError
    
    def diff(self, them):
	raise NotImplementedError

    def twm(self, diff, base):
	"""
	Performs a three way merge. Base is the original information,
	diff is one of the changes, and self is the (already changed)
	object. Returns a boolean saying whether or not the merge failed
	"""
	raise NotImplementedError

    def __eq__(self, them):
	raise NotImplementedError

    def __ne__(self, them):
	return not self.__eq__(them)

class NumericStream(InfoStream):

    __slots__ = "val"

    def value(self):
	return self.val

    def set(self, val):
	self.val = val

    def merge(self, other):
	if other.val != None:
	    self.val = other.val

    def freeze(self):
	return struct.pack(self.format, self.val)

    def diff(self, them):
	if self.val != them.val:
	    return struct.pack(self.format, self.val)

	return ""

    def thaw(self, frz):
	self.val = struct.unpack(self.format, frz)[0]

    def twm(self, diff, base):
	if not diff: return False

	newVal = struct.unpack(self.format, diff)[0]
	if self.val == base.val:
	    self.val = newVal
	    return False
	elif self.val != newVal:
	    return True

	return False

    def __eq__(self, other):
	return other.__class__ == self.__class__ and \
	       self.val == other.val

    def __init__(self, val = None):
	if type(val) == str:
	    self.thaw(val)
	else:
	    self.val = val

class ShortStream(NumericStream):

    format = "!H"

class IntStream(NumericStream):

    __slots__ = ( "val", )

    format = "!I"

class MtimeStream(NumericStream):

    format = "!I"

    def __eq__(self, other):
	# don't ever compare mtimes
	return True

    def twm(self, diff, base):
	# and don't let merges fail
	NumericStream.twm(self, diff, base)
	return False

class LongLongStream(NumericStream):

    format = "!Q"

class StringStream(InfoStream):
    """
    Stores a simple string; used for the target of symbolic links
    """

    __slots__ = "s"

    def value(self):
	return self.s

    def set(self, val):
        assert(type(val) is str)
	self.s = val

    def merge(self, other):
	if other.s != None:
	    self.s = other.s

    def freeze(self):
	return self.s

    def diff(self, them):
	if self.s != them.s:
	    return self.s

	return ""

    def thaw(self, frz):
	self.s = frz

    def twm(self, diff, base):
	if not diff: return False

	if self.s == base.s:
	    self.s = diff
	    return False
	elif self.s != diff:
	    return True

	return False

    def __eq__(self, other):
	return other.__class__ == self.__class__ and \
	       self.s == other.s

    def __init__(self, s = ''):
	self.thaw(s)

class Sha1Stream(StringStream):

    # stores the sha1 as a binary, but can present it as a string

    def __str__(self):
	return "%x%x%x%x%x" % struct.unpack("!5I", self.s)

class FrozenVersionStream(InfoStream):

    __slots__ = "v"

    def value(self):
	return self.v

    def set(self, val):
	assert(not val or min(val.timeStamps()) > 0)
	self.v = val

    def merge(self, other):
	if other.v != None:
	    self.v = other.v

    def freeze(self):
	if self.v:
	    return self.v.freeze()
	else:
	    return ""

    def diff(self, them):
	if self.v != them.v:
	    return self.v.freeze()

	return ""

    def thaw(self, frz):
	if frz:
	    self.v = versions.ThawVersion(frz)
	else:
	    self.v = None

    def twm(self, diff, base):
	if not diff: return False

	if self.v == base.v:
	    self.v = diff
	    return False
	elif self.v != diff:
	    return True

	return False

    def __eq__(self, other):
	return other.__class__ == self.__class__ and \
	       self.v == other.v

    def __init__(self, v = None):
	self.thaw(v)

class DependenciesStream(InfoStream):
    """
    Stores list of strings; used for requires/provides lists
    """

    __slots__ = 'deps'

    def value(self):
	return self.deps

    def set(self, val):
	self.deps = val

    def merge(self, other):
        self.deps = other.deps

    def freeze(self):
        if self.deps is None:
            return ''
        return self.deps.freeze()

    def diff(self, them):
	if self.deps != them.deps:
	    return self.freeze()

	return ''

    def thaw(self, frz):
        self.deps = deps.ThawDependencySet(frz)
        
    def twm(self, diff, base):
	if not diff: return False

        self.thaw(diff)
        return False

    def __eq__(self, other):
	return other.__class__ == self.__class__ and self.deps == other.deps

    def __init__(self, dep = ''):
        assert(type(dep) is str)
        self.deps = None
        self.thaw(dep)

class StringsStream(InfoStream):
    """
    Stores list of arbitrary strings
    """

    __slots__ = 'l'

    def value(self):
	return self.l

    def set(self, val):
        assert(type(val) is str)
	if val not in self.l:
	    self.l.append(val)
	    self.l.sort()
    
    def __contains__(self, val):
	return val in self.l

    def __delitem__(self, val):
	if val in self.l:
	    self.l.remove(val)

    def merge(self, other):
        self.l = other.l

    def __iter__(self):
	return self.l.__iter__()

    def freeze(self):
        if self.l is None:
            return ''
        return '\0'.join(self.l)

    def diff(self, them):
	if self.l != them.l:
	    return self.freeze()
	return ''

    def thaw(self, frz):
	if len(frz) == 0:
	    self.l = []
	else:
	    self.l = frz.split('\0')
        
    def twm(self, diff, base):
	if not diff:
	    return False
        self.thaw(diff)
        return False

    def __eq__(self, other):
	return other.__class__ == self.__class__ and self.l == other.l

    def __init__(self, frz = ''):
	self.thaw(frz)

class TupleStream(InfoStream):

    __slots__ = "items"

    def __eq__(self, other):
	return other.__class__ == self.__class__ and other.items == self.items

    def freeze(self):
	rc = []
	items = self.items
	makeup = self.makeup
	for (i, (name, itemType, size)) in enumerate(makeup):
	    if type(size) == int or (i + 1 == len(makeup)):
		rc.append(items[i].freeze())
	    else:
		s = items[i].freeze()
		rc.append(struct.pack(size, len(s)) + s)

	return "".join(rc)

    def merge(self, other):
	for i in xrange(len(self.makeup)):
	    self.items[i].merge(other.items[i])

    def diff(self, them):
	code = 0
	rc = []
	for (i, (name, itemType, size)) in enumerate(self.makeup):
	    d = self.items[i].diff(them.items[i])
	    if d:
		if type(size) == int or (i + 1) == len(self.makeup):
		    rc.append(d)
		else:
		    rc.append(struct.pack(size, len(d)) + d)
		code |= (1 << i)
		
	return struct.pack("B", code) + "".join(rc)

    def twm(self, diff, base):
	what = struct.unpack("B", diff[0])[0]
	idx = 1
	conflicts = False

	for (i, (name, itemType, size)) in enumerate(self.makeup):
	    if what & (1 << i):
		if type(size) == int:
		    pass
		elif (i + 1) == len(self.makeup):
		    size = len(diff) - idx
		else:
		    if size == "B":
			size = struct.unpack("B", diff[idx])[0]
			idx += 1
		    elif size == "!H":
			size = struct.unpack("!H", diff[idx:idx + 2])[0]
			idx += 2
		    else:
			raise AssertionError

		conflicts = conflicts or \
		    self.items[i].twm(diff[idx:idx + size], base.items[i])
		idx += size

	return conflicts

    def thaw(self, s):
	items = []
	makeup = self.makeup
	idx = 0

	for (i, (name, itemType, size)) in enumerate(makeup):
	    if type(size) == int:
		items.append(itemType(s[idx:idx + size]))
	    elif (i + 1) == len(makeup):
		items.append(itemType(s[idx:]))
		size = 0
	    else:
		if size == "B":
		    size = struct.unpack("B", s[idx])[0]
		    idx += 1
		elif size == "!H":
		    size = struct.unpack("!H", s[idx:idx + 2])[0]
		    idx += 2
		else:
		    raise AssertionError

		items.append(itemType(s[idx:idx + size]))

	    idx += size

	self.items = items

    def __init__(self, first = None, *rest):
	if first == None:
	    items = []
	    for (i, (name, itemType, size)) in enumerate(self.makeup):
		items.append(itemType())
	    self.items = items
	elif type(first) == str and not rest:
	    self.thaw(first)
	else:
	    all = (first, ) + rest
	    items = []
	    for (i, (name, itemType, size)) in enumerate(self.makeup):
		items.append(itemType(all[i]))
	    self.items = items

class StreamSet(InfoStream):

    def __init__(self, data = None):
	for streamType, name in self.streamDict.itervalues():
	    self.__setattr__(name, streamType())

	if data: 
	    i = 0
            dataLen = len(data)
	    while i < dataLen:
                assert(i < dataLen)
		(streamId, size) = struct.unpack("!BH", data[i:i+3])
		(streamType, name) = self.streamDict[streamId]
		i += 3
		self.__setattr__(name, streamType(data[i:i + size]))
		i += size

	    assert(i == dataLen)

    def diff(self, other):
	if self.lsTag != other.lsTag:
	    d = self.freeze()
	    return struct.pack("!BH", 0, len(d)) + d

	rc = [ "\x01", self.lsTag ]
	for streamId, (streamType, name) in self.streamDict.iteritems():
	    d = self.__getattribute__(name).diff(other.__getattribute__(name))
	    rc.append(struct.pack("!BH", streamId, len(d)) + d)

	return "".join(rc)

    def __eq__(self, other):
	for streamType, name in self.streamDict.itervalues():
	    if not self.__getattribute__(name) == other.__getattribute__(name):
		return False

	return True

    def __ne__(self, other):
	return not self.__eq__(other)

    def freeze(self):
	rc = []
	for streamId, (streamType, name) in self.streamDict.iteritems():
	    s = self.__getattribute__(name).freeze()
	    if len(s):
		rc.append(struct.pack("!BH", streamId, len(s)) + s)
	return "".join(rc)

    def copy(self):
	new = copy.deepcopy(self)
        for streamClass, name in self.streamDict.itervalues():
            stream = self.__getattribute__(name).copy()
            new.__setattr__(name, stream)
        return new

    def twm(self, diff, base, skip = None):
	i = 0
	conflicts = False
	
	while i < len(diff):
	    streamId, size = struct.unpack("!BH", diff[i:i+3])

	    streamType, name = self.streamDict[streamId]

	    i += 3
	    if name != skip:
		w = self.__getattribute__(name).twm(diff[i:i+size], 
					       base.__getattribute__(name))
		conflicts = conflicts or w
	    i += size

	assert(i == len(diff))

	return conflicts
