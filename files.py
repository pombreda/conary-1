#
# Copyright (c) 2004 Specifix, Inc.
# All rights reserved
#
import string
import os
import versioned
import sha1helper
import stat
import pwd
import grp
import util
import types
from datastore import DataStore

class FileMode:

    # new can be an integer file mode or a string (ls style) listing
    def perms(self, new = None):
	if (new != None and new != "-"):
	    self.thePerms = new

	return self.thePerms

    def owner(self, new = None):
	if (new != None and new != "-"):
	    self.theOwner = new

	return self.theOwner

    def group(self, new = None):
	if (new != None and new != "-"):
	    self.theGroup = new

	return self.theGroup

    def mtime(self, new = None):
	if (new != None and new != "-"):
	    self.theMtime = new

	return self.theMtime

    def infoLine(self):
	return "%o %s %s %s" % (self.thePerms, self.theOwner, self.theGroup,
				self.theMtime)

    def diff(self, them):
	if not them:
	    return self.infoLine()

	selfLine = string.split(self.infoLine())
	themLine = string.split(them.infoLine())

	if selfLine[0] == themLine[0] and len(selfLine) == len(themLine):
	    rc = selfLine[0]
	    for i in range(1, len(selfLine)):
		if selfLine[i] == themLine[i]:
		    rc = rc + " -"
		else:
		    rc = rc + " " + selfLine[i]

	    return rc + "\n"
	else:
	    return self.infoLine()

    def same(self, other):
	if self.thePerms == other.thePerms and \
	   self.theOwner == other.theOwner and \
	   self.theGroup == other.theGroup:
	    return 1

	return 0

    def applyChangeLine(self, line):
	(p, o, g, m) = string.split(line)
	if p == "-": 
	    p = None
	else:
	    p = int(p)

	self.perms(p)
	self.owner(o)
	self.group(g)
	self.mtime(m)

    def __init__(self, info = None):
	if info:
	    self.applyChangeLine(info)
	else:
	    self.thePerms = None
	    self.theOwner = None
	    self.theGroup = None
	    self.theMtime = None
	
class File(FileMode):

    def infoLine(self):
	return self.infoTag + " " + FileMode.infoLine(self)

    def id(self, new = None):
	if new:
	    self.theId = new

	return self.theId

    def restore(self, target):
	self.chmod(target)
	self.setOwnerGroup(target)

    def chmod(self, target):
	os.chmod(target, self.thePerms)

    def setOwnerGroup(self, target):
	if os.getuid(): return

	uid = pwd.getpwnam(self.owner())[2]
	gid = grp.getgrnam(self.group())[2]

	# FIXME: this needs to use lchown, which is in 2.3, and
	# this should happen unconditionally
	os.chown(target, uid, gid)

    # copies a files contents into the repository, if necessary
    def archive(self, repos, source):
	# most file types don't need to do this
	pass

    # public interface to applyChangeLine
    #
    # returns 1 if the change worked, 0 if the file changed too much for
    # the change to apply (which means this is a different file type)
    def applyChange(self, line):
	(tag, line) = string.split(line, None, 1)
	assert(tag == self.infoTag)
	self.applyChangeLine(line)

    def __init__(self, fileId, info = None, infoTag = None):
	self.theId = fileId
	self.infoTag = infoTag
	FileMode.__init__(self, info)

