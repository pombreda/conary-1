#
# Copyright (c) 2004 Specifix, Inc.
# All rights reserved
#

# implements the SRS system repository

import changeset
import datastore
import dbhash
import fcntl
import files
import os
import package
import util
import versioned

class Repository:

    def commitChangeSet(self, sourcePathTemplate, cs, eraseOld = 0):
	pkgList = []
	oldFileList = []
	oldPackageList = []
	fileMap = {}

	# build todo set
	for csPkg in cs.getPackageList():
	    newVersion = csPkg.getNewVersion()
	    old = csPkg.getOldVersion()

	    # we can't erase the oldVersion for abstract change sets
	    assert(old or not eraseOld)
	
	    if self.hasPackage(csPkg.getName()):
		pkgSet = self._getPackageSet(csPkg.getName())

		if pkgSet.hasVersion(newVersion):
		    raise KeyError, "version %s for %s exists" % \
			    (newVersion.asString(), csPkg.getName())
	    else:
		pkgSet = None

	    if old:
		oldPackageList.append((csPkg.getName(), old))
		newPkg = pkgSet.getVersion(old)
		newPkg.changeVersion(newVersion)
	    else:
		newPkg = package.Package(csPkg.name, newVersion)

	    newFileMap = newPkg.applyChangeSet(csPkg)
	    pkgList.append((csPkg.getName(), newPkg, newVersion))
	    fileMap.update(newFileMap)

	    if old:
		oldPackage = self.getPackageVersion(csPkg.getName(), old)
		for fileId in csPkg.getOldFileList():
		    version = oldPackage.getFile(fileId)[1]
		    oldFileList.append((fileId, version))

	# Create the file objects we'll need for the commit. This handles
	# files which were added and files which have changed
	fileList = []
	for (fileId, (oldVer, newVer, infoLine)) in cs.getFileList():
	    if oldVer:
		fileDB = self._getFileDB(fileId)
		file = fileDB.getVersion(oldVer)
		file.applyChange(infoLine)
		del fileDB
	    else:
		file = files.FileFromInfoLine(infoLine, fileId)

	    assert(newVer.equal(fileMap[fileId][1]))
	    fileList.append((fileId, newVer, file))

	# commit changes
	pkgsDone = []
	filesDone = []
	filesToArchive = {}
	try:
	    for (pkgName, newPkg, newVersion) in pkgList:
		pkgSet = self._getPackageSet(pkgName)
		pkgSet.addVersion(newVersion, newPkg)
		pkgsDone.append((pkgSet, newVersion))

	    for (fileId, fileVersion, file) in fileList:
		infoFile = self._getFileDB(fileId)
		pathInPkg = fileMap[fileId][0]
		pkgName = fileMap[fileId][2]

		# this version may already exist, abstract change sets
		# include redundant files quite often
		if not infoFile.hasVersion(fileVersion):
		    infoFile.addVersion(fileVersion, file)
		    infoFile.close()
		    filesDone.append(fileId)
		    filesToArchive[pathInPkg] = ((file, pathInPkg, pkgName))

	    # sort paths and store in order (to make sure that directories
	    # are stored before the files that reside in them in the case of
	    # restore to a local file system
	    pathsToArchive = filesToArchive.keys()
	    pathsToArchive.sort()
	    for pathInPkg in pathsToArchive:
		(file, path, pkgName) = filesToArchive[pathInPkg]
		if isinstance(file, files.SourceFile):
		    basePkgName = pkgName.split(':')[-2]
		    d = { 'pkgname' : basePkgName }
		    path = (sourcePathTemplate) % d + "/" + path

		self.storeFileFromChangeset(cs, file, path)
	except:
	    # something went wrong; try to unwind our commits
	    for fileId in filesDone:
		infoFile = self._getFileDB(fileId)
		(path, fileVersion) = fileMap[fileId][0:2]
		infoFile.eraseVersion(fileVersion)

	    for (pkgSet, newVersion) in pkgsDone:
		pkgSet.eraseVersion(newVersion)

	    raise 

	# at this point the new version is in the repository, and we
	# can't undo that anymore. if erasing the old version fails, we
	# need to just commit the inverse change set; fortunately erasing
	# rarely fails
	for (fileId, version) in oldFileList:
	    filesDB = self._getFileDB(fileId)
	    filesDB.eraseVersion(version)

	for (pkgName, pkgVersion) in oldPackageList:
	    pkgSet = self._getPackageSet(pkgName)
	    pkgSet.eraseVersion(pkgVersion)

    # packageList is a list of (pkgName, oldVersion, newVersion) tuples
    def createChangeSet(self, packageList):

	cs = changeset.ChangeSetFromRepository(self)

	for (packageName, oldVersion, newVersion) in packageList:
	    pkgSet = self._getPackageSet(packageName)

	    new = pkgSet.getVersion(newVersion)
	 
	    if oldVersion:
		old = pkgSet.getVersion(oldVersion)
	    else:
		old = None

	    (pkgChgSet, filesNeeded) = new.diff(old, oldVersion, newVersion)
	    cs.addPackage(pkgChgSet)

	    for (fileId, oldVersion, newVersion) in filesNeeded:
		filedb = self._getFileDB(fileId)

		oldFile = None
		if oldVersion:
		    oldFile = filedb.getVersion(oldVersion)
		newFile = filedb.getVersion(newVersion)

		(filecs, hash) = changeset.fileChangeSet(fileId, oldFile, 
							 newFile)

		cs.addFile(fileId, oldVersion, newVersion, filecs)
		if hash: cs.addFileContents(hash)

	return cs

    def _getPackageSet(self, name):
	return _PackageSet(self.pkgDB, name)

    def _getFileDB(self, fileId):
	return _FileDB(self.fileDB, fileId)

    def pullFileContents(self, fileId, targetFile):
	srcFile = self.contentsStore.openFile(fileId)
	targetFile.write(srcFile.read())
	srcFile.close()

    def pullFileContentsObject(self, fileId):
	return self.contentsStore.openFile(fileId)

    def newFileContents(self, fileId, srcFile):
	targetFile = self.contentsStore.newFile(fileId)
	targetFile.write(srcFile.read())
	targetFile.close()

    def hasFileContents(self, fileId):
	return self.contentsStore.hasFile(fileId)

    def getPackageList(self, groupName = ""):
	if self.pkgDB.hasFile(groupName):
	    return [ groupName ]

	allPackages = self.pkgDB.fileList()
	list = []
	groupName = groupName + ":"

	for pkgName in allPackages:
	    if pkgName.startswith(groupName):
		list.append(pkgName)

	list.sort()

	return list

    def hasPackage(self, pkg):
	return self.pkgDB.hasFile(pkg)

    def hasPackageVersion(self, pkgName, version):
	return self._getPackageSet(pkgName).hasVersion(version)

    def pkgLatestVersion(self, pkgName, branch):
	return self._getPackageSet(pkgName).getLatestVersion(branch)

    def getLatestPackage(self, pkgName, branch):
	return self._getPackageSet(pkgName).getLatestPackage(branch)

    def getPackageVersion(self, pkgName, version):
	return self._getPackageSet(pkgName).getVersion(version)

    def getPackageVersionList(self, pkgName):
	return self._getPackageSet(pkgName).versionList()

    def fileLatestVersion(self, fileId, branch):
	fileDB = self._getFileDB(fileId)
	return fileDB.getLatestVersion(branch)
	
    def getFileVersion(self, fileId, version):
	fileDB = self._getFileDB(fileId)
	return fileDB.getVersion(version)

    def storeFileFromChangeset(self, chgSet, file, pathToFile):
	if isinstance(file, files.RegularFile):
	    f = chgSet.getFileContents(file.sha1())
	    file.archive(self, f)
	    f.close()

    def open(self, mode):
	if self.pkgDB:
	    self.close()

	self.lockfd = os.open(self.top + "/lock", os.O_CREAT | os.O_RDWR)

	if (mode == "r"):
	    fcntl.lockf(self.lockfd, fcntl.LOCK_SH)
	else:
	    fcntl.lockf(self.lockfd, fcntl.LOCK_EX)

	self.pkgDB = versioned.FileIndexedDatabase(self.top + "/pkgs.db")
	self.fileDB = versioned.Database(self.top + "/files.db")

	self.mode = mode

    def close(self):
	if self.pkgDB:
	    self.pkgDB = None
	    self.fileDB = None
	    os.close(self.lockfd)

    def __del__(self):
	self.close()

    def __init__(self, path, mode = "c"):
	self.top = path
	self.pkgDB = None
	
	self.contentsDB = self.top + "/contents"
	util.mkdirChain(self.contentsDB)

	self.contentsStore = datastore.DataStore(self.contentsDB)

	self.open(mode)

