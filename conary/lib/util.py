#
# Copyright (c) 2004-2005 rPath, Inc.
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

import bdb
from conary.build import fixedglob
import bz2
import epdb
import errno
import log
import misc
import os
import select
import shutil
import stackutil
import stat
import string
import struct
import sys
import tempfile
import traceback
import weakref

# Simple ease-of-use extensions to python libraries

def normpath(path):
    s = os.path.normpath(path)
    if s.startswith(os.sep + os.sep):
	return s[1:]
    return s

def isregular(path):
    return stat.S_ISREG(os.lstat(path)[stat.ST_MODE])

def mkdirChain(*paths):
    for path in paths:
        if path[0] != os.sep:
            path = os.getcwd() + os.sep + path
            
        paths = path.split(os.sep)
            
        for n in (range(2,len(paths) + 1)):
            p = string.join(paths[0:n], os.sep)
            if not os.path.exists(p):
                # don't die in case of the race condition where someone
                # made the directory after we stat'ed for it.
                try:
                    os.mkdir(p)
                except OSError, exc:
                    if exc.errno == errno.EEXIST:
                        s = os.lstat(p)
                        if stat.S_ISDIR(s.st_mode):
                            pass
                        else:
                            raise
                    else:
                        raise

def _searchVisit(arg, dirname, names):
    file = arg[0]
    path = arg[1]
    testname = '%s%s%s' %(dirname, os.sep, file)
    if file in names:
	path[0] = testname
	del names

def searchPath(file, basepath):
    path = [ None ]
    # XXX replace with os.walk in python 2.3, to cut short properly
    os.path.walk(basepath, _searchVisit, (file, path))
    return path[0]

def searchFile(file, searchdirs, error=None):
    for dir in searchdirs:
        s = "%s%s%s" %(dir, os.sep, file)
        if os.path.exists(s):
            return s
    if error:
        raise OSError, (errno.ENOENT, os.strerror(errno.ENOENT))
    return None

def findFile(file, searchdirs):
    return searchFile(file, searchdirs, error=1)

def genExcepthook(dumpStack=True, debugCtrlC=False, prefix='conary-stack-'):
    def excepthook(type, value, tb):
        if type is bdb.BdbQuit:
            sys.exit(1)
        sys.excepthook = sys.__excepthook__
        if type == KeyboardInterrupt and not debugCtrlC:
            sys.exit(1)
        lines = traceback.format_exception(type, value, tb)
        if dumpStack:
            try:
                (tbfd,path) = tempfile.mkstemp('', prefix)
                output = os.fdopen(tbfd, 'w')
                stackutil.printTraceBack(tb, output, type, value)
                output.close()
                print "*** Note *** An extended traceback has been saved to %s " % path
            except Exception, msg:
                log.warning("Could not write extended traceback: %s" % msg)
        sys.stderr.write(string.joinfields(lines, ""))
        if sys.stdout.isatty() and sys.stdin.isatty():
            epdb.post_mortem(tb, type, value)
        else:
            sys.exit(1)
    return excepthook



def _handle_rc(rc, cmd):
    if rc:
	if not os.WIFEXITED(rc):
	    info = 'Shell command "%s" killed with signal %d' \
		    %(cmd, os.WTERMSIG(rc))
	if os.WEXITSTATUS(rc):
	    info = 'Shell command "%s" exited with exit code %d' \
		    %(cmd, os.WEXITSTATUS(rc))
        log.error(info)
	raise RuntimeError, info

def execute(cmd, destDir=None, verbose=True):
    if verbose:
	log.debug(cmd)
    if destDir:
	rc = os.system('cd %s; %s' %(destDir, cmd))
    else:
	rc = os.system(cmd)
    _handle_rc(rc, cmd)

class popen:
    """
    Version of popen() that throws errors on close(), unlike os.popen()
    """
    # unfortunately, can't derive from os.popen.  Add methods as necessary.
    def __init__(self, *args):
	self.p = os.popen(*args)
        self.write = self.p.write
        self.read = self.p.read
        self.readline = self.p.readline
        self.readlines = self.p.readlines
        self.writelines = self.p.writelines

    def close(self, *args):
	rc = self.p.close(*args)
	_handle_rc(rc, self.p.name)
        return rc

# string extensions

def find(s, subs, start=0):
    ret = -1
    found = None
    for sub in subs:
	this = string.find(s, sub, start)
	if this > -1 and ( ret < 0 or this < ret):
	    ret = this
	    found = s[this:this+1]
    return (ret, found)

def literalRegex(s):
    "escape all regex magic characters in s"
    l = []
    for character in s:
        if character in '+*[].&^$+{}\\':
            l.append('\\')
        l.append(character)
    return ''.join(l)


# shutil module extensions, with {}-expansion and globbing