class SymbolicLink(File):

    def linkTarget(self, newLinkTarget = None):
	if (newLinkTarget and newLinkTarget != "-"):
	    self.theLinkTarget = newLinkTarget

	return self.theLinkTarget

    def infoLine(self):
	return "l %s %s" % (self.theLinkTarget, FileMode.infoLine(self))

    def same(self, other):
	if self.theLinkTarget == other.theLinkTarget:
	    # recursing does a permission check, which doens't apply 
	    # to symlinks under Linux
	    return 1

	return 0

    def chmod(self, target):
	# chmod() on a symlink follows the symlink
	pass

    def setOwnerGroup(self, target):
	# chmod() on a symlink follows the symlink
	pass

    def restore(self, repos, target):
	if os.path.exists(target) or os.path.islink(target):
	    os.unlink(target)
	os.symlink(self.theLinkTarget, target)
	File.restore(self, target)

    def applyChangeLine(self, line):
	(target, line) = string.split(line, None, 1)
	self.linkTarget(target)
	File.applyChangeLine(self, line)

    def __init__(self, fileId, info = None):
	if (info):
	    self.applyChangeLine(line)
	else:
	    self.theLinkTarget = None

	File.__init__(self, fileId, info, infoTag = "l")

class Socket(File):

    def same(self, other):
	return File.same(self, other)

    def copy(self, source, target):
	pass

    def __init__(self, fileId, info = None):
	File.__init__(self, fileId, info, infoTag = "s")

class NamedPipe(File):

    def same(self, other):
	return File.same(self, other)

    def restore(self, repos, target):
	if os.path.exists(target) or os.path.islink(target):
	    os.unlink(target)
	os.mkfifo(target)
	File.restore(self, target)

    def __init__(self, fileId, info = None):
	File.__init__(self, fileId, info, infoTag = "p")

class Directory(File):

    def same(self, other):
	return File.same(self, other)

    def restore(self, repos, target):
	if not os.path.isdir(target):
	    os.mkdir(target)

	File.restore(self, target)

    def __init__(self, fileId, info = None):
	File.__init__(self, fileId, info, infoTag = "d")

class DeviceFile(File):

    def infoLine(self):
	return "v %c %d %d %s" % (self.type, self.major, self.minor,
				  FileMode.infoLine(self))

    def same(self, other):
	if (self.type == other.type and self.major == other.major and
			self.minor == other.minor):
	    return File.same(self, other)
	
	return 0

    def restore(self, repos, target):
	if os.path.exists(target) or os.path.islink(target):
	    os.unlink(target)

	# FIXME os.mknod is in 2.3
	os.system("mknod %s %c %d %d" % (target, self.type, self.major,
					self.minor))

	File.restore(self, target)

    def majorMinor(self, type = None, major = None, minor = None):
	if type and type != "-":
	    self.type = type
	if major:
	    self.major = major
	if minor:
	    self.minor = minor
	
	return (self.type, self.major, self.minor)

    def applyChangeLine(self, line):
	(t, ma, mi, line) = string.split(line, None, 3)

	if ma == "-":
	    ma = None
	else:
	    ma = int(ma)
	    
	if mi == "-":
	    mi = None
	else:
	    mi = int(mi)

	self.majorMinor(t, ma, mi)
	File.applyChangeLine(self, line)

    def __init__(self, fileId, info = None):
	if (info):
	    self.applyChangeLine(info)

	File.__init__(self, fileId, info, infoTag = "v")

class RegularFile(File):

    def sha1(self, sha1 = None):
	if sha1 and sha1 != "-":
	    self.thesha1 = sha1

	return self.thesha1

    def infoLine(self):
	return "%s %s %s" % (self.infoTag, self.thesha1, 
			     FileMode.infoLine(self))

    def same(self, other):
	if self.thesha1 == other.thesha1:
	    return File.same(self, other)

	return 0

    def restore(self, repos, target):
	if os.path.exists(target) or os.path.islink(target):
	    os.unlink(target)
	else:
	    path = os.path.dirname(target)
	    util.mkdirChain(path)

	f = open(target, "w")
	repos.pullFileContents(self.sha1(), f)
	f.close()
	File.restore(self, target)

    def archive(self, repos, file):
	if repos.hasFileContents(self.sha1()):
	    return

	repos.newFileContents(self.sha1(), file)

    def applyChangeLine(self, line):
	(sha, line) = string.split(line, None, 1)
	self.sha1(sha)
	File.applyChangeLine(self, line)

    def __init__(self, fileId, info = None, infoTag = "f"):
	if (info):
	    self.applyChangeLine(info)
	else:
	    self.thesha1 = None

	self.infoTag = infoTag

	File.__init__(self, fileId, info, infoTag = self.infoTag)

