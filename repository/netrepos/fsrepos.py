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

# implements a db-based repository

from deps import deps
import os
from lib import util, stackutil, log
import repository
import repository.netclient
from repository.repository import AbstractRepository
from repository.repository import ChangeSetJob
from repository.repository import DataStoreRepository
from repository.repository import DuplicateBranch
from repository.repository import RepositoryError
from repository.repository import TroveMissing
from repository import changeset
from repository import filecontents
import sys
import trovestore
import versions

class FilesystemRepository(DataStoreRepository, AbstractRepository):

    ### Package access functions

    def thawFlavor(self, flavor):
	if flavor and flavor != "none":
	    return deps.ThawDependencySet(flavor)

	return None

    def iterAllTroveNames(self):
	a = self.troveStore.iterTroveNames()
	return a

    def getAllTroveLeafs(self, troveNameList):
	d = {}
	for (troveName, troveLeafList) in \
		self.troveStore.iterAllTroveLeafs(troveNameList):
	    d[troveName] = [ versions.VersionFromString(x) for x in
				troveLeafList ]
	return d

    def getTroveLeavesByLabel(self, troveNameList, label):
	d = {}
	labelStr = label.asString()
	for troveName in troveNameList:
	    d[troveName] = [ x for x in 
		self.troveStore.iterTroveLeafsByLabel(troveName, labelStr) ]

	return d

    def getTroveVersionsByLabel(self, troveNameList, label):
	d = {}
	labelStr = label.asString()
	for troveName in troveNameList:
	    d[troveName] = [ x for x in 
		self.troveStore.iterTroveVersionsByLabel(troveName, labelStr) ]

	return d

    def getTroveFlavorsLatestVersion(self, troveName, branch):
	return [ (versions.VersionFromString(x[0], 
			timeStamps = [ float(z) for z in x[1].split(":")]),
		  self.thawFlavor(x[2])) for x in 
		    self.troveStore.iterTrovePerFlavorLeafs(troveName, 
							    branch.asString()) ]
	
    def getTroveVersionFlavors(self, troveDict):
	newD = self.troveStore.getTroveFlavors(troveDict)

	for troveName in newD.iterkeys():
	    for version in newD[troveName].iterkeys():
		newD[troveName][version] = \
		    [ self.thawFlavor(x) for x in newD[troveName][version] ]

	return newD

    def hasPackage(self, serverName, pkgName):
	assert(serverName == self.name)
	return self.troveStore.hasTrove(pkgName)

    def hasTrove(self, pkgName, version, flavor):
	return self.troveStore.hasTrove(pkgName, troveVersion = version,
					troveFlavor = flavor)

    def getTroveLatestVersion(self, pkgName, branch):
        try:
            return self.troveStore.troveLatestVersion(pkgName, branch)
        except KeyError:
            raise TroveMissing(pkgName, branch)

    def getTrove(self, pkgName, version, flavor, pristine = True,
                 withFiles = True):
	try:
	    return self.troveStore.getTrove(pkgName, version, flavor,
                                            withFiles = withFiles)
	except KeyError:
	    raise TroveMissing(pkgName, version)

    def eraseTrove(self, pkgName, version, flavor):
	self.troveStore.eraseTrove(pkgName, version, flavor)

    def addTrove(self, pkg):
	return self.troveStore.addTrove(pkg)

    def addTroveDone(self, pkg):
	self.troveStore.addTroveDone(pkg)

    def addPackage(self, pkg):
	return self.troveStore.addTrove(pkg)

    def addPackageDone(self, pkgId):
	self.troveStore.addTroveDone(pkgId)

    def commit(self):
	self.troveStore.commit()

    def rollback(self):
	self.troveStore.rollback()

    def branchesOfTroveLabel(self, troveName, label):
	return self.troveStore.branchesOfTroveLabel(troveName, label)

    def createTroveBranch(self, pkgName, branch):
	log.debug("creating branch %s for %s", branch.asString(), pkgName)
        return self.troveStore.createTroveBranch(pkgName, branch)

    def findFileVersion(self, troveName, troveVersion, fileId, fileVersion):
        return self.troveStore.findFileVersion(troveName, troveVersion,
                                               fileId, fileVersion)

    def iterFilesInTrove(self, troveName, version, flavor,
                         sortByPath = False, withFiles = False):
	gen = self.troveStore.iterFilesInTrove(troveName, version, flavor,
						    sortByPath, withFiles)

	for (fileId, path, version, fileObj) in gen:
	    if fileObj:
		yield fileId, path, version, fileObj

	    # if fileObj is None, we need to get the fileObj from a remote
	    # repository

	    fileObj = self.getFileVersion(fileId, version)
	    yield fileId, path, version, fileObj

    ### File functions

    def getFileVersion(self, fileId, fileVersion, withContents = 0):
	# the get trove netclient provides doesn't work with a 
	# FilesystemRepository (it needs to create a change set which gets 
	# passed)
	if fileVersion.branch().label().getHost() != self.name:
	    assert(not withContents)
	    return self.reposSet.getFileVersion(fileId, fileVersion)

	file = self.troveStore.getFile(fileId, fileVersion)
	if withContents:
	    if file.hasContents:
		cont = filecontents.FromDataStore(self.contentsStore, 
						    file.contents.sha1(), 
						    file.contents.size())
	    else:
		cont = None

	    return (file, cont)

	return file

    def getFileVersions(self, l):
	return self.troveStore.getFiles(l)

    def addFileVersion(self, troveInfo, fileId, fileObj, path, fileVersion):
	# don't add duplicated to this repository
	#if not self.troveStore.hasFile(fileObj.id(), fileVersion):
	self.troveStore.addFile(troveInfo, fileId, fileObj, path, fileVersion)

    def eraseFileVersion(self, fileId, version):
	self.troveStore.eraseFile(fileId, version)

    ###

    def __del__(self):
	self.close()

    def createBranch(self, newBranch, where, troveList = []):

	if newBranch.getHost() != self.name:
	    raise RepositoryError("cannot create branch for %s on %s",
		      newBranch.getHost(), self.name)

        self.troveStore.begin()
	
	troveList = [ (x, where) for x in troveList ]

	branchedTroves = {}
	branchedFiles = {}

	while troveList:
	    troveName = troveList[0][0]
	    location = troveList[0][1]
	    del troveList[0]

	    if branchedTroves.has_key(troveName): continue
	    branchedTroves[troveName] = 1

	    if isinstance(location, versions.Version):
		verDict = { troveName : [ location ] }
		serverName = location.branch().label().getHost()
	    else:
		serverName = location.getHost()

		if serverName == self.name:
		    verDict = self.getTroveLeavesByLabel([troveName], location)
		else:
		    verDict = self.reposSet.getTroveLeavesByLabel([troveName], location)

	    if serverName == self.name:
		d = self.getTroveVersionFlavors(verDict)
	    else:
		d = self.reposSet.getTroveVersionFlavors(verDict)

	    fullList = []
	    for (version, flavors) in d[troveName].iteritems():
		fullList += [ (troveName, version, x) for x in flavors ]

	    if serverName == self.name:
		troves = self.getTroves(fullList)
	    else:
		troves = self.reposSet.getTroves(fullList)

	    for trove in troves:
		branchedVersion = trove.getVersion().fork(newBranch, 
							  sameVerRel = 1)
                try:
                    self.createTroveBranch(troveName, branchedVersion.branch())
                except DuplicateBranch:
                    raise DuplicateBranch("%s already has branch %s"
                            % (troveName, branchedVersion.branch().asString()))

		trove.changeVersion(branchedVersion)

		# make a copy of this list since we're going to update it
		l = [ x for x in trove.iterTroveList() ]
		for (name, version, flavor) in l:
		    troveList.append((name, version))

		    branchedVersion = version.fork(newBranch, sameVerRel = 1)
		    trove.delTrove(name, version, flavor, False)
		    trove.addTrove(name, branchedVersion, flavor)

		troveInfo = self.addTrove(trove)
		for (fileId, path, version) in trove.iterFileList():
		    self.addFileVersion(troveInfo, fileId, None, path, version)
		self.addTroveDone(troveInfo)

        # commit branch to the repository
        self.commit()

	return True
		    
    def open(self):
	if self.troveStore is not None:
	    self.close()

	self.troveStore = trovestore.TroveStore(self.sqlDB)
	sb = os.stat(self.sqlDB)
	self.sqlDeviceInode = (sb.st_dev, sb.st_ino)

    def reopen(self):
	sb = os.stat(self.sqlDB)

	sqlDeviceInode = (sb.st_dev, sb.st_ino)
	if self.sqlDeviceInode != sqlDeviceInode:
	    del self.troveStore
	    self.troveStore = trovestore.TroveStore(self.sqlDB)
	    sb = os.stat(self.sqlDB)
	    self.sqlDeviceInode = (sb.st_dev, sb.st_ino)

    def commitChangeSet(self, cs):
	# let's make sure commiting this change set is a sane thing to attempt
	for pkg in cs.iterNewPackageList():
	    v = pkg.getNewVersion()
	    label = v.branch().label()
	    if isinstance(label, versions.EmergeBranch):
		raise repository.repository.CommitError, \
		    "can not commit items on localhost@local:EMERGE"
	    
	    if isinstance(label, versions.CookBranch):
		raise repository.repository.CommitError, \
		    "can not commit items on localhost@local:COOK"
	    
        self.troveStore.begin()
        try:
            # a little odd that creating a class instance has the side
            # effect of modifying the repository...
            ChangeSetJob(self, cs)
        except:
            print >> sys.stderr, "exception occurred while committing change set"
            stackutil.printTraceBack()
            print >> sys.stderr, "attempting rollback"
            self.rollback()
            raise
        else:
            self.commit()

    def resolveRequirements(self, label, depSetList):
        return self.troveStore.resolveRequirements(label, depSetList)

    def getFileContents(self, troveName, troveVersion, 
		        fileId, fileVersion, fileObj):
	# the get trove netclient provides doesn't work with a 
	# FilesystemRepository (it needs to create a change set which gets 
	# passed)
	if fileVersion.branch().label().getHost() == self.name:
	    return filecontents.FromDataStore(self.contentsStore, 
					      fileObj.contents.sha1(), 
					      fileObj.contents.size())
	else:
	    # a bit of sleight of hand here... we look for this file in
	    # the trove it was first built in
	    #
	    # this could cause us to run out of file descriptors on large
	    # troves. it might be better to close the file and return
	    # a filecontents object?
	    return self.reposSet.getFileContents(troveName, fileVersion, 
                                      fileId, fileVersion)

    def createChangeSet(self, troveList, recurse = True, withFiles = True,
                        withFileContents = True):
	"""
	troveList is a list of (troveName, flavor, oldVersion, newVersion, 
        absolute) tuples. 

	if oldVersion == None and absolute == 0, then the trove is assumed
	to be new for the purposes of the change set

	if newVersion == None then the trove is being removed
	"""
	cs = changeset.ChangeSetFromRepository(self)
	for (name, (oldV, oldFlavor), (newV, newFlavor), absolute) in troveList:
	    cs.addPrimaryPackage(name, newV, newFlavor)

        externalTroveList = []
        externalFileList = []

	dupFilter = {}

	# make a copy to remove things from
	troveList = troveList[:]

	# don't use a for in here since we grow troveList inside of
	# this loop
	while troveList:
	    (troveName, (oldVersion, oldFlavor), 
		        (newVersion, newFlavor), absolute) = \
		troveList[0]
	    del troveList[0]

	    # make sure we haven't already generated this changeset; since
	    # troves can be included from other troves we could try
	    # to generate quite a few duplicates
	    if dupFilter.has_key((troveName, oldFlavor, newFlavor)):
		match = False
		for (otherOld, otherNew) in \
				dupFilter[(troveName, oldFlavor, newFlavor)]:
		    if not otherOld and not oldVersion:
			same = True
		    elif not otherOld and oldVersion:
			same = False
		    elif otherOld and not oldVersion:
			same = False
		    else:
			same = otherOld == newVersion

		    if same and otherNew == newVersion:
			match = True
			break
		
		if match: continue

		dupFilter[(troveName, oldFlavor, newFlavor)].append(
				    (oldVersion, newVersion))
	    else:
		dupFilter[(troveName, oldFlavor, newFlavor)] = \
				    [(oldVersion, newVersion)]

	    if not newVersion:
		# remove this trove and any trove contained in it
		old = self.getTrove(troveName, oldVersion, oldFlavor)
		cs.oldPackage(troveName, oldVersion, oldFlavor)
		for (name, version, flavor) in old.iterTroveList():
                    # it's possible that a component of a trove
                    # was erased, make sure that it is installed
                    if self.hasTrove(name, version, flavor):
                        troveList.append((name, flavor, version, None, 
					    absolute))
		    
		continue

            if newVersion.branch().label().getHost() != self.name or \
               (oldVersion and 
                oldVersion.branch().label().getHost() != self.name):
                # don't try to make changesets between repositories; the
                # client can do that itself
                externalTroveList = (troveName, (oldVersion, oldFlavor),
                                     (newVersion, newFlavor), absolute)
                continue

            new = self.getTrove(troveName, newVersion, newFlavor, 
                                withFiles = withFiles)
	 
	    if oldVersion:
                old = self.getTrove(troveName, oldVersion, oldFlavor,
                                    withFiles = withFiles)
	    else:
		old = None

	    (pkgChgSet, filesNeeded, pkgsNeeded) = \
				new.diff(old, absolute = absolute)

	    if recurse:
		for (pkgName, old, new, oldFlavor, newFlavor) in pkgsNeeded:
		    troveList.append((pkgName, (old, oldFlavor),
					       (new, newFlavor), absolute))

	    cs.newPackage(pkgChgSet)

	    # sort the set of files we need into bins based on the server
	    # name
	    serverIdx = {}
            getList = []
            newFilesNeeded = []

	    for (fileId, oldFileVersion, newFileVersion) in filesNeeded:
                # if either the old or new file version is on a different
                # repository, creating this diff is someone else's problem
                if newFileVersion.branch().label().getHost() != self.name or \
                   (oldFileVersion and
                    oldFileVersion.branch().label().getHost() != self.name):
                    externalFileList.append((fileId, troveName,
                                     (oldVersion, oldFlavor, oldFileVersion),
                                     (newVersion, newFlavor, newFileVersion)))
                else:
                    newFilesNeeded.append((fileId, oldFileVersion,
                                             newFileVersion))
                    if oldFileVersion:
                        getList.append((fileId, oldFileVersion))
                    getList.append((fileId, newFileVersion))

            filesNeeded = newFilesNeeded
            del newFilesNeeded
            idIdx = self.getFileVersions(getList)

            # Walk this in reverse order. This may seem odd, but the
            # order in the final changeset is set by sorting that happens
            # in the change set object itself. The only reason we sort
            # here at all is to make sure PTR file types come before the
            # file they refer to. Reverse shorting makes this a bit easier.
            filesNeeded.sort()
            filesNeeded.reverse()

            ptrTable = {}
	    for (fileId, oldFileVersion, newFileVersion) in filesNeeded:
		oldFile = None
		if oldFileVersion:
		    oldFile = idIdx[(fileId, oldFileVersion)]

		oldCont = None
		newCont = None

		newFile = idIdx[(fileId, newFileVersion)]

		(filecs, hash) = changeset.fileChangeSet(fileId, oldFile, 
							 newFile)

		cs.addFile(fileId, oldFileVersion, newFileVersion, filecs)

		# this test catches files which have changed from not
		# config files to config files; these need to be included
		# unconditionally so we always have the pristine contents
		# to include in the local database
		if withFileContents and \
                    (hash or (oldFile and newFile.flags.isConfig() 
                                      and not oldFile.flags.isConfig())):
		    if oldFileVersion :
			oldCont = self.getFileContents(troveName, oldVersion, 
				    fileId, oldFileVersion, 
				    fileObj = oldFile)

		    newCont = self.getFileContents(troveName, newVersion, 
				    fileId, newFileVersion, 
				    fileObj = newFile)

		    (contType, cont) = changeset.fileContentsDiff(oldFile, 
						oldCont, newFile, newCont)

                    # we don't let config files be ptr types; if they were
                    # they could be ptrs to things which aren't config files,
                    # which would completely hose the sort order we use. this
                    # could be relaxed someday to let them be ptr's to other
                    # config files
                    if not newFile.flags.isConfig() and \
                                contType == changeset.ChangedFileTypes.file:
                        hash = newFile.contents.sha1()
                        ptr = ptrTable.get(hash, None)
                        if ptr is not None:
                            contType = changeset.ChangedFileTypes.ptr
                            cont = filecontents.FromString(ptr)
                        else:
                            ptrTable[hash] = fileId

		    cs.addFileContents(fileId, contType, cont, 
				       newFile.flags.isConfig())

	return (cs, externalTroveList, externalFileList)

    def close(self):
	if self.troveStore is not None:
	    self.troveStore.db.close()
	    self.troveStore = None

    def __init__(self, name, path, repositoryMap):
	self.top = path
	self.troveStore = None
	self.name = name
	map = dict(repositoryMap)
	map[name] = self
	self.reposSet = repository.netclient.NetworkRepositoryClient(map)
	
	self.sqlDB = self.top + "/sqldb"

	try:
	    util.mkdirChain(self.top)
	except OSError, e:
	    raise repository.OpenError(str(e))

        self.open()

	DataStoreRepository.__init__(self, path)
	AbstractRepository.__init__(self)