def braceExpand(path):
    obrace = string.find(path, "{")
    if obrace < 0:
	return [path]

    level=1
    pathlist = []
    h = obrace
    while level:
	(h, it) = find(path, "{}", h)
	if h < 0:
	    raise ValueError, 'path %s has unbalanced {}' %path
	if it == "{":
	    level = level + 1
	    obrace = h
	else:
	    segments = path[obrace+1:h].split(',')
	    start = path[:obrace]
	    end = path[h+1:]
	    for segment in segments:
		newbits = braceExpand(start+segment+end)
		for bit in newbits:
		    if not bit in pathlist:
			pathlist.append(bit)
	    return pathlist
	h = h + 1

def braceGlob(paths):
    pathlist = []
    for path in braceExpand(paths):
	pathlist.extend(fixedglob.glob(path))
    return pathlist

def rmtree(paths, ignore_errors=False, onerror=None):
    for path in braceGlob(paths):
	log.debug('deleting [tree] %s', path)
	# act more like rm -rf -- allow files, too
	if (os.path.islink(path) or 
                (os.path.exists(path) and not os.path.isdir(path))):
	    os.remove(path)
	else:
	    os.path.walk(path, _permsVisit, None)
	    shutil.rmtree(path, ignore_errors, onerror)

def _permsVisit(arg, dirname, names):
    for name in names:
	path = dirname + os.sep + name
	mode = os.lstat(path)[stat.ST_MODE]
	# has to be executable to cd, readable to list, writeable to delete
	if stat.S_ISDIR(mode) and (mode & 0700) != 0700:
	    log.warning("working around illegal mode 0%o at %s", mode, path)
	    mode |= 0700
	    os.chmod(path, mode)

def remove(paths):
    for path in braceGlob(paths):
	if os.path.isdir(path) and not os.path.islink(path):
	    log.warning('Not removing directory %s', path)
	elif os.path.exists(path) or os.path.islink(path):
	    log.debug('deleting [file] %s', path)
	    os.remove(path)
	else:
	    log.warning('file %s does not exist when attempting to delete [file]', path)

def copyfile(sources, dest, verbose=True):
    for source in braceGlob(sources):
	if verbose:
	    log.debug('copying %s to %s', source, dest)
	shutil.copy2(source, dest)

def copyfileobj(source, dest, callback = None, digest = None, 
                abortCheck = None, bufSize = 128*1024):
    total = 0
    buf = source.read(bufSize)

    if abortCheck:
        sourceFd = source.fileno()
    else:
        sourceFd = None

    while True:
        if not buf:
            break

	total += len(buf)
	dest.write(buf)
	if digest: digest.update(buf)
        if callback: callback(total)

        if abortCheck:
            # if we need to abortCheck, make sure we check it every time
            # read returns, and every five seconds
            l1 = []
            while not l1:
                if abortCheck and abortCheck():
                    return None
                l1, l2, l3 = select.select([ sourceFd ], [], [], 5)
        buf = source.read(bufSize)

    return total

def rename(sources, dest):
    for source in braceGlob(sources):
	log.debug('renaming %s to %s', source, dest)
	os.rename(source, dest)

def _copyVisit(arg, dirname, names):
    sourcelist = arg[0]
    sourcelen = arg[1]
    dest = arg[2]
    filemode = arg[3]
    dirmode = arg[4]
    if dirmode:
	os.chmod(dirname, dirmode)
    for name in names:
	if filemode:
	    os.chmod(dirname+os.sep+name, filemode)
	sourcelist.append(os.path.normpath(
	    dest + os.sep + dirname[sourcelen:] + os.sep + name))

def copytree(sources, dest, symlinks=False, filemode=None, dirmode=None):
    """
    Copies tree(s) from sources to dest, returning a list of
    the filenames that it has written.
    """
    sourcelist = []
    for source in braceGlob(sources):
	if os.path.isdir(source):
	    if source[-1] == '/':
		source = source[:-1]
	    thisdest = '%s%s%s' %(dest, os.sep, os.path.basename(source))
	    log.debug('copying [tree] %s to %s', source, thisdest)
	    shutil.copytree(source, thisdest, symlinks)
	    if dirmode:
		os.chmod(thisdest, dirmode)
	    os.path.walk(source, _copyVisit,
			 (sourcelist, len(source), thisdest, filemode, dirmode))
	else:
	    log.debug('copying [file] %s to %s', source, dest)
	    shutil.copy2(source, dest)
	    if dest.endswith(os.sep):
		thisdest = dest + os.sep + os.path.basename(source)
	    else:
		thisdest = dest
	    if filemode:
		os.chmod(thisdest, filemode)
	    sourcelist.append(thisdest)
    return sourcelist

def checkPath(binary, root=None):
    """
    Examine $PATH to determine if a binary exists, returns full pathname
    if it exists; otherwise None.
    """
    path = os.environ.get('PATH', '')
    for path in path.split(os.pathsep):
        if root:
            path = joinPaths(root, path)
        candidate = os.path.join(path, binary)
        if os.access(candidate, os.X_OK):
            if root:
                return candidate[len(root):]
            return candidate
    return None