# This is a repository which includes a mapping from a sha1 to a path
class Database(Repository):

    def storeFileFromChangeset(self, chgSet, file, pathToFile):
	file.restore(chgSet, self.root + pathToFile)
	if isinstance(file, files.RegularFile):
	    self.fileIdMap[file.sha1()] = pathToFile

    def pullFileContents(self, fileId, targetFile):
	srcFile = open(self.root + self.fileIdMap[fileId], "r")
	targetFile.write(srcFile.read())
	srcFile.close()

    def pullFileContentsObject(self, fileId):
	return open(self.root + self.fileIdMap[fileId], "r")

    def close(self):
	if self.fileIdMap:
	    self.fileIdMap = None
	Repository.close(self)

    def open(self, mode):
	Repository.open(self, mode)
	self.fileIdMap = dbhash.open(self.top + "/fileid.db", mode)
	self.rollbackCache = self.top + "/rollbacks"
	self.rollbackStatus = self.rollbackCache + "/status"
	if not os.path.exists(self.rollbackCache):
	    os.mkdir(self.rollbackCache)
	if not os.path.exists(self.rollbackStatus):
	    f = open(self.rollbackStatus, "w")
	    f.write("0 -1\n")
	    f.close()

    def addRollback(self, changeset):
	if self.mode == "r":
	    raise IOError, "database is read only"

	f = open(self.rollbackStatus)
	(first, last) = f.read()[:-1].split()
	last = int(last)

	fn = self.rollbackCache + ("/r.%d" % (last + 1))
	changeset.writeToFile(fn)
	f.close()

	newStatus = self.rollbackCache + ".new"

	f = open(newStatus, "w")
	f.write("%s %d\n" % (first, last + 1))
	f.close()

	os.rename(newStatus, self.rollbackStatus)

    def getRollbackList(self):
	f = open(self.rollbackStatus)
	(first, last) = f.read()[:-1].split()
	first = int(first)
	last = int(last)

	list = []
	for i in range(first, last):
	    list.append(self.rollbackCache + "/r.%d" % i)

    def __init__(self, root, path, mode = "c"):
	self.root = root
	fullPath = root + "/" + path
	Repository.__init__(self, fullPath, mode)