class SourceFile(RegularFile):

    def __init__(self, fileId, info = None):
	RegularFile.__init__(self, fileId, info, infoTag = "src")

class FileDB:

    # see if the head of the specified branch is a duplicate
    # of the file object passed; it so return the version object
    # for that duplicate
    def checkBranchForDuplicate(self, branch, file):
	version = self.f.findLatestVersion(branch)
	if not version:
	    return None

	f1 = self.f.getVersion(version)
	lastFile = FileFromInfoLine(f1.read(), self.fileId)
	f1.close()

	if file.same(lastFile):
	    return version

	return None

    def addVersion(self, version, file):
	if self.f.hasVersion(version):
	    raise KeyError, "duplicate version for database"
	else:
	    if file.id() != self.fileId:
		raise KeyError, "file id mismatch for file database"
	
	self.f.addVersion(version, "%s\n" % file.infoLine())

    def getVersion(self, version):
	f1 = self.f.getVersion(version)
	file = FileFromInfoLine(f1.read(), self.fileId)
	f1.close()
	return file

    def hasVersion(self, version):
	return self.f.hasVersion(version)

    def eraseVersion(self, version):
	self.f.eraseVersion(version)

    def close(self):
	if self.f:
	    self.f.close()
	    self.f = None

    def __del__(self):
	self.close()

    def __init__(self, dbpath, fileId):
	self.fileId = fileId
	store = DataStore(dbpath)
	if store.hasFile(fileId):
	    f = store.openFile(fileId, "r+")
	else:
	    f = store.newFile(fileId)

	self.f = versioned.open(f)

def FileFromFilesystem(path, fileId, type = None):
    s = os.lstat(path)

    if type == "src":
	f = SourceFile(fileId)
	f.sha1(sha1helper.hashFile(path))
    elif (stat.S_ISREG(s.st_mode)):
	f = RegularFile(fileId)
	f.sha1(sha1helper.hashFile(path))
    elif (stat.S_ISLNK(s.st_mode)):
	f = SymbolicLink(fileId)
	f.linkTarget(os.readlink(path))
    elif (stat.S_ISDIR(s.st_mode)):
	f = Directory(fileId)
    elif (stat.S_ISSOCK(s.st_mode)):
	f = Socket(fileId)
    elif (stat.S_ISFIFO(s.st_mode)):
	f = NamedPipe(fileId)
    elif (stat.S_ISBLK(s.st_mode)):
	f = DeviceFile(fileId)
	f.majorMinor("b", s.st_rdev >> 8, s.st_rdev & 0xff)
    elif (stat.S_ISCHR(s.st_mode)):
	f = DeviceFile(fileId)
	f.majorMinor("c", s.st_rdev >> 8, s.st_rdev & 0xff)
    else:
	raise TypeError, "unsupported file type for %s" % path

    f.perms(s.st_mode & 07777)
    f.owner(pwd.getpwuid(s.st_uid)[0])
    f.group(grp.getgrgid(s.st_gid)[0])
    f.mtime(s.st_mtime)

    return f

def FileFromInfoLine(infoLine, fileId):
    (type, infoLine) = string.split(infoLine, None, 1)
    if type == "f":
	return RegularFile(fileId, infoLine)
    elif type == "l":
	return SymbolicLink(fileId, infoLine)
    elif type == "d":
	return Directory(fileId, infoLine)
    elif type == "p":
	return NamedPipe(fileId, infoLine)
    elif type == "v":
	return DeviceFile(fileId, infoLine)
    elif type == "s":
	return Socket(fileId, infoLine)
    elif type == "src":
	return SourceFile(fileId, infoLine)
    else:
	raise KeyError, "bad infoLine %s" % infoLine