def joinPaths(*args):
    return normpath(os.sep.join(args))

def assertIteratorAtEnd(iter):
    try:
	iter.next()
	raise AssertionError
    except StopIteration:
	return True

class ObjectCache(weakref.WeakKeyDictionary):
    """
    Implements a cache of arbitrary (hashable) objects where an object
    can be looked up and have its cached value retrieved. This allows
    a single copy of immutable objects to be kept in memory.
    """
    def __setitem__(self, key, value):
	weakref.WeakKeyDictionary.__setitem__(self, key, weakref.ref(value))

    def __getitem__(self, key):
	return weakref.WeakKeyDictionary.__getitem__(self, key)()

def memsize():
    pfn = "/proc/%d/status" % os.getpid()
    lines = open(pfn).readlines()
    f = lines[10].split()
    return int(f[1])

def createLink(src, to):
    name = os.path.basename(to)
    path = os.path.dirname(to)
    mkdirChain(path)
    tmpfd, tmpname = tempfile.mkstemp(name, '.ct', path)
    os.close(tmpfd)
    os.remove(tmpname)
    os.link(src, tmpname)
    os.rename(tmpname, to)

def tupleListBsearchInsert(haystack, newItem, cmpFn):
    """
    Inserts newItem into haystack, maintaining the sorted order. The
    cmpIdx is the item number in the list of tuples to base comparisons on.
    Duplicates items aren't added.

    @type l: list of tuples
    @type cmpIdx: int
    @type needle: object
    @type newItem: tuple
    """
    start = 0
    finish = len(haystack) - 1
    while start < finish:
        i = (start + finish) / 2

        rc = cmpFn(haystack[i], newItem)
        if rc == 0:
            start = i
            finish = i
            break
        elif rc < 0:
            start = i + 1
        else:
            finish = i - 1

    if start >= len(haystack):
        haystack.append(newItem)
    else:
        rc = cmpFn(haystack[start], newItem)
        if rc < 0:
            haystack.insert(start + 1, newItem)
        elif rc > 0:
            haystack.insert(start, newItem)

_tempdir = tempfile.gettempdir()
def settempdir(tempdir):
    # XXX add locking if we ever go multi-threadded
    global _tempdir
    _tempdir = tempdir

def mkstemp(suffix="", prefix=tempfile.template, dir=None, text=False):
    """
    a wrapper for tempfile.mkstemp that uses a common prefix which
    is set through settempdir()
    """
    if dir is None:
        global _tempdir
        dir = _tempdir
    return tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)

class NestedFile:

    def close(self):
	pass

    def read(self, bytes = -1):
        if self.needsSeek:
            self.file.seek(self.pos + self.start, 0)
            self.needsSeek = False

	if bytes < 0 or (self.end - self.pos) <= bytes:
	    # return the rest of the file
	    count = self.end - self.pos
	    self.pos = self.end
	    return self.file.read(count)
	else:
	    self.pos = self.pos + bytes
	    return self.file.read(bytes)

    def __init__(self, file, size):
	self.file = file
	self.size = size
	self.end = self.size
	self.pos = 0
        self.start = 0
        self.needsSeek = False

class SeekableNestedFile(NestedFile):

    def __init__(self, file, size, start = -1):
        NestedFile.__init__(self, file, size)

        if start == -1:
            self.start = file.tell()
        else:
            self.start = start

        self.needsSeek = True

    def read(self, bytes = -1):
        self.needsSeek = True
        return NestedFile.read(self, bytes)

    def seek(self, offset, whence = 0):
        if whence == 0:
            newPos = offset
        elif whence == 1:
            newPos = self.pos + offset
        else:
            newPos = self.size + offset
            
        if newPos > self.size or newPos < 0:
            raise IOError
        
        self.pos = newPos
        self.needsSeek = True

    def tell(self):
        return self.pos

class BZ2File:
    def __init__(self, fobj):
        self.decomp = bz2.BZ2Decompressor()
        self.fobj = fobj
        self.leftover = ''

    def read(self, bytes):
        while 1:
            buf = self.fobj.read(2048)
            if not buf:
                # ran out of compressed input
                if self.leftover:
                    # we have some uncompressed stuff left, return
                    # it
                    rc = self.leftover[:]
                    self.leftover = None
                    return rc
                # done returning all data, return None as the EOF
                return None
            # decompressed the newly read compressed data
            self.leftover += self.decomp.decompress(buf)
            # if we have at least what the caller asked for, return it
            if len(self.leftover) > bytes:
                rc = self.leftover[:bytes]
                self.leftover = self.leftover[bytes:]
                return rc
            # read some more data and try to get enough uncompressed
            # data to return

exists = misc.exists
removeIfExists = misc.removeIfExists