# this is a set of all of the versions of a single packages 
class _PackageSet:
    def getVersion(self, version):
	f1 = self.f.getVersion(version)
	p = package.PackageFromFile(self.name, f1, version)
	f1.close()
	return p

    def hasVersion(self, version):
	return self.f.hasVersion(version)

    def eraseVersion(self, version):
	self.f.eraseVersion(version)

    def addVersion(self, version, package):
	self.f.addVersion(version, package.formatString())

    def versionList(self):
	return self.f.versionList()

    def getLatestPackage(self, branch):
	return self.getVersion(self.f.findLatestVersion(branch))

    def getLatestVersion(self, branch):
	return self.f.findLatestVersion(branch)

    def close(self):
	self.f.close()
	self.f = None

    def __del__(self):
	self.f = None

    def __init__(self, db, name):
	self.name = name
	self.f = db.openFile(name)

class _FileDB:

    def getLatestVersion(self, branch):
	return self.f.findLatestVersion(branch)

    def addVersion(self, version, file):
	if self.f.hasVersion(version):
	    raise KeyError, "duplicate version for database"
	else:
	    if file.id() != self.fileId:
		raise KeyError, "file id mismatch for file database"
	
	self.f.addVersion(version, "%s\n" % file.infoLine())

    def getVersion(self, version):
	f1 = self.f.getVersion(version)
	file = files.FileFromInfoLine(f1.read(), self.fileId)
	f1.close()
	return file

    def hasVersion(self, version):
	return self.f.hasVersion(version)

    def eraseVersion(self, version):
	self.f.eraseVersion(version)

    def close(self):
	self.f = None

    def __del__(self):
	self.close()

    def __init__(self, db, fileId):
	self.f = db.openFile(fileId)
	self.fileId = fileId

